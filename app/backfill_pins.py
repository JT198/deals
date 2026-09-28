"""One-off: collect map pins for live Craigslist listings that don't have one (python -m app.backfill_pins)."""
import asyncio
import random

from . import db, scan
from .sources import craigslist


async def main():
    db.init()
    con = db.connect()
    rows = con.execute("""SELECT id, url, price FROM listings WHERE source = 'craigslist' AND lat IS NULL
                          AND status IN ('active', 'pending') ORDER BY relevant DESC, last_seen DESC""").fetchall()
    print(f"{len(rows)} Craigslist listings without a pin", flush=True)
    done = gone = 0
    async with craigslist.client() as http:
        for i, r in enumerate(rows, 1):
            try:
                d = await craigslist.detail(http, r["url"])
                if d is not None:
                    scan.apply_detail(con, r["id"], d, r["price"])
                    con.commit()
                    done += d.get("lat") is not None
                    gone += d.get("status") == "gone"
            except Exception as e:
                print("  !", r["url"], e, flush=True)
            if i % 100 == 0:
                print(f"  {i}/{len(rows)} pins={done} gone={gone}", flush=True)
            await asyncio.sleep(random.uniform(1.0, 2.0))
    print(f"done: pins={done} gone={gone} of {len(rows)}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
