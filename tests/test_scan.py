"""Regression tests for the alert, detail and Craigslist fixes. Plain asserts, no pytest needed:

    DEALS_DB=/tmp/deals-test.db python -m tests.test_scan
"""
import asyncio
import json
import os
import tempfile

os.environ["DEALS_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")

from app import db, notify, scan  # noqa: E402
from app.sources import craigslist  # noqa: E402

SENT: list[str] = []
FAIL = {"on": False}


async def fake_send_listing(http, r, header=None):
    await asyncio.sleep(0.05)          # widen the window for the overlap test
    if FAIL["on"]:
        return False
    SENT.append(r["id"])
    return True


async def fake_send_text(http, text):
    return not FAIL["on"]

notify.send_listing = fake_send_listing
notify.send_text = fake_send_text


def reset(rows, **settings):
    db.init()
    con = db.connect()
    con.executescript("DELETE FROM listings; DELETE FROM settings; DELETE FROM alert_log;")   # every test starts clean
    con.commit()
    db.init()
    con.execute("DELETE FROM listings")
    for k, v in settings.items():
        con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (k, v))
    t = db.now()
    for i, r in enumerate(rows):
        base = dict(id=f"facebook:{i}", source="facebook", ext_id=str(i), url="u", title=f"t{i}", price=10000,
                    location=None, first_seen=t, last_seen=t, listed_at=t - 60, status="active", parsed=1,
                    relevant=1, category="utv4", family="RZR XP 4", year=2022, score=80, red_flags="[]",
                    is_dealer=0, is_new=0, reasons="[]")
        base.update(r)
        cols = ",".join(base)
        con.execute(f"INSERT INTO listings({cols}) VALUES ({','.join('?' * len(base))})", list(base.values()))
    con.commit()
    SENT.clear()
    FAIL["on"] = False
    return con


def rules(**per_cat):
    r = db.alert_rules({})
    for cat, v in per_cat.items():
        r[cat].update(v)
    return json.dumps(r)


async def alerts(con):
    return await scan.send_alerts(con, None, db.settings(con))


def test_failed_send_is_retried():
    con = reset([{}])
    FAIL["on"] = True
    asyncio.run(alerts(con))
    row = con.execute("SELECT alerted_score, fresh_alerted FROM listings").fetchone()
    assert row["alerted_score"] is None and row["fresh_alerted"] is None, dict(row)
    FAIL["on"] = False
    asyncio.run(alerts(con))
    assert SENT == ["facebook:0"], SENT
    assert con.execute("SELECT alerted_score FROM listings").fetchone()[0] == 80


def test_fresh_alerts_independent_of_deal_switch():
    con = reset([{"score": 55}], alert_rules=rules(utv4={"enabled": False, "fresh": True}))
    asyncio.run(alerts(con))
    assert SENT == ["facebook:0"], SENT


def test_fresh_skips_red_flags():
    con = reset([{"score": 70, "red_flags": json.dumps(["needs engine work"])}])
    asyncio.run(alerts(con))
    assert SENT == [], SENT


def test_overlapping_scans_alert_once():
    reset([{}, {"id": "facebook:9", "ext_id": "9", "score": 55}])

    async def both():
        await asyncio.gather(scan.send_alerts(db.connect(), None, db.settings(db.connect())),
                             scan.send_alerts(db.connect(), None, db.settings(db.connect())))
    asyncio.run(both())
    assert sorted(SENT) == ["facebook:0", "facebook:9"], SENT


def test_unreadable_detail_is_not_removal():
    con = reset([{"detail_fetched": 0}])
    scan.apply_detail(con, "facebook:0", None, 10000)
    scan.record_miss(con, "facebook:0")
    row = con.execute("SELECT status, detail_fetched, detail_misses FROM listings").fetchone()
    assert (row["status"], row["detail_fetched"], row["detail_misses"]) == ("active", 0, 1), dict(row)
    for _ in range(scan.MAX_DETAIL_MISSES - 1):
        scan.record_miss(con, "facebook:0")
    row = con.execute("SELECT status, detail_fetched FROM listings").fetchone()
    assert (row["status"], row["detail_fetched"]) == ("gone", 1), dict(row)
    scan.apply_detail(con, "facebook:0", {"status": "gone"}, 10000)   # explicit removal still works
    con.commit()


