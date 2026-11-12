"""Sub-model within a family: engine class, Ranger XP vs non-XP, Defender HD number, mower series, budget brands.

A family such as "Ranger Crew 1000" or "Polaris Sportsman" holds machines that sell thousands apart: a Crew 900,
a 1000 non-XP, an XP 1000 and a 1500; a Sportsman 450 and a 1000. Priced against the whole family, the smaller
one always looked like a deal and the bigger one overpriced (11/11 review: Defender MAX HD9 -19% vs "typical",
Ranger 1000 non-XP -14 to -20%, Sportsman 1000 +58%, Outlander 450 -16%; mowers: Bad Boy MZ -23% vs Rogue +50%,
Scag Cheetah +59%, Kubota Z700 +54%; jet skis: Yamaha VX under 850cc -26%). score._expected_one narrows comps to
the same sub-model when there are enough of them - the same idea as track length for snowmobiles.
"""
import re

APPLIES = ("utv4", "utv2", "atv", "pwc", "mower")
MIN_COMPS = 8        # same sub-model comps needed in the family before comps are narrowed to them
MIN_BY_CAT = {"mower": 6}     # mower families are small


def min_comps(category) -> int:
    return MIN_BY_CAT.get(category, MIN_COMPS)


# engine sizes as sellers write them, when the model didn't give a cc; not "500 miles" or "$1000"
_SIZE = re.compile(r"(?<![$\d.,])\b(1500|1000|999|976|900|875|850|800|760|750|700|650|570|520|500|450|420|400|350|330|300|250)\b"
                   r"(?!\s*(?:miles?|mi|hours?|hrs?|lbs?|pounds?|obo|firm|cash|dollars?)\b)")
_HD = re.compile(r"\bhd\s?-?(\d{1,2})\b")
# Defender editions that only come with the HD10 engine
_HD10_ONLY = re.compile(r"\blimited\b|\blone\s?star\b|\bx\s?-?mr\b|\bpro\b")
# Ranger 1000: the XP is the bigger-engined, better-equipped model; these editions only come as XPs
_XP = re.compile(r"\bxp\b|north\s?star|trail\s?boss|texas|high\s?lifter|waterfowl|back\s?country")
CLASSES = ((1400, "1500 class"), (950, "1000 class"), (840, "850-900 class"), (690, "700-800 class"),
           (600, "650 class"), (540, "570 class"), (430, "450-500 class"), (330, "330-420 class"), (0, "under 330cc"))
PWC_CLASSES = ((1700, "1800cc"), (1400, "1500-1630cc"), (950, "1000-1100cc"), (850, "900cc"), (0, "under 850cc"))
# In the catch-all "Other ..." families a $6k Vitacci was priced against CFMoto, Arctic Cat and Segway machines
_BUDGET = re.compile(r"\b(vitacci|odes|landmaster|land\s?master|coleman|massimo|hisun|bennche|kandi|tao\s?(tao|motor)?|trail\s?master"
                     r"|axis|big\s?horn|tracker|bms|linhai|joyner|kayo|aodes|ssr|tgb|icebear|ice\s?bear|rps|venom|dongfang|bulldog)\b")

