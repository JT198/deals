"""Expected price from our own comps, then a 0-100 deal score.

Comps are every relevant listing we've seen in the last 180 days (asking
prices, not sold prices - so "expected" means "what these usually list for").
"""
import json
import math
import statistics
import time

DEPRECIATION = 0.08          # per model year, used to line up +/-1 year comps
COMP_WINDOW = 180 * 86400
NOW_YEAR = time.localtime().tm_year


def _comps(con) -> dict[str, list[tuple]]:
    rows = con.execute(
        """SELECT id, family, year, price FROM listings
           WHERE relevant = 1 AND family IS NOT NULL AND year IS NOT NULL
             AND COALESCE(is_new, 0) = 0 AND price >= 1500 AND last_seen >= ?""",
        (int(time.time()) - COMP_WINDOW,)).fetchall()
    by_fam: dict[str, list[tuple]] = {}
    for r in rows:
        by_fam.setdefault(r["family"], []).append((r["id"], r["year"], r["price"]))
    # drop junk prices (payments, deposits, parts) - anything under 30% of the family median
    for fam, lst in by_fam.items():
        med = statistics.median(p for _, _, p in lst)
        by_fam[fam] = [c for c in lst if c[2] >= 0.3 * med]
    return by_fam


def _fit(comps):
    """log(price) = a + b*year, least squares, slope clipped to a sane range."""
    xs = [y for _, y, _ in comps]
    ys = [math.log(p) for _, _, p in comps]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    var = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var if var else 0.08
    b = min(0.15, max(0.03, b))
    return my - b * mx, b


def expected_price(listing, comps_by_fam) -> tuple[int | None, int]:
    fam, year = listing["family"], listing["year"]
    if not fam or not year:
        return None, 0
    others = [c for c in comps_by_fam.get(fam, []) if c[0] != listing["id"]]
    near = [c for c in others if abs(c[1] - year) <= 1]
    if len(near) >= 4:
        adj = [p * (1 + DEPRECIATION) ** (year - y) for _, y, p in near]
        return int(statistics.median(adj)), len(near)
    if len(others) >= 5 and len({y for _, y, _ in others}) >= 2:
        a, b = _fit(others)
        return int(math.exp(a + b * year)), len(others)
    return None, len(others)


def score(listing, expected) -> tuple[int, float | None, list[str]]:
    reasons: list[str] = []
    price = listing["price"]
    deal_pct = None
    if expected and price:
        deal_pct = (expected - price) / expected
        s = 45 + 150 * deal_pct
        if deal_pct >= 0.05:
            reasons.append(f"{deal_pct:.0%} under typical ${expected:,}")
        elif deal_pct <= -0.05:
            reasons.append(f"{-deal_pct:.0%} over typical ${expected:,}")
    else:
        s = 40
        reasons.append("not enough comps yet")

    was = max(x for x in (listing["strike_price"], listing["first_price"], 0) if x is not None)
    if price and was and was > price:
        drop = (was - price) / was
        s += 5 + min(10, drop * 50)
        reasons.append(f"price cut ${was - price:,} ({drop:.0%})")

    if listing["motivated"]:
        s += 5
        reasons.append("motivated seller")

    age = max(1, NOW_YEAR - (listing["year"] or NOW_YEAR) + 1)
    if listing["miles"] is not None or listing["hours"] is not None:
        mpy = (listing["miles"] or 0) / age
        hpy = (listing["hours"] or 0) / age
        if (listing["miles"] is not None and mpy < 800) or (listing["hours"] is not None and hpy < 60):
            s += 4
            reasons.append("low use")
        elif mpy > 3000 or hpy > 200:
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
        s -= min(30, 12 * len(flags))
        reasons.append("red flags: " + ", ".join(flags))

    if deal_pct is not None and deal_pct > 0.45:
        s = min(s, 80)
        reasons.append("far below market - verify it's real")

    return max(0, min(100, round(s))), deal_pct, reasons


def rescore_all(con) -> None:
    comps = _comps(con)
    rows = con.execute(
        "SELECT * FROM listings WHERE parsed = 1 AND relevant = 1 AND status != 'gone'").fetchall()
    for r in rows:
        exp, n = expected_price(r, comps)
        s, pct, reasons = score(r, exp)
        con.execute(
            "UPDATE listings SET expected=?, comps=?, deal_pct=?, score=?, reasons=? WHERE id=?",
            (exp, n, pct, s, json.dumps(reasons), r["id"]))
    con.commit()
