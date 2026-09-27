"""SQLite storage. One file, WAL mode; the scanner and the web app share it."""
import json
import os
import re
import sqlite3
import time

from .categories import CATEGORIES

DB_PATH = os.environ.get("DEALS_DB", "/opt/deals/data/deals.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS searches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  query TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 1,
  category TEXT NOT NULL DEFAULT 'utv4',
  last_run INTEGER                  -- last Facebook run (unix)
);

CREATE TABLE IF NOT EXISTS listings (
  id TEXT PRIMARY KEY,              -- "<source>:<ext_id>"
  source TEXT NOT NULL,             -- facebook | craigslist
  ext_id TEXT NOT NULL,
  url TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT,
  price INTEGER,
  first_price INTEGER,              -- price the first time we saw it
  strike_price INTEGER,             -- seller's crossed-out "was" price (FB)
  location TEXT,
  image TEXT,
  seller_type TEXT,                 -- private | dealer | NULL (unknown)
  listed_at INTEGER,                -- unix seconds, from the site
  first_seen INTEGER NOT NULL,
  last_seen INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',   -- active | pending | sold | gone
  detail_fetched INTEGER NOT NULL DEFAULT 0,
  last_checked INTEGER,             -- last item-page status check
  -- LLM-parsed
  parsed INTEGER NOT NULL DEFAULT 0,
  relevant INTEGER,                 -- 1 = a complete machine in one of our categories, for sale
  category TEXT,                    -- see categories.py
  year INTEGER, make TEXT, model TEXT, family TEXT, trim TEXT,
  seats INTEGER, hours INTEGER, miles INTEGER, turbo INTEGER, deck_in INTEGER, engine TEXT,
  is_dealer INTEGER, is_new INTEGER, motivated INTEGER, extras TEXT, red_flags TEXT, summary TEXT,
  -- scoring
  expected INTEGER, comps INTEGER, deal_pct REAL, score INTEGER, reasons TEXT,
  alerted_score INTEGER,            -- score at last alert (re-alert on big improvement)
  starred INTEGER NOT NULL DEFAULT 0,
  hidden INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS listings_family ON listings(family, year);
CREATE INDEX IF NOT EXISTS listings_status ON listings(status);


CREATE TABLE IF NOT EXISTS price_history (
  listing_id TEXT NOT NULL, ts INTEGER NOT NULL, price INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ph_listing ON price_history(listing_id);

-- one row per delivered Telegram alert; cross-post suppression checks the full history
CREATE TABLE IF NOT EXISTS alert_log (
  listing_id TEXT NOT NULL, title_key TEXT NOT NULL, price INTEGER, kind TEXT NOT NULL, ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS alert_log_key ON alert_log(title_key, price);

CREATE TABLE IF NOT EXISTS geocache (place TEXT PRIMARY KEY, lat REAL, lon REAL);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started INTEGER, finished INTEGER, source TEXT, found INTEGER, new INTEGER,
  alerts INTEGER, errors TEXT
);
"""

def _default_rule(cat: str) -> dict:
    return {"enabled": True, "fresh": cat in ("utv4", "mower", "trailer"), "max_price": "", "min_year": ""}


DEFAULT_SETTINGS = {
    "home_label": "Plymouth, MN 55446",
    "home_zip": "55446",
    "home_lat": "45.0400",
    "home_lon": "-93.4900",
    "radius_mi": "100",
    "fb_location": "plymouth-mn",  # FB marketplace city slug; radius is applied around it
    "alert_threshold": "75",
    "alert_private_only": "1",
    "max_price": "",
    "min_year": "",
    "active_hours": "6-23",        # local hours the scanner runs
    # per category: {"utv4": {"enabled": true, "max_price": "", "min_year": ""}, ...}
    "alert_rules": json.dumps({c: _default_rule(c) for c in CATEGORIES}),
    "tow_capacity_lb": "12700",    # Jon's 2024 F-150 PowerBoost (max rating; see door-jamb sticker)
    "fresh_window_min": "120",     # "just listed" = posted within this many minutes
    "fresh_min_score": "50",       # ...and not overpriced / not red-flagged
    "seed_version": "1",
}

SEED_VERSION = 5   # bump when categories.py gains default searches

def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _add_columns(con, table, cols: dict):
    have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
    for name, decl in cols.items():
        if name not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init() -> None:
    con = connect()
    con.executescript(SCHEMA)
    _add_columns(con, "listings", {"is_new": "INTEGER", "category": "TEXT",
                                   "deck_in": "INTEGER", "engine": "TEXT"})
    _add_columns(con, "searches", {"category": "TEXT NOT NULL DEFAULT 'utv4'", "last_run": "INTEGER",
                                   "quick": "INTEGER NOT NULL DEFAULT 0"})
    _add_columns(con, "listings", {
        "fresh_alerted": "INTEGER", "detail_misses": "INTEGER NOT NULL DEFAULT 0",
        "alerted_price": "INTEGER",                              # price when the last alert went out
        "expected_base": "INTEGER", "usage_note": "TEXT",        # typical before any adjustment; adjustment notes
        "expected_pre": "INTEGER",                               # typical before equipment (learns equipment worth)
        "equipment": "TEXT",                                     # detected cab / heat / A-C / plow / trailer
        "offer_open": "INTEGER", "offer_aim": "INTEGER", "offer_walk": "INTEGER", "offer_notes": "TEXT",
        "offer_rough": "INTEGER",                                # offer from asking price only (few comps / new)
        "watch_price": "INTEGER", "watch_status": "TEXT",        # starred: last state Jon was told about
        "ended_at": "INTEGER", "end_price": "INTEGER",           # when it went sold/gone, and its last asking price
        "seen_active": "INTEGER NOT NULL DEFAULT 1",             # was ever seen for sale (sold-pull rows never were)
        "user_gone": "INTEGER NOT NULL DEFAULT 0",               # Jon pressed Gone: searches don't resurrect it
        "parse_attempts": "INTEGER NOT NULL DEFAULT 0",
        "notes": "TEXT",                                         # Jon's / Alex's own note on a listing (shared)
        "expected_sold": "INTEGER", "sold_comps": "INTEGER",     # "typically sells around" and what it's based on
        "sold_basis": "TEXT",                                    # 'sold' = sold listings of this family, 'est' = category ratio
        # trailers
        "trailer_type": "TEXT", "len_ft": "REAL", "width_ft": "REAL", "height_ft": "REAL",
        "axles": "INTEGER", "gvwr_lb": "INTEGER", "brakes": "INTEGER", "utv_fit": "TEXT",
        # jet skis / snowmobiles
        "units": "INTEGER",                                      # machines the one price buys (a pair of skis = 2)
        "track_in": "INTEGER", "cc": "INTEGER",                  # sled track length; engine displacement
    })
    con.execute("CREATE INDEX IF NOT EXISTS listings_category ON listings(category)")
    if con.execute("SELECT COUNT(*) FROM alert_log").fetchone()[0] == 0:
        # seed history from listings alerted before the log existed
        for r in con.execute("""SELECT id, title, COALESCE(alerted_price, price) p, first_seen FROM listings
                                WHERE alerted_score IS NOT NULL OR fresh_alerted = 1""").fetchall():
            con.execute("INSERT INTO alert_log VALUES (?, ?, ?, 'seed', ?)", (r["id"], title_key(r["title"]), r["p"], r["first_seen"]))
    for k, v in DEFAULT_SETTINGS.items():
        con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    seeded = int(con.execute("SELECT value FROM settings WHERE key='seed_version'").fetchone()[0])
    if seeded < SEED_VERSION or con.execute("SELECT COUNT(*) FROM searches").fetchone()[0] == 0:
        for cat, c in CATEGORIES.items():
            for q in c["searches"] + c.get("quick", []):
                con.execute("INSERT OR IGNORE INTO searches(query, category) VALUES (?, ?)", (q, cat))
            for q in c.get("quick", []):
                con.execute("UPDATE searches SET quick = 1 WHERE query = ?", (q,))
        con.execute("UPDATE settings SET value = ? WHERE key = 'seed_version'", (str(SEED_VERSION),))
    con.commit()
    con.close()


def prune(con) -> None:
    """Keep the DB from growing forever: old run rows, old alert history, old ended listings (never starred)."""
    cutoff = now() - 90 * 86400
    con.execute("DELETE FROM runs WHERE started < ?", (cutoff,))
    con.execute("DELETE FROM alert_log WHERE ts < ?", (cutoff,))
    old = [r[0] for r in con.execute("""SELECT id FROM listings WHERE status IN ('gone', 'sold') AND starred = 0
                                        AND COALESCE(ended_at, last_seen) < ?""", (now() - 200 * 86400,))]
    if old:
        con.executemany("DELETE FROM price_history WHERE listing_id = ?", [(i,) for i in old])
        con.executemany("DELETE FROM listings WHERE id = ?", [(i,) for i in old])
    con.commit()


def settings(con) -> dict:
    return {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM settings")}


def title_key(title: str | None) -> str:
    """Normalized title for spotting cross-posts ("2014 Arctic Cat 500 HDX!" == "2014 arctic cat 500 hdx")."""
    return re.sub(r"[^a-z0-9]", "", (title or "").lower())


def alert_rules(st: dict) -> dict:
    try:
        rules = json.loads(st.get("alert_rules") or "{}")
    except ValueError:
        rules = {}
    for c in CATEGORIES:
        rules.setdefault(c, _default_rule(c))
        rules[c].setdefault("fresh", _default_rule(c)["fresh"])
    return rules


def now() -> int:
    return int(time.time())


def row_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in ("extras", "red_flags", "reasons", "offer_notes"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d