def test_craigslist_result_fields():
    page = '''<ol><li class="cl-static-search-result" title="2021 RZR XP 4">
        <a href="https://www.craigslist.org/view/d/x/abc123">
            <div class="title">2021 RZR XP 4</div>
            <div class="details"><div class="price">$12,000</div>
                <div class="location">
                    Plymouth
                </div></div></a></li>
      <li class="cl-static-search-result" title="No price">
        <a href="https://www.craigslist.org/view/d/y/def456"><div class="title">No price</div>
            <div class="details"><div class="location">Anoka</div></div></a></li></ol>'''
    got = []
    for m in craigslist.ITEM.finditer(page):
        b = m.group("body")
        p, loc = craigslist.PRICE.search(b), craigslist.LOC.search(b)
        got.append((craigslist._price(p.group(1)) if p else None, loc.group(1).strip() if loc else None))
    assert got == [(12000, "Plymouth"), (None, "Anoka")], got


def test_placeholder_price_never_alerts():
    from app import score
    row = {"price": 3, "is_new": 0, "strike_price": None, "first_price": 3, "motivated": 0, "year": 2021,
           "miles": None, "hours": None, "is_dealer": 0, "seller_type": None, "red_flags": "[]", "category": "utv4"}
    s, pct, reasons = score.score(row, 16740, 10)
    assert s < 50 and pct is None and "placeholder" in reasons[0], (s, reasons)


def test_cross_posts_alert_once():
    reset([{"title": "2014 Arctic cat 500 Hdx", "price": 4000},
           {"id": "craigslist:2", "source": "craigslist", "ext_id": "2", "title": "2014 Arctic Cat 500 HDX!", "price": 4000},
           {"id": "craigslist:3", "source": "craigslist", "ext_id": "3", "title": "2014 Arctic cat 500 Hdx", "price": 3500}])
    asyncio.run(alerts(db.connect()))
    assert len(SENT) == 2, SENT          # the $4,000 twin is skipped; the $3,500 repost is a real price change


def test_just_listed_then_deal_alert():
    con = reset([{"score": 55}])
    asyncio.run(alerts(con))
    assert SENT == ["facebook:0"], SENT                      # just-listed alert
    con.execute("UPDATE listings SET score = 85"); con.commit()
    asyncio.run(alerts(con))
    assert SENT == ["facebook:0", "facebook:0"], SENT        # later becomes a great deal -> deal alert


def test_price_drop_realerts():
    con = reset([{"score": 75, "price": 10000}])
    asyncio.run(alerts(con))
    con.execute("UPDATE listings SET price = 7000, score = 90"); con.commit()
    asyncio.run(alerts(con))
    assert SENT == ["facebook:0", "facebook:0"], SENT


def test_twin_blocked_even_after_its_own_alert_price_changes():
    # twin A alerted at $4,000; B is a cross-post at $4,000 -> skipped, even if A later drops to $3,500
    con = reset([{"title": "Arctic Cat HDX", "price": 4000},
                 {"id": "craigslist:2", "source": "craigslist", "ext_id": "2", "title": "Arctic Cat HDX", "price": 4000}])
    asyncio.run(alerts(con))
    assert SENT == ["facebook:0"], SENT
    con.execute("UPDATE listings SET price = 3500 WHERE id = 'facebook:0'"); con.commit()
    asyncio.run(alerts(con))
    assert SENT == ["facebook:0"], SENT


