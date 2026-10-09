"""The buy box: per category, what Jon would actually buy. Instant Telegram alerts and the morning
digest both go through it, so a listing outside it never interrupts him.

A rule (settings.alert_rules[category]) holds:
  enabled   instant deal alerts         fresh   just-listed alerts      digest  in the morning digest
  models    families to include ([] = any)
  min_year / max_price / max_miles / max_hours   '' = no limit
  within_mi how far he will drive ('' = the search radius + 10)
Unknown values pass: a listing that doesn't state its miles isn't ruled out for it (the card says so).
"""
from . import geo

FIELDS = ("min_year", "max_price", "max_miles", "max_hours", "within_mi")


def _num(rule, k):
    try:
        return int(float(rule.get(k))) if str(rule.get(k) or "").strip() else None
    except (TypeError, ValueError, OverflowError):
        return None


def reach(rule, radius: int) -> int:
    return _num(rule, "within_mi") or radius + 10


def fits(rule: dict, r, dist: float | None, radius: int) -> bool:
    """Does this listing fall inside the category's buy box?"""
    models = rule.get("models") or []
    if models and r["family"] not in models:
        return False
    if dist is not None and dist > reach(rule, radius):
        return False
    doubt = bool(r["usage_doubt"]) if "usage_doubt" in r.keys() else False
    for k, col, over in (("min_year", "year", False), ("max_price", "price", True),
                         ("max_miles", "miles", True), ("max_hours", "hours", True)):
        lim, val = _num(rule, k), r[col]
        if lim is None or val is None or (doubt and col in ("miles", "hours")):
            continue
        if (val > lim) if over else (val < lim):
            return False
    return True


def distance_fn(con, st):
    home = (float(st["home_lat"]), float(st["home_lon"]))
    places = {r["place"]: (r["lat"], r["lon"]) for r in con.execute("SELECT * FROM geocache")}
    return lambda r: geo.distance(r, home, places)
