"""Listing photos kept on our own disk. Facebook photo links are signed and expire after a few days
(`oe=` in the URL is the expiry, hex unix time); once half the dashboard's photos had gone blank, the
fix was to download each photo while its link still works and serve it from here (/img/...).

  python -m app.images              # cache what's missing (every full scan also does a slice)
  python -m app.images --all        # one-off backfill, no per-run limit
"""
import asyncio
import hashlib
import os
import re
import sys
import time

import httpx

from . import db

IMG_DIR = os.path.join(os.path.dirname(db.DB_PATH), "img")
PER_RUN = 300
MAX_BYTES = 2_000_000
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"


def expired(url: str | None, now: float | None = None) -> bool:
    """True when a signed Facebook photo link is past its expiry (other links never expire here)."""
    m = re.search(r"[?&]oe=([0-9A-Fa-f]{6,10})\b", url or "")
    return bool(m) and int(m.group(1), 16) < (now or time.time()) + 600


def file_name(lid: str) -> str:
    return hashlib.sha1(lid.encode()).hexdigest()[:20] + ".jpg"


def public(row) -> str | None:
    """What the page should load for this listing's photo: our copy, else a still-valid link, else nothing."""
    if row["img_file"]:
        return "/img/" + row["img_file"]
    return None if not row["image"] or expired(row["image"]) else row["image"]


async def cache(con, http: httpx.AsyncClient, limit: int | None = PER_RUN, concurrency: int = 4) -> int:
    """Download photos for live listings that don't have a copy yet and whose link still works.
    Highest scores first, so the cards you see get theirs first."""
    os.makedirs(IMG_DIR, exist_ok=True)
    rows = con.execute(
        """SELECT id, image FROM listings WHERE img_file IS NULL AND image IS NOT NULL AND image != ''
             AND status IN ('active', 'pending') AND COALESCE(relevant, 1) != 0
           ORDER BY COALESCE(score, 0) DESC""" + (" LIMIT ?" if limit else ""), (limit * 3,) if limit else ()).fetchall()
    todo = [r for r in rows if not expired(r["image"])][:limit] if limit else [r for r in rows if not expired(r["image"])]
    gate = asyncio.Semaphore(concurrency)
    saved = 0

    async def one(r):
        nonlocal saved
        async with gate:
            try:
                resp = await http.get(r["image"])
                if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith("image/") \
                        or len(resp.content) > MAX_BYTES:
                    return
                name = file_name(r["id"])
                tmp = os.path.join(IMG_DIR, name + ".part")
                with open(tmp, "wb") as f:
                    f.write(resp.content)
                os.replace(tmp, os.path.join(IMG_DIR, name))
                con.execute("UPDATE listings SET img_file = ? WHERE id = ?", (name, r["id"]))
                saved += 1
                if saved % 50 == 0:
                    con.commit()
            except httpx.HTTPError:
                return
    await asyncio.gather(*(one(r) for r in todo))
    con.commit()
    if saved:
        db.bump_rev(con)
    return saved


def forget(lids) -> None:
    """Delete the photo files of listings that are being removed from the database."""
    for lid in lids:
        try:
            os.remove(os.path.join(IMG_DIR, file_name(lid)))
        except FileNotFoundError:
            pass


def main():
    db.init()
    con = db.connect()

    async def go():
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}, follow_redirects=True) as http:
            n = await cache(con, http, None if "--all" in sys.argv else PER_RUN, concurrency=6 if "--all" in sys.argv else 4)
            print(f"images: {n} saved")
    asyncio.run(go())


if __name__ == "__main__":
    main()
