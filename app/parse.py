"""Turn a messy listing into structured fields with the local model on .76.

Uses the pinned qwen model with think:false and no num_ctx override (a
different num_ctx would force Ollama to reload the model for every app).
"""
import json
import os

import httpx

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://10.10.10.76:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.8:27b-64k")

FAMILIES = [
    "RZR XP 4", "RZR Pro XP 4", "RZR Pro R 4", "RZR Turbo R 4", "RZR 4 (other)",
    "Ranger Crew 1000", "Ranger Crew 570", "General 4",
    "Defender MAX", "Maverick MAX (2014-2016, pre-X3)", "Maverick X3 MAX", "Maverick Sport MAX", "Maverick Trail/Sport MAX", "Commander MAX",
    "Pioneer 1000-5/6", "Pioneer 700-4", "Talon 4",
    "Teryx4", "Teryx KRX4", "Mule Pro-FXT",
    "Wolverine X4", "Wolverine RMAX4", "Viking VI",
    "Gator XUV 4-seat", "CFMoto ZForce/UForce 4-seat", "Other 4-seat UTV",
]

PROMPT = """You read used-vehicle classified ads for a buyer shopping for a 4-seat (or larger) UTV / side-by-side.
Return ONLY a JSON object with these keys:
Seat hints: "MAX", "Crew", "XP 4", "4-seat", "Teryx4", "KRX4", "X4", "RMAX4", "General 4", "Pioneer 1000-5/6", "6-passenger", "Viking VI" all mean 4+ seats - never call those 2-seat.
A plain "General", "General 1000", "Ranger XP 1000", "Ranger 570", "RZR XP 1000", "RZR Pro XP", "Defender HD10", "Pioneer 1000", "Pioneer 700", "Teryx", "Wolverine X2" with no 4-seat marker are 2-3 seat models. Only mark relevant=true when the title, description or model name actually indicates 4+ seats; if unsure, relevant=false and seats=null.
Can-Am Maverick X3 started with model year 2017; an earlier "Maverick MAX 1000R" is the pre-X3 family.
- relevant: true only if this ad is selling a complete side-by-side / UTV with 4 or more seats. false for 2-seat machines, ATVs/quads, snowmobiles, trailers, parts, accessories, "wanted"/"looking for" ads, rentals, and services.
- year: model year as an integer, or null
- make: e.g. "Polaris", "Can-Am", "Honda", "Kawasaki", "Yamaha", "John Deere", "CFMoto", or null
- model: the model as the seller wrote it, cleaned up (e.g. "RZR XP 4 1000 Premium")
- family: exactly one of {families}. A 2-seat ad that is not relevant can still get its closest family or null.
- trim: trim/edition words if any (e.g. "Premium", "Ultimate", "NorthStar", "Limited", "XT", "DPS"), else null
- seats: integer seat count, or null
- hours: engine hours as an integer, or null
- miles: odometer miles as an integer, or null (convert km to miles)
- turbo: true/false/null
- is_dealer: true if a dealership/business is selling (financing offers, "call Dave at <dealer>", stock numbers, "plus tax/fees", MSRP/"save $X"), false if it reads like a private owner, null if unclear
- is_new: true if this is a new/unregistered unit (dealer stock, current or next model year with no miles, "new", "demo"/"demonstrator" counts as new), false if used
- motivated: true if the seller signals urgency (must sell, moving, divorce, need it gone, priced to sell, first $X takes it, OBO, make an offer, price drop), else false
- extras: list of up to 6 short strings for notable add-ons (cab/doors, heat, winch, plow, trailer included, new tires, audio, warranty)
- red_flags: list of short strings for real concerns: salvage/rebuilt title, no title, needs engine/trans work, doesn't run, accident damage, flood, shipping-only/deposit-first/"I'm deployed" style scam signs, price that is obviously a monthly payment or a down payment
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
        families=json.dumps(FAMILIES),
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
    year = num(d.get("year"))
    return {
        "relevant": flag(d.get("relevant")) or 0,
        "year": year if year and 1995 <= year <= 2030 else None,
        "make": d.get("make"),
        "model": d.get("model"),
        "family": fam if fam in FAMILIES else None,
        "trim": d.get("trim"),
        "seats": num(d.get("seats")),
        "hours": num(d.get("hours")),
        "miles": num(d.get("miles")),
        "turbo": flag(d.get("turbo")),
        "is_dealer": flag(d.get("is_dealer")),
        "is_new": flag(d.get("is_new")),
        "motivated": flag(d.get("motivated")) or 0,
        "extras": json.dumps([str(x) for x in (d.get("extras") or [])][:6]),
        "red_flags": json.dumps([str(x) for x in (d.get("red_flags") or [])][:6]),
        "summary": d.get("summary"),
    }
