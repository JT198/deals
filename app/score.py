"""Expected price from our own comps, then a 0-100 deal score.

Comps are every relevant listing we've seen in the last 180 days (asking
prices, not sold prices - so "expected" means "what these usually list for").
"""
import json
import math
import statistics
import time

from .categories import cfg
from .equipment import APPLIES, DOLLAR_BY_CAT, DOLLAR_PRIOR, LABEL, MULT_PRIOR, detect

COMP_WINDOW = 180 * 86400
NOW_YEAR = time.localtime().tm_year


def _v(listing, key):
    """Row or dict field, None if absent (the market chart scores synthetic listings)."""
    try:
        return listing[key]
    except (KeyError, IndexError):
        return None


class Comp(tuple):
    """(id, year, price per machine, deck_in, miles, hours, len_ft, axles, equip, track_in, cc)"""
    id, year, price, deck, miles, hours, len_ft, axles, equip, track, cc = (property(lambda t, i=i: t[i]) for i in range(11))


def _comps(con, sold: bool = False) -> dict[str, list[Comp]]:
    """family -> used asking prices, or (sold=True) the last price of listings marked sold."""
    rows = con.execute(
        """SELECT id, family, year, COALESCE(end_price, price) * 1.0 / MAX(1, COALESCE(units, 1)) price,
                  deck_in, miles, hours, len_ft, axles, equipment, track_in, cc
           FROM listings WHERE relevant = 1 AND family IS NOT NULL
             AND COALESCE(is_new, 0) = 0 AND COALESCE(end_price, price) >= 300 AND """ +
        # sold comps skip listings with known problems: non-runners and parts machines sell cheap and get marked sold
        ("status = 'sold' AND COALESCE(red_flags, '[]') = '[]' AND COALESCE(ended_at, last_seen) >= ?"
         if sold else "last_seen >= ?"),
        (int(time.time()) - COMP_WINDOW,)).fetchall()
    by_fam: dict[str, list[Comp]] = {}
    for r in rows:
        by_fam.setdefault(r["family"], []).append(
            Comp((r["id"], r["year"], int(r["price"]), r["deck_in"], r["miles"], r["hours"], r["len_ft"], r["axles"],
                  frozenset(json.loads(r["equipment"] or "[]")), r["track_in"], r["cc"])))
    # drop junk prices (payments, deposits, parts) - anything under 30% of the family median
    for fam, lst in by_fam.items():
        med = statistics.median(c.price for c in lst)
        by_fam[fam] = [c for c in lst if c.price >= 0.3 * med]
    return by_fam


def _fit(comps):
    """log(price) = a + b*year, least squares, slope clipped to a sane range."""
    xs = [c.year for c in comps]
    ys = [math.log(c.price) for c in comps]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    var = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var if var else 0.08
    b = min(0.15, max(0.03, b))
    return my - b * mx, b


# ---- usage (miles / hours) --------------------------------------------------------------------
USAGE_SHRINK = 15          # listings' worth of weight on the category prior
USAGE_CLAMP = (0.6, 1.4)   # a single comp is never moved more than -40% / +40% for usage


def _usage_metric(listing, category):
    """Which usage number to compare on: mowers use hours; vehicles miles, else hours."""
    prior = cfg(category)["usage"]
    for m in ("miles", "hours"):
        if m in prior and _v(listing, m) is not None:
            return m
    return None


def usage_slope(comps, metric, category):
    """log-price change per unit of use, from year+use least squares, shrunk toward the prior."""
    per, unit = cfg(category)["usage"][metric]
    prior = per / unit
    pts = [(c.year, getattr(c, metric), math.log(c.price)) for c in comps
           if c.year and getattr(c, metric) is not None]
    n = len(pts)
    if n >= 6:
        my, mu, ml = (statistics.fmean(p[i] for p in pts) for i in range(3))
        syy = sum((p[0] - my) ** 2 for p in pts)
        suu = sum((p[1] - mu) ** 2 for p in pts)
        syu = sum((p[0] - my) * (p[1] - mu) for p in pts)
        syl = sum((p[0] - my) * (p[2] - ml) for p in pts)
        sul = sum((p[1] - mu) * (p[2] - ml) for p in pts)
        det = syy * suu - syu * syu
        fit = (syy * sul - syu * syl) / det if det > 1e-9 and suu > 0 else prior
    else:
        fit, n = prior, 0
    slope = (n * fit + USAGE_SHRINK * prior) / (n + USAGE_SHRINK)
    return min(0.0, max(3 * prior, slope))          # more use never raises the price


