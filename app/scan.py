"""One scan pass: search every source, fetch details for new hits, parse with the
LLM, re-score everything, send alerts. Run by deals-scan.timer.

  python -m app.scan            # normal run (skipped outside active_hours)
  python -m app.scan --force    # run now regardless of the clock
  python -m app.scan --backfill # first run: "best match" sort + no alert flood
  python -m app.scan --backfill --no-search   # just finish item pages + parsing
  python -m app.scan --quick    # 5-minute fast lane: only searches marked quick, newest first
  python -m app.scan --quiet    # normal scan, but record alerts as seen instead of sending (rollouts)
  python -m app.scan --sold     # Facebook "Sold" filter on every search: sold prices for "typically sells around"
"""
import asyncio
import fcntl
import html
import os
import json
import random
import re
import sys
import time
import traceback

import httpx
from playwright.async_api import async_playwright

from . import buybox, db, geo, notify, parse, score
from .categories import cfg
from .sources import craigslist
from .sources.facebook import Facebook, pause

MAX_ALERTS_PER_RUN = 6
REALERT_CUT = 0.07            # an alerted listing alerts again when its price drops this much (or the score jumps 10 on a cut)
MAX_FRESH_PER_RUN = 8
LOCK_DIR = os.path.join(os.path.dirname(db.DB_PATH), "locks")
ALERT_LOCK = os.path.join(LOCK_DIR, "alert.lock")
PROBLEM_ALERT_EVERY = 6 * 3600   # Telegram "scanner has a problem" at most this often
FB_SEARCHES_PER_RUN = 20      # ~5 min of searching; keep Facebook traffic modest (it blocks heavy IPs)
FB_BACKOFF_HOURS = (2, 4, 8)  # pause all Facebook traffic this long after it returns nothing; escalates on repeats
FB_DETAILS_PER_RUN = 40
SOLD_DETAILS_PER_RUN = 120    # item pages for newly seen sold listings (the first pull has a backlog)
FB_RECHECKS_PER_RUN = 6
STALE_RECHECKS_PER_RUN = 12   # listings nobody has seen in a while: their page decides, not the calendar
FB_HOURLY_BUDGET = 300        # Facebook page loads per hour across every lane (blocked once at ~410)
FB_BUDGET_RESERVE = 45        # ...of which the long lanes leave this many for the fast lane
QUICK_LOCK_WAIT = 180         # the fast lane outwaits a daytime sweep slice (~2 min) instead of skipping its run
CL_DETAILS_PER_RUN = 40
PARSES_PER_RUN = 120
PARSE_CONCURRENCY = 3           # parallel requests to Ollama on .76
STALE_AFTER = 5 * 86400
MAX_STALE = 14 * 86400


def stale_after(st) -> int:
    """Seconds without a sighting before a listing is assumed gone. Older listings are only ever seen
    by the deep sweep, so it has to be longer than one full sweep round (the last one, or the one
    still running if that is already longer)."""
    started = int(st.get("sweep_round_started") or 0)
    longest = max(int(st.get("sweep_round_secs") or 0), db.now() - started if started else 0)
    return min(MAX_STALE, max(STALE_AFTER, int(1.5 * longest)))


