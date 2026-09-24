"""Turn a messy listing into structured fields with the local model on .76.

Uses the pinned qwen model with think:false and no num_ctx override (a
different num_ctx would force Ollama to reload the model for every app).
"""
import json
import os

import httpx

from .categories import CATEGORIES, FAMILY_CATEGORY

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://10.10.10.76:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.8:27b-64k")

FAMILY_MENU = "\n".join(f'  {cat} ({c["label"]}): {json.dumps(c["families"])}' for cat, c in CATEGORIES.items())

PROMPT = """You read used classified ads for a buyer shopping for powersports machines and zero-turn mowers.
Categories:
- utv4: side-by-side / UTV with 4 or more seats
- utv2: side-by-side / UTV with 2 or 3 seats
- atv: ATV / quad / four-wheeler (straddle seat, handlebars, 4 wheels)
- trike: 3-wheeler ATV (e.g. Honda ATC, Yamaha Tri-Z). NOT 3-wheel motorcycles like Can-Am Spyder/Ryker or Polaris Slingshot.
- mower: zero-turn riding mower (lap-bar or steering-wheel zero-turn such as Cub Cadet RZT S). NOT lawn tractors, push mowers, or walk-behinds.

Seat hints: "MAX", "Crew", "XP 4", "4-seat", "Teryx4", "KRX4", "X4", "RMAX4", "General 4", "Pioneer 1000-5/6", "6-passenger", "Viking VI" mean 4+ seats (utv4). A plain "General", "General 1000", "Ranger XP 1000", "Ranger 570", "RZR XP 1000", "RZR Pro XP", "Defender HD10", "Pioneer 1000", "Pioneer 700", "Teryx", "Wolverine X2" with no 4-seat marker are 2-3 seat models (utv2). Only use utv4 when the ad actually indicates 4+ seats.
Can-Am Maverick X3 started with model year 2017; an earlier "Maverick MAX 1000R" is the pre-X3 family.

Return ONLY a JSON object with these keys:
- category: one of "utv4", "utv2", "atv", "trike", "mower", or "none" (anything else: snowmobiles, dirt bikes, golf carts, lawn tractors, trailers, boats, cars)
- relevant: true only if the ad sells one complete machine in one of the categories above. false for parts, accessories, attachments alone, "wanted"/"looking for" ads, rentals, services, and category "none".
- family: exactly one family from the list for that category (or null):
{families}
- year: model year as an integer, or null
- make: e.g. "Polaris", "Can-Am", "Honda", "Kawasaki", "Yamaha", "John Deere", "Cub Cadet", "Toro", or null
- model: the model as the seller wrote it, cleaned up (e.g. "RZR XP 4 1000 Premium", "RZT S 42", "ATC 250R")
- trim: trim/edition words if any (e.g. "Premium", "Ultimate", "NorthStar", "EPS", "Zeta"), else null
- seats: integer seat count for UTVs, else null
- hours: engine hours as an integer, or null
- miles: odometer miles as an integer, or null (convert km to miles)
- deck_in: mower cutting deck width in inches as an integer (mowers only), else null
- engine: short engine description if stated (e.g. "Kohler 22 HP", "Kawasaki FR691V 23 HP", "850cc", "EFI 1000"), else null
- turbo: true/false/null
- is_new: true if this is a new/unregistered unit (dealer stock, current or next model year with no use, "new", "demo"/"demonstrator" counts as new), false if used
- is_dealer: true if a dealership/business is selling (financing offers, "call Dave at <dealer>", stock numbers, "plus tax/fees", MSRP/"save $X"), false if it reads like a private owner, null if unclear
- motivated: true if the seller signals urgency (must sell, moving, divorce, need it gone, priced to sell, first $X takes it, OBO, make an offer, price drop), else false
- extras: list of up to 6 short strings for notable add-ons (cab/doors, heat, winch, plow, trailer included, new tires, bagger, mulch kit, warranty)
- red_flags: list of short strings for real concerns: salvage/rebuilt title, no title, needs engine/trans/hydro work, doesn't run, smokes, accident damage, flood, shipping-only/deposit-first/"I'm deployed" style scam signs, price that is obviously a monthly payment or a down payment
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
        families=FAMILY_MENU,
        source=listing["source"], title=listing["title"],
        price=f"${listing['price']:,}" if listing.get("price") else "not stated",
        location=listing.get("location") or "unknown",
        seller=listing.get("seller_type") or "unknown",
        description=(listing.get("description") or "(no description)")[:3500],
    )
    r = await http.post(f"{OLLAMA_URL}/api/generate", json={
        "model": OLLAMA_MODEL, "prompt": prompt, "format": "json", "stream": False,
        "think": False, "keep_alive": "30m", "options": {"temperature": 0},
    }, timeout=180)
    r.raise_for_status()
    try:
        d = json.loads(r.json()["response"])
    except (ValueError, KeyError):
        return None

    def num(v):
        try:
            return int(float(str(v).replace(",", ""))) if v not in (None, "") else None
        except ValueError:
            return None

    def flag(v):
        return None if v is None else 1 if v is True or str(v).lower() == "true" else 0

    fam = d.get("family")
    fam = fam if fam in FAMILY_CATEGORY else None
    cat = d.get("category") if d.get("category") in CATEGORIES else None
    if fam and cat != FAMILY_CATEGORY[fam]:
        cat = FAMILY_CATEGORY[fam]    # the family is the more specific answer
    year = num(d.get("year"))
    deck = num(d.get("deck_in"))
    return {
        "relevant": 1 if (flag(d.get("relevant")) and cat) else 0,
        "category": cat,
        "year": year if year and 1965 <= year <= 2030 else None,
        "make": d.get("make"),
        "model": d.get("model"),
        "family": fam,
        "trim": d.get("trim"),
        "seats": num(d.get("seats")),
        "hours": num(d.get("hours")),
        "miles": num(d.get("miles")),
        "deck_in": deck if deck and 28 <= deck <= 80 else None,
        "engine": d.get("engine"),
        "turbo": flag(d.get("turbo")),
        "is_new": flag(d.get("is_new")),
        "is_dealer": flag(d.get("is_dealer")),
        "motivated": flag(d.get("motivated")) or 0,
        "extras": json.dumps([str(x) for x in (d.get("extras") or [])][:6]),
        "red_flags": json.dumps([str(x) for x in (d.get("red_flags") or [])][:6]),
        "summary": d.get("summary"),
    }
