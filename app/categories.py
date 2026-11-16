"""What we shop for. Each category has its own model families, searches, and scoring knobs.

dep        yearly depreciation used to line up comps from neighbouring model years
window     +/- model years that count as "near" comps
fit        allow the log-linear price-vs-year fit when near comps are thin
low_hpy / high_hpy   engine hours per year that count as light / heavy use
low_mpy / high_mpy   same for miles per year (default 800 / 3000)
cl_cat     Craigslist category to search (sna = atvs/utvs/snowmobiles, grd = farm & garden, boo = boats/PWC)
usage      prior for how price falls with use: {"miles": (log-change, per N miles), "hours": (...)}.
           Each family blends this with what its own listings show (more data -> more weight).
every_min  how often each search in this category runs on Facebook
quick      searches the 5-minute fast lane also runs (newest first), to be first to message a seller
bands      asking-price edges the deep sweep starts from (sweep.py); a band that comes back full is split further
"""
import re

CATEGORIES = {
    "utv4": {
        "label": "4-seat UTVs", "emoji": "🚙",
        "dep": 0.08, "window": 1, "fit": True, "low_hpy": 60, "high_hpy": 200,
        "usage": {"miles": (-0.025, 1000), "hours": (-0.03, 100)},
        "cl_cat": "sna", "every_min": 60,
        # the fast lane (every 10 min, newest first, QUICK_SEARCHES_PER_RUN at a time): every Polaris / Can-Am 4-seat
        # search - Jon's buy box (11/16: a quarter of box listings were found 15+ hours after posting)
        "quick": ["rzr xp 4", "rzr 4 seater", "rzr pro xp 4", "ranger crew", "ranger xp 1000 crew", "ranger northstar",
                  "can am defender max", "can am maverick max", "can am commander max", "can am max", "polaris general 4",
                  "general xp 4", "4 seat side by side"],
        "bands": [1500, 5000, 8000, 11000, 14000, 17000, 20000, 24000, 29000, 36000, 70000],
        "families": [
            "RZR XP 4", "RZR Pro XP 4", "RZR Pro R 4", "RZR Turbo R 4", "RZR 4 (other)",
            "Ranger Crew 1000", "Ranger Crew 570", "General 4",
            "Defender MAX", "Maverick MAX (2014-2016, pre-X3)", "Maverick X3 MAX", "Maverick R MAX", "Maverick Sport MAX",
            "Maverick Trail/Sport MAX", "Commander MAX",
            "Pioneer 1000-5/6", "Pioneer 700-4", "Talon 4",
            "Teryx4", "Teryx KRX4", "Mule Pro-FXT",
            "Wolverine X4", "Wolverine RMAX4", "Viking VI",
            "Gator XUV 4-seat", "CFMoto ZForce/UForce 4-seat", "Other 4-seat UTV",
        ],
        "searches": [
            "rzr xp 4", "rzr 4 seater", "rzr pro xp 4", "ranger crew", "ranger xp 1000 crew",
            "can am defender max", "can am maverick max", "can am commander max",
            "honda pioneer 1000-5", "kawasaki teryx4", "teryx krx4", "polaris general 4",
            "yamaha wolverine x4", "4 seat side by side", "crew utv", "ranger northstar", "general xp 4",
        ],
    },
    "utv2": {
        "label": "2-seat UTVs", "emoji": "🛻",
        "dep": 0.08, "window": 1, "fit": True, "low_hpy": 60, "high_hpy": 200,
        "usage": {"miles": (-0.025, 1000), "hours": (-0.03, 100)},
        "cl_cat": "sna", "every_min": 120,
        "bands": [1000, 3500, 5500, 7500, 9500, 12000, 15000, 18000, 22000, 28000, 60000],
        "families": [
            "RZR XP 1000/Turbo (2-seat)", "RZR Pro XP/Pro R (2-seat)", "RZR 900/570/Trail (2-seat)",
            "Ranger XP 1000/1500 (2-seat)", "Ranger 570/500 (2-seat)", "General 1000 (2-seat)",
            "Defender (2-seat)", "Maverick (2013-2018, pre-X3, 2-seat)", "Maverick X3 (2-seat)", "Maverick R (2-seat)", "Maverick Sport/Trail (2-seat)", "Commander (2-seat)",
            "Pioneer 1000/700/520 (2-seat)", "Talon 1000 (2-seat)", "Teryx/KRX 1000 (2-seat)",
            "Mule (2-seat)", "Wolverine X2/RMAX2", "Viking (3-seat)", "Gator XUV/HPX",
            "Kubota RTV", "CFMoto ZForce/UForce (2-seat)", "Other 2-seat UTV",
        ],
        "searches": [
            "rzr xp 1000", "polaris ranger xp 1000", "polaris general 1000", "can am defender hd10",
            "can am maverick x3", "honda pioneer 1000", "kawasaki mule", "side by side utv",
            "can am maverick", "polaris ranger",
        ],
    },
    "atv": {
        "label": "ATVs", "emoji": "🏍️",
        "dep": 0.07, "window": 1, "fit": True, "low_hpy": 40, "high_hpy": 150,
        "usage": {"miles": (-0.03, 1000), "hours": (-0.03, 100)},
        "cl_cat": "sna", "every_min": 120,
        "bands": [400, 1200, 2000, 3000, 4000, 5000, 6500, 8500, 11000, 20000],
        "families": [
            "Polaris Sportsman", "Polaris Scrambler", "Can-Am Outlander", "Can-Am Renegade",
            "Honda Foreman/Rubicon", "Honda Rancher", "Honda sport (TRX 250X/400EX/450R)",
            "Yamaha Grizzly", "Yamaha Kodiak", "Yamaha sport (Raptor/Banshee/YFZ)",
            "Kawasaki Brute Force", "Suzuki KingQuad", "Arctic Cat/Textron Alterra", "CFMoto CForce",
            "Youth ATV (under 150cc)", "Other ATV",
        ],
        "searches": [
            "polaris sportsman", "can am outlander", "honda foreman", "honda rancher",
            "yamaha grizzly", "kawasaki brute force", "four wheeler 4x4", "atv 4x4",
            "suzuki atv", "polaris atv",
        ],
    },
    "trike": {
        # vintage ATCs hold or gain value with age, so no depreciation and a wide year window
        "label": "3-wheelers", "emoji": "🛺",
        "dep": 0.0, "window": 3, "fit": False, "low_hpy": 20, "high_hpy": 100,
        "usage": {},
        "cl_cat": "sna", "every_min": 120,
        "bands": [300, 1000, 2000, 3500, 6000, 15000],
        "families": [
            "Honda ATC 250R", "Honda ATC 200 series (200X/200S/200E/Big Red)",
            "Honda ATC 110/125/90/70 (small)", "Honda ATC 185/250ES/other",
            "Yamaha Tri-Z/Tri-Moto", "Kawasaki Tecate", "Suzuki ALT/LT 3-wheeler", "Other 3-wheeler",
        ],
        "searches": ["honda atc", "three wheeler", "3 wheeler", "atc 250r"],
    },
    "mower": {
        "label": "Zero-turn mowers", "emoji": "🌱",
        "dep": 0.08, "window": 1, "fit": True, "low_hpy": 25, "high_hpy": 100,
        "usage": {"hours": (-0.04, 100)},
        "cl_cat": "grd", "every_min": 60,
        "quick": ["zero turn", "zero turn mower"],   # also run by the 5-minute fast lane
        "bands": [300, 1000, 1800, 2600, 3500, 4500, 6000, 8000, 12000, 25000],
        "families": [
            "Cub Cadet RZT S (steering wheel)", "Cub Cadet ZT1/ZT2 (lap bar)", "Cub Cadet Ultima ZT",
            "Cub Cadet Pro Z (commercial)", "John Deere Z300 series", "John Deere Z500 series",
            "John Deere ZTrak commercial (Z700+/Z900+)", "Toro TimeCutter", "Toro TITAN",
            "Toro Z Master (commercial)", "Husqvarna Z200 series", "Husqvarna Z400/Xcite",
            "Ariens Ikon", "Ariens Apex/Edge/Zenith", "Bad Boy", "Hustler", "Scag", "Exmark",
            "Ferris", "Gravely", "Kubota Z series", "Craftsman/Troy-Bilt/MTD zero-turn", "Other zero-turn",
        ],
        "searches": [
            "zero turn mower", "zero turn", "cub cadet zero turn", "john deere zero turn",
            "toro zero turn", "husqvarna zero turn", "bad boy mower", "ariens zero turn",
            "scag zero turn", "exmark", "cub cadet rzt s",
        ],
    },
    "trailer": {
        # compared by type + size (length within 2 ft, same axle count) - see score.expected_price.
        # Alerts, the digest and the buy box only take trailers that fit a 4-seat UTV (score.utv_fit == "yes");
        # the others are still scanned because they price the ones that do.
        "label": "Trailers", "emoji": "🚚",
        "dep": 0.04, "window": 2, "fit": True, "low_hpy": 0, "high_hpy": 10**9,
        "usage": {},
        "cl_cat": "tra", "every_min": 60,
        # the fast lane: trailers big enough for a 4-seat UTV
        "quick": ["utility trailer", "enclosed trailer", "utv trailer", "tandem axle trailer", "car hauler trailer",
                  "7x16 trailer", "flatbed trailer", "tilt trailer", "landscape trailer", "16 ft trailer", "18 ft trailer"],
        "bands": [500, 1500, 2500, 3500, 4500, 6000, 8000, 11000, 16000, 40000],
        "families": [
            "Enclosed cargo", "Enclosed car hauler (8.5 wide)", "Open utility (rails / mesh sides)",
            "Landscape (tandem, rear gate)", "Tilt / car hauler flatbed", "Equipment / deckover",
            "Dump trailer", "Snowmobile / ATV drive-on (sled deck)", "Other trailer",
        ],
        "searches": [
            "tandem axle trailer", "car hauler trailer", "7x16 trailer", "7x14 trailer",
            "flatbed trailer", "tilt trailer", "sled trailer", "landscape trailer", "16 ft trailer", "18 ft trailer",
        ],
    },
    "pwc": {
        # jet skis often sell in pairs on a double trailer: the listing price is split per ski (see score.expected_price)
        "label": "Jet skis", "emoji": "🌊",
        "dep": 0.08, "window": 1, "fit": True, "low_hpy": 20, "high_hpy": 60,
        "usage": {"hours": (-0.06, 100)},
        "cl_cat": "boo", "every_min": 180,
        "bands": [500, 2000, 3500, 5000, 7000, 9000, 12000, 16000, 30000],
        "families": [
            "Sea-Doo Spark", "Sea-Doo GTI/GTS", "Sea-Doo GTX/Wake/Explorer/FishPro (touring)",
            "Sea-Doo RXP/RXT (performance)", "Yamaha EX", "Yamaha VX", "Yamaha FX", "Yamaha GP/SuperJet",
            "Kawasaki Ultra", "Kawasaki STX/SX-R", "Vintage / 2-stroke PWC", "Other jet ski",
        ],
        "searches": [
            "jet ski", "sea doo", "sea doo spark", "waverunner", "yamaha waverunner", "kawasaki ultra", "pwc",
        ],
    },
    "sled": {
        # a 129" trail sled and a 154" mountain sled are different markets: comps match on track length and cc
        "label": "Snowmobiles", "emoji": "❄️",
        "dep": 0.09, "window": 1, "fit": True, "low_hpy": 20, "high_hpy": 80, "low_mpy": 700, "high_mpy": 2500,
        "usage": {"miles": (-0.04, 1000)},
        "cl_cat": "sna", "every_min": 180,
        "bands": [300, 1200, 2200, 3500, 5000, 7000, 9500, 13000, 25000],
        "families": [
            "Ski-Doo MXZ/Renegade/Backcountry (trail/crossover)", "Ski-Doo Summit/Freeride (mountain)",
            "Ski-Doo Expedition/Skandic/Grand Touring (utility/touring)",
            "Polaris Indy/Switchback/Rush (trail/crossover)", "Polaris RMK/Khaos (mountain)",
            "Polaris Titan/Voyageur/Widetrak (utility)",
            "Arctic Cat ZR/Riot/Blast (trail/crossover)", "Arctic Cat M/Alpha One (mountain)",
            "Arctic Cat Norseman/Bearcat/Pantera (utility/touring)",
            "Yamaha Sidewinder/SRViper (trail/crossover)", "Yamaha Venture/Transporter/VK (utility/touring)",
            "Youth snowmobile (120/200cc)", "Vintage snowmobile (pre-2000)", "Other snowmobile",
        ],
        "searches": [
            "snowmobile", "ski doo", "polaris snowmobile", "arctic cat snowmobile", "yamaha sidewinder",
            "polaris indy", "ski doo renegade", "polaris switchback",
        ],
    },
}

