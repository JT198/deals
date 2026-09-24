"""What we shop for. Each category has its own model families, searches, and scoring knobs.

dep        yearly depreciation used to line up comps from neighbouring model years
window     +/- model years that count as "near" comps
fit        allow the log-linear price-vs-year fit when near comps are thin
low_hpy / high_hpy   engine hours per year that count as light / heavy use
cl_cat     Craigslist category to search (sna = atvs/utvs/snowmobiles, grd = farm & garden)
every_min  how often each search in this category runs on Facebook
"""

CATEGORIES = {
    "utv4": {
        "label": "4-seat UTVs", "emoji": "🚙",
        "dep": 0.08, "window": 1, "fit": True, "low_hpy": 60, "high_hpy": 200,
        "cl_cat": "sna", "every_min": 20,
        "families": [
            "RZR XP 4", "RZR Pro XP 4", "RZR Pro R 4", "RZR Turbo R 4", "RZR 4 (other)",
            "Ranger Crew 1000", "Ranger Crew 570", "General 4",
            "Defender MAX", "Maverick MAX (2014-2016, pre-X3)", "Maverick X3 MAX", "Maverick Sport MAX",
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
            "yamaha wolverine x4", "4 seat side by side", "crew utv",
        ],
    },
    "utv2": {
        "label": "2-seat UTVs", "emoji": "🛻",
        "dep": 0.08, "window": 1, "fit": True, "low_hpy": 60, "high_hpy": 200,
        "cl_cat": "sna", "every_min": 60,
        "families": [
            "RZR XP 1000/Turbo (2-seat)", "RZR Pro XP/Pro R (2-seat)", "RZR 900/570/Trail (2-seat)",
            "Ranger XP 1000/1500 (2-seat)", "Ranger 570/500 (2-seat)", "General 1000 (2-seat)",
            "Defender (2-seat)", "Maverick X3 (2-seat)", "Maverick Sport/Trail (2-seat)", "Commander (2-seat)",
            "Pioneer 1000/700/520 (2-seat)", "Talon 1000 (2-seat)", "Teryx/KRX 1000 (2-seat)",
            "Mule (2-seat)", "Wolverine X2/RMAX2", "Viking (3-seat)", "Gator XUV/HPX",
            "Kubota RTV", "CFMoto ZForce/UForce (2-seat)", "Other 2-seat UTV",
        ],
        "searches": [
            "rzr xp 1000", "polaris ranger xp 1000", "polaris general 1000", "can am defender hd10",
            "can am maverick x3", "honda pioneer 1000", "kawasaki mule", "side by side utv",
        ],
    },
    "atv": {
        "label": "ATVs", "emoji": "🏍️",
        "dep": 0.07, "window": 1, "fit": True, "low_hpy": 40, "high_hpy": 150,
        "cl_cat": "sna", "every_min": 60,
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
        ],
    },
    "trike": {
        # vintage ATCs hold or gain value with age, so no depreciation and a wide year window
        "label": "3-wheelers", "emoji": "🛺",
        "dep": 0.0, "window": 3, "fit": False, "low_hpy": 20, "high_hpy": 100,
        "cl_cat": "sna", "every_min": 60,
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
        "cl_cat": "grd", "every_min": 20,
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
}

FAMILY_CATEGORY = {f: c for c, cfg in CATEGORIES.items() for f in cfg["families"]}


def cfg(category: str | None) -> dict:
    return CATEGORIES.get(category or "utv4", CATEGORIES["utv4"])
