"""Dashboard + JSON API. uvicorn binds 127.0.0.1:8000 behind nginx.

Requests arriving through the Cloudflare tunnel (CF-Gateway, 10.10.10.5) must
carry a Cloudflare Access identity from ALLOWED_EMAILS; LAN requests are trusted.
"""
import asyncio
import ipaddress
import json
import re
import time
import os
import subprocess
from pathlib import Path

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import db, geo, score
from .categories import CATEGORIES, FAMILY_CATEGORY
from .equipment import APPLIES
from .parse import OLLAMA_MODEL, OLLAMA_URL

TUNNEL_IPS = set(os.environ.get("TUNNEL_IPS", "10.10.10.5").split(","))
LAN_NETS = [ipaddress.ip_network(n) for n in os.environ.get("LAN_NETS", "10.10.10.0/24,127.0.0.0/8").split(",")]


def _lan(ip: str) -> bool:
    try:
        return any(ipaddress.ip_address(ip) in n for n in LAN_NETS)
    except ValueError:
        return False
ALLOWED = {e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()}
STATIC = Path(__file__).parent / "static"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
db.init()


@app.middleware("http")
async def gate(request: Request, call_next):
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "")
    if ip in TUNNEL_IPS:
        email = (request.headers.get("cf-access-authenticated-user-email") or "").lower()
        if not email or email not in ALLOWED:
            return JSONResponse({"error": "forbidden"}, status_code=403)
    elif not _lan(ip):
        return JSONResponse({"error": "forbidden"}, status_code=403)      # default deny, not default allow
    if request.method != "GET" and request.headers.get("sec-fetch-site") == "cross-site":
        return JSONResponse({"error": "cross-site request"}, status_code=403)
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


def _geo(con):
    st = db.settings(con)
    try:
        hlat, hlon = float(st["home_lat"]), float(st["home_lon"])
    except (TypeError, ValueError):       # a bad Setup entry must not take the whole dashboard down
        hlat, hlon = float(db.DEFAULT_SETTINGS["home_lat"]), float(db.DEFAULT_SETTINGS["home_lon"])
    cache = {r["place"]: (r["lat"], r["lon"]) for r in con.execute("SELECT * FROM geocache")}

    def dist(location):
        c = cache.get(geo.place_key(location))
        return round(geo.miles(hlat, hlon, *c)) if c and c[0] is not None else None
    return dist


LIST_COLS = """id, source, category, deck_in, engine, url, title, price, first_price, strike_price, location, image, seller_type,
  listed_at, first_seen, last_seen, status, relevant, year, make, model, family, trim, seats, hours,
  miles, turbo, is_dealer, is_new, motivated, detail_misses, extras, red_flags, summary, expected, comps, deal_pct, score,
  reasons, starred, hidden, notes, expected_base, usage_note, offer_open, offer_aim, offer_walk, offer_notes, offer_rough, equipment, expected_sold, sold_comps, sold_basis,
  trailer_type, len_ft, width_ft, height_ft, axles, gvwr_lb, brakes, utv_fit,
  units, track_in, cc"""


@app.get("/api/listings")
def listings(include_gone: int = 0, include_irrelevant: int = 0):
    con = db.connect()
    dist = _geo(con)
    where = ["parsed = 1"]
    if not include_irrelevant:
        where.append("relevant = 1")
    if not include_gone:
        where.append("status IN ('active','pending')")
    rows = con.execute(f"SELECT {LIST_COLS} FROM listings WHERE {' AND '.join(where)}").fetchall()
    out = []
    for r in rows:
        d = db.row_dict(r)
        d["distance"] = dist(d["location"])
        d["dealer"] = bool(d["is_dealer"] == 1 or d["seller_type"] == "dealer")
        out.append(d)
    return out


@app.get("/api/search")
def search(q: str):
    """Listing ids whose ad text matches - the dashboard only holds titles/summaries, not descriptions."""
    q = q.strip()
    if len(q) < 2:
        return []
    con = db.connect()
    like = f"%{q}%"
    return [r["id"] for r in con.execute(
        """SELECT id FROM listings WHERE status IN ('active', 'pending') AND relevant = 1 AND (
             title LIKE ? OR description LIKE ? OR summary LIKE ? OR extras LIKE ? OR model LIKE ? OR notes LIKE ?)""",
        (like,) * 6)]


