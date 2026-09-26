"""Equipment that moves a machine's price: cab, heat, A/C, plow, trailer included.

Detected from the ad text (title, description, the model's extras and summary) so no re-parse is
needed. Each feature's worth is learned from the listings themselves (see score.equipment_effects),
starting from the priors below.
"""
import json
import re

# multiplicative features: learned from the data, shrunk toward the prior
MULT_PRIOR = {"cab": 0.08, "heat": 0.05, "ac": 0.03}
# fixed-dollar features: roughly what the add-on sells for on its own
DOLLAR_PRIOR = {"plow": 600, "trailer": 1500}

APPLIES = {
    "utv4": ("cab", "heat", "ac", "plow", "trailer"),
    "utv2": ("cab", "heat", "ac", "plow", "trailer"),
    "atv": ("plow", "trailer"),
}

LABEL = {"cab": "cab", "heat": "heat", "ac": "A/C", "plow": "plow", "trailer": "trailer included"}

PATTERNS = {
    # Polaris "NorthStar" / Can-Am "CAB" editions come with a factory cab (+ heat, and A/C on most)
    "cab": r"\bnorth\s?star\b|\b(full|factory|enclosed|hard|glass|poly|soft|lexan)\s+cab\b|\bcab\b(?!\s*(frame|mount))"
           r"|\bfull(y)?\s+enclosed\b|\bfull\s+doors?\s+(and|&|w/)\s+(windshield|glass)",
    "heat": r"\bnorth\s?star\b|\bhvac\b|\bheat(er)?\b(?!\s*(shield|sink|ed\s+(grips|seats)))|\bcab\s+heat",
    # not a bare "AC": that's also shorthand for Arctic Cat ("AC Wildcat")
    "ac": r"\bnorth\s?star\s+ultimate\b|\bhvac\b|\ba/c\b|\bair\s*condition(ing|er)?\b"
          r"|heat\s*(and|&|/|\+)\s*ac\b|\bac\s*(and|&|/|\+)\s*heat",
    "plow": r"\bplow\b",
    "trailer": r"(with|w/|incl\w*|comes\s+with|plus|\+|&|and)\s+(an?\s+|the\s+|his\s+|my\s+)?"
               r"([\w\-'\"./]+\s+){0,3}trailer\b(?!\s*(hitch|plug|wiring|light))",
}

NEGATIONS = r"\b(no|without|w/o|not|minus)\s+(\w+\s+){0,2}%s|%s\s+(\w+\s+){0,3}(not\s+included|sold\s+separately|" \
            r"available\s+(separately|for\s+(an\s+)?(extra|additional))|extra\s+\$|for\s+an?\s+additional|negotiable\s+separately)"
NEG_WORD = {"cab": r"cab", "heat": r"heat\w*", "ac": r"(a/c|ac|air)", "plow": r"plow", "trailer": r"trailer"}


def detect(row) -> list[str]:
    """Features present in this listing (only those that matter for its category)."""
    cat = row["category"]
    feats = APPLIES.get(cat, ())
    if not feats:
        return []
    extras = row["extras"] or "[]"
    try:
        extras = " ".join(json.loads(extras)) if isinstance(extras, str) else " ".join(extras)
    except ValueError:
        pass
    text = " ".join(str(x or "") for x in (row["title"], row["description"], extras, row["summary"])).lower()
    found = []
    for f in feats:
        if not re.search(PATTERNS[f], text):
            continue
        w = NEG_WORD[f]
        if re.search(NEGATIONS % (w, w), text) and not re.search(r"\bnorth\s?star\b", text):
            continue
        if f == "plow" and re.search(r"plow\s+(mount|frame|push\s*tubes?)\s+only", text):
            continue
        found.append(f)
    if "heat" in found and "cab" not in found:     # heat without a cab is heated grips/seats, not cab heat
        found.remove("heat")
    return found