FAMILY_CATEGORY = {f: c for c, cfg in CATEGORIES.items() for f in cfg["families"]}

# The model files some ads under a family the machine can't belong to (11/11 review: a $21k RZR Pro S 4 priced as a
# $33k Pro R 4, Maverick R under X3 MAX, 2019 "Turbo S 4" under the 2022+ Turbo R 4, a 2023 Defender under the
# 2014-2016 Maverick). fix_family() repairs what the title settles and drops the family (no typical price, not a
# comp) when the model year rules it out.
FAMILY_FIX_VERSION = 7        # bump when the rules change: every listing is re-checked once (score.rescore_all)
_FIRST_YEAR = {"Maverick X3 MAX": 2017, "Maverick X3 (2-seat)": 2017, "Maverick R MAX": 2024, "Maverick R (2-seat)": 2024,
               "Defender MAX": 2016, "Defender (2-seat)": 2016, "RZR Pro XP 4": 2020, "RZR Pro R 4": 2022,
               "RZR Turbo R 4": 2022, "Teryx KRX4": 2023, "Wolverine RMAX4": 2021, "Talon 4": 2020, "General 4": 2017}
_LAST_YEAR = {"Maverick MAX (2014-2016, pre-X3)": 2018, "Maverick (2013-2018, pre-X3, 2-seat)": 2018}
_MAVERICK_R = re.compile(r"\bmaverick\s+r\b(?!\s*[/&])")
_PRO_S = re.compile(r"\bpro\s?-?s\b")