# Zero-turns: the family is often just the brand, and a brand's homeowner line and its commercial line are
# thousands apart. First match wins. Names not listed stay unlabeled (compared with the whole family).
MOWER_SERIES = {
    "Bad Boy": (("Rogue / Rebel / Renegade", r"\b(rogue|rebel|renegade|revolt)\b"), ("Outlaw", r"\boutlaw\b"),
                ("Maverick", r"\bmaverick\b"), ("ZT Elite", r"\bzt\s?elite\b"), ("MZ", r"\bmz\b|\bmagnum\b|\brambler\b")),
    "Hustler": (("Super Z / X-One", r"\bsuper\s?z\b|\bx-?\s?one\b|\bhyper\s?drive\b"), ("FasTrak", r"\bfas\s?trak\b"),
                ("Raptor / Dash", r"\braptor\b|\bdash\b")),
    "Scag": (("Turf Tiger", r"\bturf\s?tiger\b"), ("Cheetah", r"\bcheetah\b"), ("V-Ride", r"\bv-?\s?ride\b"),
             ("Tiger Cat", r"\btiger\s?cat\b"), ("Patriot", r"\bpatriot\b"), ("Liberty / Freedom Z", r"\bliberty\b|\bfreedom\b")),
    "Exmark": (("Lazer Z", r"\blazer\b"), ("Radius", r"\bradius\b"), ("Pioneer", r"\bpioneer\b"), ("Quest", r"\bquest\b"),
               ("stand-on (Vantage / Staris)", r"\bvantage\b|\bstaris\b"), ("walk-behind (Turf Tracer)", r"\bturf\s?tracer\b|\bwalk\s?-?behind\b")),
    "Ferris": (("IS / ISX", r"\bisx?\s?-?\d{3,4}|\bisx\b"), ("stand-on (SRS)", r"\bsrs\b|\bstand\s?-?on\b"), ("S series", r"\b[345]00\s?s\b")),
    "Gravely": (("Pro-Turn", r"\bpro-?\s?turn\b"), ("stand-on (Pro-Stance)", r"\bpro-?\s?stance\b"), ("ZT", r"\bzt\b|\bzt-?\s?(x|hd|xl)\b")),
    "Kubota Z series": (("Z700 / ZD", r"\bz7\d\d|\bzd\s?-?\d"), ("Z400", r"\bz4\d\d"), ("Z200", r"\bz2\d\d"), ("Z100", r"\bz1\d\d")),
    "Toro TimeCutter": (("MyRide", r"\bmy\s?ride\b"), ("HD / MAX", r"\btimecutter\s+(hd|max)\b|\b(hd|max)\s+\d\d\b")),
    "Other zero-turn": tuple((name, rx) for name, rx in (
        ("Walker", r"\bwalker\b"), ("Grasshopper", r"\bgrasshopper\b"), ("Dixie Chopper", r"\bdixie\b"),
        ("Country Clipper", r"\bcountry\s?clipper\b"), ("Wright", r"\bwright\b"), ("Bobcat", r"\bbob-?\s?cat\b"),
        ("Big Dog", r"\bbig\s?dog\b"), ("Spartan", r"\bspartan\b"), ("Simplicity", r"\bsimplicity\b"),
        ("Snapper", r"\bsnapper\b"), ("Dixon", r"\bdixon\b"), ("Swisher", r"\bswisher\b"), ("Encore", r"\bencore\b"),
        ("battery (EGO / Ryobi / Greenworks)", r"\bego\b|\bryobi\b|\bgreenworks\b|\bbattery\b|\belectric\b"))),
}
MOWER_SERIES = {fam: tuple((name, re.compile(rx)) for name, rx in rules) for fam, rules in MOWER_SERIES.items()}


def _get(row, key):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def variant(row) -> str | None:
    """Short label of the sub-model ("HD9", "1000 non-XP", "570 class", "diesel", "Lazer Z"), or None when the ad
    doesn't say (then the listing is compared with the whole family, as before)."""
    cat = _get(row, "category")
    if cat not in APPLIES:
        return None
    fam = _get(row, "family") or ""
    text = " ".join(str(_get(row, k) or "") for k in ("title", "model", "trim")).lower()
    if cat == "mower":
        return next((name for name, rx in MOWER_SERIES.get(fam, ()) if rx.search(text)), None)
    cc = _get(row, "cc")
    if cat == "pwc":      # names carry horsepower ("GTI 130", "VX 110"), so only a parsed engine size counts
        return next(name for floor, name in PWC_CLASSES if cc >= floor) if cc else None
    if "Defender" in fam:      # HD9 and HD10 share an engine size; the HD number is the model
        m = _HD.search(text)
        if not m:
            return "HD10" if _HD10_ONLY.search(text) else None
        return "HD5-HD8" if m.group(1) in ("5", "7", "8") else f"HD{m.group(1)}"
    if fam.startswith("Other") and _BUDGET.search(f"{text} {str(_get(row, 'make') or '').lower()}"):
        return "budget brand"
    if "diesel" in text or "diesel" in str(_get(row, "engine") or "").lower():
        return "diesel"
    if not cc:
        m = _SIZE.search(text)
        cc = int(m.group(1)) if m else None
    if not cc:
        return None
    label = next(name for floor, name in CLASSES if cc >= floor)
    if "Ranger" in fam and label == "1000 class":
        return "1000 XP" if _XP.search(text) else "1000 non-XP"
    return label