@app.get("/api/listing/{lid:path}/history")
def history(lid: str):
    con = db.connect()
    return [dict(r) for r in con.execute(
        "SELECT ts, price FROM price_history WHERE listing_id = ? ORDER BY ts", (lid,))]


@app.post("/api/listing/{lid:path}")
def mark(lid: str, body: dict = Body(...)):
    con = db.connect()
    if not con.execute("SELECT 1 FROM listings WHERE id = ?", (lid,)).fetchone():
        raise HTTPException(404, "no such listing")
    for k in ("starred", "hidden"):
        if k in body:
            con.execute(f"UPDATE listings SET {k} = ? WHERE id = ?", (1 if body[k] else 0, lid))
    if "notes" in body:
        note = str(body["notes"] or "").strip()[:2000]
        con.execute("UPDATE listings SET notes = ? WHERE id = ?", (note or None, lid))
    if body.get("gone"):     # "Gone" button: the listing is no longer up (Jon checked); searches won't bring it back
        con.execute("UPDATE listings SET status = 'gone', user_gone = 1, last_checked = ? WHERE id = ?", (db.now(), lid))
        score.mark_ended(con)
    if "starred" in body:   # watching starts from the current price/status
        con.execute("""UPDATE listings SET watch_price = CASE WHEN starred = 1 THEN price END,
                         watch_status = CASE WHEN starred = 1 THEN status END WHERE id = ?""", (lid,))
    con.commit()
    return {"ok": True}


