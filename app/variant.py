"""Sub-model within a family: engine class, Ranger XP vs non-XP, Defender HD number.

A family such as "Ranger Crew 1000" or "Polaris Sportsman" holds machines that sell thousands apart: a Crew 900,
a 1000 non-XP, an XP 1000 and a 1500; a Sportsman 450 and a 1000. Priced against the whole family, the smaller
one always looked like a deal and the bigger one overpriced (11/11 review: Defender MAX HD9 -19% vs "typical",
Ranger 1000 non-XP -14 to -20%, Sportsman 1000 +58%, Outlander 450 -16%). score._expected_one narrows comps to
the same sub-model when there are enough of them - the same idea as track length for snowmobiles.
"""
import re

APPLIES = ("utv4", "utv2", "atv")
MIN_COMPS = 8        # same sub-model comps needed in the family before comps are narrowed to them

# engine sizes as sellers write them, when the model didn't give a cc; not "500 miles" or "$1000"
_SIZE = re.compile(r"(?<![$\d.,])\b(1500|1000|999|976|900|875|850|800|760|750|700|650|570|520|500|450|420|400|350|330|300|250)\b"
                   r"(?!\s*(?:miles?|mi|hours?|hrs?|lbs?|pounds?|obo|firm|cash|dollars?)\b)")
_HD = re.compile(r"\bhd\s?-?(\d{1,2})\b")
# Ranger 1000: the XP is the bigger-engined, better-equipped model; these editions only come as XPs
_XP = re.compile(r"\bxp\b|north\s?star|trail\s?boss|texas|high\s?lifter|waterfowl|back\s?country")
CLASSES = ((1400, "1500 class"), (950, "1000 class"), (840, "850-900 class"), (690, "700-800 class"),
           (600, "650 class"), (540, "570 class"), (430, "450-500 class"), (330, "330-420 class"), (0, "under 330cc"))


def _get(row, key):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def variant(row) -> str | None:
    """Short label of the sub-model ("HD9", "1000 non-XP", "570 class", "diesel"), or None when the ad doesn't say
    (then the listing is compared with the whole family, as before)."""
    if _get(row, "category") not in APPLIES:
        return None
    fam = _get(row, "family") or ""
    text = " ".join(str(_get(row, k) or "") for k in ("title", "model", "trim")).lower()
    if "Defender" in fam:      # HD9 and HD10 share an engine size; the HD number is the model
        m = _HD.search(text)
        if not m:
            return None
        return "HD5-HD8" if m.group(1) in ("5", "7", "8") else f"HD{m.group(1)}"
    if "diesel" in text or "diesel" in str(_get(row, "engine") or "").lower():
        return "diesel"
    cc = _get(row, "cc")
    if not cc:
        m = _SIZE.search(text)
        cc = int(m.group(1)) if m else None
    if not cc:
        return None
    label = next(name for floor, name in CLASSES if cc >= floor)
    if "Ranger" in fam and label == "1000 class":
        return "1000 XP" if _XP.search(text) else "1000 non-XP"
    return label