def _use_per_year(comps, metric):
    rates = [getattr(c, metric) / max(1, NOW_YEAR - c.year + 1) for c in comps
             if c.year and getattr(c, metric) is not None]
    return statistics.median(rates) if len(rates) >= 5 else None


def _equip(listing) -> frozenset:
    try:
        return frozenset(json.loads(_v(listing, "equipment") or "[]"))
    except ValueError:
        return frozenset()


def expected_price(listing, comps_by_fam, effects=None):
    """-> (expected, comps used, expected before any adjustment, plain-English note, expected before equipment).

    A listing selling several machines for one price (a pair of jet skis) is priced per machine, then
    multiplied back up, so the numbers still compare with its asking price.
    """
    units = _v(listing, "units") or 1
    if units <= 1:
        return _expected_one(listing, comps_by_fam, effects)
    # one trailer carries them all: price each machine without it, then add the trailer once
    feats = _equip(listing)
    trailer = (effects or {}).get(_v(listing, "category"), {}).get("trailer", 0) if "trailer" in feats else 0
    one = dict(listing)
    one["equipment"] = json.dumps(sorted(feats - {"trailer"}))
    exp, n, base, note, pre = _expected_one(one, comps_by_fam, effects)
    if not exp:
        return exp, n, base, note, pre
    each = f"{units} machines at about ${exp:,} each" + (f" + ${trailer:,} trailer" if trailer else "")
    return (exp * units + trailer, n, base * units, f"{each}; {note}" if note else each, pre * units)


