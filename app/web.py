"""Dashboard + JSON API. uvicorn binds 127.0.0.1:8000 behind nginx.

Requests arriving through the Cloudflare tunnel (CF-Gateway, 10.10.10.5) must
carry a Cloudflare Access identity from ALLOWED_EMAILS; LAN requests are trusted.
"""
import json
import os
import subprocess
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import db, geo, score
from .categories import CATEGORIES, FAMILY_CATEGORY

TUNNEL_IPS = set(os.environ.get("TUNNEL_IPS", "10.10.10.5").split(","))
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
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


def _geo(con):
    st = db.settings(con)
    hlat, hlon = float(st["home_lat"]), float(st["home_lon"])
    cache = {r["place"]: (r["lat"], r["lon"]) for r in con.execute("SELECT * FROM geocache")}

    def dist(location):
        c = cache.get(geo.place_key(location))
        return round(geo.miles(hlat, hlon, *c)) if c and c[0] is not None else None
    return dist


LIST_COLS = """id, source, category, deck_in, engine, url, title, price, first_price, strike_price, location, image, seller_type,
  listed_at, first_seen, last_seen, status, relevant, year, make, model, family, trim, seats, hours,
  miles, turbo, is_dealer, is_new, motivated, extras, red_flags, summary, expected, comps, deal_pct, score,
  reasons, starred, hidden"""


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


@app.get("/api/listing/{lid:path}/history")
def history(lid: str):
    con = db.connect()
    return [dict(r) for r in con.execute(
        "SELECT ts, price FROM price_history WHERE listing_id = ? ORDER BY ts", (lid,))]


@app.post("/api/listing/{lid:path}")
def mark(lid: str, body: dict = Body(...)):
    con = db.connect()
    for k in ("starred", "hidden"):
        if k in body:
            con.execute(f"UPDATE listings SET {k} = ? WHERE id = ?", (1 if body[k] else 0, lid))
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
    comps = score._comps(con)
    years = sorted({p["year"] for p in pts})
    curve = []
    if years:
        for y in range(years[0], years[-1] + 1):
            fake = {"id": "", "family": family, "year": y, "category": FAMILY_CATEGORY.get(family),
                    "deck_in": None}
            exp, n = score.expected_price(fake, comps)
            if exp:
                curve.append({"year": y, "price": exp})
    for p in pts:
        p["dealer"] = bool(p["is_dealer"] == 1 or p["seller_type"] == "dealer")
    return {"points": pts, "curve": curve,
            "undated": {"count": len(undated), "median": undated[len(undated) // 2] if undated else None}}


@app.get("/api/families")
def families():
    con = db.connect()
    counts = {r["family"]: r["n"] for r in con.execute(
        "SELECT family, COUNT(*) n FROM listings WHERE relevant = 1 AND family IS NOT NULL GROUP BY family")}
    return [{"family": f, "category": c, "count": counts[f]}
            for c, cf in CATEGORIES.items() for f in cf["families"] if counts.get(f)]


@app.get("/api/categories")
def categories():
    return [{"key": k, "label": c["label"], "emoji": c["emoji"]} for k, c in CATEGORIES.items()]


@app.get("/api/settings")
def get_settings():
    con = db.connect()
    st = db.settings(con)
    return {"settings": st, "alert_rules": db.alert_rules(st),
            "searches": [dict(r) for r in con.execute("SELECT * FROM searches ORDER BY category, id")]}


EDITABLE = {"radius_mi", "alert_threshold", "alert_private_only", "alert_rules",
            "active_hours", "home_zip", "home_lat", "home_lon", "home_label", "fb_location"}


@app.put("/api/settings")
def put_settings(body: dict = Body(...)):
    con = db.connect()
    for k, v in body.items():
        if k not in EDITABLE:
            raise HTTPException(400, f"unknown setting {k}")
        if k == "alert_rules":
            v = json.dumps({c: {"enabled": bool(r.get("enabled")), "max_price": str(r.get("max_price") or ""),
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
    con.execute("UPDATE searches SET enabled = ? WHERE id = ?", (1 if body.get("enabled") else 0, sid))
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
    states = subprocess.run(["systemctl", "is-active", "deals-scan.service", "deals-scan-now.service"],
                            capture_output=True, text=True).stdout.split()
    running = "activating" in states
    return {"runs": runs, "counts": dict(c), "scanning": running}


@app.post("/api/scan")
def scan_now():
    subprocess.run(["systemctl", "start", "--no-block", "deals-scan-now.service"], check=False)
    return {"ok": True}