@app.get("/api/market")
def market(family: str):
    """Asking prices by year for one family, plus the expected-price curve."""
    con = db.connect()
    rows = [dict(r) for r in con.execute(
        """SELECT id, title, year, price, status, is_dealer, seller_type, url, miles, hours, location,
                  deck_in, is_new
           FROM listings WHERE relevant = 1 AND family = ? AND price >= 300
           ORDER BY year""", (family,))]
    pts = [p for p in rows if p["year"]]
    undated = sorted(p["price"] for p in rows if not p["year"])
    comps, _ = _market_inputs(con)
    years = sorted({p["year"] for p in pts})
    curve = []
    if years:
        for y in range(years[0], years[-1] + 1):
            fake = {"id": "", "family": family, "year": y, "category": FAMILY_CATEGORY.get(family),
                    "deck_in": None}
            exp, n, *_ = score.expected_price(fake, comps)
            if exp:
                curve.append({"year": y, "price": exp})
    for p in pts:
        p["dealer"] = bool(p["is_dealer"] == 1 or p["seller_type"] == "dealer")
    return {"points": pts, "curve": curve,
            "undated": {"count": len(undated), "median": undated[len(undated) // 2] if undated else None}}


_MARKET_CACHE: dict = {}


def _market_inputs(con):
    """Comps + equipment effects, recomputed only after a scan has finished (they scan the whole table)."""
    key = tuple(con.execute("SELECT (SELECT MAX(id) FROM runs WHERE finished IS NOT NULL), COUNT(*), MAX(rowid), "
                            "SUM(parsed) FROM listings").fetchone())
    if "comps" not in _MARKET_CACHE or _MARKET_CACHE["key"] != key:
        _MARKET_CACHE.update(key=key, comps=score._comps(con), effects=score.equipment_effects(con))
    return _MARKET_CACHE["comps"], _MARKET_CACHE["effects"]


@app.get("/api/trends")
def trends(category: str):
    """Weekly market pulse for a category: supply, pricing vs typical, and how fast things go."""
    con = db.connect()
    wk = "strftime('%Y-%W', {col}, 'unixepoch', 'localtime')"
    new = {r["w"]: dict(r) for r in con.execute(
        f"""SELECT {wk.format(col='COALESCE(listed_at, first_seen)')} w, COUNT(*) n, MIN(COALESCE(listed_at, first_seen)) t
            FROM listings WHERE relevant = 1 AND category = ? AND COALESCE(is_new, 0) = 0
              AND COALESCE(listed_at, first_seen) > ? GROUP BY w""", (category, db.now() - 120 * 86400))}
    pct: dict[str, list] = {}
    for r in con.execute(
            f"""SELECT {wk.format(col='COALESCE(listed_at, first_seen)')} w, deal_pct FROM listings
                WHERE relevant = 1 AND category = ? AND COALESCE(is_new, 0) = 0 AND deal_pct IS NOT NULL
                  AND COALESCE(listed_at, first_seen) > ?""", (category, db.now() - 120 * 86400)):
        pct.setdefault(r["w"], []).append(r["deal_pct"])
    sold_vs: dict[str, list] = {}
    for r in con.execute(
            f"""SELECT {wk.format(col='ended_at')} w, COALESCE(end_price, price) * 1.0 / expected q FROM listings
                WHERE relevant = 1 AND category = ? AND status = 'sold' AND COALESCE(is_new, 0) = 0
                  AND COALESCE(expected, 0) > 0 AND ended_at > ?""", (category, db.now() - 120 * 86400)):
        if 0.4 <= r["q"] <= 1.6:
            sold_vs.setdefault(r["w"], []).append(r["q"])
    ended: dict[str, list] = {}
    for r in con.execute(
            f"""SELECT {wk.format(col='ended_at')} w, ended_at - COALESCE(listed_at, first_seen) secs FROM listings
                WHERE relevant = 1 AND category = ? AND COALESCE(is_new, 0) = 0 AND ended_at > ?""",
            (category, db.now() - 120 * 86400)):
        ended.setdefault(r["w"], []).append(max(0, r["secs"] or 0) / 86400)
    import statistics as st
    out = []
    for w in sorted(set(new) | set(ended)):
        p = pct.get(w, [])
        e = ended.get(w, [])
        out.append({"week": w, "start": new.get(w, {}).get("t"), "new": new.get(w, {}).get("n", 0),
                    "vs_typical": round(-st.median(p) * 100, 1) if len(p) >= 3 else None,   # + = asking above typical
                    "priced": len(p), "ended": len(e), "days_listed": round(st.median(e), 1) if e else None,
                    "sold_vs_typical": round((st.median(sold_vs[w]) - 1) * 100, 1) if len(sold_vs.get(w, [])) >= 3 else None})
    return out


@app.get("/api/ended")
def ended(family: str):
    """Recently sold or removed listings of one family - last asking price is the closest thing to a sold price."""
    con = db.connect()
    return [dict(r) for r in con.execute(
        """SELECT title, url, source, year, miles, hours, deck_in, len_ft, width_ft, first_price, end_price, status,
                  ended_at, (ended_at - COALESCE(listed_at, first_seen)) / 86400 days
           FROM listings WHERE family = ? AND ended_at IS NOT NULL AND relevant = 1 AND COALESCE(is_new, 0) = 0
           ORDER BY ended_at DESC LIMIT 40""", (family,))]


CONDITION = {"excellent": 1.05, "good": 1.0, "fair": 0.9, "needs work": 0.75}


def _appraisal(body: dict) -> dict:
    con = db.connect()
    cat = body.get("category")
    if cat not in CATEGORIES:
        raise HTTPException(400, "unknown category")
    def num(k):
        v = str(body.get(k) or "").strip()
        if not v:
            return None
        try:
            return float(v)
        except ValueError:
            raise HTTPException(400, f"{k} must be a number")
    me = {"id": "", "category": cat, "family": body.get("family"), "year": int(num("year")) if num("year") else None,
          "miles": int(num("miles")) if num("miles") else None, "hours": int(num("hours")) if num("hours") else None,
          "deck_in": int(num("deck_in")) if num("deck_in") else None, "len_ft": num("len_ft"),
          "axles": int(num("axles")) if num("axles") else None,
          "equipment": json.dumps([f for f in (body.get("equipment") or []) if isinstance(f, str)])}
    comps, effects = _market_inputs(con)
    exp, n, base, note, pre = score.expected_price(me, comps, effects)
    cond = CONDITION.get(body.get("condition") or "good", 1.0)
    pace = score.days_to_sell(con).get(me["family"])
    # closest comparable listings (same family): nearest year, then use, then size
    rows = con.execute("""SELECT title, url, source, year, miles, hours, deck_in, len_ft, price, status, is_dealer,
                                 seller_type, location FROM listings
                          WHERE family = ? AND relevant = 1 AND COALESCE(is_new, 0) = 0 AND price >= 100
                            AND last_seen > ?""", (me["family"], db.now() - 180 * 86400)).fetchall()

    def distance(r):
        d = abs((r["year"] or 0) - (me["year"] or r["year"] or 0)) * 2 if me["year"] else 0
        for k, unit in (("miles", 2000), ("hours", 150)):
            if me[k] is not None and r[k] is not None:
                d += abs(r[k] - me[k]) / unit
        if me["deck_in"] and r["deck_in"]:
            d += abs(r["deck_in"] - me["deck_in"]) / 6
        if me["len_ft"] and r["len_ft"]:
            d += abs(r["len_ft"] - me["len_ft"]) / 2
        return d
    near = sorted(rows, key=distance)[:8]
    result = {"typical": exp, "comps": n, "note": note, "condition": body.get("condition") or "good",
              "days_to_sell": pace, "similar": [dict(r) for r in near]}
    rough = False
    if not exp:
        # too few for a proper typical: rough price from the closest few listings, labeled as such
        priced = [r["price"] for r in near[:5] if r["price"]]
        if len(priced) >= 2:
            import statistics as st
            exp, rough = int(st.median(priced)), True
    result["rough"] = rough
    if exp:
        result["typical"] = exp
        target = exp * cond
        result.update(list_price=score._nice(target * 1.06 + 99), target=score._nice(target),
                      quick_sale=score._nice(target * 0.9))
    return result


@app.post("/api/appraise")
def appraise(body: dict = Body(...)):
    return _appraisal(body)


_DRAFT_LOCK = asyncio.Semaphore(1)


@app.post("/api/appraise/draft")
async def appraise_draft(body: dict = Body(...)):
    """Local model writes a for-sale ad from the seller's details (only when asked - it takes a few seconds)."""
    a = _appraisal(body)
    facts = {k: v for k, v in body.items() if v not in (None, "", [])}
    price = a.get("list_price")
    prompt = (
        "Write a Facebook Marketplace / Craigslist for-sale ad for a private seller in Minnesota. Plain, honest, "
        "friendly; no emojis, no hype words like 'beast' or 'must see'. Structure: a title line (year make model "
        "and the key selling point), then 3-6 short lines of details (use, maintenance, equipment, condition "
        "including any flaws they mentioned), then pickup/payment terms (cash, local pickup, serious buyers). "
        "Only use facts given; do not invent service history, features, or a model year - if no year is given, "
        "do not mention one.\n\n"
        f"Details: {json.dumps(facts)}\n"
        + (f"Asking price: ${price:,}\n" if price else "") +
        "Return just the ad text.")
    async with _DRAFT_LOCK, httpx.AsyncClient() as http:
        r = await http.post(f"{OLLAMA_URL}/api/generate", json={
            "model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "think": False,
            "keep_alive": "30m", "options": {"temperature": 0.2}}, timeout=110)
    r.raise_for_status()
    text = r.json().get("response", "").strip()
    if not facts.get("year"):   # belt and braces: never let the model invent a model year
        text = re.sub(r"\b(19[6-9]\d|20[0-3]\d)\s+(?=[A-Z])", "", text)
    return {"text": text, "list_price": price}


@app.get("/api/families")
def families():
    con = db.connect()
    counts = {r["family"]: r["n"] for r in con.execute(
        "SELECT family, COUNT(*) n FROM listings WHERE relevant = 1 AND family IS NOT NULL GROUP BY family")}
    return [{"family": f, "category": c, "count": counts[f]}
            for c, cf in CATEGORIES.items() for f in cf["families"] if counts.get(f)]


@app.get("/api/categories")
def categories():
    return [{"key": k, "label": c["label"], "emoji": c["emoji"], "families": c["families"],
             "equipment": list(APPLIES.get(k, ()))} for k, c in CATEGORIES.items()]


@app.get("/api/settings")
def get_settings():
    con = db.connect()
    st = db.settings(con)
    return {"settings": st, "alert_rules": db.alert_rules(st),
            "searches": [dict(r) for r in con.execute("SELECT * FROM searches ORDER BY category, id")]}


# key -> (min, max, blank allowed)
NUMERIC = {"radius_mi": (5, 500, False), "alert_threshold": (0, 100, False), "tow_capacity_lb": (0, 40000, True),
           "fresh_window_min": (5, 1440, False), "fresh_min_score": (0, 100, False),
           "home_lat": (-90, 90, False), "home_lon": (-180, 180, False), "home_zip": (501, 99950, False)}
EDITABLE = {"tow_capacity_lb", "radius_mi", "alert_threshold", "alert_private_only", "alert_rules", "fresh_window_min", "fresh_min_score",
            "active_hours", "home_zip", "home_lat", "home_lon", "home_label", "fb_location"}


@app.put("/api/settings")
def put_settings(body: dict = Body(...)):
    con = db.connect()
    for k, v in body.items():
        if k not in EDITABLE:
            raise HTTPException(400, f"unknown setting {k}")
        if k in NUMERIC:
            lo, hi, blank_ok = NUMERIC[k]
            sv = str(v).strip()
            if not sv and blank_ok:
                v = ""
            else:
                try:
                    f = float(sv)
                except ValueError:
                    raise HTTPException(400, f"{k} must be a number")
                if not lo <= f <= hi:
                    raise HTTPException(400, f"{k} must be between {lo} and {hi}")
                v = str(int(f)) if k not in ("home_lat", "home_lon") else sv
        if k == "active_hours" and not re.fullmatch(r"\d{1,2}-\d{1,2}", str(v).strip()):
            raise HTTPException(400, "active_hours looks like 6-23")
        if k == "alert_rules":
            if not isinstance(v, dict) or not all(isinstance(r, dict) for r in v.values()):
                raise HTTPException(400, "alert_rules must be an object of objects")
            for r in v.values():
                for f in ("max_price", "min_year"):
                    if str(r.get(f) or "").strip() and not str(r.get(f)).strip().lstrip("-").replace(".", "", 1).isdigit():
                        raise HTTPException(400, f"{f} must be a number")
            v = json.dumps({c: {"enabled": bool(r.get("enabled")), "fresh": bool(r.get("fresh")),
                                "max_price": str(r.get("max_price") or ""),
                                "min_year": str(r.get("min_year") or "")}
                            for c, r in v.items() if c in CATEGORIES})
        con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (k, str(v).strip()))
    con.commit()
    return get_settings()


@app.post("/api/searches")
def add_search(body: dict = Body(...)):
    q = (body.get("query") or "").strip().lower()
    if not q:
        raise HTTPException(400, "query required")
    con = db.connect()
    cat = body.get("category") if body.get("category") in CATEGORIES else "utv4"
    con.execute("INSERT OR IGNORE INTO searches(query, category) VALUES (?, ?)", (q, cat))
    con.commit()
    return get_settings()


@app.patch("/api/searches/{sid}")
def toggle_search(sid: int, body: dict = Body(...)):
    con = db.connect()
    for k in ("enabled", "quick"):
        if k in body:
            con.execute(f"UPDATE searches SET {k} = ? WHERE id = ?", (1 if body[k] else 0, sid))
    con.commit()
    return get_settings()


@app.delete("/api/searches/{sid}")
def del_search(sid: int):
    con = db.connect()
    con.execute("DELETE FROM searches WHERE id = ?", (sid,))
    con.commit()
    return get_settings()


@app.get("/api/status")
def status():
    con = db.connect()
    runs = [dict(r) for r in con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 8")]
    for r in runs:
        r["errors"] = json.loads(r["errors"]) if r["errors"] else []
    c = con.execute("""SELECT COUNT(*) total,
                         SUM(relevant = 1 AND status IN ('active','pending')) live,
                         SUM(parsed = 0) unparsed FROM listings""").fetchone()
    running = _scanning()
    return {"runs": runs, "counts": dict(c), "scanning": running}


_SCAN_STATE = {"t": 0.0, "v": False}


def _scanning() -> bool:
    if time.time() - _SCAN_STATE["t"] > 5:      # several tabs poll this; one systemctl call per 5 s is plenty
        states = subprocess.run(["systemctl", "is-active", "deals-scan.service", "deals-scan-now.service"],
                                capture_output=True, text=True).stdout.split()
        _SCAN_STATE.update(t=time.time(), v="activating" in states)
    return _SCAN_STATE["v"]


@app.post("/api/scan")
def scan_now():
    subprocess.run(["systemctl", "start", "--no-block", "deals-scan-now.service"], check=False)
    return {"ok": True}
