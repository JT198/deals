"""How serious is a red flag?

The model writes red flags as free text, and until 11/14 every one of them cost the same: 15 points, a score
capped at 70, and the listing dropped from the comps. But the commonest flags on 38% of private ATVs were
"needs battery", "sold as-is" and "no title" - next to "does not run". Three levels now:

  PROBLEM  the machine may not be what the price assumes: doesn't run, engine / transmission work, salvage,
           frame or flood damage, sat for years, scam signs. Scored as before and kept out of the comps.
  NOTE     a known, modest cost or a cosmetic issue: battery, tires, carb clean, torn seat, cracked windshield,
           surface rust, a dead speedometer. A few points off; still a fair comp.
  NOISE    not a defect at all: "sold as-is" (every private sale is), a feature the model never had, no
           description. Ignored.

Anything the patterns don't recognise is a NOTE: the model was told to list real concerns, but an unknown one
should not sink a listing on its own. "No title" is a PROBLEM only for a trailer big enough to be titled - Minnesota and
Wisconsin register ATVs, UTVs and snowmobiles through the DNR without issuing titles, and small utility trailers
are registered without one, so a bill of sale is normal for all of those.
"""
import json
import re

NOISE, NOTE, PROBLEM = 0, 1, 2
LABEL = {NOISE: "noise", NOTE: "note", PROBLEM: "problem"}

_NOISE = re.compile(
    r"\b(sold|selling|sale)\s+(is\s+)?as[\s-]?is\b|^as[\s-]?is\b|\bno\s+warranty\b|\bno\s+description\b|\bno\s+returns?\b"
    r"|\b(no\s+trailer|trailer\s+not\s+included|no\s+ramps?|no\s+spare(\s+tire)?|no\s+battery\s+included)\b"
    r"|^no\s+(heat|a/?c|air\s+conditioning|power\s+steering|electric\s+start|hour\s+meter|winch|cab|roof|windshield|doors?|radio|reverse\s+camera)"
    r"(\s+(or|and|/)\s+(heat|a/?c|air\s+conditioning|power\s+steering))?\b"
    r"|\bvintage\s+model\b|\b(cash\s+only|no\s+trades?|price\s+(is\s+)?firm)\b|^(high|low)\s+(miles|mileage|hours)\b")

_PROBLEM = re.compile(
    # doesn't run / won't start
    r"\b(does\s*n[o']?t|doesnt|will\s+not|won'?t|wont|can'?t|cannot|not)\s+(currently\s+)?(run|start|move|turn\s+over|crank|stay\s+running|idle|shift|engage)"
    r"|\bnot\s+(currently\s+)?running\b|\bnon[\s-]?run(ning|ner)\b|\bno\s+(spark|compression|start)\b|\blow\s+compression\b"
    # engine / drivetrain
    r"|\b(engine|motor)\s+(is\s+)?(locked|seized|blown|bad|knock\w*|shot|toast|stuck)|\b(seized\s+(engine|motor)|blown\s+(motor|engine|head|up))\b"
    r"|\b(bad|blown|junk)\s+(motor|engine|trans(mission)?|rear\s+end|diff\w*)\b"
    r"|\bneeds?\s+(a\s+)?(new\s+|another\s+)?(engine|motor|trans(mission)?|top\s+end|bottom\s+end|rebuild|crank(shaft)?|pistons?|rings|head\s+gasket"
    r"|diff(erential)?|hydro(s|static)?(\s+pump)?|wheel\s+motor|pump)\b"
    r"|\bneeds?\s+(engine|motor|transmission|trans|mechanical|major|hydro|electrical)\s+(work|repair)"
    r"|\b(engine|motor|transmission|trans|hydro\w*)\s+needs?\s+(work|repair|rebuil\w+|replac\w+)"
    r"|\b(transmission|trans|hydro\w*)\b.{0,20}\b(slip\w*|issue|problem|leak)"
    r"|\b(smokes?|smoking|burns?\s+oil|knock(s|ing)?|overheat\w*|milky\s+oil|rod\s+knock)\b"
    # condition unknown / project
    # (the machine as a whole: "brakes need work" or "plow needs repair" is a note, not this)
    r"|^(it\s+|unit\s+|machine\s+|both\s+|one\s+(unit\s+)?|still\s+)?needs?\s+(some\s+|a\s+little\s+|lots?\s+of\s+|a\s+lot\s+of\s+|major\s+)?"
    r"(work|repairs?|tlc|restoration|fixing)\b|\bmechanic'?s?\s+special\b"
    r"|\bproject\b|\bparts\s+(machine|only|sled|quad|bike|unit|mower)\b|\bfor\s+parts\b"
    r"|^(unknown\s+|undiagnosed\s+|has\s+|some\s+)?(mechanical|electrical)\s+(issue|problem)s?\b"
    r"|\b(sitting|sat|stored|parked)\s+(for\s+)?(a\s+)?(#|\d+|couple|few|several|many)?\s*(of\s+)?(years|yrs|a\s+while|a\s+long\s+time)\b"
    r"|\bhas\s*n[o']?t\s+(run|been\s+(run|started|used))\b|\bunknown\s+(mechanical|condition|if\s+it\s+runs)"
    # damage
    r"|\b(frame|chassis)\s+(is\s+)?(damage\w*|bent|cracked|rust\w*|rot\w*|broken)|\b(bent|broken|cracked)\s+(frame|axle|a-?arm|chassis|spindle)\b"
    r"|\b(accident|wreck\w*|crash\w*|roll(ed)?[\s-]?over|rolled|flood\w*|fire|water)\s*(damage\w*)?\b(?<!water)|\b(sunk|submerged|totaled)\b"
    r"|\brott?(ed|en|ing)\s+(floor|frame|deck|wood|boards?)|\bfloor\s+(is\s+)?rott?(ed|en|ing)"
    # title / legal (title words for trailers only - see level())
    r"|\b(salvage|rebuilt|branded|junk)\s+title\b|\bstolen\b|\bno\s+vin\b|\blien\b"
    # scams / unreal prices
    r"|\bscam\w*|\bshipping\s+only\b|\bdeposit\b|\bdeployed\b|\b(monthly|down)\s+payment\b|\bprice\s+(discrepancy|is\s+(a\s+)?(down\s+)?payment)"
    r"|\bpayments?\s+of\b|\btoo\s+good\s+to\s+be\s+true\b")