def test_twin_stays_blocked_after_original_realerts_at_new_price():
    # Codex's sequence: A alerts at $4,000 (twin B suppressed) -> A cuts to $3,500 and re-alerts
    # -> B, still at $4,000, must stay suppressed on the next scan
    con = reset([{"title": "Arctic Cat HDX", "price": 4000, "score": 80},
                 {"id": "craigslist:2", "source": "craigslist", "ext_id": "2", "title": "Arctic Cat HDX",
                  "price": 4000, "score": 80}])
    asyncio.run(alerts(con))
    assert SENT == ["facebook:0"], SENT
    con.execute("UPDATE listings SET price = 3500, score = 90 WHERE id = 'facebook:0'"); con.commit()
    asyncio.run(alerts(con))
    assert SENT == ["facebook:0", "facebook:0"], SENT      # price-cut re-alert goes out
    asyncio.run(alerts(con))
    assert SENT == ["facebook:0", "facebook:0"], SENT      # stale $4,000 twin still suppressed


def _family(con, n=12):
    """n used RZR XP 4 comps, 2021-2023, whose price falls with miles."""
    t = db.now()
    for i in range(n):
        year, miles = 2021 + i % 3, 1000 + 1000 * i
        price = int(16000 * 1.08 ** (year - 2022) * (1 - 0.025 * miles / 1000))
        con.execute("""INSERT INTO listings(id, source, ext_id, url, title, price, first_seen, last_seen, status,
                         parsed, relevant, category, family, year, miles, is_new, red_flags, reasons)
                       VALUES (?, 'facebook', ?, 'u', 'c', ?, ?, ?, 'active', 1, 1, 'utv4', 'RZR XP 4', ?, ?, 0, '[]', '[]')""",
                    (f"facebook:c{i}", f"c{i}", price, t, t, year, miles))
    con.commit()


def test_usage_moves_typical_the_right_way():
    from app import score
    con = reset([])
    _family(con)
    comps = score._comps(con)
    base_row = {"id": "x", "family": "RZR XP 4", "year": 2022, "category": "utv4", "deck_in": None}
    high = score.expected_price(dict(base_row, miles=15000, hours=None), comps)
    low = score.expected_price(dict(base_row, miles=500, hours=None), comps)
    none = score.expected_price(dict(base_row, miles=None, hours=None), comps)
    assert high[0] < high[2] and "more use" in high[3], high
    assert low[0] > low[2] and "less use" in low[3], low
    assert none[0] == none[2] and none[3] is None, none
    slope = score.usage_slope(comps["RZR XP 4"], "miles", "utv4")
    assert -0.00004 < slope < -0.00001, slope      # learned ~-2.5%/1000 mi, not something wild


def test_offer_numbers_are_ordered():
    from app import score
    con = reset([{"price": 15000, "motivated": 1}])
    r = con.execute("SELECT * FROM listings").fetchone()
    o = score.offer(r, 16000, (16000 - 15000) / 16000, 10)
    assert o["open"] <= o["aim"] <= o["walk"] <= 15000, o
    assert o["open"] >= 15000 * 0.75, o
    great = score.offer(r, 20000, 0.25, 10)            # 25% under typical: don't lowball
    assert great["open"] >= 15000 * 0.95 - 250 and "don't lowball" in " ".join(great["notes"]), great


def test_alert_text_includes_offer():
    con = reset([{"offer_open": 13000, "offer_aim": 13500, "offer_walk": 14500, "usage_note": "−$900 for use"}])
    cap = notify.listing_caption(con.execute("SELECT * FROM listings").fetchone())
    assert "Offer $13,000 · aim $13,500 · walk away above $14,500" in cap and "for use" in cap, cap


def test_trailer_utv_fit():
    from app.score import utv_fit
    t = lambda **k: dict({"category": "trailer", "len_ft": None, "width_ft": None, "height_ft": None,
                          "axles": None, "gvwr_lb": None, "trailer_type": "open"}, **k)
    assert utv_fit(t(len_ft=16, width_ft=7, axles=2)) == "yes"
    assert utv_fit(t(len_ft=16, width_ft=7, axles=2, trailer_type="enclosed", height_ft=7)) == "yes"
    assert utv_fit(t(len_ft=16, width_ft=7, axles=2, trailer_type="enclosed")) == "maybe"      # height unknown
    assert utv_fit(t(len_ft=12, width_ft=6.5, axles=1)) == "maybe"                               # short, single axle
    assert utv_fit(t(len_ft=10, width_ft=5)) == "no"
    assert utv_fit(t(len_ft=16, width_ft=7, axles=2, trailer_type="dump")) == "no"
    assert utv_fit(t()) == "unknown"
    assert utv_fit({"category": "utv4"}) is None


