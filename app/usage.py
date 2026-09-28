"""Is the stated mileage / hours really the machine's total?

Ads say things like "clutches replaced 60 miles ago" or "800 miles on new top end"; the model sometimes
reads that as the odometer. A wrong low number makes a machine look like a bargain, so when the number is
doubtful we treat use as unknown (no price adjustment) and tell the buyer to ask.
"""
import re
import time

UNIT = {"miles": r"(?:miles?|mi)\b", "hours": r"(?:hours?|hrs?)\b"}
# what follows the number when it measures time since a repair, not the machine's life
SINCE = re.compile(r"\s+(?:ago|since)\b|\s+on\s+(?:the\s+|a\s+|its\s+|it's\s+)?(?:brand\s+)?"
                   r"(?:new|rebuilt|fresh|replacement|top\s+end|rebuild)")
# clear statements of total use
TOTAL = (r"(?:only|just|has|with|original|actual|total|odometer|showing|shows|reads)\s+(?:about\s+|around\s+|under\s+|~\s*)?"
         r"{v}\s*{u}|{v}\s*{u}\s+(?:on\s+(?:it|her|the\s+(?:machine|unit|odometer|clock|sled|ski|engine|motor))|total|original)")
LOW = {"miles": 150, "hours": 10}        # below this on a 2+ year old machine is worth a second look
VEHICLES = ("utv4", "utv2", "atv", "sled", "pwc")


def _get(row, key):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def doubt(row) -> str | None:
    """A plain-English reason the stated use can't be trusted, or None if it looks fine."""
    text = f"{_get(row, 'title') or ''} {_get(row, 'description') or ''}".lower().replace(",", "")
    for field in ("miles", "hours"):
        v = _get(row, field)
        if v is None:
            continue
        hits = list(re.finditer(rf"(?<![\d.]){v}\s*{UNIT[field]}", text))
        since = [m for m in hits if SINCE.match(text, m.end())]
        if hits and len(since) == len(hits):
            return (f"the ad only mentions {v:,} {field} since a repair or replacement - "
                    f"the machine's total {field} aren't stated")
        year = _get(row, "year")
        stated = any(not SINCE.match(text, m.end()) for m in re.finditer(TOTAL.format(v=v, u=UNIT[field]), text))
        if (_get(row, "category") in VEHICLES and _get(row, "is_new") != 1 and year
                and year <= time.localtime().tm_year - 2 and v < LOW[field] and not stated):
            return f"{v:,} {field} is unusually low for a {year} - confirm it with the seller"
    if _get(row, "miles") is None and _get(row, "hours") is None and _get(row, "category") in VEHICLES:
        for field in ("miles", "hours"):   # nothing recorded, but the ad gives a since-repair number
            for m in re.finditer(rf"(?<![\d.])(\d{{1,6}})\s*{UNIT[field]}", text):
                if SINCE.match(text, m.end()):
                    return (f"the ad only mentions {int(m.group(1)):,} {field} since a repair or replacement - "
                            f"the machine's total {field} aren't stated")
    return None
