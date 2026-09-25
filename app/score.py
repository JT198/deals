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


def _comps(con) -> dict[str, list[tuple]]:
    """family -> [(id, year|None, price, deck_in|None)] of used asking prices."""
    rows = con.execute(
        """SELECT id, family, year, price, deck_in FROM listings
           WHERE relevant = 1 AND family IS NOT NULL
             AND COALESCE(is_new, 0) = 0 AND price >= 300 AND last_seen >= ?""",
        (int(time.time()) - COMP_WINDOW,)).fetchall()
    by_fam: dict[str, list[tuple]] = {}
    for r in rows:
        by_fam.setdefault(r["family"], []).append((r["id"], r["year"], r["price"], r["deck_in"]))
    # drop junk prices (payments, deposits, parts) - anything under 30% of the family median
    for fam, lst in by_fam.items():
        med = statistics.median(c[2] for c in lst)
        by_fam[fam] = [c for c in lst if c[2] >= 0.3 * med]
    return by_fam


def _fit(comps):
    """log(price) = a + b*year, least squares, slope clipped to a sane range."""
    xs = [c[1] for c in comps]
    ys = [math.log(c[2]) for c in comps]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    var = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var if var else 0.08
    b = min(0.15, max(0.03, b))
    return my - b * mx, b


def expected_price(listing, comps_by_fam) -> tuple[int | None, int]:
    fam, year = listing["family"], listing["year"]
    if not fam:
        return None, 0
    c = cfg(listing["category"])
    others = [x for x in comps_by_fam.get(fam, []) if x[0] != listing["id"]]
    deck = listing["deck_in"]
    if deck:   # mowers: a 42" and a 60" of the same series are different machines
        same_deck = [x for x in others if x[3] and abs(x[3] - deck) <= 6]
        if len(same_deck) >= 4:
            others = same_deck
    if not year:
        return (int(statistics.median(x[2] for x in others)), len(others)) if len(others) >= 5 else (None, len(others))
    dated = [x for x in others if x[1]]
    near = [x for x in dated if abs(x[1] - year) <= c["window"]]
    if len(near) >= 4:
        adj = [x[2] * (1 + c["dep"]) ** (year - x[1]) for x in near]
        return int(statistics.median(adj)), len(near)
    yrs = [x[1] for x in dated]
    if c["fit"] and len(dated) >= 5 and len(set(yrs)) >= 2 and min(yrs) - 1 <= year <= max(yrs) + 1:
        a, b = _fit(dated)
        return int(math.exp(a + b * year)), len(dated)
    return None, len(others)


def score(listing, expected, comps: int = 0) -> tuple[int, float | None, list[str]]:
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
    if listing["year"] and (listing["miles"] is not None or listing["hours"] is not None):
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


def rescore_all(con) -> None:
    comps = _comps(con)
    rows = con.execute(
        "SELECT * FROM listings WHERE parsed = 1 AND relevant = 1 AND status != 'gone'").fetchall()
    for r in rows:
        exp, n = expected_price(r, comps)
        if r["is_new"] == 1:
            exp = None
        s, pct, reasons = score(r, exp, n)
        con.execute(
            "UPDATE listings SET expected=?, comps=?, deal_pct=?, score=?, reasons=? WHERE id=?",
            (exp, n, pct, s, json.dumps(reasons), r["id"]))
    con.commit()
