"""Approximate distance from home, via OpenStreetMap Nominatim (cached, 1 req/s)."""
import asyncio
import math

import httpx

UA = "alerolabs-deals/1.0 (personal UTV deal finder)"


def miles(lat1, lon1, lat2, lon2) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def place_key(location: str | None) -> str | None:
    if not location:
        return None
    loc = location.strip()
    return loc if "," in loc else f"{loc}, MN"   # Craigslist gives bare town names


def distance(row, home: tuple[float, float], cache: dict) -> float | None:
    """Miles from home: the listing's own map pin if it has one, else its town from the geocache."""
    try:
        lat, lon = row["lat"], row["lon"]
    except (KeyError, IndexError):
        lat = lon = None
    if lat is None or lon is None:
        c = cache.get(place_key(row["location"]))
        if not c or c[0] is None:
            return None
        lat, lon = c
    return miles(home[0], home[1], lat, lon)


async def fill(con, limit: int = 25) -> None:
    places = {place_key(r["location"]) for r in con.execute(
        "SELECT DISTINCT location FROM listings WHERE location IS NOT NULL")}
    known = {r["place"] for r in con.execute("SELECT place FROM geocache")}
    todo = [p for p in places if p and p not in known][:limit]
    if not todo:
        return
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=20) as http:
        for p in todo:
            try:
                lat, lon = await lookup(http, p)
            except (httpx.HTTPError, ValueError):
                continue
            con.execute("INSERT OR REPLACE INTO geocache(place, lat, lon) VALUES (?,?,?)", (p, lat, lon))
            con.commit()
            await asyncio.sleep(1.1)


async def lookup(http, place: str) -> tuple[float | None, float | None]:
    """The town, not the county of the same name: "Clearwater, MN" is a town near St Cloud, but a plain
    search returns Clearwater County, 130 miles further north. Settlements first, then anything."""
    for extra in ({"featureType": "settlement"}, {}):
        r = await http.get("https://nominatim.openstreetmap.org/search",
                           params={"q": place, "format": "json", "limit": 1, "countrycodes": "us", **extra})
        hit = r.json()[:1] if r.status_code == 200 else []
        if hit:
            return float(hit[0]["lat"]), float(hit[0]["lon"])
        await asyncio.sleep(1.1)
    return None, None


async def refresh_all() -> None:
    """One-off: look every cached town up again with lookup() and fix the ones that moved."""
    from . import db
    con = db.connect()
    places = con.execute("SELECT place, lat, lon FROM geocache").fetchall()
    moved = 0
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=20) as http:
        for row in places:
            try:
                lat, lon = await lookup(http, row["place"])
            except (httpx.HTTPError, ValueError):
                continue
            if lat is not None and (row["lat"] is None or miles(lat, lon, row["lat"], row["lon"]) > 5):
                print(f"{row['place']}: moved {'?' if row['lat'] is None else round(miles(lat, lon, row['lat'], row['lon']))} mi", flush=True)
                con.execute("UPDATE geocache SET lat = ?, lon = ? WHERE place = ?", (lat, lon, row["place"]))
                con.commit()
                moved += 1
            await asyncio.sleep(1.1)
    print(f"checked {len(places)}, fixed {moved}")


if __name__ == "__main__":
    import sys
    if "--refresh" in sys.argv:
        asyncio.run(refresh_all())
