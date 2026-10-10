"""Equipment that moves a machine's price: cab, heat, A/C, plow, trailer included.

Detected from the ad text (title, description, the model's extras and summary) so no re-parse is
needed. Each feature's worth is learned from the listings themselves (see score.equipment_effects),
starting from the priors below.
"""
import json
import re

# multiplicative features: learned from the data, shrunk toward the prior
# (base_trim / top_trim: the trim level within a model family - see TRIMS)
MULT_PRIOR = {"cab": 0.08, "heat": 0.05, "ac": 0.03, "base_trim": -0.08, "top_trim": 0.06}
# fixed-dollar features: roughly what the add-on sells for on its own
DOLLAR_PRIOR = {"plow": 600, "trailer": 1500}
# ...where a category's add-on is worth something else (a jet ski trailer is cheaper than a UTV trailer)
DOLLAR_BY_CAT = {"pwc": {"trailer": 900}, "sled": {"trailer": 1200}}

APPLIES = {
    "utv4": ("cab", "heat", "ac", "plow", "trailer"),
    "utv2": ("cab", "heat", "ac", "plow", "trailer"),
    "atv": ("plow", "trailer"),
    "pwc": ("trailer",),
    "sled": ("trailer",),
}

TRIM_FEATS = ("base_trim", "top_trim")
LABEL = {"cab": "cab", "heat": "heat", "ac": "A/C", "plow": "plow", "trailer": "trailer included",
         "base_trim": "lower trim", "top_trim": "top trim"}

# Trim level within a family, for families where trim moves the price on its own (cab / heat / A-C editions
# like NorthStar or Defender Limited are already priced as equipment). First match wins, so top trims go first.
# Read from the title, the parsed trim and the start of the description only: "upgraded to RR" deep in an ad
# isn't the trim. Measured 2026-10-04 (asking, year-adjusted): X3 DS ~13% under the family, Pro XP Sport ~12%
# under / Ultimate ~4% over, Commander XT ~12% under / X mr + XT-P ~12% over, RZR XP Turbo ~5-9% over.
_X3 = (("top_trim", r"\bx\s?-?(rs|ds|rc|mr)\b"),
       (None, r"\brs\b|\brr\b|turbo\s*r\b"),           # RS / Turbo R / Turbo RR: the middle
       ("base_trim", r"\bds\b"))
TRIMS = {
    "Maverick X3 MAX": _X3,
    "Maverick X3 (2-seat)": _X3,
    # "sport" only right after the model: dealer names ("... Power and Sport") are common
    "RZR Pro XP 4": (("top_trim", r"\bultimate\b"), (None, r"\bpremium\b"),
                     ("base_trim", r"(xp\s?-?4?|pro)\s+sport\b|^sport$")),
    "RZR XP 4": (("top_trim", r"\bturbo\b"),),
    "RZR XP 1000/Turbo (2-seat)": (("top_trim", r"\bturbo\b"),),
    "Commander MAX": (("top_trim", r"\bx\s?-?mr\b|\bxt-?p\b"), ("base_trim", r"\bxt\b|\bdps\b")),
    "Commander (2-seat)": (("top_trim", r"\bx\s?-?mr\b|\bxt-?p\b"), ("base_trim", r"\bxt\b|\bdps\b")),
}


def trim_level(row) -> str | None:
    """'base_trim' / 'top_trim' / None (middle or not stated)."""
    keys = row.keys() if hasattr(row, "keys") else row
    rules = TRIMS.get(row["family"] if "family" in keys else None)
    if not rules:
        return None
    text = " ".join(str(row[k] or "") for k in ("title", "trim") if k in keys)
    text = (text + " " + str((row["description"] if "description" in keys else "") or "")[:300]).lower()
    parsed = str((row["trim"] if "trim" in keys else "") or "").strip().lower()
    for level, rx in rules:
        if re.search(rx, text) or (parsed and re.search(rx, parsed)):
            return level
    return None

PATTERNS = {
    # Polaris "NorthStar" / Can-Am "CAB" editions come with a factory cab (+ heat, and A/C on most)
    "cab": r"\bnorth\s?star\b|\b(full|factory|enclosed|hard|glass|poly|soft|lexan)\s+cab\b|\bcab\b(?!\s*(frame|mount))"
           r"|\bfull(y)?\s+enclosed\b|\bfull\s+doors?\s+(and|&|w/)\s+(windshield|glass)",
    "heat": r"\bnorth\s?star\b|\bhvac\b|\bheat(er)?\b(?!\s*(shield|sink|ed\s+(grips|seats)))|\bcab\s+heat",
    # not a bare "AC": that's also shorthand for Arctic Cat ("AC Wildcat")
    "ac": r"\bnorth\s?star\s+ultimate\b|\bhvac\b|\ba/c\b|\bair\s*condition(ing|er)?\b"
          r"|heat\s*(and|&|/|\+)\s*ac\b|\bac\s*(and|&|/|\+)\s*heat",
    "plow": r"\bplow\b",
    "trailer": r"(with|w/|incl\w*|comes\s+with|plus|\+|&|and)\s+(an?\s+|the\s+)?"
               r"([\w\-'\"./]+\s+){0,3}trailer\b(?!\s*(hitch|plug|wiring|light|tie|strap|is\s+sold|not\b|sold\b))",
}

NEGATIONS = r"\b(no|without|w/o|not|minus)\s+(\w+\s+){0,2}%s|%s\s+(\w+\s+){0,3}(not\s+included|sold\s+separately|" \
            r"available\s+(separately|for\s+(an\s+)?(extra|additional))|extra\s+\$|for\s+an?\s+additional|negotiable\s+separately)"
# "no cab heater" / "no cab heat" negates the heat, not the cab
NEG_WORD = {"cab": r"cab\b(?!\s*heat)", "heat": r"heat\w*", "ac": r"(a/c|ac|air)", "plow": r"plow", "trailer": r"trailer"}


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
        if f == "plow" and (re.search(r"plow\s+(mount|frame|push\s*tubes?)\s+only", text)
                            or (re.search(r"plow\s*(ready|mount|bracket|frame|prep|tubes?)\b", text)
                                and not re.search(r"\b(plow|blade)\s+(and|&|with|w/|\+)\s+(blade|mount|bracket|frame)"
                                                  r"|\b(with|w/|comes\s+with|incl\w*)\s+(a\s+|the\s+)?(\d+\s*(\"|in\w*)\s+)?plow\b", text))):
            continue      # "plow ready" / "plow mount included" is not a plow
        if f == "trailer" and re.search(r"\b(my|his|our|your|their)\s+trailer\b|\bdeliver\w*\s+(it\s+)?(with|on)\b", text):
            continue      # the seller's own trailer brings it to you; it isn't included
        found.append(f)
    if "heat" in found and "cab" not in found:     # heat without a cab is heated grips/seats, not cab heat
        found.remove("heat")
    level = trim_level(row)
    if level:
        found.append(level)
    return found
