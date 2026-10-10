"""Craigslist: the no-JS search page lists every hit as plain HTML.

Searches a category (sna = atvs/utvs/snowmobiles, grd = farm & garden; owner + dealer) within
radius_mi of the home zip; posting pages supply the date, body, image and
whether it was posted by owner or dealer.
"""
import html
import re
from datetime import datetime
from urllib.parse import quote

import httpx

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
# one match per complete result element; fields are then read inside that element only
ITEM = re.compile(r'<li class="cl-static-search-result" title="(?P<title>[^"]*)">(?P<body>.*?)</li>', re.S)
HREF = re.compile(r'<a href="([^"]+)"')
PRICE = re.compile(r'<div class="price">([^<]*)</div>')
LOC = re.compile(r'<div class="location">\s*([^<]*?)\s*</div>', re.S)


def coords(page: str) -> tuple[float | None, float | None]:
    """Map pin on the posting page. The location text is free-form (dealer names, phone numbers, several
    towns at once) and often missing, but nearly every posting has the pin."""
    lat = re.search(r'data-latitude="(-?\d+(?:\.\d+)?)"', page)
    lon = re.search(r'data-longitude="(-?\d+(?:\.\d+)?)"', page)
    if not (lat and lon):
        return None, None
    la, lo = float(lat.group(1)), float(lon.group(1))
    return (la, lo) if (abs(la) > 1 and abs(lo) > 1 and abs(la) <= 90 and abs(lo) <= 180) else (None, None)


def _price(s):
    digits = re.sub(r"[^\d]", "", s or "")
    return int(digits) if digits else None


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"User-Agent": UA}, timeout=30, follow_redirects=True)


async def search(http: httpx.AsyncClient, query: str, zip_code: str, radius_mi: int,
                 cat: str = "sna") -> list[dict]:
    url = (f"https://minneapolis.craigslist.org/search/{cat}?query={quote(query)}"
           f"&search_distance={radius_mi}&postal={zip_code}")
    r = await http.get(url)
    r.raise_for_status()
    out = {}
    for m in ITEM.finditer(r.text):
        body = m.group("body")
        href, price, loc = HREF.search(body), PRICE.search(body), LOC.search(body)
        if not href:
            continue
        link = href.group(1)
        ext_id = link.rstrip("/").rsplit("/", 1)[-1]
        out[ext_id] = {
            "ext_id": ext_id,
            "url": link,
            "title": html.unescape(m.group("title")).strip(),
            "price": _price(price.group(1)) if price else None,
            "strike_price": None,
            "location": html.unescape(loc.group(1)).strip() or None if loc else None,
            "image": None,
            "listed_at": None,
            "status": "active",
        }
    return list(out.values())


async def detail(http: httpx.AsyncClient, url: str) -> dict | None:
    r = await http.get(url)
    if r.status_code in (404, 410):
        return {"status": "gone"}
    r.raise_for_status()
    h = r.text
    if any(x in h for x in ("This posting has been deleted", "This posting has expired", "has been flagged for removal")):
        return {"status": "gone"}
    body = re.search(r'<section id="postingbody">(.*?)</section>', h, re.S)
    text = ""
    if body:
        chunk = re.sub(r'<div class="print-information.*?</div>\s*</div>', "", body.group(1), flags=re.S)
        text = html.unescape(re.sub(r"<[^>]+>", " ", chunk))
        text = re.sub(r"[ \t]+", " ", text).strip()
    attrs = re.findall(r'<div class="attr[^"]*">(.*?)</div>', h, re.S)
    attr_text = "; ".join(
        re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", a))).strip() for a in attrs)
    posted = re.search(r'<time class="date timeago" datetime="([^"]+)"', h)
    listed_at = None
    if posted:
        try:
            listed_at = int(datetime.strptime(posted.group(1), "%Y-%m-%dT%H:%M:%S%z").timestamp())
        except ValueError:
            pass
    img = re.search(r'<meta property="og:image" content="([^"]+)"', h)
    price = re.search(r'<span class="price">([^<]*)</span>', h)
    crumbs = re.search(r'<ul class="breadcrumbs">(.*?)</ul>', h, re.S)
    crumbs = crumbs.group(1) if crumbs else ""
    seller = "dealer" if "by dealer" in crumbs else "private" if "by owner" in crumbs else None
    lat, lon = coords(h)
    return {
        "lat": lat, "lon": lon,
        "description": "\n".join(x for x in (text, attr_text) if x) or None,
        "seller_type": seller,
        "listed_at": listed_at,
        "image": img.group(1) if img else None,
        "status": "active",
        "price": _price(price.group(1)) if price else None,
    }