def test_trailer_alerts_gate_on_fit():
    base = {"category": "trailer", "family": "Open utility (rails / mesh sides)"}
    con = reset([dict(base, utv_fit="yes", score=80),
                 dict(base, id="facebook:1", ext_id="1", title="small", utv_fit="no", score=80),
                 dict(base, id="facebook:2", ext_id="2", title="steal", utv_fit="no", score=90),
                 dict(base, id="facebook:3", ext_id="3", title="fresh small", utv_fit="maybe", score=55)])
    asyncio.run(alerts(con))
    assert sorted(SENT) == ["facebook:0", "facebook:2"], SENT   # fits, or a small one that's an exceptional deal


def test_rough_offer_without_comps():
    from app import score
    con = reset([{"price": 13400}])
    r = con.execute("SELECT * FROM listings").fetchone()
    o = score.offer(r, None, None, 2)
    assert o and o["rough"] and o["walk"] == 13400 and o["open"] <= o["aim"] < 13400, o
    assert "standard private-sale numbers" in o["notes"][0], o["notes"][0]
    assert score.offer(dict(r) | {"price": 3}, None, None, 2) is None       # placeholder price


def test_equipment_detection():
    from app.equipment import detect
    d = lambda title, desc="", cat="utv4": detect({"category": cat, "title": title, "description": desc,
                                                   "extras": "[]", "summary": ""})
    assert d("2022 Ranger XP 1000 NorthStar Ultimate") == ["cab", "heat", "ac"]
    assert d("Defender MAX", "full cab with heat and AC, plow") == ["cab", "heat", "ac", "plow"]
    assert d("RZR XP 4", "comes with 7x14 tandem trailer") == ["trailer"]
    assert d("RZR XP 4", "trailer not included") == []
    assert d("AC Wildcat 4X") == [] and d("General 4", "heated grips") == []
    assert d("Sportsman 570", "with plow", cat="atv") == ["plow"] and d("Toro", cat="mower") == []


def test_equipment_moves_typical():
    import json as _j
    from app import score
    con = reset([])
    _family(con)
    con.execute("UPDATE listings SET equipment = ? WHERE id IN ('facebook:c0','facebook:c1','facebook:c2')",
                (_j.dumps(["cab", "heat"]),))
    con.execute("UPDATE listings SET equipment = '[]' WHERE equipment IS NULL"); con.commit()
    comps = score._comps(con)
    eff = {"utv4": {"cab": 0.08, "heat": 0.05, "ac": 0.03, "plow": 600, "trailer": 1500}}
    row = {"id": "x", "family": "RZR XP 4", "year": 2022, "category": "utv4", "deck_in": None, "miles": None, "hours": None}
    cab = score.expected_price(dict(row, equipment='["cab","heat"]'), comps, eff)
    bare = score.expected_price(dict(row, equipment="[]"), comps, eff)
    assert cab[0] > cab[4] and "has cab" in cab[3], cab
    assert bare[0] <= bare[4], bare
    plow = score.expected_price(dict(row, equipment='["plow"]'), comps, eff)
    assert 500 <= plow[0] - bare[0] <= 700, (plow, bare)   # fixed-dollar feature: ~$600 over the same machine without


