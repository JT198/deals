"""Turn a messy listing into structured fields with the local model on .76.

Uses the pinned qwen model with think:false and no num_ctx override (a
different num_ctx would force Ollama to reload the model for every app).
"""
import json
import os
import time

import httpx

from .categories import CATEGORIES, FAMILY_CATEGORY

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://10.10.10.76:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.8:27b-64k")

FAMILY_MENU = "\n".join(f'  {cat} ({c["label"]}): {json.dumps(c["families"])}' for cat, c in CATEGORIES.items())

PROMPT = """You read used classified ads for a buyer shopping for powersports machines (incl. jet skis and snowmobiles) and zero-turn mowers.
Categories:
- utv4: side-by-side / UTV with 4 or more seats
- utv2: side-by-side / UTV with 2 or 3 seats
- atv: ATV / quad / four-wheeler (straddle seat, handlebars, 4 wheels)
- trike: 3-wheeler ATV (e.g. Honda ATC, Yamaha Tri-Z). NOT 3-wheel motorcycles like Can-Am Spyder/Ryker or Polaris Slingshot.
- mower: zero-turn riding mower (lap-bar or steering-wheel zero-turn such as Cub Cadet RZT S). NOT lawn tractors, push mowers, or walk-behinds.
- pwc: personal watercraft / jet ski / Sea-Doo / WaveRunner / Kawasaki Jet Ski. NOT boats, pontoons, jet boats or kayaks.
- sled: snowmobile (Ski-Doo, Polaris, Arctic Cat, Yamaha). NOT snowblowers, snow plows, or "sled" trailers/decks sold alone.
- trailer: a towable trailer sold on its own - utility/landscape, enclosed cargo, car hauler, tilt/flatbed, equipment/deckover, dump, snowmobile/ATV drive-on. NOT campers/RVs/fifth-wheel campers, boat or jet-ski-only trailers, or a machine that merely comes "with trailer" (that ad's category is the machine).

The ad may already be marked SOLD - classify it exactly as if it were still for sale (sold ads are used as price history).

Seat hints: "MAX", "Crew", "XP 4", "4-seat", "Teryx4", "KRX4", "X4", "RMAX4", "General 4", "Pioneer 1000-5/6", "6-passenger", "Viking VI" mean 4+ seats (utv4). A plain "General", "General 1000", "Ranger XP 1000", "Ranger 570", "RZR XP 1000", "RZR Pro XP", "Defender HD10", "Pioneer 1000", "Pioneer 700", "Teryx", "Wolverine X2" with no 4-seat marker are 2-3 seat models (utv2). Only use utv4 when the ad actually indicates 4+ seats.
Jet skis are often sold as a PAIR on a double trailer ("two Sea-Doos", "his and hers", "2 skis") - that is one pwc listing with units 2,
but ONLY when the one listed price buys both. "$3,500 each" / "price is per ski" means the price is for ONE machine: units 1.
Today is {today}; the current model year is {model_year}. A used machine of the current or next model year is still used.
Can-Am Maverick X3 started with model year 2017; an earlier "Maverick MAX 1000R" is the pre-X3 family.

Return ONLY a JSON object with these keys:
- category: one of "utv4", "utv2", "atv", "trike", "mower", "trailer", "pwc", "sled", or "none" (anything else: dirt bikes, golf carts, lawn tractors, campers, boats, cars)
- relevant: true only if the ad sells one or more complete machines in one of the categories above. false for parts, accessories, attachments alone, "wanted"/"looking for" ads, rentals, services, and category "none".
- family: exactly one family from the list for that category (or null):
{families}
- year: model year as an integer, or null
- make: e.g. "Polaris", "Can-Am", "Honda", "Kawasaki", "Yamaha", "John Deere", "Cub Cadet", "Toro", or null
- model: the model as the seller wrote it, cleaned up (e.g. "RZR XP 4 1000 Premium", "RZT S 42", "ATC 250R")
- trim: trim/edition words if any (e.g. "Premium", "Ultimate", "NorthStar", "EPS", "Zeta"), else null
- seats: integer seat count for UTVs, else null
- hours: the machine's TOTAL engine hours as an integer, or null
- miles: the machine's TOTAL odometer miles as an integer, or null (convert km to miles)
  Only total use counts. "clutches replaced 60 miles ago", "800 miles on new top end", "6500 miles on rebuilt motor",
  "20 hours since rebuild" describe a repair, NOT the machine's total - use null for miles/hours in that case and do
  not mention that number as the mileage in the summary.
- units: how many complete machines the ONE listed price buys (a pair of jet skis for one price = 2), else 1. Priced "each" = 1
- track_in (snowmobiles only): track length in inches (e.g. "129", "137", "146", "154", "165"; "15x137" means 137), else null
- cc: engine displacement in cc as an integer if stated or implied by the model name (e.g. "850" = 850, "600R" = 600, "1.8L" = 1800, "Spark 90" = 900), else null
- deck_in: mower cutting deck width in inches as an integer (mowers only), else null
- trailer_type (trailers only, else null): "enclosed", "open", "equipment", "tilt", "dump", "deckover", "drive-on" or "other".
  "equipment" = flat deck between the fenders (about 82-83 in wide), car / equipment hauler. "deckover" ONLY when the deck
  sits over the wheels (96-102 in wide, "wide body"). Trailers have no odometer: miles and hours are null for a trailer.
- len_ft / width_ft (trailers only): deck or box length and width in feet as numbers. "7x16" or "16x7" means 7 wide, 16 long; "82 inch between fenders" is about 6.8 wide; an 8.5-wide car hauler is 8.5. Exclude the tongue and V-nose from length. null if not stated.
- height_ft (enclosed trailers only): interior height in feet (e.g. "6'6 interior" = 6.5, "7 ft tall inside" = 7), else null
- axles (trailers only): number of axles (single = 1, tandem = 2), else null
- gvwr_lb (trailers only): GVWR / capacity in pounds (a "7K" or "7000 lb" trailer = 7000), else null
- brakes (trailers only): true if it has electric or surge brakes, false if it says no brakes, else null
- engine: short engine description if stated (e.g. "Kohler 22 HP", "Kawasaki FR691V 23 HP", "850cc", "EFI 1000", "Rotax 300 supercharged", "Patriot 9R"), else null
- turbo: true if turbocharged or supercharged, false if not, else null
- is_new: true if this is a new/unregistered unit (dealer stock, current or next model year with no use, "new", "demo"/"demonstrator" counts as new), false if used
- is_dealer: true if a dealership/business is selling (financing offers, "call Dave at <dealer>", stock numbers, "plus tax/fees", MSRP/"save $X"), false if it reads like a private owner, null if unclear
- motivated: true if the seller signals urgency (must sell, moving, divorce, need it gone, priced to sell, first $X takes it, OBO, make an offer, price drop), else false
- extras: list of up to 6 short strings for notable add-ons (cab/doors, heat, winch, plow, trailer included, new tires, bagger, mulch kit, warranty)
- red_flags: list of short strings for real, CURRENT, unresolved concerns. Work already done ("changed the parking brake",
  "new wheel hub just installed"), a spare part the seller already has, and normal cosmetic wear are NOT red flags.
  Examples: salvage/rebuilt title, no title (for trailers: "no title" or "bill of sale only" on a trailer over 3,000 lb or so), rotted floor/frame, bent axle, needs engine/trans/hydro work, doesn't run, smokes, accident damage, flood, shipping-only/deposit-first/"I'm deployed" style scam signs, price that is obviously a monthly payment or a down payment
- summary: one plain-English sentence a buyer would want (condition, use, anything notable)

Ad source: {source}
Title: {title}
Price: {price}
Location: {location}
Seller type from site: {seller}
Description:
{description}
"""