_TITLE = re.compile(r"\bno\s+(clear\s+)?title\b|\bbill\s+of\s+sale\b|\bno\s+paperwork\b|\bno\s+registration\b|\bmissing\s+title\b|\blost\s+title\b")
_NOT_A_PROBLEM = re.compile(r"\b(rebuilt|new|fresh)\s+(engine|motor|top\s+end|trans(mission)?)\b|\b(engine|motor)\s+(was\s+|just\s+)?rebuilt\b")


def heavy_trailer(row) -> bool:
    """A trailer big enough that Minnesota titles it (over about 4,000 lb gross): no title there is a real problem.
    Small utility trailers are only registered, so "bill of sale only" is normal for them."""
    def g(k):
        try:
            return row[k]
        except (KeyError, IndexError, TypeError):
            return None
    return g("category") == "trailer" and bool((g("gvwr_lb") or 0) > 4000 or (g("axles") or 0) >= 2 or (g("len_ft") or 0) >= 16)


def level(flag: str, category: str | None = None, heavy: bool = False) -> int:
    """NOISE / NOTE / PROBLEM for one red-flag string. heavy: the listing is a trailer that should have a title."""
    f = re.sub(r"\s+", " ", str(flag or "").lower().strip())
    if not f:
        return NOISE
    if _TITLE.search(f):
        return PROBLEM if category == "trailer" and heavy else NOTE
    if _NOT_A_PROBLEM.search(f) and not re.search(r"\bneeds?\b|\bbad\b|\bblown\b", f):
        return NOTE            # work that was done, not work that is owed
    if _PROBLEM.search(f):
        return PROBLEM
    if _NOISE.search(f):
        return NOISE
    return NOTE


def levels(red_flags, category: str | None = None, heavy: bool = False) -> list[int]:
    """Level of each flag in a listing's red_flags (a JSON string or a list)."""
    if isinstance(red_flags, str):
        try:
            red_flags = json.loads(red_flags or "[]")
        except ValueError:
            red_flags = []
    return [level(f, category, heavy) for f in (red_flags or [])]


def worst(red_flags, category: str | None = None, heavy: bool = False) -> int:
    """The listing's level: its most serious flag (NOISE when it has none)."""
    return max(levels(red_flags, category, heavy), default=NOISE)


def of_row(row) -> list[int]:
    """Levels for a listing row / dict (reads red_flags, category and the trailer's size)."""
    try:
        return levels(row["red_flags"], row["category"], heavy_trailer(row))
    except (KeyError, IndexError, TypeError):
        return []