def _expected_one(listing, comps_by_fam, effects=None):
    """Expected price of one machine (see expected_price).

    Comps are lined up to this machine: model year (dep/yr), then miles/hours, then equipment
    (cab/heat/A-C as learned percentages, plow/trailer as dollars). effects comes from equipment_effects().
    """
    none = (None, 0, None, None, None)
    fam, year = _v(listing, "family"), _v(listing, "year")
    if not fam:
        return none
    cat = _v(listing, "category")
    c = cfg(cat)
    others = [x for x in comps_by_fam.get(fam, []) if x.id != _v(listing, "id")]
    deck = _v(listing, "deck_in")
    if deck:   # mowers: a 42" and a 60" of the same series are different machines
        same_deck = [x for x in others if x.deck and abs(x.deck - deck) <= 6]
        if len(same_deck) >= 4:
            others = same_deck
    for field, tol in (("track", 9), ("cc", 150)):   # sleds: a 129" 600 and a 154" 850 are different machines
        mine = _v(listing, "track_in" if field == "track" else field)
        if mine and cat == "sled":
            same = [x for x in others if getattr(x, field) and abs(getattr(x, field) - mine) <= tol]
            if len(same) >= 4:
                others = same
    length = _v(listing, "len_ft")
    if length:   # trailers: a 5x8 single-axle and a 7x18 tandem are different things
        axles = _v(listing, "axles")
        same_size = [x for x in others if x.len_ft and abs(x.len_ft - length) <= 2
                     and (not axles or not x.axles or x.axles == axles)]
        if len(same_size) >= 4:
            others = same_size
        else:
            # Not enough same-size trailers of this type: trailer prices scale roughly with length, so
            # price it per foot from every trailer of this type (same axle count when there are enough).
            sized = [x for x in others if x.len_ft]
            same_axles = [x for x in sized if axles and x.axles == axles]
            pool = same_axles if len(same_axles) >= 5 else sized
            if len(pool) >= 5:
                def per_ft(x):   # line up for age too when both years are known
                    return x.price / x.len_ft * ((1 + c["dep"]) ** (year - x.year) if year and x.year else 1)
                exp = int(statistics.median(per_ft(x) for x in pool) * length)
                return exp, len(pool), exp, f"priced per foot from {len(pool)} {fam.lower()} trailers of other sizes", exp
            return None, len(same_size), None, None, None

    metric = _usage_metric(listing, cat)
    use = _v(listing, metric) if metric else None
    slope = usage_slope(others, metric, cat) if metric else 0.0
    rate = _use_per_year(others, metric) if metric else None
    feats = _equip(listing)
    eff = (effects or {}).get(cat, {})

    def comp_use(x):
        u = getattr(x, metric) if metric else None
        if u is None and rate is not None and x.year:
            u = rate * max(1, NOW_YEAR - x.year + 1)     # typical use for its age
        return u

    def usage_factor(u):
        if use is None or u is None:
            return 1.0
        return min(USAGE_CLAMP[1], max(USAGE_CLAMP[0], math.exp(slope * (use - u))))

    def equip_adjust(p, has):
        """Move a comp priced p with equipment `has` (a set, or {feature: share} for a typical comp) to ours."""
        if not eff:
            return p
        share = has if isinstance(has, dict) else {f: 1.0 for f in has}
        mult, dollars = 1.0, 0.0
        for f, e in eff.items():
            d = (f in feats) - share.get(f, 0.0)
            if f in MULT_PRIOR:
                mult *= (1 + e) ** d
            else:
                dollars += e * d
        return min(1.6 * p, max(0.6 * p, p * mult + dollars))

    def shares(pool):
        return {f: sum(f in x.equip for x in pool) / len(pool) for f in eff} if pool else {}

    if not year:
        # no model year (common for mowers): compare against the whole family, still adjusted for use/equipment
        if len(others) < 5:
            return None, len(others), None, None, None
        base = int(statistics.median(x.price for x in others))
        with_use = [getattr(x, metric) for x in others if metric and getattr(x, metric) is not None]
        typical_use = statistics.median(with_use) if len(with_use) >= 5 else None
        pre = int(base * usage_factor(typical_use))
        exp = int(equip_adjust(pre, shares(others)))
        n = len(others)
    else:
        dated = [x for x in others if x.year]
        near = [x for x in dated if abs(x.year - year) <= c["window"]]
        if len(near) >= 4:
            aligned = [(x.price * (1 + c["dep"]) ** (year - x.year), comp_use(x), x.equip) for x in near]
            base = int(statistics.median(p for p, _, _ in aligned))
            pre = int(statistics.median(p * usage_factor(u) for p, u, _ in aligned))
            exp = int(statistics.median(equip_adjust(p * usage_factor(u), eq) for p, u, eq in aligned))
            typical_use = [u for _, u, _ in aligned if u is not None]
            typical_use = statistics.median(typical_use) if typical_use else None
            pool, n = near, len(near)
        else:
            yrs = [x.year for x in dated]
            if not (c["fit"] and len(dated) >= 5 and len(set(yrs)) >= 2 and min(yrs) - 1 <= year <= max(yrs) + 1):
                return None, len(others), None, None, None
            a, b = _fit(dated)
            base = int(math.exp(a + b * year))
            typical_use = rate * max(1, NOW_YEAR - year + 1) if rate is not None else None
            pre = int(base * usage_factor(typical_use))
            exp = int(equip_adjust(pre, shares(dated)))
            pool, n = dated, len(dated)
        others = pool

    notes = [x for x in (_usage_note(use, typical_use, metric, pre, base),
                         _equip_note(feats, shares(others), exp, pre)) if x]
    return exp, n, base, "; ".join(notes) or None, pre


def _equip_note(feats, share, exp, pre):
    if abs(exp - pre) < max(100, 0.015 * pre):
        return None
    if exp > pre:
        what = [LABEL[f] for f in share if f in feats and share[f] < 0.9] or [LABEL[f] for f in feats]
        return f"+${exp - pre:,} for equipment: has {', '.join(what)}"
    what = [LABEL[f] for f in share if f not in feats and share[f] >= 0.3]
    return f"−${pre - exp:,} for equipment: no {' / '.join(what) or 'extras'} (common on similar ones)"