async def parse(http: httpx.AsyncClient, listing: dict) -> dict | None:
    prompt = PROMPT.format(
        families=FAMILY_MENU, today=time.strftime("%B %Y"), model_year=time.localtime().tm_year + 1,
        source=listing["source"], title=listing["title"],
        price=f"${listing['price']:,}" if listing.get("price") else "not stated",
        location=listing.get("location") or "unknown",
        seller=listing.get("seller_type") or "unknown",
        description=(listing.get("description") or "(no description)")[:3500],
    )
    r = await http.post(f"{OLLAMA_URL}/api/generate", json={
        "model": OLLAMA_MODEL, "prompt": prompt, "format": "json", "stream": False,
        "think": False, "keep_alive": "30m", "options": {"temperature": 0},
    }, timeout=90)
    r.raise_for_status()
    try:
        d = json.loads(r.json()["response"])
    except (ValueError, KeyError):
        return None
    if not isinstance(d, dict):     # "null", a bare string, a list - deterministic at temperature 0, so don't retry forever
        return None

    def text(v):
        """Free-text field: the model sometimes answers with a list or a number."""
        if v is None or v == "":
            return None
        if isinstance(v, (list, tuple)):
            return ", ".join(str(x) for x in v if x is not None)[:200] or None
        return str(v)[:200]

    def items(v):
        if isinstance(v, str):
            v = [v]
        return [str(x)[:80] for x in (v or []) if x is not None][:6] if isinstance(v, (list, tuple)) else []

    def num(v):
        try:
            return int(float(str(v).replace(",", ""))) if v not in (None, "") else None
        except ValueError:
            return None

    def flag(v):
        return None if v is None else 1 if v is True or str(v).lower() == "true" else 0

    fam = d.get("family")
    fam = fam if isinstance(fam, str) and fam in FAMILY_CATEGORY else None
    cat = d.get("category")
    cat = cat if isinstance(cat, str) and cat in CATEGORIES else None
    if fam and cat != FAMILY_CATEGORY[fam]:
        cat = FAMILY_CATEGORY[fam]    # the family is the more specific answer
    year = num(d.get("year"))
    deck = num(d.get("deck_in"))
    units = num(d.get("units"))
    track = num(d.get("track_in")) if cat == "sled" else None
    cc = num(d.get("cc"))

    def feet(v, lo, hi):
        try:
            f = float(str(v).replace("'", "").replace("ft", "").strip())
        except (TypeError, ValueError):
            return None
        return f if lo <= f <= hi else None
    trailer = cat == "trailer"
    ttype = d.get("trailer_type") if trailer and isinstance(d.get("trailer_type"), str) and d.get("trailer_type") in (
        "enclosed", "open", "equipment", "tilt", "dump", "deckover", "drive-on", "other") else None
    axles = num(d.get("axles")) if trailer else None
    gvwr = num(d.get("gvwr_lb")) if trailer else None
    return {
        "relevant": 1 if (flag(d.get("relevant")) and cat) else 0,
        "category": cat,
        "year": year if year and 1965 <= year <= 2030 else None,
        "make": text(d.get("make")),
        "model": text(d.get("model")),
        "family": fam,
        "trim": text(d.get("trim")),
        "seats": num(d.get("seats")),
        "hours": num(d.get("hours")),
        "miles": num(d.get("miles")),
        "units": units if units and 2 <= units <= 6 else 1,
        "track_in": track if track and 100 <= track <= 180 else None,
        "cc": cc if cc and 49 <= cc <= 2500 else None,
        "deck_in": deck if deck and 28 <= deck <= 80 else None,
        "engine": text(d.get("engine")),
        "trailer_type": ttype,
        "len_ft": feet(d.get("len_ft"), 4, 53) if trailer else None,
        "width_ft": feet(d.get("width_ft"), 3, 9) if trailer else None,
        "height_ft": feet(d.get("height_ft"), 3, 9) if trailer else None,
        "axles": axles if axles and 1 <= axles <= 4 else None,
        "gvwr_lb": gvwr if gvwr and 500 <= gvwr <= 30000 else None,
        "brakes": flag(d.get("brakes")) if trailer else None,
        "turbo": flag(d.get("turbo")),
        "is_new": flag(d.get("is_new")),
        "is_dealer": flag(d.get("is_dealer")),
        "motivated": flag(d.get("motivated")) or 0,
        "extras": json.dumps(items(d.get("extras"))),
        "red_flags": json.dumps(items(d.get("red_flags"))),
        "summary": text(d.get("summary")),
    }
