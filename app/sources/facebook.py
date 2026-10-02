"""Facebook Marketplace, logged out, through headless Chromium.

Search pages embed listing JSON in <script type="application/json"> blocks and
fetch more over /api/graphql as you scroll; we read both. No account is used,
so there is nothing to get banned - the worst case is FB showing a login wall,
which shows up as zero results in the run log.
"""
import asyncio
import json
import random
import re
from urllib.parse import quote

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
JSON_SCRIPT = re.compile(r'<script type="application/json"[^>]*>(.*?)</script>', re.S)


def _walk(o, key, out):
    if isinstance(o, dict):
        if key in o and "id" in o:
            out.append(o)
        for v in o.values():
            _walk(v, key, out)
    elif isinstance(o, list):
        for v in o:
            _walk(v, key, out)


def _money(p):
    if not p:
        return None
    try:
        return int(float(p.get("amount")))
    except (TypeError, ValueError):
        return None


def _normalize(o: dict) -> dict:
    loc = ((o.get("location") or {}).get("reverse_geocode") or {})
    place = ", ".join(x for x in (loc.get("city"), loc.get("state")) if x)
    img = (((o.get("primary_listing_photo") or {}).get("image")) or {}).get("uri")
    status = "sold" if o.get("is_sold") else "pending" if o.get("is_pending") else "active"
    return {
        "ext_id": str(o["id"]),
        "url": f"https://www.facebook.com/marketplace/item/{o['id']}/",
        "title": (o.get("marketplace_listing_title") or "").strip(),
        "price": _money(o.get("listing_price")),
        "strike_price": _money(o.get("strikethrough_price")),
        "location": place or None,
        "image": img,
        "listed_at": int(o["creation_time"]) if o.get("creation_time") else None,
        "status": status,
    }


def _seller(v):
    """FB's own label; many dealers post as 'private_seller', so the LLM has the final say."""
    v = str(v or "").lower()
    return "dealer" if "dealer" in v else "private" if "private" in v else None


def search_url(location: str, query: str, radius_mi: int, sort: str = "newest", sold: bool = False) -> str:
    """Marketplace search URL. sold=True uses the Availability: Sold filter (works logged out)."""
    km = max(1, round(radius_mi * 1.609))
    order = "&sortBy=creation_time_descend" if sort == "newest" else ""
    return (f"https://www.facebook.com/marketplace/{location}/search/?query={quote(query)}&radius={km}{order}"
            f"&exact=false" + ("&availability=out%20of%20stock" if sold else ""))


def proxy_config(url: str | None) -> dict | None:
    """'http://user:pass@host:port' -> Playwright's proxy option. http/https proxies with a password work
    natively; socks5 with a password does NOT (Chromium limitation) - put a local forwarder in front."""
    if not url:
        return None
    from urllib.parse import urlsplit, unquote
    u = urlsplit(url.strip())
    if u.scheme not in ("http", "https", "socks5") or not u.hostname:
        raise ValueError("proxy must look like http://user:pass@host:port")
    cfg = {"server": f"{u.scheme}://{u.hostname}:{u.port or (1080 if u.scheme == 'socks5' else 8080)}"}
    if u.username:
        cfg["username"], cfg["password"] = unquote(u.username), unquote(u.password or "")
    return cfg


class Facebook:
    def __init__(self, pw, proxy: str | None = None):
        self.pw = pw
        self.proxy = proxy_config(proxy)
        self.browser = None
        self.ctx = None

    async def __aenter__(self):
        self.browser = await self.pw.chromium.launch(args=["--no-sandbox"], proxy=self.proxy)
        self.ctx = await self.browser.new_context(
            user_agent=UA, viewport={"width": 1300, "height": 900}, locale="en-US",
            timezone_id="America/Chicago")
        return self

    async def __aexit__(self, *exc):
        await self.browser.close()

    async def search(self, query: str, location: str, radius_mi: int, sort: str = "newest",
                     scrolls: int | None = None, sold: bool = False) -> list[dict]:
        page = await self.ctx.new_page()
        found: list[dict] = []

        async def on_response(resp):
            if "/api/graphql" not in resp.url:
                return
            try:
                body = await resp.text()
            except Exception:
                return
            for line in body.splitlines():
                try:
                    _walk(json.loads(line), "marketplace_listing_title", found)
                except ValueError:
                    pass

        page.on("response", on_response)
        url = search_url(location, query, radius_mi, sort, sold)
        try:
            await page.goto(url, timeout=45000, wait_until="domcontentloaded")
            await page.wait_for_timeout(4000 + random.randint(0, 2000))
            for m in JSON_SCRIPT.finditer(await page.content()):
                try:
                    _walk(json.loads(m.group(1)), "marketplace_listing_title", found)
                except ValueError:
                    pass
            n = scrolls if scrolls is not None else (2 if sort == "newest" else 6)
            for _ in range(n):  # each scroll pulls the next graphql page
                await page.mouse.wheel(0, 5000)
                await page.wait_for_timeout(2000 + random.randint(0, 1500))
        finally:
            await page.close()
        out = {}
        for o in found:
            if o.get("creation_time") is None and o.get("listing_price") is None:
                continue
            n = _normalize(o)
            if n["title"]:
                out[n["ext_id"]] = n
        return list(out.values())

    async def detail(self, ext_id: str) -> dict | None:
        """Description, seller type and current status from the item page.

        {"status": "gone"} only when FB explicitly says the content isn't available.
        None means "couldn't read it" (removed listings redirect to login, but so can a
        login wall) - the caller counts misses instead of assuming it was removed.
        """
        page = await self.ctx.new_page()
        try:
            await page.goto(f"https://www.facebook.com/marketplace/item/{ext_id}/",
                            timeout=45000, wait_until="domcontentloaded")
            await page.wait_for_timeout(2500 + random.randint(0, 1500))
            hits: list[dict] = []
            html = await page.content()
            if "/marketplace/item/" in page.url and "content isn't available" in html.replace("&#039;", "'"):
                return {"status": "gone"}
            for m in JSON_SCRIPT.finditer(html):
                try:
                    _walk(json.loads(m.group(1)), "redacted_description", hits)
                except ValueError:
                    pass
        finally:
            await page.close()
        for o in hits:
            if str(o.get("id")) != ext_id:
                continue
            desc = (o.get("redacted_description") or {}).get("text")
            attrs = {a.get("attribute_name"): a.get("label") for a in (o.get("attribute_data") or [])}
            seller = o.get("vehicle_seller_type") or ("dealer" if o.get("should_show_business_seller_label") else None)
            status = "sold" if o.get("is_sold") else "pending" if o.get("is_pending") else "active"
            extra = "; ".join(f"{k}: {v}" for k, v in attrs.items() if k and v)
            return {
                "description": "\n".join(x for x in (desc, extra) if x) or None,
                "seller_type": _seller(seller),
                "status": status,
                "price": _money(o.get("listing_price")),
            }
        return None


async def pause():
    await asyncio.sleep(random.uniform(5, 12))
