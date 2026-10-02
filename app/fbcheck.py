"""Can we reach Facebook Marketplace right now, via the home IP or the VPN proxy?

  python -m app.fbcheck --route home|proxy      (prints JSON; used by the Setup tab's Test buttons)

One search (no scrolling) and one listing page. "searched" false with no error means Facebook answered
but withheld results - the login wall - which is what a block looks like.
"""
import asyncio
import json
import sys
import time

from playwright.async_api import async_playwright

from . import db
from .sources.facebook import Facebook, proxy_config


async def check(route: str) -> dict:
    db.init()
    con = db.connect()
    st = db.settings(con)
    proxy = (st.get("fb_proxy") or "").strip() if route == "proxy" else None
    out = {"route": route, "ok": False, "searched": False, "items": 0, "item_page": None, "error": None}
    if route == "proxy" and not proxy:
        out["error"] = "no proxy configured"
        return out
    try:
        proxy_config(proxy)        # validate the URL before launching a browser
    except ValueError as e:
        out["error"] = str(e)
        return out
    row = con.execute("""SELECT ext_id FROM listings WHERE source = 'facebook' AND status = 'active' AND relevant = 1
                         ORDER BY last_checked DESC LIMIT 1""").fetchone()
    t = time.time()
    try:
        async with async_playwright() as pw, Facebook(pw, proxy) as fb:
            items = await fb.search("ranger crew", st.get("fb_location", "plymouth-mn"), int(st.get("radius_mi") or 100),
                                    scrolls=0)
            out["items"] = len(items)
            out["searched"] = bool(items)
            if row:
                d = await fb.detail(row["ext_id"])
                out["item_page"] = "readable" if d else "blocked or removed"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    out["seconds"] = round(time.time() - t)
    out["ok"] = out["searched"] and out["error"] is None
    return out


def main():
    route = sys.argv[sys.argv.index("--route") + 1] if "--route" in sys.argv else "home"
    print(json.dumps(asyncio.run(check(route))))


if __name__ == "__main__":
    main()