def equipment_effects(con) -> dict:
    """category -> {feature: effect}. Cab/heat/A-C as a fraction of price, learned from how much listings
    with the feature ask above their pre-equipment typical price vs listings without; shrunk toward the prior.
    Plow / trailer use their dollar priors."""
    rows = con.execute(
        """SELECT category, equipment, price, expected_pre FROM listings
           WHERE relevant = 1 AND parsed = 1 AND COALESCE(is_new, 0) = 0 AND price >= 300
             AND expected_pre IS NOT NULL AND category IN ('utv4', 'utv2', 'atv')""").fetchall()
    ratios: dict[str, list] = {}
    for r in rows:
        group = "utv" if r["category"] in ("utv4", "utv2") else r["category"]
        ratios.setdefault(group, []).append((set(json.loads(r["equipment"] or "[]")), r["price"] / r["expected_pre"]))
    out = {}
    for cat, feats in APPLIES.items():
        group = "utv" if cat in ("utv4", "utv2") else cat
        pts = ratios.get(group, [])
        eff = {}
        for f in feats:
            if f in MULT_PRIOR:
                prior = MULT_PRIOR[f]
                has = [q for e, q in pts if f in e]
                hasnt = [q for e, q in pts if f not in e]
                n = len(has) if len(has) >= 5 and len(hasnt) >= 5 else 0
                data = statistics.median(has) / statistics.median(hasnt) - 1 if n else prior
                eff[f] = min(2.5 * prior, max(0.0, (n * data + USAGE_SHRINK * prior) / (n + USAGE_SHRINK)))
            else:
                eff[f] = DOLLAR_BY_CAT.get(cat, {}).get(f, DOLLAR_PRIOR[f])
        out[cat] = eff
    return out


def _usage_note(use, typical_use, metric, exp, base):
    if use is None or typical_use is None or abs(exp - base) < max(100, 0.02 * base):
        return None
    unit = "mi" if metric == "miles" else "hrs"
    more = "more" if use > typical_use else "less"
    return (f"{'−' if exp < base else '+'}${abs(base - exp):,} for use: {use:,} {unit} vs about "
            f"{int(round(typical_use, -2 if typical_use >= 1000 else -1)):,} {unit} on similar ones ({more} use)")


def score(listing, expected, comps: int = 0, usage_adjusted: bool = False,
          sold_typical: int | None = None) -> tuple[int, float | None, list[str]]:
    """50 = a normal listing. 'Great' (75+) needs a real discount plus something else going for it."""
    reasons: list[str] = []
    price = listing["price"]
    deal_pct = None
    if listing["is_new"] == 1:
        expected = None   # our comps are used machines; new units need an MSRP comparison
    if price is not None and (price < 100 or (expected and price < 0.2 * expected)):
        # "$1", "$3", "$123" - sellers who want offers, not a real price
        return min(35, 45), None, [f"price ${price:,} looks like a placeholder"]
    if expected and price:
        deal_pct = (expected - price) / expected
        s = 45 + 100 * max(-0.4, min(0.4, deal_pct))
        if comps < 8:   # thin comps: trust the discount less
            s = 45 + (s - 45) * (0.5 + comps / 16)
        if not listing["year"]:   # compared against every year of the family
            s = 45 + (s - 45) * 0.6
            reasons.append("year not stated")
        if deal_pct >= 0.05:
            reasons.append(f"{deal_pct:.0%} under typical ${expected:,}")
        elif deal_pct <= -0.05:
            reasons.append(f"{-deal_pct:.0%} over typical ${expected:,}")
    else:
        s = 40
        reasons.append("new unit - compare to MSRP" if listing["is_new"] == 1 else "not enough comps yet")

    was = max(x for x in (listing["strike_price"], listing["first_price"], 0) if x is not None)
    if price and was and was > price:
        drop = (was - price) / was
        s += 3 + min(7, drop * 35)
        reasons.append(f"price cut ${was - price:,} ({drop:.0%})")

    if sold_typical and price and price <= 0.95 * sold_typical:
        s += 4
        reasons.append(f"below what similar ones sold for (~${sold_typical:,})")

    if listing["motivated"]:
        s += 3
        reasons.append("motivated seller")

    age = max(1, NOW_YEAR - (listing["year"] or NOW_YEAR) + 1)
    # when "typical" is already adjusted for this machine's miles/hours, use is priced in - no extra points
    if not usage_adjusted and listing["year"] and (listing["miles"] is not None or listing["hours"] is not None):
        mpy = (listing["miles"] or 0) / age
        hpy = (listing["hours"] or 0) / age
        c = cfg(listing["category"])
        if (listing["miles"] is not None and mpy < c.get("low_mpy", 800)) or (listing["hours"] is not None and hpy < c["low_hpy"]):
            s += 3
            reasons.append("low use")
        elif mpy > c.get("high_mpy", 3000) or hpy > c["high_hpy"]:
            s -= 6
            reasons.append("high use")

    if listing["is_new"] == 1:
        s -= 5
        reasons.append("new / dealer stock")

    dealer = listing["is_dealer"] == 1 or listing["seller_type"] == "dealer"
    if dealer:
        s -= 10
        reasons.append("dealer")

    flags = json.loads(listing["red_flags"] or "[]")
    if flags:
        s = min(s - min(35, 15 * len(flags)), 70)   # never "Great" (or alert-worthy) with known problems
        reasons.append("red flags: " + ", ".join(flags))

    if deal_pct is not None and deal_pct > 0.45:
        s = min(s, 78)
        reasons.append("far below market - verify it's real")

    return max(0, min(100, round(s))), deal_pct, reasons