# Lawn and garden tractors the model files as zero-turns (11/14: 192 of 3,518 live "zero-turns", priced against real
# ones): the words, or a tractor model number, with nothing in the ad that says zero-turn.
_TRACTOR = re.compile(
    r"\b(lawn|garden|yard)\s*tractor\b|\bxt[123]\b|\bltx?\s?-?\d{2,4}\b|\b[esd]\s?1\d\d\b|\bla\s?1\d\d\b|\bl1\d\d\b"
    r"|\bx[357]\d\d\b|\byth\s?\d|\bts\s?\d{3}\b|\blgt\b|\bgt\s?\d{4}\b|\bt\d{3,4}\b|\b(super\s*)?bronco\b|\bpony\b|\bsabre\b|\bstx\s?\d\d\b", re.I)
_ZERO_TURN = re.compile(r"zero|\bz\s?-?turn|\bztr?\b|\bzt\s?-?[\dx]|\bz\d{3}|\brzt\b|time\s?cutter|lap\s*bar|\bz\s?master\b|stand[\s-]?on", re.I)


def is_lawn_tractor(title: str | None, model: str | None = None) -> bool:
    text = f"{title or ''} {model or ''}"
    return bool(_TRACTOR.search(text)) and not _ZERO_TURN.search(text)


# A family belongs to one brand. When the title names a different brand outright, the family is wrong (11/15:
# "2023 Can am xt 700" filed as a Honda Pioneer and shown as a Great deal; 80 live listings like it). The brand
# a family belongs to comes from its model names; the title's brand only from the brand's own name.
_FAMILY_BRAND = {
    "polaris": r"\brzr\b|\branger\b|\bgeneral\b|sportsman|scrambler|\bindy\b|\brmk\b|titan|polaris",
    "canam": r"maverick|defender|commander|outlander|renegade|ski-doo|sea-doo|can-am",
    "honda": r"pioneer|foreman|rubicon|rancher|\btrx\b|\batc\b|talon|honda",
    "yamaha": r"wolverine|grizzly|kodiak|viking|\bex\b|\bvx\b|\bfx\b|superjet|sidewinder|srviper|venture|tri-z|yamaha",
    "kawasaki": r"teryx|\bkrx|\bmule\b|brute\s?force|kawasaki|tecate|\bultra\b|stx",
    "arcticcat": r"arctic\s?cat|alterra|\bzr\b|norseman|bearcat",
    "johndeere": r"gator|john\s?deere", "cfmoto": r"cfmoto|cforce|zforce|uforce", "suzuki": r"suzuki|kingquad",
}
_TITLE_BRAND = {"polaris": r"\bpolaris\b", "canam": r"\bcan[\s-]?am\b|\bcam[\s-]?am\b|\bski[\s-]?doo\b|\bsea[\s-]?doo\b|\bbrp\b",
                "honda": r"\bhonda\b", "yamaha": r"\byamaha\b", "kawasaki": r"\bkawasaki\b", "arcticcat": r"\barctic\s?cat\b",
                "johndeere": r"\bjohn\s?deere\b", "cfmoto": r"\bcf\s?moto\b", "suzuki": r"\bsuzuki\b"}


