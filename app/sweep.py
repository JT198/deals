"""Deep sweep: reach the Facebook listings a plain search never shows. Run by deals-sweep.timer.

A logged-out Marketplace search shows only its first 15-24 listings, and scrolling loads no more. The
regular scans read newest first, so they only ever see what was posted recently: anything older than the
scanner (or than a category) stays invisible. Limiting a search to an asking-price band returns a
different page, so this lane walks every search through price bands in "best match" order (newest-first
within a band is mostly cars and furniture), splitting a band in two whenever it comes back full and is
still turning up listings we didn't have.

It runs day and night; at night it also opens item pages and parses, since nothing else is using Facebook
or the model then. Round 1 digs up the backlog, and the old listings it finds don't alert - see
scan.upsert. Later rounds repeat the bands the previous round ended with: that keeps older listings'
"last seen" fresh (scan.stale_after waits for a full round) and catches what the newest-first scans missed.

  python -m app.sweep            # one slice of the work list
  python -m app.sweep --status   # where it is
"""
import asyncio
import fcntl
import html
import json
import os
import sys
import traceback

import httpx
from playwright.async_api import async_playwright

from . import db, geo, notify, scan
from .categories import CATEGORIES
from .sources.facebook import Facebook, pause

PAGE_FULL = 13           # a best-match page holds 14-19; this many back means the band is probably cut off
SPLIT_MIN_NEW = 3        # ...and it is only worth splitting while a full page still shows this many unseen listings
SPLIT_REACH = 1.5        # ...within this multiple of the search radius (unknown distance counts as near)
MIN_WIDTH = 100          # don't split a band narrower than 2x this ($)
DAY_SEARCHES = 6         # per run (every 10 min) while the other lanes are busy - about 90 s of Facebook
NIGHT_SEARCHES = 12      # outside active hours, when no other lane is running
NIGHT_DETAILS = 25
NIGHT_PARSES = 90
LOCK_WAIT = 540         # seconds to wait for the other lanes to finish with Facebook (the timer fires every 10 min)
CANARY = "ranger crew"   # a plain search that always has results: empty means Facebook is walling us
ORDER = ["utv4", "utv2", "atv", "pwc", "mower", "trailer", "sled", "trike"]   # what Jon is shopping for first
PRIORITY = "CASE category " + " ".join(f"WHEN '{c}' THEN {i}" for i, c in enumerate(ORDER)) + " ELSE 99 END"
# a job is only live while its search is still enabled in Setup (disabled ones wait; deleted ones are dropped)
LIVE = "EXISTS (SELECT 1 FROM searches s WHERE s.query = sweep_queue.query AND s.enabled = 1)"


def default_bands(category: str) -> list[tuple[int, int]]:
    edges = CATEGORIES.get(category, CATEGORIES["utv4"])["bands"]
    return [(edges[i], edges[i + 1] - 1) for i in range(len(edges) - 1)]


