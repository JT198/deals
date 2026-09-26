"""Expected price from our own comps, then a 0-100 deal score.

Comps are every relevant listing we've seen in the last 180 days (asking
prices, not sold prices - so "expected" means "what these usually list for").
"""
import json
import math
import statistics
import time

from .categories import cfg

COMP_WINDOW = 180 * 86400
NOW_YEAR = time.localtime().tm_year


def _v(listing, key):
    """Row or dict field, None if absent (the market chart scores synthetic listings)."""
    try:
        return listing[key]
    except (KeyError, IndexError):
        return None


class Comp(tuple):
    """(id, year, price, deck_in, miles, hours, len_ft, axles)"""
    id, year, price, deck, miles, hours, len_ft, axles = (property(lambda t, i=i: t[i]) for i in range(8))


def _comps(con) -> dict[str, list[Comp]]:
    """family -> used asking prices."""
    rows = con.execute(
        """SELECT id, family, year, price, deck_in, miles, hours, len_ft, axles FROM listings
           WHERE relevant = 1 AND family IS NOT NULL
             AND COALESCE(is_new, 0) = 0 AND price >= 300 AND last_seen >= ?""",
        (int(time.time()) - COMP_WINDOW,)).fetchall()
    by_fam: dict[str, list[Comp]] = {}
    for r in rows:
        by_fam.setdefault(r["family"], []).append(
            Comp((r["id"], r["year"], r["price"], r["deck_in"], r["miles"], r["hours"], r["len_ft"], r["axles"])))
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


def expected_price(listing, comps_by_fam) -> tuple[int | None, int, int | None, str | None]:
    """(expected, comps used, expected before the usage adjustment, plain-English usage note)."""
    fam, year = _v(listing, "family"), _v(listing, "year")
    if not fam:
        return None, 0, None, None
    cat = _v(listing, "category")
    c = cfg(cat)
    others = [x for x in comps_by_fam.get(fam, []) if x.id != _v(listing, "id")]
    deck = _v(listing, "deck_in")
    if deck:   # mowers: a 42" and a 60" of the same series are different machines
        same_deck = [x for x in others if x.deck and abs(x.deck - deck) <= 6]
        if len(same_deck) >= 4:
            others = same_deck
    length = _v(listing, "len_ft")
    if length:   # trailers: a 5x8 single-axle and a 7x18 tandem are different things
        axles = _v(listing, "axles")
        same_size = [x for x in others if x.len_ft and abs(x.len_ft - length) <= 2
                     and (not axles or not x.axles or x.axles == axles)]
        if len(same_size) >= 4:
            others = same_size
    metric = _usage_metric(listing, cat)
    use = _v(listing, metric) if metric else None
    slope = usage_slope(others, metric, cat) if metric else 0.0
    rate = _use_per_year(others, metric) if metric else None

    def comp_use(x):
        u = getattr(x, metric) if metric else None
        if u is None and rate is not None and x.year:
            u = rate * max(1, NOW_YEAR - x.year + 1)     # typical use for its age
        return u

    def usage_factor(u):
        if use is None or u is None:
            return 1.0
        return min(USAGE_CLAMP[1], max(USAGE_CLAMP[0], math.exp(slope * (use - u))))

    if not year:
        # no model year (common for mowers): compare against the whole family, still adjusted for use
        if len(others) < 5:
            return None, len(others), None, None
        base = int(statistics.median(x.price for x in others))
        with_use = [getattr(x, metric) for x in others if metric and getattr(x, metric) is not None]
        typical_use = statistics.median(with_use) if len(with_use) >= 5 else None
        exp = int(base * usage_factor(typical_use))
        n = len(others)
        return exp, n, base, _usage_note(use, typical_use, metric, exp, base)

    dated = [x for x in others if x.year]
    near = [x for x in dated if abs(x.year - year) <= c["window"]]
    if len(near) >= 4:
        aligned = [(x.price * (1 + c["dep"]) ** (year - x.year), comp_use(x)) for x in near]
        base = int(statistics.median(p for p, _ in aligned))
        exp = int(statistics.median(p * usage_factor(u) for p, u in aligned))
        typical_use = [u for _, u in aligned if u is not None]
        typical_use = statistics.median(typical_use) if typical_use else None
        n = len(near)
    else:
        yrs = [x.year for x in dated]
        if not (c["fit"] and len(dated) >= 5 and len(set(yrs)) >= 2 and min(yrs) - 1 <= year <= max(yrs) + 1):
            return None, len(others), None, None
        a, b = _fit(dated)
        base = int(math.exp(a + b * year))
        typical_use = rate * max(1, NOW_YEAR - year + 1) if rate is not None else None
        exp = int(base * usage_factor(typical_use))
        n = len(dated)

    return exp, n, base, _usage_note(use, typical_use, metric, exp, base)