def family_brand(family: str | None) -> str | None:
    if not family or family.startswith(("Other", "Youth", "Vintage")) or family not in FAMILY_CATEGORY:
        return None
    if FAMILY_CATEGORY[family] in ("mower", "trailer", "trike"):
        return None
    hits = [b for b, rx in _FAMILY_BRAND.items() if re.search(rx, family, re.I)]
    return hits[0] if len(hits) == 1 else None


def title_brands(title: str | None) -> set:
    return {b for b, rx in _TITLE_BRAND.items() if re.search(rx, title or "", re.I)}


def fix_family(family: str | None, year: int | None, title: str | None) -> str | None:
    """The family this listing should be filed under, or None when its model year rules the family out."""
    if not family:
        return family
    t = (title or "").lower()
    fb, tb = family_brand(family), title_brands(title)
    if fb and tb and fb not in tb:
        return None           # the title names another brand: no typical price rather than the wrong one
    if family in ("Maverick X3 MAX", "Maverick X3 (2-seat)") and _MAVERICK_R.search(t) and (not year or year >= 2024):
        family = "Maverick R MAX" if family == "Maverick X3 MAX" else "Maverick R (2-seat)"
    if family in ("Maverick X3 MAX", "Maverick X3 (2-seat)") and year and year < 2017:
        # an X3 is 2017+; an earlier Maverick is the pre-X3 model
        family = "Maverick MAX (2014-2016, pre-X3)" if family == "Maverick X3 MAX" else "Maverick (2013-2018, pre-X3, 2-seat)"
    if family == "RZR Pro R 4" and _PRO_S.search(t):
        family = "RZR Turbo R 4"            # the Pro S replaced the Turbo R for 2025: same class and price range
    if family == "RZR Turbo R 4" and year and year < 2022:
        family = "RZR XP 4"                 # "Turbo" / "Turbo S" 4-seaters before 2022 are XP 4s
    if year and (year < _FIRST_YEAR.get(family, 0) or year > _LAST_YEAR.get(family, 9999)):
        return None
    return family