def split(lo: int, hi: int) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Two halves of a full band, or None when it is already too narrow to be worth splitting
    (asking prices bunch up on round numbers, so there is a floor to what splitting can separate)."""
    if hi - lo < 2 * MIN_WIDTH:
        return None
    mid = round((lo + hi) / 2 / 50) * 50
    return (lo, mid), (mid + 1, hi)


def start_round(con, rnd: int) -> int:
    """Fill the work list for round `rnd`: each enabled search gets the bands the previous round ended
    with (so earlier splits aren't rediscovered), or its category's default bands."""
    n = 0
    for s in con.execute("SELECT query, category FROM searches WHERE enabled = 1 ORDER BY id").fetchall():
        bands = [(r["lo"], r["hi"]) for r in con.execute(
            "SELECT lo, hi FROM sweep_queue WHERE round = ? AND query = ? AND state = 'done' ORDER BY lo",
            (rnd - 1, s["query"]))] or default_bands(s["category"])
        con.executemany("INSERT INTO sweep_queue(round, query, category, lo, hi) VALUES (?,?,?,?,?)",
                        [(rnd, s["query"], s["category"], lo, hi) for lo, hi in bands])
        n += len(bands)
    con.execute("DELETE FROM sweep_queue WHERE round < ?", (rnd,))
    con.executemany("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                    [("sweep_round", str(rnd)), ("sweep_round_started", str(db.now()))])
    con.commit()
    return n


def status(con) -> dict:
    st = db.settings(con)
    rnd = int(st.get("sweep_round") or 0)
    q = con.execute("""SELECT COUNT(*) total, COALESCE(SUM(state = 'todo'), 0) todo,
                         COALESCE(SUM(new), 0) new FROM sweep_queue WHERE round = ?""", (rnd,)).fetchone()
    # pending = still waiting for its item page or for the model to read it
    b = con.execute("""SELECT COUNT(*) found, COALESCE(SUM(relevant = 1), 0) machines,
                         COALESCE(SUM((parsed = 0 AND status != 'gone')
                                      OR (detail_fetched = 0 AND status = 'active'
                                          AND COALESCE(relevant, 1) != 0)), 0) pending
                       FROM listings WHERE backlog = 1""").fetchone()
    return {"round": rnd, "started": int(st.get("sweep_round_started") or 0), "searches": q["total"],
            "todo": q["todo"], "new": q["new"], "backlog_found": b["found"], "backlog_machines": b["machines"],
            "backlog_pending": b["pending"]}


async def maybe_summary(con, http) -> None:
    """Once, after round 1 is done and everything it found has been read: tell Jon what turned up."""
    st = db.settings(con)
    s = status(con)
    if st.get("sweep_summary_sent") or s["round"] < 2 or s["backlog_pending"]:
        return
    best = con.execute("""SELECT title, price, score, url FROM listings
                          WHERE backlog = 1 AND relevant = 1 AND status = 'active' AND hidden = 0
                            AND COALESCE(is_dealer, 0) = 0 AND COALESCE(is_new, 0) = 0 AND score >= 70
                          ORDER BY score DESC LIMIT 6""").fetchall()
    lines = [f"🔎 <b>Deep sweep finished its first pass</b>: {s['backlog_found']:,} older Facebook listings the "
             f"regular scans never saw, {s['backlog_machines']:,} of them machines or trailers we track."]
    if best:
        lines.append("Best of the older ones still for sale:")
        lines += [f'• {r["score"]} · <a href="{r["url"]}">{html.escape(r["title"][:60])}</a>'
                  + (f' · ${r["price"]:,}' if r["price"] else "") for r in best]
    lines.append(f'All of them are on the <a href="{notify.DASHBOARD_URL}">dashboard</a>. '
                 "The sweep keeps going in the background from here.")
    if await notify.send_text(http, "\n".join(lines)):
        con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('sweep_summary_sent', ?)", (str(db.now()),))
        con.commit()


async def run() -> None:
    db.init()
    con = db.connect()
    st = db.settings(con)
    rnd = int(st.get("sweep_round") or 0)
    con.execute("DELETE FROM sweep_queue WHERE query NOT IN (SELECT query FROM searches)")
    con.commit()
    if not con.execute(f"SELECT 1 FROM sweep_queue WHERE round = ? AND state = 'todo' AND {LIVE} LIMIT 1",
                       (rnd,)).fetchone():
        if rnd:     # how long a full round takes tells the scanner how long "not seen lately" has to be
            con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('sweep_round_secs', ?)",
                        (str(db.now() - int(st.get("sweep_round_started") or db.now())),))
        rnd += 1
        print(f"sweep: starting round {rnd} with {start_round(con, rnd)} band searches")
    first = rnd == 1
    night = not scan.in_active_hours(st)
    async with httpx.AsyncClient(timeout=30) as http:
        if not first:
            await maybe_summary(con, http)
        route, until = scan.fb_pick_route(st, db.now())
        if route is None:
            print("sweep: facebook is paused, skipping")
            return
        lock = await scan.acquire_fb_lock(LOCK_WAIT, yield_to_quick=True)
        if lock is None:
            print("sweep: facebook busy with another scan lane, skipping")
            return
        # up to 9 minutes may have passed: if the scan we waited on got blocked, its pause must stand
        st = db.settings(con)
        route, until = scan.fb_pick_route(st, db.now())
        if route is None:
            lock.close()
            print("sweep: facebook was paused while waiting, skipping")
            return
        jobs = con.execute(f"""SELECT * FROM sweep_queue WHERE round = ? AND state = 'todo' AND {LIVE}
                               ORDER BY {PRIORITY}, id LIMIT ?""",
                           (rnd, NIGHT_SEARCHES if night else DAY_SEARCHES)).fetchall()
        loc, radius = st.get("fb_location", "plymouth-mn"), int(st.get("radius_mi") or 100)
        home = (float(st["home_lat"]), float(st["home_lon"]))
        places = {r["place"]: (r["lat"], r["lon"]) for r in con.execute("SELECT * FROM geocache")}

        def near(item) -> bool:
            d = geo.distance({"lat": None, "lon": None, "location": item.get("location")}, home, places)
            return d is None or d <= radius * SPLIT_REACH
        errors: list[str] = []
        found = new = splits = 0
        came_back_empty: list[int] = []
        walled = False
        try:
            async with async_playwright() as pw, Facebook(pw, st.get("fb_proxy") if route == "proxy" else None) as fb:
                for j in jobs:
                    try:
                        items = await fb.search(j["query"], loc, radius, sort="best_match", scrolls=0,
                                                price=(j["lo"], j["hi"]))
                    except Exception as e:      # left on the list; it is first in line next run
                        errors.append(f"fb '{j['query']}' ${j['lo']}-{j['hi']}: {e}")
                        await pause()
                        continue
                    fresh = [i for i in items if scan.upsert(con, "facebook", i, backlog=first)]
                    n_new = len(fresh)
                    # Facebook pads a price band with listings far outside the radius (Iowa, the Dakotas);
                    # only unseen listings within reach are a reason to dig deeper
                    near_new = sum(1 for i in fresh if near(i))
                    halves = split(j["lo"], j["hi"]) if len(items) >= PAGE_FULL and near_new >= SPLIT_MIN_NEW else None
                    if halves:
                        splits += 1
                        con.executemany("INSERT INTO sweep_queue(round, query, category, lo, hi) VALUES (?,?,?,?,?)",
                                        [(rnd, j["query"], j["category"], lo, hi) for lo, hi in halves])
                    con.execute("UPDATE sweep_queue SET state = ?, found = ?, new = ?, ts = ? WHERE id = ?",
                                ("split" if halves else "done", len(items), n_new, db.now(), j["id"]))
                    con.commit()
                    found += len(items)
                    new += n_new
                    if not items:
                        came_back_empty.append(j["id"])
                    await pause()
                if jobs and len(came_back_empty) == len(jobs):
                    # thin bands can all be empty honestly - or Facebook is answering with nothing
                    walled = not await fb.search(CANARY, loc, radius, scrolls=0)
                    if walled:      # those answers meant nothing: put the bands back on the list
                        con.executemany("UPDATE sweep_queue SET state = 'todo', found = NULL, new = NULL WHERE id = ?",
                                        [(i,) for i in came_back_empty])
                        con.commit()
                        errors.append("facebook returned nothing for every search (login wall?)")
                if night and not walled:
                    await scan.fb_details(con, fb, scan.pending_details(con, NIGHT_DETAILS), errors)
        except Exception as e:
            errors.append(f"facebook: {e}")
        finally:
            lock.close()
        walled = walled or any("login wall" in e for e in errors)
        msg = scan.fb_backoff(con, st, walled=walled, fb_found=found, route=route)
        if msg:
            errors.append(msg)
        if night and not walled:
            await scan.parse_pending(con, http, NIGHT_PARSES, errors)
        left = con.execute(f"SELECT COUNT(*) FROM sweep_queue WHERE round = ? AND state = 'todo' AND {LIVE}", (rnd,)).fetchone()[0]
        print(f"sweep r{rnd}: searches={len(jobs)} found={found} new={new} splits={splits} left={left} errors={len(errors)}")
        for e in errors[:10]:
            print("  !", e)
        if walled:
            await scan.problem_alert(con, http, "sweep", "; ".join(
                e for e in errors if "login wall" in e or e.startswith("pausing Facebook")))


def main():
    if "--status" in sys.argv:
        db.init()
        print(json.dumps(status(db.connect()), indent=1))
        return
    os.makedirs(scan.LOCK_DIR, exist_ok=True)
    lock = open(os.path.join(scan.LOCK_DIR, "sweep.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another sweep is running")
        return
    try:
        asyncio.run(run())
    except Exception:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