def upsert(con, source: str, item: dict, backlog: bool = False) -> bool:
    """backlog=True (the first deep sweep): a listing already more than a day old when we first see it
    is old news - it is stored and scored like any other, but its first deal alert is recorded, not sent."""
    lid = f"{source}:{item['ext_id']}"
    t = db.now()
    status = item.get("status", "active")
    old_news = int(backlog and bool(item.get("listed_at")) and item["listed_at"] < t - 86400)
    row = con.execute("SELECT price, status, user_gone FROM listings WHERE id = ?", (lid,)).fetchone()
    if row is None:
        # OR IGNORE: the quick lane and a full scan can both see a new listing in the same minute
        cur = con.execute(
            """INSERT OR IGNORE INTO listings(id, source, ext_id, url, title, price, first_price, strike_price,
                 location, image, listed_at, first_seen, last_seen, status, seen_active, backlog)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (lid, source, item["ext_id"], item["url"], item["title"], item["price"], item["price"],
             item.get("strike_price"), item.get("location"), item.get("image"),
             item.get("listed_at"), t, t, status, int(status == "active"), old_news))
        if cur.rowcount and item["price"]:
            con.execute("INSERT INTO price_history VALUES (?,?,?)", (lid, t, item["price"]))
        return bool(cur.rowcount)
    if row["user_gone"] and status != "sold":
        status = "gone"          # Jon said it's gone; a cached search result doesn't overrule him
    con.execute(
        """UPDATE listings SET last_seen = ?, status = ?, title = ?, detail_misses = 0,
             seen_active = CASE WHEN ? = 'active' THEN 1 ELSE seen_active END,
             strike_price = COALESCE(?, strike_price), image = COALESCE(?, image),
             listed_at = COALESCE(listed_at, ?), location = COALESCE(location, ?)
           WHERE id = ?""",
        (t, status, item["title"], status, item.get("strike_price"), item.get("image"),
         item.get("listed_at"), item.get("location"), lid))
    set_price(con, lid, row["price"], item["price"])
    return False


def set_price(con, lid, old, new):
    if new and new != old:
        con.execute("UPDATE listings SET price = ?, first_price = COALESCE(first_price, ?) WHERE id = ?",
                    (new, new, lid))
        con.execute("INSERT INTO price_history VALUES (?,?,?)", (lid, db.now(), new))


MAX_DETAIL_MISSES = 3


def apply_detail(con, lid, d: dict | None, old_price):
    """d = page data, {"status": "gone"} (site confirmed removal), or None (unreadable - see record_miss)."""
    t = db.now()
    if d is None:
        return
    if d.get("status") == "gone":
        con.execute("UPDATE listings SET status='gone', detail_fetched=1, last_checked=? WHERE id=?", (t, lid))
        return
    # a listing that waited over an hour for its page was parsed from the title alone:
    # read it again now that the description is in
    con.execute(
        """UPDATE listings SET parsed = CASE WHEN description IS NULL AND ? IS NOT NULL THEN 0 ELSE parsed END,
             description = COALESCE(?, description), seller_type = COALESCE(?, seller_type),
             listed_at = COALESCE(?, listed_at), image = COALESCE(?, image),
             status = CASE WHEN user_gone = 1 AND ? != 'sold' THEN 'gone' ELSE ? END,
             lat = COALESCE(?, lat), lon = COALESCE(?, lon),
             detail_fetched = 1, detail_misses = 0, last_checked = ?, last_seen = ?,
             seen_active = CASE WHEN ? = 'active' THEN 1 ELSE seen_active END WHERE id = ?""",
        (d.get("description"), d.get("description"), d.get("seller_type"), d.get("listed_at"), d.get("image"),
         d.get("status", "active"), d.get("status", "active"), d.get("lat"), d.get("lon"), t, t,
         d.get("status", "active"), lid))
    set_price(con, lid, old_price, d.get("price"))


HOT_SCORE = 60                # listings worth showing get re-checked every HOT_RECHECK_SECS
HOT_RECHECK_SECS = 6 * 3600
HOT_RECHECKS_PER_RUN = 6


def recheck_candidates(con) -> list:
    """Facebook item pages to re-open this full scan, to notice price cuts, pending, sold and removed:
      1. suspects - failed to load last time; retried every scan so a removed listing is confirmed in ~an hour
      2. starred listings, every scan (watch alerts)
      3. listings scoring HOT_SCORE+ not checked in HOT_RECHECK_SECS (the ones Jon actually looks at)
      4. stale - not seen by any search or sweep in stale_after(): the page says whether it is gone
      5. a round-robin of everything else relevant, least recently seen first"""
    live = "source='facebook' AND detail_fetched=1 AND status IN ('active','pending')"
    now = db.now()
    groups = [
        con.execute(f"""SELECT id, ext_id, price FROM listings WHERE {live} AND detail_misses > 0
                        ORDER BY COALESCE(last_checked, 0) ASC LIMIT 20""").fetchall(),
        con.execute(f"SELECT id, ext_id, price FROM listings WHERE {live} AND starred = 1").fetchall(),
        con.execute(f"""SELECT id, ext_id, price FROM listings WHERE {live} AND relevant = 1 AND score >= ?
                        AND COALESCE(last_checked, 0) < ? ORDER BY score DESC LIMIT ?""",
                    (HOT_SCORE, now - HOT_RECHECK_SECS, HOT_RECHECKS_PER_RUN)).fetchall(),
        stale_candidates(con, STALE_RECHECKS_PER_RUN),
        con.execute(f"""SELECT id, ext_id, price FROM listings WHERE {live} AND relevant = 1
                        ORDER BY last_seen ASC LIMIT ?""", (FB_RECHECKS_PER_RUN,)).fetchall(),
    ]
    out, seen = [], set()
    for g in groups:
        for r in g:
            if r["id"] not in seen:
                seen.add(r["id"])
                out.append(r)
    return out


def stale_candidates(con, limit: int) -> list:
    """Live Facebook listings no search or sweep has shown for stale_after(): a search only ever shows
    its first page, so not being seen proves nothing - the item page does."""
    return con.execute("""SELECT id, ext_id, price FROM listings
                          WHERE source='facebook' AND detail_fetched=1 AND status IN ('active','pending')
                            AND relevant = 1 AND last_seen < ? ORDER BY last_seen ASC LIMIT ?""",
                       (db.now() - stale_after(db.settings(con)), limit)).fetchall()


def title_only_candidates(con, limit: int) -> list:
    """Ads the model dropped from their title alone, without ever seeing the page (the deep sweep's
    finds wait longest for theirs): worth a second look when Facebook is idle."""
    return con.execute("""SELECT id, ext_id, price FROM listings
                          WHERE source='facebook' AND detail_fetched=0 AND status='active' AND title_only = 1
                            AND relevant = 0 AND detail_misses = 0 ORDER BY first_seen DESC LIMIT ?""",
                       (limit,)).fetchall()


class FBBudget:
    """One hourly budget of Facebook page loads shared by every lane (table fb_loads). The full scan,
    the sweep and the sold pull stop when the hour is nearly spent; the fast lane may use the rest."""

    def __init__(self, con, reserve: int = 0):
        self.con, self.reserve = con, reserve

    def record(self) -> None:
        self.con.execute("INSERT INTO fb_loads(ts) VALUES (?)", (db.now(),))
        self.con.execute("DELETE FROM fb_loads WHERE ts < ?", (db.now() - 7200,))
        self.con.commit()

    def used(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM fb_loads WHERE ts >= ?", (db.now() - 3600,)).fetchone()[0]

    def left(self) -> int:
        return FB_HOURLY_BUDGET - self.reserve - self.used()


class BudgetSpent(Exception):
    """This lane's share of the hourly Facebook budget is gone; finish the run without Facebook."""


class SkipFacebook(Exception):
    """Facebook is paused or another lane has the browser - skip this run's Facebook work."""


QUICK_WAITING = "quick-waiting"   # marker file: the fast lane is waiting for Facebook
QUICK_REPEAT_SECS = 8 * 60        # the fast lane skips a search any lane ran this recently
CANARY = "ranger crew"            # a search that always has results: empty means Facebook is walling us


async def acquire_fb_lock(wait_secs: int, yield_to_quick: bool = False, quick: bool = False):
    """Only one scan lane talks to Facebook at a time. Returns the open lock file, or None after wait_secs.
    quick=True marks the fast lane as waiting; a yield_to_quick waiter (the sweep) lets it go first, so
    the sweep slipping in after a full scan doesn't cost a "just listed" run."""
    os.makedirs(LOCK_DIR, exist_ok=True)
    f = open(os.path.join(LOCK_DIR, "facebook.lock"), "w")
    marker = os.path.join(LOCK_DIR, QUICK_WAITING)
    deadline = time.time() + wait_secs
    if quick:
        open(marker, "w").close()
    try:
        return await _wait_for(f, marker, deadline, yield_to_quick)
    finally:
        if quick:
            try:
                os.remove(marker)
            except FileNotFoundError:
                pass


def quick_waiting() -> bool:
    marker = os.path.join(LOCK_DIR, QUICK_WAITING)
    try:
        return time.time() - os.path.getmtime(marker) < 600
    except FileNotFoundError:
        return False


async def step_aside(lock, route: str = "home") -> None:
    """Called by the long lanes (full scan, sweep, sold pull) between Facebook pages: if the fast lane
    is waiting, hand it the Facebook lock for its one-minute run, then take it back. Without this the
    full scan (17-23 minutes of every 20) made the fast lane skip about a third of its runs.
    The browser stays open; only one lane talks to Facebook at a time either way."""
    if lock is None or not quick_waiting():
        return
    fcntl.flock(lock, fcntl.LOCK_UN)
    deadline = time.time() + 120
    while quick_waiting() and time.time() < deadline:   # the marker goes once the fast lane has the lock
        await asyncio.sleep(1)
    await asyncio.to_thread(fcntl.flock, lock, fcntl.LOCK_EX)
    if int(db.settings(db.connect()).get(f"fb_backoff_until:{route}") or 0) > db.now():
        raise SkipFacebook      # the fast lane got walled on this route meanwhile: don't keep hitting it


async def _wait_for(f, marker, deadline, yield_to_quick):
    while True:
        try:
            if yield_to_quick and os.path.exists(marker) and time.time() - os.path.getmtime(marker) < 600:
                raise BlockingIOError      # the fast lane is waiting: let it have Facebook first
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except BlockingIOError:
            if time.time() >= deadline:
                f.close()
                return None
            await asyncio.sleep(5)


ROUTE_LABEL = {"home": "home IP", "proxy": "VPN proxy"}


def fb_routes(st) -> list[str]:
    """Which ways to reach Facebook, in order of preference."""
    mode = st.get("fb_route") or "auto"
    proxy = bool((st.get("fb_proxy") or "").strip())
    if mode == "proxy":
        return ["proxy"] if proxy else ["home"]
    if mode == "home" or not proxy:
        return ["home"]
    return ["home", "proxy"]


def fb_pick_route(st, now: int) -> tuple[str | None, int]:
    """First route that isn't paused, or (None, when the earliest pause ends)."""
    soonest = 0
    for r in fb_routes(st):
        until = int(st.get(f"fb_backoff_until:{r}") or 0)
        if until <= now:
            return r, 0
        soonest = until if not soonest else min(soonest, until)
    return None, soonest


def fb_backoff(con, st, walled: bool, fb_found: int, route: str = "home") -> str | None:
    """Facebook answered every search with nothing: it is blocking this route's IP. Stop using that route
    for a while (2 h, then 4, then 8 if it keeps happening); a good run on it resets the ladder."""
    level = int(st.get(f"fb_backoff_level:{route}") or 0)
    if walled:
        level = min(level + 1, len(FB_BACKOFF_HOURS))
        hours = FB_BACKOFF_HOURS[level - 1]
        until = db.now() + hours * 3600
        con.executemany("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                        [(f"fb_backoff_until:{route}", str(until)), (f"fb_backoff_level:{route}", str(level))])
        con.commit()
        others = [r for r in fb_routes(st) if r != route and int(st.get(f"fb_backoff_until:{r}") or 0) <= db.now()]
        nxt = f"; switching to the {ROUTE_LABEL[others[0]]}" if others else "; Craigslist continues"
        return (f"pausing Facebook via {ROUTE_LABEL[route]} for {hours} h "
                f"(until {time.strftime('%H:%M', time.localtime(until))}){nxt}")
    if fb_found > 0 and level:
        con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, '0')", (f"fb_backoff_level:{route}",))
        con.commit()
    return None


def pending_details(con, limit: int, where: str = "status='active'") -> list:
    """Facebook listings whose item page we haven't read yet: fresh finds first, then what the deep sweep
    dug up. Ads the model already dropped from their title alone wait for title_only_candidates()."""
    return con.execute(
        """SELECT id, ext_id, price FROM listings
           WHERE source='facebook' AND detail_fetched=0 AND (parsed = 0 OR COALESCE(relevant, 1) != 0) AND """ + where + """
           ORDER BY backlog ASC, first_seen DESC LIMIT ?""", (limit,)).fetchall()


async def fb_details(con, fb, todo, errors: list, lock=None, budget=None, route: str = "home") -> None:
    """Open each listing's page and store what it says. Pages that won't load are counted as misses,
    unless most of a batch fails - that is Facebook walling us, not a batch of removed listings."""
    ok, missed, failures = 0, [], 0
    for r in todo:
        if failures >= 3:        # the browser is gone, not three pages in a row
            errors.append("facebook: item pages keep failing - browser dead? (rest of the batch skipped)")
            break
        if budget and budget.left() <= 0:
            errors.append(f"facebook hourly page budget reached - {len(todo) - ok - len(missed)} item pages wait")
            break
        await step_aside(lock, route)
        try:
            d = await fb.detail(r["ext_id"])
            failures = 0
            if d is None:
                missed.append(r["id"])
            else:
                ok += 1
                apply_detail(con, r["id"], d, r["price"])
                con.commit()
        except SkipFacebook:
            raise
        except Exception as e:
            failures += 1
            errors.append(f"fb detail {r['ext_id']}: {e}")
        await asyncio.sleep(random.uniform(3, 6))
    if len(missed) >= 3 and len(missed) > ok:      # more pages fail than load: a wall, not removals
        errors.append(f"facebook item pages unreadable ({len(missed)}/{ok + len(missed)}) - login wall?")
    else:
        for lid in missed:
            record_miss(con, lid)
        con.commit()


async def parse_pending(con, http, limit: int, errors: list, concurrency: int = PARSE_CONCURRENCY,
                        only: str = "") -> None:
    """LLM parse, once the description is in - or from the title alone after an hour without it (a day
    for the deep sweep's backlog, which queues behind everything else for its page). Fresh finds go first."""
    rows = con.execute(
        """SELECT * FROM listings WHERE parsed = 0 AND status != 'gone'
             AND (detail_fetched = 1 OR first_seen < CASE WHEN backlog = 1 THEN ? ELSE ? END)""" + only + """
           ORDER BY backlog ASC, first_seen DESC LIMIT ?""", (db.now() - 86400, db.now() - 3600, limit)).fetchall()
    gate = asyncio.Semaphore(concurrency)
    failures = {"streak": 0}

    async def parse_one(r):
        async with gate:
            if failures["streak"] >= 3:      # Ollama is down or wedged: stop hammering it, still score + alert
                return
            try:
                p = await parse.parse(http, dict(r))
            except httpx.HTTPError as e:     # can't reach the model at all
                failures["streak"] += 1
                errors.append(f"parse {r['id']}: {e}")
                return
            except Exception as e:           # the model answered, but with something we can't use
                errors.append(f"parse {r['id']}: {type(e).__name__}: {e}")
                p = None
            failures["streak"] = 0
            if p is not None:
                # equipment is re-detected by the next rescore: the text it came from may have changed
                cols = ", ".join(f"{k} = ?" for k in p)
                con.execute(f"UPDATE listings SET {cols}, parsed = 1, equipment = NULL, title_only = ? WHERE id = ?",
                            (*p.values(), int(r["detail_fetched"] == 0), r["id"]))
                db.apply_correction(con, r["id"])      # what Jon fixed by hand stays fixed
            else:
                # unusable output; the model is deterministic, so give up after a few tries
                con.execute("UPDATE listings SET parse_attempts = parse_attempts + 1 WHERE id = ?", (r["id"],))
                con.execute("""UPDATE listings SET parsed = 1, relevant = 0, summary = 'could not read this ad'
                               WHERE id = ? AND parse_attempts >= 3""", (r["id"],))
            con.commit()
    await asyncio.gather(*(parse_one(r) for r in rows))
    if failures["streak"] >= 3:
        errors.append("LLM parsing failed repeatedly - Ollama down? (skipped the rest this run)")


def record_miss(con, lid):
    """An item page we couldn't read. Retry; after a few misses stop waiting for the description
    (parse from the title) and, for FB, treat it as removed - deleted listings redirect to login."""
    con.execute("UPDATE listings SET detail_misses = detail_misses + 1, last_checked = ? WHERE id = ?", (db.now(), lid))
    con.execute("""UPDATE listings SET
                     status = CASE WHEN source = 'facebook' AND detail_fetched = 1 AND status = 'active'
                                   THEN 'gone' ELSE status END,
                     detail_fetched = 1
                   WHERE id = ? AND detail_misses >= ?""", (lid, MAX_DETAIL_MISSES))


def in_active_hours(st) -> bool:
    try:
        a, b = (int(x) for x in st.get("active_hours", "0-24").split("-"))
    except ValueError:
        return True
    return a <= time.localtime().tm_hour < b


async def run(force=False, backfill=False, search=True, quick=False, quiet=False, sold=False) -> None:
    db.init()
    con = db.connect()
    st = db.settings(con)
    if not (force or backfill or in_active_hours(st)):
        print("outside active hours, skipping")
        return
    radius = int(st.get("radius_mi") or 100)
    boost = 5 if backfill else 1   # catch-up runs take bigger bites
    searches = [dict(r) for r in con.execute(
        "SELECT * FROM searches WHERE enabled = 1" + (" AND quick = 1" if quick else ""))]
    random.shuffle(searches)
    t0 = db.now()
    fb_route, fb_until = fb_pick_route(st, t0)
    fb_paused = fb_route is None
    if sold:
        due = searches          # every search, Facebook only, Sold filter
    elif quick:
        due = searches
    else:
        # Facebook: each search runs on its category's cadence (4-seat UTVs + mowers every run, others hourly)
        due = [s for s in searches if backfill or not s["last_run"]
               or s["last_run"] <= t0 - cfg(s["category"])["every_min"] * 60 + 180]
        due.sort(key=lambda s: (s["last_run"] or 0) + cfg(s["category"])["every_min"] * 60)   # most overdue first
        if not backfill:
            due = due[:FB_SEARCHES_PER_RUN]
    # the fast lane and the sold pull only follow up on what they found themselves
    only_new = " AND first_seen >= %d" % t0 if (quick or sold) else ""
    fb_status = "status='sold'" if sold else "status='active'"
    if not search:
        searches, due = [], []
    # we hold the lock, so any unfinished run was killed part-way
    mode = "sold" if sold else "quick" if quick else "all"
    con.execute("""UPDATE runs SET finished = started, errors = '["interrupted"]'
                   WHERE finished IS NULL AND source = ?""", (mode,))
    run_id = con.execute("INSERT INTO runs(started, source) VALUES (?, ?)", (db.now(), mode)).lastrowid
    con.commit()
    errors: list[str] = []
    found = new = alerts = cl_found = 0

    async with craigslist.client() as http:
        # --- Craigslist search (Craigslist has no sold listings - skipped by the sold pull)
        for srch in ([] if sold else searches):
            q = srch["query"]
            try:
                items = await craigslist.search(http, q, st.get("home_zip", "55446"), radius,
                                                cfg(srch["category"])["cl_cat"])
                found += len(items)
                cl_found += len(items)
                new += sum(upsert(con, "craigslist", i) for i in items)
                con.commit()
            except Exception as e:  # keep going; one bad query shouldn't kill the run
                errors.append(f"cl '{q}': {e}")
            await asyncio.sleep(random.uniform(1.5, 3))
        cl_failed = sum(e.startswith("cl '") for e in errors)
        if not sold and searches and cl_failed == len(searches):
            errors.append(f"craigslist failed for every search ({cl_failed}) - blocked?")

        # --- Facebook search + item pages (one browser at a time across the scan lanes)
        fb_found = 0
        fb_lock = await acquire_fb_lock(QUICK_LOCK_WAIT if quick else 900, quick=quick)
        if fb_lock:     # the wait can be long: another lane may have been blocked and paused a route meanwhile
            st = db.settings(con)
            fb_route, fb_until = fb_pick_route(st, db.now())
            fb_paused = fb_route is None
        if fb_paused:
            errors.append(f"facebook paused until {time.strftime('%H:%M', time.localtime(fb_until))} after it blocked us")
        elif fb_route == "proxy":
            print("facebook via the VPN proxy this run")
        elif fb_lock is None:
            errors.append("facebook busy with another scan lane - skipped this run")
        try:
            if fb_paused or fb_lock is None:
                raise SkipFacebook
            budget = FBBudget(con, reserve=0 if quick else FB_BUDGET_RESERVE)
            async with async_playwright() as pw, Facebook(pw, st.get("fb_proxy") if fb_route == "proxy" else None,
                                                          on_load=budget.record) as fb:
                empty = failures = 0
                if quick:   # the full scan may have run the same search minutes ago
                    due = [s for s in due if (s["last_run"] or 0) < t0 - QUICK_REPEAT_SECS]
                for srch in due:
                    if budget.left() <= 0:
                        errors.append(f"facebook hourly page budget reached ({budget.used()} loads) - rest of the run skipped")
                        raise BudgetSpent
                    if failures >= 3:
                        errors.append("facebook: searches keep failing - browser dead? (rest of the run skipped)")
                        raise SkipFacebook
                    if not quick:
                        await step_aside(fb_lock, fb_route)
                    q = srch["query"]
                    try:
                        items = await fb.search(q, st.get("fb_location", "plymouth-mn"), radius,
                                                sort="best_match" if (backfill or sold) else "newest",
                                                scrolls=0 if quick else 3 if sold else None, sold=sold)
                        if sold:     # the Sold filter can include a stray available item - keep only sold ones
                            items = [i for i in items if i["status"] == "sold"]
                        found += len(items)
                        fb_found += len(items)
                        empty += not items
                        new += sum(upsert(con, "facebook", i) for i in items)
                        if not sold:
                            con.execute("UPDATE searches SET last_run = ? WHERE id = ?", (db.now(), srch["id"]))
                        con.commit()
                        failures = 0
                    except SkipFacebook:
                        raise
                    except Exception as e:
                        failures += 1
                        errors.append(f"fb '{q}': {e}")
                    await pause()
                if due and empty == len(due):  # (skipped when --no-search)
                    # a short list of thin searches can all be empty honestly: ask one that never is
                    if len(due) >= 3 or not await fb.search(CANARY, st.get("fb_location", "plymouth-mn"), radius, scrolls=0):
                        errors.append("facebook returned nothing for every search (login wall?)")

                todo = pending_details(con, (SOLD_DETAILS_PER_RUN if sold else FB_DETAILS_PER_RUN) * boost,
                                       fb_status + ("" if sold else only_new))
                if not (quick or sold):
                    seen_ids = {r["id"] for r in todo}
                    todo += [r for r in recheck_candidates(con) if r["id"] not in seen_ids]
                await fb_details(con, fb, todo, errors, lock=None if quick else fb_lock, budget=budget, route=fb_route)
        except (SkipFacebook, BudgetSpent):
            pass
        except Exception as e:
            errors.append(f"facebook: {e}")
        finally:
            # record a block while we still hold the lock, so the next lane in sees the pause first thing
            msg = fb_backoff(con, st, walled=any("login wall" in e for e in errors), fb_found=fb_found, route=fb_route or "home")
            if msg:
                errors.append(msg)
            if fb_lock:
                fb_lock.close()

        # --- Craigslist posting pages (new ones, plus every starred one on full scans)
        cl_todo = con.execute(
                """SELECT id, url, price FROM listings WHERE source='craigslist' AND detail_fetched=0""" + only_new + """
                   ORDER BY first_seen DESC LIMIT ?""", (CL_DETAILS_PER_RUN * boost,)).fetchall()
        if sold:
            cl_todo = []
        elif not quick:
            cl_todo += con.execute("""SELECT id, url, price FROM listings WHERE source='craigslist' AND starred=1
                                      AND detail_fetched=1 AND status IN ('active','pending')""").fetchall()
        for r in cl_todo:
            try:
                d = await craigslist.detail(http, r["url"])
                if d is None:
                    record_miss(con, r["id"])
                else:
                    apply_detail(con, r["id"], d, r["price"])
                con.commit()
            except Exception as e:
                errors.append(f"cl detail {r['url']}: {e}")
            await asyncio.sleep(random.uniform(1, 2.5))

        # --- LLM parse (once the description is in, or after an hour without one)
        await parse_pending(con, http, PARSES_PER_RUN * boost, errors,
                            concurrency=1 if (quick or sold) else PARSE_CONCURRENCY, only=only_new if quick else "")

        # Craigslist search shows every live posting, so not seen in a while there means gone (only judged
        # when Craigslist answered this run). A Facebook search shows only its first page, so a Facebook
        # listing is marked gone by its item page (fb_details / record_miss), never by the calendar - except
        # ads that aren't machines, which nobody re-checks.
        if not (quick or sold):
            cutoff = db.now() - stale_after(db.settings(con))
            if cl_found > 0:
                con.execute("""UPDATE listings SET status='gone' WHERE status IN ('active','pending')
                               AND source = 'craigslist' AND last_seen < ?""", (cutoff,))
            con.execute("""UPDATE listings SET status='gone' WHERE status IN ('active','pending')
                           AND source = 'facebook' AND COALESCE(relevant, 0) = 0 AND parsed = 1 AND last_seen < ?""", (cutoff,))
            con.commit()

        score.rescore_all(con)
        if not (quick or sold):
            db.prune(con)
            try:
                score.snapshot_scorecard(con)
            except Exception as e:      # a report, never worth failing a scan over
                errors.append(f"scorecard: {e}")
        try:
            await geo.fill(con)
        except Exception as e:
            errors.append(f"geocode: {e}")

        # the 04:30 sold pull only reports on watched listings; deals wait for the morning's first scan
        alerts = await send_alerts(con, http, st, quiet=backfill or quiet, watch_only=sold)

    con.execute("UPDATE runs SET finished=?, found=?, new=?, alerts=?, errors=? WHERE id=?",
                (db.now(), found, new, alerts, json.dumps(errors[:30]) if errors else None, run_id))
    con.commit()
    print(f"found={found} new={new} alerts={alerts} errors={len(errors)}")
    for e in errors[:10]:
        print("  !", e)
    serious = [e for e in errors if "login wall" in e or "Ollama down" in e or e.startswith("pausing Facebook")
               or e.startswith("facebook: ") or e.startswith("craigslist failed")]
    if serious and not quiet:
        async with httpx.AsyncClient(timeout=30) as http:
            await problem_alert(con, http, mode, "; ".join(serious))


async def problem_alert(con, http, mode: str, what: str) -> None:
    """The scanner itself has a problem. Tell Jon, at most once every PROBLEM_ALERT_EVERY."""
    last = con.execute("SELECT value FROM settings WHERE key = 'last_problem_alert'").fetchone()
    if last and db.now() - int(last[0]) < PROBLEM_ALERT_EVERY:
        return
    if await notify.send_text(http, f"⚠️ <b>Deal Finder problem</b> ({mode} scan): {html.escape(what)}\n"
                                    f"Check Setup → Recent scans on the <a href=\"{notify.DASHBOARD_URL}\">dashboard</a>."):
        con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('last_problem_alert', ?)", (str(db.now()),))
        con.commit()


async def send_alerts(con, http, st, quiet=False, watch_only=False) -> int:
    # The full scan and the fast lane both alert. One shared lock around select -> send -> mark
    # means a listing is claimed by exactly one of them.
    os.makedirs(LOCK_DIR, exist_ok=True)
    with open(ALERT_LOCK, "w") as lock:
        await asyncio.to_thread(fcntl.flock, lock, fcntl.LOCK_EX)
        if watch_only:
            return await _watch_alerts(con, http, quiet)
        return await _send_alerts(con, http, st, quiet)


async def _send_alerts(con, http, st, quiet) -> int:
    threshold = int(st.get("alert_threshold") or 75)
    private = ("AND COALESCE(is_dealer, 0) = 0 AND COALESCE(seller_type, '') != 'dealer'"
               if st.get("alert_private_only") == "1" else "")
    rules = db.alert_rules(st)
    radius = int(st.get("radius_mi") or 100)
    dist = buybox.distance_fn(con, st)

    min_pct = float(st.get("alert_min_pct") or 0) / 100
    min_usd = float(st.get("alert_min_usd") or 0)

    def enough_savings(r):
        """A deal alert must be worth acting on: the discount against typical asking (or what similar
        ones sell for, when that's lower) clears both the % and the $ floors from Setup."""
        if not (min_pct or min_usd):
            return True
        ref = min(x for x in (r["expected"], r["expected_sold"]) if x) if (r["expected"] or r["expected_sold"]) else None
        if not ref or not r["price"]:
            return False
        saving = ref - r["price"]
        return saving >= min_usd and saving / ref >= min_pct

    def passes_limits(r):
        """The category's buy box (models, year, price, use, distance), shared by both alert types;
        each type has its own on/off switch."""
        return buybox.fits(rules.get(r["category"]) or {}, r, dist(r), radius)

    def fits_need(r, kind):
        """Trailers that can't carry a 4-seat UTV only alert when the deal is exceptional."""
        if r["category"] != "trailer" or r["utv_fit"] == "yes":
            return True
        return kind == "enabled" and (r["score"] or 0) >= cfg("trailer")["fit_gate"]

    def switched_on(r, kind):
        return bool((rules.get(r["category"]) or {}).get(kind))

    def mark(sql, rows, kind=None):
        """Record the alert from the rows as they were selected and sent - another lane may have changed
        the listing's price or score in the meantime, and a marker from the newer row would swallow the
        price-drop alert that change deserves."""
        con.executemany(sql, [(r["score"], r["price"], r["id"]) for r in rows])
        if kind:   # delivered (or silently accepted by a backfill): remember title + price for cross-posts
            con.executemany("INSERT INTO alert_log(listing_id, title_key, price, kind, ts) VALUES (?, ?, ?, ?, ?)",
                            [(r["id"], db.title_key(r["title"]), r["price"], kind, db.now()) for r in rows])
        con.commit()

    deal_mark = "UPDATE listings SET alerted_score = ?, fresh_alerted = 1, alerted_price = ? WHERE id = ?"
    fresh_mark = "UPDATE listings SET fresh_alerted = 1, alerted_price = ?2 WHERE id = ?3"

    # Cross-posts / reposts: a DIFFERENT listing with the same title has alerted at this price at any
    # point in the last 30 days (full history in alert_log), or was picked earlier in this run.
    # A listing never blocks its own follow-up alerts (score jump, price cut).
    # ...and a twin must look like the same seller: the other site, or the same town (titles like
    # "2021 Polaris Ranger 1000" at a round price belong to many different machines).
    seen: dict[tuple, dict] = {}
    for r in con.execute("""SELECT a.listing_id, a.title_key, a.price, l.source, l.location FROM alert_log a
                            JOIN listings l ON l.id = a.listing_id WHERE a.ts >= ?""", (db.now() - 30 * 86400,)):
        seen.setdefault((r["title_key"], r["price"]), {})[r["listing_id"]] = (r["source"], (r["location"] or "").lower())

    def first_copy(r):
        group = seen.setdefault((db.title_key(r["title"]), r["price"]), {})
        mine = (r["source"], (r["location"] or "").lower())
        if any(src != mine[0] or loc == mine[1] for lid, (src, loc) in group.items() if lid != r["id"]):
            return False
        group[r["id"]] = mine
        return True

    def why_not(r) -> str | None:
        """The first reason a candidate above the threshold is not alerted (recorded for the activity panel)."""
        if not switched_on(r, "enabled"):
            return "instant alerts are off for this category"
        if not fits_need(r, "enabled"):
            return "trailer too small for a UTV"
        if not passes_limits(r):
            return "outside the buy box"
        if not enough_savings(r):
            return "savings under the alert floor"
        if not first_copy(r):
            return "same ad already alerted (cross-post)"
        return None

    deals, activity = [], []
    for r in con.execute(
            f"""SELECT * FROM listings WHERE relevant = 1 AND status = 'active' AND hidden = 0
                  AND score >= ? AND (alerted_score IS NULL
                                      OR price <= alerted_price * (1 - ?)                 -- a real price cut
                                      OR (score >= alerted_score + 10 AND price < alerted_price)) {private}
                ORDER BY score DESC""", (threshold, REALERT_CUT)).fetchall():
        reason = why_not(r)
        if reason is None:
            deals.append(r)
        elif reason != "instant alerts are off for this category" or r["alerted_score"] is None:
            activity.append((r["id"], "skipped", reason))
    # a skip is recorded once per listing per reason per day, not every 10 minutes
    recent = {(a["listing_id"], a["reason"]) for a in con.execute(
        "SELECT listing_id, reason FROM alert_activity WHERE outcome = 'skipped' AND ts >= ?", (db.now() - 86400,))}
    con.executemany("INSERT INTO alert_activity(ts, listing_id, outcome, reason) VALUES (?, ?, ?, ?)",
                    [(db.now(), lid, o, why) for lid, o, why in activity if (lid, why) not in recent])
    con.commit()

    # Old listings the first deep sweep dug up: Jon gets a summary of those, not a Telegram flood.
    # Recorded as alerted at this score, so a later price cut that lifts the score still alerts.
    old_news = [r for r in deals if r["backlog"] and r["alerted_score"] is None]
    if old_news:
        mark(deal_mark, old_news, "backlog")
        skip = {r["id"] for r in old_news}
        deals = [r for r in deals if r["id"] not in skip]

    # "Just listed": fresh private listings priced normally or better with no known problems,
    # so Jon can message the seller first
    window = int(st.get("fresh_window_min") or 120) * 60
    deal_ids = {r["id"] for r in deals}
    fresh = [r for r in con.execute(
        f"""SELECT * FROM listings WHERE relevant = 1 AND status = 'active' AND hidden = 0
              AND fresh_alerted IS NULL AND COALESCE(is_new, 0) = 0 AND score >= ?
              AND COALESCE(red_flags, '[]') = '[]'
              AND listed_at IS NOT NULL AND listed_at >= ? {private}
            ORDER BY COALESCE(listed_at, first_seen) DESC""",
        (int(st.get("fresh_min_score") or 50), db.now() - window)).fetchall()
        if r["id"] not in deal_ids and switched_on(r, "fresh") and fits_need(r, "fresh") and passes_limits(r)
        and first_copy(r)]

    def log(rows, outcome, reason=None):
        con.executemany("INSERT INTO alert_activity(ts, listing_id, outcome, reason) VALUES (?, ?, ?, ?)",
                        [(db.now(), r["id"], outcome, reason) for r in rows])
        con.commit()

    if old_news:
        log(old_news, "recorded", "older listing dug up by the deep sweep - no alert, it's on the dashboard")
    if quiet:   # backfill: remember everything as seen so the first real run doesn't flood
        log(deals, "recorded", "quiet run")
        mark(deal_mark, deals, "backfill")
        mark(fresh_mark, fresh, "backfill")
        await _watch_alerts(con, http, quiet=True)
        return 0

    # markers are written only after Telegram accepts the message, so failures retry next run
    sent = 0
    for r in deals[:MAX_ALERTS_PER_RUN]:
        if await notify.send_listing(http, r):
            sent += 1
            mark(deal_mark, [r], "deal")
            log([r], "sent", f"deal alert at score {r['score']}" + (" (price cut)" if r["alerted_score"] is not None else ""))
        else:
            log([r], "failed", "Telegram did not accept the message - will retry next run")
    rest = deals[MAX_ALERTS_PER_RUN:]      # go out one by one over the next runs, nothing is swallowed
    if rest:
        await notify.send_text(http, f"…and {len(rest)} more above {threshold} coming, or see the "
                                     f'<a href="{notify.DASHBOARD_URL}">dashboard</a>.')
    for r in fresh[:MAX_FRESH_PER_RUN]:   # any beyond the cap go out next run (still fresh)
        mins = max(1, (db.now() - (r["listed_at"] or r["first_seen"])) // 60)
        if await notify.send_listing(http, r, header=f"🆕 <b>Just listed</b> {mins} min ago - be first to message"):
            sent += 1
            mark(fresh_mark, [r], "fresh")
            log([r], "sent", "just-listed alert")
    sent += await _watch_alerts(con, http, quiet)
    return sent


async def _watch_alerts(con, http, quiet) -> int:
    """Starred listings: tell Jon when the price drops, it goes pending, or it's sold / removed.
    watch_price / watch_status hold the last state he was told about (set when starred)."""
    rows = con.execute("""SELECT * FROM listings WHERE starred = 1 AND watch_status IS NOT NULL
                          AND (price < watch_price OR status != watch_status)""").fetchall()
    sent = 0
    for r in rows:
        header = None
        if r["status"] != r["watch_status"] and r["status"] in ("pending", "sold", "gone"):
            header = {"pending": "⭐ <b>Now pending</b> - ask to be next in line if it falls through",
                      "sold": "⭐ <b>Marked sold</b>", "gone": "⭐ <b>Removed</b> (sold or taken down)"}[r["status"]]
        elif r["watch_price"] and r["price"] and r["price"] < r["watch_price"]:
            header = (f"⭐ <b>Price drop</b> ${r['watch_price']:,} → ${r['price']:,} "
                      f"(−${r['watch_price'] - r['price']:,}) on a listing you're watching")
        elif r["status"] == "active" and r["watch_status"] in ("pending", "sold", "gone"):
            header = "⭐ <b>Back on the market</b>"
        ok = quiet or header is None or await notify.send_listing(http, r, header=header, ask=False)
        if ok:      # a failed send keeps the old state, so it's retried next scan
            con.execute("UPDATE listings SET watch_price = ?, watch_status = ? WHERE id = ?",
                        (r["price"], r["status"], r["id"]))
            con.commit()
            sent += bool(header) and not quiet
    return sent


def main():
    quick, sold = "--quick" in sys.argv, "--sold" in sys.argv
    os.makedirs(LOCK_DIR, exist_ok=True)
    lock = open(os.path.join(LOCK_DIR, "quick.lock" if quick else "sold.lock" if sold else "scan.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another scan is running")
        return
    try:
        asyncio.run(run(force="--force" in sys.argv, backfill="--backfill" in sys.argv,
                        search="--no-search" not in sys.argv, quick=quick, quiet="--quiet" in sys.argv, sold=sold))
    except Exception as e:
        traceback.print_exc()
        try:      # the run crashed outright - say so, or Telegram just goes quiet
            con = db.connect()
            asyncio.run(_crash_alert(con, quick, sold, f"{type(e).__name__}: {e}"[:300]))
        except Exception:
            traceback.print_exc()
        sys.exit(1)


async def _crash_alert(con, quick, sold, what):
    async with httpx.AsyncClient(timeout=30) as http:
        await problem_alert(con, http, "sold" if sold else "quick" if quick else "full", "crashed: " + what)


if __name__ == "__main__":
    main()