# ---- 11/16: one checking layer after the AI (resolve) -------------------------------------------------------
# Jon: "this site shouldn't be so dumb with the brand and model". A hand audit of the 220 best-scoring machines
# found ~1 in 12 materially wrong: 2-seaters in 4-seat families, model names the AI filed under the wrong family,
# snowmobiles filed as ATVs. resolve() applies what the ad says outright over what the AI guessed.

# 4-seat family -> its 2-seat counterpart (None: there is no 2-seat version we track)
TWO_SEAT = {
    "RZR XP 4": "RZR XP 1000/Turbo (2-seat)", "RZR Pro XP 4": "RZR Pro XP/Pro R (2-seat)", "RZR Pro R 4": "RZR Pro XP/Pro R (2-seat)",
    "RZR Turbo R 4": "RZR Pro XP/Pro R (2-seat)", "RZR 4 (other)": "RZR 900/570/Trail (2-seat)",
    "Ranger Crew 1000": "Ranger XP 1000/1500 (2-seat)", "Ranger Crew 570": "Ranger 570/500 (2-seat)", "General 4": "General 1000 (2-seat)",
    "Defender MAX": "Defender (2-seat)", "Maverick MAX (2014-2016, pre-X3)": "Maverick (2013-2018, pre-X3, 2-seat)",
    "Maverick X3 MAX": "Maverick X3 (2-seat)", "Maverick R MAX": "Maverick R (2-seat)", "Maverick Sport MAX": "Maverick Sport/Trail (2-seat)",
    "Maverick Trail/Sport MAX": "Maverick Sport/Trail (2-seat)", "Commander MAX": "Commander (2-seat)",
    "Pioneer 1000-5/6": "Pioneer 1000/700/520 (2-seat)", "Pioneer 700-4": "Pioneer 1000/700/520 (2-seat)", "Talon 4": "Talon 1000 (2-seat)",
    "Teryx4": "Teryx/KRX 1000 (2-seat)", "Teryx KRX4": "Teryx/KRX 1000 (2-seat)", "Mule Pro-FXT": "Mule (2-seat)",
    "Wolverine X4": "Wolverine X2/RMAX2", "Wolverine RMAX4": "Wolverine X2/RMAX2", "Viking VI": "Viking (3-seat)",
    "Gator XUV 4-seat": "Gator XUV/HPX", "CFMoto ZForce/UForce 4-seat": "CFMoto ZForce/UForce (2-seat)", "Other 4-seat UTV": "Other 2-seat UTV",
}
FOUR_SEAT = {two: four for four, two in TWO_SEAT.items() if two and four not in ("Pioneer 700-4", "Wolverine RMAX4", "RZR Pro R 4",
                                                                                    "RZR Turbo R 4", "Maverick Trail/Sport MAX", "Teryx KRX4")}