def test_trailer_priced_per_foot_when_sizes_are_thin():
    from app import score
    con = reset([])
    t = db.now()
    for i, length in enumerate((10, 12, 14, 20, 24)):           # none within 2 ft of 17
        con.execute("""INSERT INTO listings(id, source, ext_id, url, title, price, first_seen, last_seen, status, parsed,
                         relevant, category, family, len_ft, axles, is_new, red_flags, reasons)
                       VALUES (?, 'facebook', ?, 'u', 't', ?, ?, ?, 'active', 1, 1, 'trailer', 'Enclosed cargo', ?, 2, 0, '[]', '[]')""",
                    (f"facebook:t{i}", f"t{i}", length * 400, t, t, length))
    con.commit()
    exp = score.expected_price({"id": "x", "family": "Enclosed cargo", "category": "trailer", "len_ft": 17, "axles": 2,
                                "year": None, "deck_in": None}, score._comps(con))
    assert exp[0] == 17 * 400 and "per foot" in exp[3], exp


def test_watch_alerts_price_drop_pending_and_retry():
    con = reset([{"starred": 1, "watch_price": 15000, "watch_status": "active", "price": 14000, "score": 40}])
    FAIL["on"] = True
    asyncio.run(alerts(con))
    assert SENT == [] and con.execute("SELECT watch_price FROM listings").fetchone()[0] == 15000   # retried later
    FAIL["on"] = False
    asyncio.run(alerts(con))
    assert SENT == ["facebook:0"] and con.execute("SELECT watch_price FROM listings").fetchone()[0] == 14000
    asyncio.run(alerts(con))
    assert SENT == ["facebook:0"], SENT                      # nothing new
    con.execute("UPDATE listings SET status = 'pending'"); con.commit()
    asyncio.run(alerts(con))
    assert SENT == ["facebook:0", "facebook:0"], SENT


def test_ended_tracking_and_days_to_sell():
    from app import score
    t = db.now()
    con = reset([{"id": f"facebook:e{i}", "ext_id": f"e{i}", "title": f"e{i}", "status": "gone",
                  "listed_at": t - (8 + i) * 86400, "price": 12000 + i} for i in range(5)] +
                [{"id": "facebook:live", "ext_id": "live", "status": "active"}])
    score.mark_ended(con)
    rows = con.execute("SELECT ended_at, end_price FROM listings WHERE status = 'gone'").fetchall()
    assert all(r["ended_at"] and r["end_price"] for r in rows), [dict(r) for r in rows]
    assert 9.5 < score.days_to_sell(con)["RZR XP 4"] < 10.5
    con.execute("UPDATE listings SET status = 'active' WHERE id = 'facebook:e0'"); con.commit()
    score.mark_ended(con)                                   # came back -> not ended any more
    assert con.execute("SELECT ended_at FROM listings WHERE id = 'facebook:e0'").fetchone()[0] is None


def test_appraise_endpoint():
    from fastapi.testclient import TestClient
    from app import web
    con = reset([])
    _family(con)
    body = {"category": "utv4", "family": "RZR XP 4", "year": "2022", "miles": "3000", "condition": "good"}
    a = TestClient(web.app).post("/api/appraise", json=body).json()
    assert a["typical"] and a["quick_sale"] < a["target"] < a["list_price"], a
    assert len(a["similar"]) == 8 and a["similar"][0]["year"] == 2022, a["similar"][0]
    worse = TestClient(web.app).post("/api/appraise", json=dict(body, condition="needs work")).json()
    assert worse["target"] < a["target"], (worse["target"], a["target"])


def test_appraise_rough_when_few_listings():
    from fastapi.testclient import TestClient
    from app import web
    con = reset([{"id": f"facebook:m{i}", "ext_id": f"m{i}", "category": "mower", "year": None,
                  "family": "Cub Cadet RZT S (steering wheel)", "price": p} for i, p in enumerate((1800, 1800, 1500, 2750))])
    a = TestClient(web.app).post("/api/appraise", json={"category": "mower", "family": "Cub Cadet RZT S (steering wheel)",
                                                        "hours": "274", "deck_in": "42", "condition": "good"}).json()
    assert a["rough"] and a["typical"] == 1800 and a["list_price"] > a["target"] > a["quick_sale"], a


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