def utv_fit(r) -> str | None:
    """Can this trailer carry a 4-seat UTV (~11.5-13.5 ft long, 62-64 in wide, 2,000-2,500 lb)?
    yes / maybe / no / unknown; None for anything that isn't a trailer."""
    if _v(r, "category") != "trailer":
        return None
    length, width, height = _v(r, "len_ft"), _v(r, "width_ft"), _v(r, "height_ft")
    axles, gvwr, kind = _v(r, "axles"), _v(r, "gvwr_lb"), _v(r, "trailer_type")
    if kind == "dump":
        return "no"
    if length is None or width is None:
        return "unknown"
    if length < 12 or width < 6.3 or (gvwr is not None and gvwr < 3500) or (kind == "enclosed" and height and height < 6):
        return "no"
    strong = (axles or 0) >= 2 or (gvwr or 0) >= 5000
    tall_enough = kind != "enclosed" or (height or 0) >= 6.5
    if length >= 14 and width >= 6.5 and strong and tall_enough:
        return "yes"
    return "maybe"


FIT_NOTE = {"yes": "Fits a 4-seat UTV (14 ft+ deck, 7 ft wide class, tandem/5,000 lb+).",
            "maybe": "Might fit a 4-seat UTV - check deck length (14 ft+), width between fenders (~80 in), "
                     "capacity (5,000 lb+ GVWR){height}.",
            "no": "Too small or light for a 4-seat UTV - only worth it as a general-purpose trailer.",
            "unknown": "Size not stated - ask for deck length, width between fenders and GVWR."}