_SEAT4 = re.compile(
    r"(?<!\br\s)(?<!\br-)\bmax\b(?!\s?-?[23]\b)|\bcrew\b|\bxp\s?-?4\b|\bpro\s?xp\s?-?4|\b(rzr|general|teryx|krx|talon|rmax|wolverine)\s?(xp\s?|pro\s?xp\s?|pro\s?r\s?|turbo\s?r?\s?)?(1000\s?)?-?\s?x?4\b"
    r"|\bx-?4\b|\b(4|four|5|five|6|six)[\s-]?(seat|seater|seats|passenger|pass|door|person|place|up)\b|\b1000\s?-?[56]\b|\b700\s?-?4\b"
    r"|\b1000\s?x\s?-?4\b|teryx\s?4|krx\s?4|\bviking\s?vi\b|\bfxt\b|\bs4\b", re.I)
# "cash for 4 seat ranger", "looking to trade for a crew": what the seller wants, not what they're selling
_WANTS = re.compile(r"\b(for|want\w*|looking|trade\w*|swap\w*)\s+(for\s+)?(a\s+|an\s+)?[\w-]*\s*[\w-]*\s*(4|four|crew|max)\b[\w\s-]{0,12}", re.I)
_OLD_MAVERICK = re.compile(r"^(?!.*\b(x3|sport|trail|max|r\s?max)\b).*\bmaverick\b", re.I)
_MAVERICK_1000R = re.compile(r"\b1000\s?r\b", re.I)
_SEAT2 = re.compile(r"\b(2|two|3|three)[\s-]?(seat|seater|seats|passenger|person)\b", re.I)