def _usage_note(use, typical_use, metric, exp, base):
    if use is None or typical_use is None or abs(exp - base) < max(100, 0.02 * base):
        return None
    unit = "mi" if metric == "miles" else "hrs"
    more = "more" if use > typical_use else "less"
    return (f"{'−' if exp < base else '+'}${abs(base - exp):,} for use: {use:,} {unit} vs about "
            f"{int(round(typical_use, -2 if typical_use >= 1000 else -1)):,} {unit} on similar ones ({more} use)")


def score(listing, expected, comps: int = 0, usage_adjusted: bool = False) -> tuple[int, float | None, list[str]]:
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

    if listing["motivated"]:
        s += 3
        reasons.append("motivated seller")

    age = max(1, NOW_YEAR - (listing["year"] or NOW_YEAR) + 1)
    # when "typical" is already adjusted for this machine's miles/hours, use is priced in - no extra points
    if not usage_adjusted and listing["year"] and (listing["miles"] is not None or listing["hours"] is not None):
        mpy = (listing["miles"] or 0) / age
        hpy = (listing["hours"] or 0) / age
        c = cfg(listing["category"])
        if (listing["miles"] is not None and mpy < 800) or (listing["hours"] is not None and hpy < c["low_hpy"]):
            s += 3
            reasons.append("low use")
        elif mpy > 3000 or hpy > c["high_hpy"]:
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


def offer(listing, expected, deal_pct, comps, tow_capacity: int | None = None) -> dict | None:
    """Opening offer / target / walk-away, plus talking points. None when we can't price it."""
    price = listing["price"]
    if not expected or not price or listing["is_new"] == 1 or price < 0.2 * expected:
        return None
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

    fair = min(price, expected)
    walk = _nice(fair)
    aim = _nice(fair * (1 - room))
    gap = 0.03 if great else 0.06                              # opening offer sits a bit under the target
    open_ = max(_nice(aim * (1 - gap)), _nice(price * (0.95 if great else 0.75)))   # never insulting / never lose a steal
    open_ = min(open_, aim)

    noun = f"similar {listing['family']}" if listing["family"] else "similar machines"
    notes.insert(0, f"Typical asking price for {noun} is about ${expected:,} ({comps} comps nearby).")
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
    return {"open": open_, "aim": aim, "walk": walk, "notes": notes}


def rescore_all(con) -> None:
    comps = _comps(con)
    try:
        tow = int(con.execute("SELECT value FROM settings WHERE key = 'tow_capacity_lb'").fetchone()[0])
    except (TypeError, ValueError):
        tow = None
    rows = con.execute(
        "SELECT * FROM listings WHERE parsed = 1 AND relevant = 1 AND status != 'gone'").fetchall()
    for r in rows:
        exp, n, base, note = expected_price(r, comps)
        if r["is_new"] == 1:
            exp = base = note = None
        s, pct, reasons = score(r, exp, n, usage_adjusted=note is not None)
        o = offer(r, exp, pct, n, tow)
        fit = utv_fit(r)
        con.execute(
            """UPDATE listings SET expected=?, expected_base=?, usage_note=?, comps=?, deal_pct=?, score=?, reasons=?,
                 offer_open=?, offer_aim=?, offer_walk=?, offer_notes=?, utv_fit=? WHERE id=?""",
            (exp, base, note, n, pct, s, json.dumps(reasons),
             o and o["open"], o and o["aim"], o and o["walk"], json.dumps(o["notes"]) if o else None, fit, r["id"]))
    con.commit()