def _nice(x: float) -> int:
    """Round down to a number people actually offer."""
    step = 50 if x < 2000 else 100 if x < 10000 else 250
    return int(x // step * step)


def offer(listing, expected, deal_pct, comps, tow_capacity: int | None = None,
          sell_days: float | None = None, sold_typical: int | None = None, sold_note: str | None = None) -> dict | None:
    """Opening offer / target / walk-away, plus talking points.

    Without a typical price (few comps, or new dealer stock) it still gives "rough" numbers off the
    asking price - standard private-sale negotiating room - and says so. None only for no/placeholder prices.
    """
    price = listing["price"]
    if not price or price < 100 or (expected and price < 0.2 * expected):
        return None
    if listing["is_new"] == 1:
        expected = None           # used comps don't apply to new units
    rough = not expected
    now = time.time()
    days = int((now - (listing["listed_at"] or listing["first_seen"])) / 86400)
    dealer = listing["is_dealer"] == 1 or listing["seller_type"] == "dealer"
    great = deal_pct is not None and deal_pct >= 0.15
    notes = []

    if great:
        room = 0.03
        notes.append(f"Already {deal_pct:.0%} under typical - don't lowball; offer close to asking and move fast.")
    else:
        room = 0.07
        if listing["motivated"]:
            room += 0.03
            notes.append("Seller signals flexibility (OBO / must sell) - lead with that.")
        if dealer:
            room += 0.03
            notes.append("Dealer - ask for fees/freight to be waived or accessories thrown in.")
    if days >= 45:
        room += 0.04
        notes.append(f"Listed {days} days - long enough that the seller is likely open to offers.")
    elif days >= 21:
        room += 0.02
        notes.append(f"Listed {days} days ago - interest has probably cooled.")
    elif days <= 1:
        room = max(0.02, room - 0.03)
        notes.append("Just listed - expect other buyers; a quick, firm offer beats a low one.")

    was = max(x for x in (listing["strike_price"], listing["first_price"], 0) if x is not None)
    if was > price:
        notes.append(f"Already cut ${was - price:,} from ${was:,} - they may go lower.")
    if sell_days:
        pace = f"Similar {listing['family']} listings are usually gone in about {sell_days:.0f} days"
        notes.append(pace + (" - this one is past that, so there's room to push." if days > sell_days * 1.5 else
                             " - good ones move fast." if sell_days <= 10 else "."))

    fair = price if rough else min(price, expected)
    if sold_typical and not rough:
        # what similar ones actually went for already includes the haggling, so anchor there with less extra room
        fair = min(price, sold_typical)
        room *= 0.6
    walk = price if fair >= price else _nice(fair)            # never "walk away above" something under the ask
    aim = _nice(fair * (1 - room))
    gap = 0.03 if great else 0.06                              # opening offer sits a bit under the target
    open_ = max(_nice(aim * (1 - gap)), _nice(price * (0.95 if great else 0.75)))   # never insulting / never lose a steal
    open_ = min(open_, aim)

    noun = f"similar {listing['family']}" if listing["family"] else "similar machines"
    if rough and listing["is_new"] == 1:
        notes.insert(0, "New / dealer unit: these are rough numbers off the asking price. Compare against MSRP and other "
                        "dealers' prices, and ask for freight, prep and doc fees to be waived.")
    elif rough:
        notes.insert(0, f"Not enough {noun} listings yet ({comps}) to know the market price - these are standard "
                        "private-sale numbers off the asking price. Look at a few comparable listings before offering.")
    else:
        notes.insert(0, f"Typical asking price for {noun} is about ${expected:,} ({comps} comps nearby)."
                     + (f" {sold_note}" if sold_note else ""))
    flags = json.loads(listing["red_flags"] or "[]")
    if flags:
        notes.append("Known issues (" + ", ".join(flags) + "): get a repair estimate and take it off these numbers.")
    if listing["category"] == "trailer":
        fit = utv_fit(listing)
        notes.append(FIT_NOTE[fit].format(height=", interior height ~7 ft" if listing["trailer_type"] == "enclosed" else ""))
        if tow_capacity and listing["gvwr_lb"] and listing["gvwr_lb"] > tow_capacity:
            notes.append(f"Rated {listing['gvwr_lb']:,} lb loaded - more than your truck's {tow_capacity:,} lb towing "
                         "rating. Fine as long as you never load it near capacity.")
        notes.append("Check: title and VIN plate match, tire date codes (older than ~6 years = budget new tires), "
                     "wheel bearings, floor boards, lights, and that the brakes work - brakes need a brake controller in the truck.")
    else:
        notes.append("Bring cash, check the title/VIN, and ask for maintenance records.")
    return {"open": open_, "aim": aim, "walk": walk, "notes": notes, "rough": rough}


def sold_ratios(con) -> dict[str, tuple[float, int]]:
    """category -> (median of last price when sold / typical asking at the time, how many). >= 8 sold only."""
    out: dict[str, list] = {}
    for r in con.execute("""SELECT category, COALESCE(end_price, price) p, expected FROM listings
                            WHERE status = 'sold' AND relevant = 1 AND COALESCE(is_new, 0) = 0
                              AND expected IS NOT NULL AND COALESCE(end_price, price) >= 300"""):
        q = r["p"] / r["expected"]
        if 0.4 <= q <= 1.6:                      # ignore mismatches (wrong family, parts, typos)
            out.setdefault(r["category"], []).append(q)
    return {c: (min(1.05, max(0.75, statistics.median(v))), len(v)) for c, v in out.items() if len(v) >= 8}


def mark_ended(con) -> None:
    """Record when a listing sold / disappeared and what it was last asking - the closest thing to a
    sold price we can see. A listing that comes back is un-ended."""
    now = int(time.time())
    con.execute("""UPDATE listings SET ended_at = ?, end_price = price
                   WHERE status IN ('sold', 'gone') AND ended_at IS NULL""", (now,))
    con.execute("""UPDATE listings SET ended_at = NULL, end_price = NULL
                   WHERE status IN ('active', 'pending') AND ended_at IS NOT NULL""")
    con.commit()


def days_to_sell(con) -> dict[str, float]:
    """family -> median days listed before it sold / disappeared (last 120 days, used only, >= 5 of them)."""
    out: dict[str, list] = {}
    for r in con.execute("""SELECT family, ended_at - COALESCE(listed_at, first_seen) secs FROM listings
                            WHERE ended_at IS NOT NULL AND relevant = 1 AND family IS NOT NULL AND seen_active = 1
                              AND COALESCE(is_new, 0) = 0 AND ended_at > ?""", (int(time.time()) - 120 * 86400,)):
        if r["secs"] and r["secs"] > 0:
            out.setdefault(r["family"], []).append(r["secs"] / 86400)
    return {f: statistics.median(v) for f, v in out.items() if len(v) >= 5}


def rescore_all(con) -> None:
    mark_ended(con)
    # 1. equipment from the ad text (cheap, deterministic - recomputed every run)
    for r in con.execute("SELECT id, category, title, description, extras, summary FROM listings "
                         "WHERE parsed = 1 AND relevant = 1 AND status != 'gone' AND equipment IS NULL").fetchall():
        con.execute("UPDATE listings SET equipment = ? WHERE id = ?", (json.dumps(detect(r)), r["id"]))
    con.commit()
    # 2. what each feature is worth, from last run's pre-equipment typical prices
    effects = equipment_effects(con)
    comps = _comps(con)
    try:
        tow = int(con.execute("SELECT value FROM settings WHERE key = 'tow_capacity_lb'").fetchone()[0])
    except (TypeError, ValueError):
        tow = None
    pace = days_to_sell(con)
    sold_comps = _comps(con, sold=True)
    ratios = sold_ratios(con)
    rows = con.execute(
        "SELECT * FROM listings WHERE parsed = 1 AND relevant = 1 AND status IN ('active', 'pending')").fetchall()
    for r in rows:
        exp, n, base, note, pre = expected_price(r, comps, effects)
        if r["is_new"] == 1:
            exp = base = note = pre = None
        # Sold listings (Facebook "Sold" filter). Their price is the last listed price, so they mostly tell us
        # whether things are selling at asking. Only when similar ones sold clearly under typical asking do we
        # show a separate "typically sells around" number and anchor the offer on it.
        sold_exp = sold_n = basis = sold_note = None
        if exp:
            se, sn, *_ = expected_price(r, sold_comps, effects)
            if se and sn >= 4:
                if se < 0.97 * exp:
                    sold_exp, sold_n, basis = int(max(0.75 * exp, se)), sn, "sold"
                    sold_note = f"Similar ones have sold for about ${sold_exp:,} ({sn} marked sold)."
                else:
                    sold_n, basis = sn, "holding"
                    sold_note = f"Similar ones have been selling at about asking ({sn} marked sold) - sellers are getting their price."
            elif r["category"] in ratios and 0.8 <= ratios[r["category"]][0] < 0.97:
                q, qn = ratios[r["category"]]
                sold_exp, sold_n, basis = int(exp * q), qn, "est"
                sold_note = (f"Sold prices in this category run about {(1 - q) * 100:.0f}% under asking "
                             f"({qn} sold), so expect about ${sold_exp:,}.")
        s, pct, reasons = score(r, exp, n, usage_adjusted=bool(note and "for use" in note), sold_typical=sold_exp)
        o = offer(r, exp, pct, n, tow, pace.get(r["family"]), sold_exp, sold_note)
        fit = utv_fit(r)
        con.execute(
            """UPDATE listings SET expected=?, expected_base=?, expected_pre=?, usage_note=?, comps=?, deal_pct=?, score=?,
                 reasons=?, offer_open=?, offer_aim=?, offer_walk=?, offer_notes=?, offer_rough=?, utv_fit=?,
                 expected_sold=?, sold_comps=?, sold_basis=? WHERE id=?""",
            (exp, base, pre, note, n, pct, s, json.dumps(reasons),
             o and o["open"], o and o["aim"], o and o["walk"], json.dumps(o["notes"]) if o else None,
             o and int(o["rough"]), fit, sold_exp, sold_n, basis, r["id"]))
    con.commit()