# Model names that settle the family (title + the AI's model text). (category, family or None, pattern, brand).
# None = no family we track: no typical price rather than a wrong one. Checked in order; first match wins.
SIGNATURES = (
    # snowmobiles filed as ATVs (11/16: an Arctic Cat M7, a Polaris XC 500)
    ("sled", None, r"\b(indy|rmk|xcr|xc\s?sp|switchback|mxz|ski[\s-]?doo|summit|sno\s?pro|phazer|sidewinder|sr\s?viper|snowmobile|zr\s?\d{3,4}|zl\s?\d{3})\b", None, ("atv",)),
    ("sled", None, r"\bart?ic\s?cat\b.*\bm\s?[5-9]\b|\bpolaris\b.*\bxc\s?\d{3}\b", None, ("atv",)),
    # Polaris ATVs that are not Sportsmen
    ("atv", "Youth ATV (under 150cc)", r"\b(x\s?-?90|sportsman\s+(90|110)|predator\s+(50|90)|outlaw\s+(50|90|110)|phoenix\s+50|scrambler\s+(50|90))\b", "polaris", None),
    ("atv", "Other ATV", r"\b(predator|outlaw|fusion|trail\s?blazer|xpress)\b", "polaris", None),
    ("atv", "Polaris Scrambler", r"\bscrambler\b", "polaris", None),
    # Yamaha utility ATVs that are not Grizzlies / Kodiaks, and not sport quads
    ("atv", "Other ATV", r"\b(bruin|big\s?bear|wolverine\s+(350|400|450)|moto-?4)\b", "yamaha", None),
    # Honda FourTrax / Recon without Foreman / Rubicon / Rancher in the ad
    ("atv", "Other ATV", r"^(?!.*\b(foreman|rubicon|rancher)\b).*\b(fourtrax|four\s?trax|recon)\b", "honda", None),
    # UTVs
    ("utv2", None, r"\bace\s?\d{3}\b|\bpolaris\s+ace\b", "polaris", ("utv2", "utv4")),        # one seat: no family we track
    # NorthStar is a Ranger edition; a General is a General (11/16: "xp 1000 northstar ultimate" filed as a RZR XP 4,
    # "General XP 1000 Sport" as a Ranger Crew)
    ("utv4", "Ranger Crew 1000", r"^(?!.*\b(rzr|general|razor)\b).*\bnorth\s?star\b", "polaris", ("utv4",)),
    ("utv2", "Ranger XP 1000/1500 (2-seat)", r"^(?!.*\b(rzr|general|razor)\b).*\bnorth\s?star\b", "polaris", ("utv2",)),
    ("utv4", "General 4", r"^(?!.*\b(ranger|rzr|razor)\b).*\bgeneral\b", "polaris", ("utv4",)),
    ("utv2", "General 1000 (2-seat)", r"^(?!.*\b(ranger|rzr|razor)\b).*\bgeneral\b", "polaris", ("utv2",)),
    ("utv4", "Ranger Crew 1000", r"^(?!.*\brzr\b).*\bhigh\s?lifter\b.*\bcrew\b|^(?!.*\brzr\b).*\bcrew\b.*\bhigh\s?lifter\b", "polaris", ("utv2", "utv4")),
    ("utv2", "Ranger XP 1000/1500 (2-seat)", r"^(?!.*\brzr\b).*\bhigh\s?lifter\b", "polaris", ("utv2",)),
    # jet skis
    ("pwc", "Sea-Doo Spark", r"\btrixx?\b(?!.*\b(rxp|gti|gts)\b)", None, ("pwc",)),
    # Polaris trail sleds the AI filed as utility / touring
    ("sled", "Polaris Indy/Switchback/Rush (trail/crossover)", r"^(?!.*\b(titan|voyageur|widetrak|wide\s?trak|rmk|khaos)\b).*\b(indy|classic|xlt|xcr?|edge|super\s?sport|sks)\b", "polaris", ("sled",)),
)
SIGNATURES = tuple((cat, fam, re.compile(rx, re.I), brand, only) for cat, fam, rx, brand, only in SIGNATURES)


def resolve(category: str | None, family: str | None, year: int | None, title: str | None, model: str | None = None,
            description: str | None = None, seats: int | None = None) -> tuple[str | None, str | None]:
    """(category, family) after checking what the AI read against what the ad says outright."""
    head = f"{title or ''} {model or ''}"
    brands = title_brands(f"{title or ''} {model or ''}") or ({family_brand(family)} if family_brand(family) else set())
    for cat, fam, rx, brand, only in SIGNATURES:
        if only and category not in only:
            continue
        if fam and (family or "").startswith(("Vintage", "Youth")):
            continue          # an old or kids' machine stays in its own family; only a category fix applies
        if brand and brand not in brands:
            continue
        if rx.search(head):
            if fam != family or cat != category:
                category, family = cat, fam
            break
    # seats: a "4-seat" machine the ad (or the AI's own seat count) says is a 2-3 seater, and nothing says 4
    said = _WANTS.sub(" ", f"{title or ''} {(description or '')[:1500]}")      # what the seller wrote
    text = f"{model or ''} {said}"
    if family in TWO_SEAT and not _SEAT4.search(text) and ((seats and seats <= 3) or _SEAT2.search(said)):
        family = TWO_SEAT[family]
        category = FAMILY_CATEGORY.get(family, "utv2") if family else "utv2"
    # ...and a "2-seat" one whose title says MAX / Crew / XP 4 / 4-seater
    elif family in FOUR_SEAT and _SEAT4.search(_WANTS.sub(" ", title or "")) and not _SEAT2.search(said):
        family = FOUR_SEAT[family]
        category = "utv4"
    # a 2-seat Maverick from before the X3 (until 11/16 there was no family for it, so the AI left it blank)
    if (category == "utv2" and family in (None, "Maverick X3 (2-seat)") and year and _OLD_MAVERICK.search(head)
            and (year <= 2016 or (year <= 2018 and _MAVERICK_1000R.search(head)))):
        family = "Maverick (2013-2018, pre-X3, 2-seat)"
    family = fix_family(family, year, title)
    return category, family


def cfg(category: str | None) -> dict:
    return CATEGORIES.get(category or "utv4", CATEGORIES["utv4"])
