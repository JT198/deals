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

from . import db, geo, notify, parse, score
from .categories import cfg
from .sources import craigslist
from .sources.facebook import Facebook, pause

MAX_ALERTS_PER_RUN = 6
MAX_FRESH_PER_RUN = 8
LOCK_DIR = os.path.join(os.path.dirname(db.DB_PATH), "locks")
ALERT_LOCK = os.path.join(LOCK_DIR, "alert.lock")
PROBLEM_ALERT_EVERY = 6 * 3600   # Telegram "scanner has a problem" at most this often
FB_SEARCHES_PER_RUN = 20      # ~5 min of searching; keep Facebook traffic modest (it blocks heavy IPs)
FB_BACKOFF_HOURS = (2, 4, 8)  # pause all Facebook traffic this long after it returns nothing; escalates on repeats
FB_DETAILS_PER_RUN = 40
SOLD_DETAILS_PER_RUN = 120    # item pages for newly seen sold listings (the first pull has a backlog)
FB_RECHECKS_PER_RUN = 6
CL_DETAILS_PER_RUN = 40
PARSES_PER_RUN = 120
PARSE_CONCURRENCY = 3           # parallel requests to Ollama on .76
STALE_AFTER = 5 * 86400


def upsert(con, source: str, item: dict) -> bool:
    lid = f"{source}:{item['ext_id']}"
    t = db.now()
    status = item.get("status", "active")
    row = con.execute("SELECT price, status, user_gone FROM listings WHERE id = ?", (lid,)).fetchone()
    if row is None:
        # OR IGNORE: the quick lane and a full scan can both see a new listing in the same minute
        cur = con.execute(
            """INSERT OR IGNORE INTO listings(id, source, ext_id, url, title, price, first_price, strike_price,
                 location, image, listed_at, first_seen, last_seen, status, seen_active)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (lid, source, item["ext_id"], item["url"], item["title"], item["price"], item["price"],
             item.get("strike_price"), item.get("location"), item.get("image"),
             item.get("listed_at"), t, t, status, int(status == "active")))
        if cur.rowcount and item["price"]:
            con.execute("INSERT INTO price_history VALUES (?,?,?)", (lid, t, item["price"]))
        return bool(cur.rowcount)
    if row["user_gone"] and status != "sold":
        status = "gone"          # Jon said it's gone; a cached search result doesn't overrule him
    con.execute(
        """UPDATE listings SET last_seen = ?, status = ?, title = ?, detail_misses = 0,
             seen_active = CASE WHEN ? = 'active' THEN 1 ELSE seen_active END,
             strike_price = COALESCE(?, strike_price), image = COALESCE(image, ?),
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
    con.execute(
        """UPDATE listings SET description = COALESCE(?, description), seller_type = COALESCE(?, seller_type),
             listed_at = COALESCE(?, listed_at), image = COALESCE(?, image), status = ?,
             lat = COALESCE(?, lat), lon = COALESCE(?, lon),
             detail_fetched = 1, detail_misses = 0, last_checked = ?, last_seen = ?,
             seen_active = CASE WHEN ? = 'active' THEN 1 ELSE seen_active END WHERE id = ?""",
        (d.get("description"), d.get("seller_type"), d.get("listed_at"), d.get("image"),
         d.get("status", "active"), d.get("lat"), d.get("lon"), t, t, d.get("status", "active"), lid))
    set_price(con, lid, old_price, d.get("price"))


HOT_SCORE = 60                # listings worth showing get re-checked every HOT_RECHECK_SECS
HOT_RECHECK_SECS = 6 * 3600
HOT_RECHECKS_PER_RUN = 6


def recheck_candidates(con) -> list:
    """Facebook item pages to re-open this full scan, to notice price cuts, pending, sold and removed:
      1. suspects - failed to load last time; retried every scan so a removed listing is confirmed in ~an hour
      2. starred listings, every scan (watch alerts)
      3. listings scoring HOT_SCORE+ not checked in HOT_RECHECK_SECS (the ones Jon actually looks at)
      4. a round-robin of everything else relevant, oldest check first"""
    live = "source='facebook' AND detail_fetched=1 AND status IN ('active','pending')"
    now = db.now()
    groups = [
        con.execute(f"""SELECT id, ext_id, price FROM listings WHERE {live} AND detail_misses > 0
                        ORDER BY COALESCE(last_checked, 0) ASC LIMIT 20""").fetchall(),
        con.execute(f"SELECT id, ext_id, price FROM listings WHERE {live} AND starred = 1").fetchall(),
        con.execute(f"""SELECT id, ext_id, price FROM listings WHERE {live} AND relevant = 1 AND score >= ?
                        AND COALESCE(last_checked, 0) < ? ORDER BY score DESC LIMIT ?""",
                    (HOT_SCORE, now - HOT_RECHECK_SECS, HOT_RECHECKS_PER_RUN)).fetchall(),
        con.execute(f"""SELECT id, ext_id, price FROM listings WHERE {live} AND relevant = 1
                        ORDER BY COALESCE(last_checked, 0) ASC LIMIT ?""", (FB_RECHECKS_PER_RUN,)).fetchall(),
    ]
    out, seen = [], set()
    for g in groups:
        for r in g:
            if r["id"] not in seen:
                seen.add(r["id"])
                out.append(r)
    return out


class SkipFacebook(Exception):
    """Facebook is paused or another lane has the browser - skip this run's Facebook work."""


async def acquire_fb_lock(wait_secs: int):
    """Only one scan lane talks to Facebook at a time. Returns the open lock file, or None after wait_secs."""
    os.makedirs(LOCK_DIR, exist_ok=True)
    f = open(os.path.join(LOCK_DIR, "facebook.lock"), "w")
    deadline = time.time() + wait_secs
    while True:
        try:
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
    found = new = alerts = 0

    async with craigslist.client() as http:
        # --- Craigslist search (Craigslist has no sold listings - skipped by the sold pull)
        for srch in ([] if sold else searches):
            q = srch["query"]
            try:
                items = await craigslist.search(http, q, st.get("home_zip", "55446"), radius,
                                                cfg(srch["category"])["cl_cat"])
                found += len(items)
                new += sum(upsert(con, "craigslist", i) for i in items)
                con.commit()
            except Exception as e:  # keep going; one bad query shouldn't kill the run
                errors.append(f"cl '{q}': {e}")
            await asyncio.sleep(random.uniform(1.5, 3))

        # --- Facebook search + item pages (one browser at a time across the scan lanes)
        fb_found = 0
        fb_lock = await acquire_fb_lock(60 if quick else 900)
        if fb_paused:
            errors.append(f"facebook paused until {time.strftime('%H:%M', time.localtime(fb_until))} after it blocked us")
        elif fb_route == "proxy":
            print("facebook via the VPN proxy this run")
        elif fb_lock is None:
            errors.append("facebook busy with another scan lane - skipped this run")
        try:
            if fb_paused or fb_lock is None:
                raise SkipFacebook
            async with async_playwright() as pw, Facebook(pw, st.get("fb_proxy") if fb_route == "proxy" else None) as fb:
                empty = 0
                for srch in due:
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
                    except Exception as e:
                        errors.append(f"fb '{q}': {e}")
                    await pause()
                if due and empty == len(due):  # (skipped when --no-search)
                    errors.append("facebook returned nothing for every search (login wall?)")

                todo = con.execute(
                    """SELECT id, ext_id, price FROM listings
                       WHERE source='facebook' AND detail_fetched=0 AND """ + fb_status + ("" if sold else only_new) + """
                       ORDER BY first_seen DESC LIMIT ?""",
                    ((SOLD_DETAILS_PER_RUN if sold else FB_DETAILS_PER_RUN) * boost,)).fetchall()
                if not (quick or sold):
                    seen_ids = {r["id"] for r in todo}
                    todo += [r for r in recheck_candidates(con) if r["id"] not in seen_ids]
                ok, missed = 0, []
                for r in todo:
                    try:
                        d = await fb.detail(r["ext_id"])
                        if d is None:
                            missed.append(r["id"])
                        else:
                            ok += 1
                            apply_detail(con, r["id"], d, r["price"])
                            con.commit()
                    except Exception as e:
                        errors.append(f"fb detail {r['ext_id']}: {e}")
                    await asyncio.sleep(random.uniform(3, 6))
                if missed and ok == 0 and len(missed) >= 3:
                    # every page failed: FB is walling us, not a batch of removed listings
                    errors.append(f"facebook item pages unreadable ({len(missed)}/{len(todo)}) - login wall?")
                else:
                    for lid in missed:
                        record_miss(con, lid)
                    con.commit()
        except SkipFacebook:
            pass
        except Exception as e:
            errors.append(f"facebook: {e}")
        finally:
            if fb_lock:
                fb_lock.close()
        msg = fb_backoff(con, st, walled=any("login wall" in e for e in errors), fb_found=fb_found, route=fb_route or "home")
        if msg:
            errors.append(msg)

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
        rows = con.execute(
            """SELECT * FROM listings WHERE parsed = 0 AND status != 'gone'
                 AND (detail_fetched = 1 OR first_seen < ?)""" + (only_new if quick else "") + """
               ORDER BY first_seen DESC LIMIT ?""", (db.now() - 3600, PARSES_PER_RUN * boost)).fetchall()
        gate = asyncio.Semaphore(1 if (quick or sold) else PARSE_CONCURRENCY)
        failures = {"streak": 0}

        async def parse_one(r):
            async with gate:
                if failures["streak"] >= 3:      # Ollama is down or wedged: stop hammering it, still score + alert
                    return
                try:
                    p = await parse.parse(http, dict(r))
                    if p is not None:
                        cols = ", ".join(f"{k} = ?" for k in p)
                        con.execute(f"UPDATE listings SET {cols}, parsed = 1 WHERE id = ?", (*p.values(), r["id"]))
                    else:
                        # the model gave unusable output; it's deterministic, so give up after a few tries
                        con.execute("UPDATE listings SET parse_attempts = parse_attempts + 1 WHERE id = ?", (r["id"],))
                        con.execute("""UPDATE listings SET parsed = 1, relevant = 0, summary = 'could not read this ad'
                                       WHERE id = ? AND parse_attempts >= 3""", (r["id"],))
                    con.commit()
                    failures["streak"] = 0
                except Exception as e:
                    failures["streak"] += 1
                    errors.append(f"parse {r['id']}: {e}")
        await asyncio.gather(*(parse_one(r) for r in rows))
        if failures["streak"] >= 3:
            errors.append("LLM parsing failed repeatedly - Ollama down? (skipped the rest this run)")

        # listings we haven't seen or confirmed in a while are probably gone
        fb_blocked = any("login wall" in e for e in errors)
        if not (quick or sold) and not fb_blocked and found > 0:
            con.execute("UPDATE listings SET status='gone' WHERE status IN ('active','pending') AND last_seen < ?",
                        (db.now() - STALE_AFTER,))
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

        alerts = await send_alerts(con, http, st, quiet=backfill or quiet)

    con.execute("UPDATE runs SET finished=?, found=?, new=?, alerts=?, errors=? WHERE id=?",
                (db.now(), found, new, alerts, json.dumps(errors[:30]) if errors else None, run_id))
    con.commit()
    print(f"found={found} new={new} alerts={alerts} errors={len(errors)}")
    for e in errors[:10]:
        print("  !", e)
    serious = [e for e in errors if "login wall" in e or "Ollama down" in e or e.startswith("pausing Facebook")]
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


async def send_alerts(con, http, st, quiet=False) -> int:
    # The full scan and the fast lane both alert. One shared lock around select -> send -> mark
    # means a listing is claimed by exactly one of them.
    os.makedirs(LOCK_DIR, exist_ok=True)
    with open(ALERT_LOCK, "w") as lock:
        await asyncio.to_thread(fcntl.flock, lock, fcntl.LOCK_EX)
        return await _send_alerts(con, http, st, quiet)


async def _send_alerts(con, http, st, quiet) -> int:
    threshold = int(st.get("alert_threshold") or 75)
    private = ("AND COALESCE(is_dealer, 0) = 0 AND COALESCE(seller_type, '') != 'dealer'"
               if st.get("alert_private_only") == "1" else "")
    rules = db.alert_rules(st)
    # FB sometimes mixes in "suggested" listings far outside the radius
    home = (float(st["home_lat"]), float(st["home_lon"]))
    radius = int(st.get("radius_mi") or 100)
    places = {r["place"]: (r["lat"], r["lon"]) for r in con.execute("SELECT * FROM geocache")}

    def in_range(r):
        d = geo.distance(r, home, places)
        return d is None or d <= radius + 10

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
        """Filters shared by both alert types; each type has its own on/off switch."""
        rule = rules.get(r["category"]) or {}
        return (in_range(r)
                and not (rule.get("max_price") and (r["price"] or 0) > int(rule["max_price"]))
                and not (rule.get("min_year") and (r["year"] or 0) < int(rule["min_year"])))

    def fits_need(r, kind):
        """Trailers that can't carry a 4-seat UTV only alert when the deal is exceptional."""
        if r["category"] != "trailer" or r["utv_fit"] == "yes":
            return True
        return kind == "enabled" and (r["score"] or 0) >= cfg("trailer")["fit_gate"]

    def switched_on(r, kind):
        return bool((rules.get(r["category"]) or {}).get(kind))

    def mark(sql, ids, kind=None):
        con.executemany(sql, [(i,) for i in ids])
        if kind:   # delivered (or silently accepted by a backfill): remember title + price for cross-posts
            for i in ids:
                r = con.execute("SELECT title, price FROM listings WHERE id = ?", (i,)).fetchone()
                con.execute("INSERT INTO alert_log(listing_id, title_key, price, kind, ts) VALUES (?, ?, ?, ?, ?)",
                            (i, db.title_key(r["title"]), r["price"], kind, db.now()))
        con.commit()

    deal_mark = "UPDATE listings SET alerted_score = score, fresh_alerted = 1, alerted_price = price WHERE id = ?"
    fresh_mark = "UPDATE listings SET fresh_alerted = 1, alerted_price = price WHERE id = ?"

    # Cross-posts / reposts: a DIFFERENT listing with the same title has alerted at this price at any
    # point in the last 30 days (full history in alert_log), or was picked earlier in this run.
    # A listing never blocks its own follow-up alerts (score jump, price cut).
    seen: dict[tuple, set] = {}
    for r in con.execute("SELECT listing_id, title_key, price FROM alert_log WHERE ts >= ?",
                         (db.now() - 30 * 86400,)):
        seen.setdefault((r["title_key"], r["price"]), set()).add(r["listing_id"])

    def first_copy(r):
        ids = seen.setdefault((db.title_key(r["title"]), r["price"]), set())
        if ids - {r["id"]}:
            return False
        ids.add(r["id"])
        return True

    deals = [r for r in con.execute(
        f"""SELECT * FROM listings WHERE relevant = 1 AND status = 'active' AND hidden = 0
              AND score >= ? AND (alerted_score IS NULL OR score >= alerted_score + 10) {private}
            ORDER BY score DESC""", (threshold,)).fetchall()
        if switched_on(r, "enabled") and fits_need(r, "enabled") and passes_limits(r) and enough_savings(r) and first_copy(r)]

    # "Just listed": fresh private listings priced normally or better with no known problems,
    # so Jon can message the seller first
    window = int(st.get("fresh_window_min") or 120) * 60
    deal_ids = {r["id"] for r in deals}
    fresh = [r for r in con.execute(
        f"""SELECT * FROM listings WHERE relevant = 1 AND status = 'active' AND hidden = 0
              AND fresh_alerted IS NULL AND COALESCE(is_new, 0) = 0 AND score >= ?
              AND COALESCE(red_flags, '[]') = '[]'
              AND COALESCE(listed_at, first_seen) >= ? {private}
            ORDER BY COALESCE(listed_at, first_seen) DESC""",
        (int(st.get("fresh_min_score") or 50), db.now() - window)).fetchall()
        if r["id"] not in deal_ids and switched_on(r, "fresh") and fits_need(r, "fresh") and passes_limits(r)
        and first_copy(r)]

    if quiet:   # backfill: remember everything as seen so the first real run doesn't flood
        mark(deal_mark, deal_ids, "backfill")
        mark(fresh_mark, [r["id"] for r in fresh], "backfill")
        await _watch_alerts(con, http, quiet=True)
        return 0

    # markers are written only after Telegram accepts the message, so failures retry next run
    sent = 0
    for r in deals[:MAX_ALERTS_PER_RUN]:
        if await notify.send_listing(http, r):
            sent += 1
            mark(deal_mark, [r["id"]], "deal")
    rest = deals[MAX_ALERTS_PER_RUN:]
    if rest and await notify.send_text(http, f"…and {len(rest)} more above {threshold} "
                                             f'on the <a href="{notify.DASHBOARD_URL}">dashboard</a>.'):
        mark(deal_mark, [r["id"] for r in rest], "deal-summary")
    for r in fresh[:MAX_FRESH_PER_RUN]:   # any beyond the cap go out next run (still fresh)
        mins = max(1, (db.now() - (r["listed_at"] or r["first_seen"])) // 60)
        if await notify.send_listing(http, r, header=f"🆕 <b>Just listed</b> {mins} min ago - be first to message"):
            sent += 1
            mark(fresh_mark, [r["id"]], "fresh")
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
        ok = quiet or header is None or await notify.send_listing(http, r, header=header)
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
