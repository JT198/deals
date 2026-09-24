"""SQLite storage. One file, WAL mode; the scanner and the web app share it."""
import json
import os
import sqlite3
import time

DB_PATH = os.environ.get("DEALS_DB", "/opt/deals/data/deals.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS searches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  query TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 1
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
  relevant INTEGER,                 -- 1 = a 4+ seat UTV actually for sale
  year INTEGER, make TEXT, model TEXT, family TEXT, trim TEXT,
  seats INTEGER, hours INTEGER, miles INTEGER, turbo INTEGER,
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

CREATE TABLE IF NOT EXISTS geocache (place TEXT PRIMARY KEY, lat REAL, lon REAL);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started INTEGER, finished INTEGER, source TEXT, found INTEGER, new INTEGER,
  alerts INTEGER, errors TEXT
);
"""

DEFAULT_SETTINGS = {
    "home_label": "Plymouth, MN",
    "home_zip": "55447",
    "home_lat": "45.0105",
    "home_lon": "-93.4555",
    "radius_mi": "100",
    "fb_location": "minneapolis",   # FB marketplace city slug; radius is applied around it
    "alert_threshold": "75",
    "alert_private_only": "1",
    "max_price": "",
    "min_year": "",
    "active_hours": "6-23",        # local hours the scanner runs
}

DEFAULT_SEARCHES = [
    "rzr xp 4", "rzr 4 seater", "rzr pro xp 4", "ranger crew", "ranger xp 1000 crew",
    "can am defender max", "can am maverick max", "can am commander max",
    "honda pioneer 1000-5", "kawasaki teryx4", "teryx krx4", "polaris general 4",
    "yamaha wolverine x4", "4 seat side by side", "crew utv",
]


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init() -> None:
    con = connect()
    con.executescript(SCHEMA)
    have = {r["name"] for r in con.execute("PRAGMA table_info(listings)")}
    if "is_new" not in have:
        con.execute("ALTER TABLE listings ADD COLUMN is_new INTEGER")
    for k, v in DEFAULT_SETTINGS.items():
        con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    if con.execute("SELECT COUNT(*) FROM searches").fetchone()[0] == 0:
        con.executemany("INSERT INTO searches(query) VALUES (?)", [(q,) for q in DEFAULT_SEARCHES])
    con.commit()
    con.close()


def settings(con) -> dict:
    return {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM settings")}


def now() -> int:
    return int(time.time())


def row_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in ("extras", "red_flags", "reasons"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d
