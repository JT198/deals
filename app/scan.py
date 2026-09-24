"""One scan pass: search every source, fetch details for new hits, parse with the
LLM, re-score everything, send alerts. Run by deals-scan.timer.

  python -m app.scan            # normal run (skipped outside active_hours)
  python -m app.scan --force    # run now regardless of the clock
  python -m app.scan --backfill # first run: "best match" sort + no alert flood
"""
import asyncio
import fcntl
import json
import random
import sys
import time
import traceback

import httpx
from playwright.async_api import async_playwright

from . import db, geo, notify, parse, score
from .sources import craigslist
from .sources.facebook import Facebook, pause

MAX_ALERTS_PER_RUN = 6
FB_DETAILS_PER_RUN = 30
FB_RECHECKS_PER_RUN = 12
CL_DETAILS_PER_RUN = 40
PARSES_PER_RUN = 120
STALE_AFTER = 5 * 86400


def upsert(con, source: str, item: dict) -> bool:
    lid = f"{source}:{item['ext_id']}"
    t = db.now()
    row = con.execute("SELECT price, status FROM listings WHERE id = ?", (lid,)).fetchone()
    if row is None:
        con.execute(
            """INSERT INTO listings(id, source, ext_id, url, title, price, first_price, strike_price,
                 location, image, listed_at, first_seen, last_seen, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (lid, source, item["ext_id"], item["url"], item["title"], item["price"], item["price"],
             item.get("strike_price"), item.get("location"), item.get("image"),
             item.get("listed_at"), t, t, item.get("status", "active")))
        if item["price"]:
            con.execute("INSERT INTO price_history VALUES (?,?,?)", (lid, t, item["price"]))
        return True
    con.execute(
        """UPDATE listings SET last_seen = ?, status = ?, title = ?,
             strike_price = COALESCE(?, strike_price), image = COALESCE(image, ?),
             listed_at = COALESCE(listed_at, ?), location = COALESCE(location, ?)
           WHERE id = ?""",
        (t, item.get("status", "active"), item["title"], item.get("strike_price"), item.get("image"),
         item.get("listed_at"), item.get("location"), lid))
    set_price(con, lid, row["price"], item["price"])
    return False


def set_price(con, lid, old, new):
    if new and new != old:
        con.execute("UPDATE listings SET price = ? WHERE id = ?", (new, lid))
        con.execute("INSERT INTO price_history VALUES (?,?,?)", (lid, db.now(), new))


def apply_detail(con, lid, d: dict | None, old_price):
    t = db.now()
    if d is None or d.get("status") == "gone":
        con.execute("UPDATE listings SET status='gone', detail_fetched=1, last_checked=? WHERE id=?", (t, lid))
        return
    con.execute(
        """UPDATE listings SET description = COALESCE(?, description), seller_type = COALESCE(?, seller_type),
             listed_at = COALESCE(?, listed_at), image = COALESCE(?, image), status = ?,
             detail_fetched = 1, last_checked = ?, last_seen = ? WHERE id = ?""",
        (d.get("description"), d.get("seller_type"), d.get("listed_at"), d.get("image"),
         d.get("status", "active"), t, t, lid))
    set_price(con, lid, old_price, d.get("price"))


def in_active_hours(st) -> bool:
    try:
        a, b = (int(x) for x in st.get("active_hours", "0-24").split("-"))
    except ValueError:
        return True
    return a <= time.localtime().tm_hour < b


async def run(force=False, backfill=False) -> None:
    db.init()
    con = db.connect()
    st = db.settings(con)
    if not (force or backfill or in_active_hours(st)):
        print("outside active hours, skipping")
        return
    radius = int(st.get("radius_mi") or 100)
    boost = 5 if backfill else 1   # catch-up runs take bigger bites
    queries = [r["query"] for r in con.execute("SELECT query FROM searches WHERE enabled = 1")]
    random.shuffle(queries)
    # we hold the lock, so any unfinished run was killed part-way
    con.execute("""UPDATE runs SET finished = started, errors = '["interrupted"]' WHERE finished IS NULL""")
    run_id = con.execute("INSERT INTO runs(started, source) VALUES (?, 'all')", (db.now(),)).lastrowid
    con.commit()
    errors: list[str] = []
    found = new = alerts = 0

    async with craigslist.client() as http:
        # --- Craigslist search
        for q in queries:
            try:
                items = await craigslist.search(http, q, st.get("home_zip", "55447"), radius)
                found += len(items)
                new += sum(upsert(con, "craigslist", i) for i in items)
                con.commit()
            except Exception as e:  # keep going; one bad query shouldn't kill the run
                errors.append(f"cl '{q}': {e}")
            await asyncio.sleep(random.uniform(1.5, 3))

        # --- Facebook search + item pages
        try:
            async with async_playwright() as pw, Facebook(pw) as fb:
                empty = 0
                for q in queries:
                    try:
                        items = await fb.search(q, st.get("fb_location", "minneapolis"), radius,
                                                sort="best_match" if backfill else "newest")
                        found += len(items)
                        empty += not items
                        new += sum(upsert(con, "facebook", i) for i in items)
                        con.commit()
                    except Exception as e:
                        errors.append(f"fb '{q}': {e}")
                    await pause()
                if empty == len(queries):
                    errors.append("facebook returned nothing for every search (login wall?)")

                todo = con.execute(
                    """SELECT id, ext_id, price FROM listings
                       WHERE source='facebook' AND detail_fetched=0 AND status='active'
                       ORDER BY first_seen DESC LIMIT ?""", (FB_DETAILS_PER_RUN * boost,)).fetchall()
                # plus a few older relevant ones, to notice price cuts / sold / removed
                todo += con.execute(
                    """SELECT id, ext_id, price FROM listings
                       WHERE source='facebook' AND detail_fetched=1 AND relevant=1 AND status IN ('active','pending')
                       ORDER BY COALESCE(last_checked, 0) ASC LIMIT ?""", (FB_RECHECKS_PER_RUN,)).fetchall()
                for r in todo:
                    try:
                        apply_detail(con, r["id"], await fb.detail(r["ext_id"]), r["price"])
                        con.commit()
                    except Exception as e:
                        errors.append(f"fb detail {r['ext_id']}: {e}")
                    await asyncio.sleep(random.uniform(2, 5))
        except Exception as e:
            errors.append(f"facebook: {e}")

        # --- Craigslist posting pages
        for r in con.execute(
                """SELECT id, url, price FROM listings WHERE source='craigslist' AND detail_fetched=0
                   ORDER BY first_seen DESC LIMIT ?""", (CL_DETAILS_PER_RUN * boost,)).fetchall():
            try:
                apply_detail(con, r["id"], await craigslist.detail(http, r["url"]), r["price"])
                con.commit()
            except Exception as e:
                errors.append(f"cl detail {r['url']}: {e}")
            await asyncio.sleep(random.uniform(1, 2.5))

        # --- LLM parse (once the description is in, or after an hour without one)
        rows = con.execute(
            """SELECT * FROM listings WHERE parsed = 0 AND status != 'gone'
                 AND (detail_fetched = 1 OR first_seen < ?)
               ORDER BY first_seen DESC LIMIT ?""", (db.now() - 3600, PARSES_PER_RUN * boost)).fetchall()
        for r in rows:
            try:
                p = await parse.parse(http, dict(r))
            except Exception as e:
                errors.append(f"parse {r['id']}: {e}")
                continue
            if p is None:
                continue
            cols = ", ".join(f"{k} = ?" for k in p)
            con.execute(f"UPDATE listings SET {cols}, parsed = 1 WHERE id = ?", (*p.values(), r["id"]))
            con.commit()

        # listings we haven't seen or confirmed in a while are probably gone
        con.execute("UPDATE listings SET status='gone' WHERE status IN ('active','pending') AND last_seen < ?",
                    (db.now() - STALE_AFTER,))
        con.commit()

        score.rescore_all(con)
        try:
            await geo.fill(con)
        except Exception as e:
            errors.append(f"geocode: {e}")

        alerts = await send_alerts(con, http, st, quiet=backfill)

    con.execute("UPDATE runs SET finished=?, found=?, new=?, alerts=?, errors=? WHERE id=?",
                (db.now(), found, new, alerts, json.dumps(errors[:30]) if errors else None, run_id))
    con.commit()
    print(f"found={found} new={new} alerts={alerts} errors={len(errors)}")
    for e in errors[:10]:
        print("  !", e)


async def send_alerts(con, http, st, quiet=False) -> int:
    threshold = int(st.get("alert_threshold") or 75)
    sql = """SELECT * FROM listings WHERE relevant = 1 AND status = 'active' AND hidden = 0
               AND score >= ? AND (alerted_score IS NULL OR score >= alerted_score + 10)"""
    args: list = [threshold]
    if st.get("alert_private_only") == "1":
        sql += " AND COALESCE(is_dealer, 0) = 0 AND COALESCE(seller_type, '') != 'dealer'"
    if st.get("max_price"):
        sql += " AND price <= ?"
        args.append(int(st["max_price"]))
    if st.get("min_year"):
        sql += " AND year >= ?"
        args.append(int(st["min_year"]))
    rows = con.execute(sql + " ORDER BY score DESC", args).fetchall()
    if not rows:
        return 0
    sent = 0
    if not quiet:
        for r in rows[:MAX_ALERTS_PER_RUN]:
            if await notify.send_listing(http, r):
                sent += 1
        if len(rows) > MAX_ALERTS_PER_RUN:
            await notify.send_text(http, f"…and {len(rows) - MAX_ALERTS_PER_RUN} more above {threshold} "
                                         f'on the <a href="{notify.DASHBOARD_URL}">dashboard</a>.')
    # mark all as alerted (a backfill run marks silently so the first real run doesn't flood)
    con.executemany("UPDATE listings SET alerted_score = score WHERE id = ?", [(r["id"],) for r in rows])
    con.commit()
    return sent


def main():
    lock = open("/tmp/deals-scan.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another scan is running")
        return
    try:
        asyncio.run(run(force="--force" in sys.argv, backfill="--backfill" in sys.argv))
    except Exception:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
