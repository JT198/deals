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


def client():
    """API test client that looks like a LAN browser (the gate default-denies unknown sources)."""
    from fastapi.testclient import TestClient
    from app import web
    return TestClient(web.app, headers={"x-real-ip": "10.10.10.50"})


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
    con = reset([{"detail_fetched": 1}])      # a listing we had read before, now failing to load
    scan.apply_detail(con, "facebook:0", None, 10000)
    scan.record_miss(con, "facebook:0")
    row = con.execute("SELECT status, detail_fetched, detail_misses FROM listings").fetchone()
    assert (row["status"], row["detail_fetched"], row["detail_misses"]) == ("active", 1, 1), dict(row)
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
    a = client().post("/api/appraise", json=body).json()
    assert a["typical"] and a["quick_sale"] < a["target"] < a["list_price"], a
    assert len(a["similar"]) == 8 and a["similar"][0]["year"] == 2022, a["similar"][0]
    worse = client().post("/api/appraise", json=dict(body, condition="needs work")).json()
    assert worse["target"] < a["target"], (worse["target"], a["target"])


def test_appraise_rough_when_few_listings():
    from fastapi.testclient import TestClient
    from app import web
    con = reset([{"id": f"facebook:m{i}", "ext_id": f"m{i}", "category": "mower", "year": None,
                  "family": "Cub Cadet RZT S (steering wheel)", "price": p} for i, p in enumerate((1800, 1800, 1500, 2750))])
    a = client().post("/api/appraise", json={"category": "mower", "family": "Cub Cadet RZT S (steering wheel)",
                                                        "hours": "274", "deck_in": "42", "condition": "good"}).json()
    assert a["rough"] and a["typical"] == 1800 and a["list_price"] > a["target"] > a["quick_sale"], a


def test_sold_search_url():
    from app.sources.facebook import search_url
    assert "availability=out%20of%20stock" in search_url("plymouth-mn", "ranger crew", 100, sold=True)
    assert "availability" not in search_url("plymouth-mn", "ranger crew", 100)


def test_typically_sells_around_from_sold_listings():
    from app import score
    con = reset([])
    _family(con)                                                        # 12 asking comps
    t = db.now()
    for i in range(6):                                                  # 6 sold ones, ~10% under asking
        year, miles = 2021 + i % 3, 2000 + 1000 * i
        price = int(0.9 * 16000 * 1.08 ** (year - 2022) * (1 - 0.025 * miles / 1000))
        con.execute("""INSERT INTO listings(id, source, ext_id, url, title, price, end_price, ended_at, first_seen, last_seen,
                         status, parsed, relevant, category, family, year, miles, is_new, red_flags, reasons)
                       VALUES (?, 'facebook', ?, 'u', 's', ?, ?, ?, ?, ?, 'sold', 1, 1, 'utv4', 'RZR XP 4', ?, ?, 0, '[]', '[]')""",
                    (f"facebook:s{i}", f"s{i}", price, price, t, t, t, year, miles))
    con.execute("""INSERT INTO listings(id, source, ext_id, url, title, price, first_seen, last_seen, status, parsed, relevant,
                     category, family, year, miles, is_new, red_flags, reasons, listed_at)
                   VALUES ('facebook:me', 'facebook', 'me', 'u', 'me', 15500, ?, ?, 'active', 1, 1, 'utv4', 'RZR XP 4', 2022, 5000,
                           0, '[]', '[]', ?)""", (t, t, t - 5 * 86400))
    con.commit()
    score.rescore_all(con)
    r = con.execute("SELECT * FROM listings WHERE id = 'facebook:me'").fetchone()
    assert r["sold_basis"] == "sold" and r["sold_comps"] >= 4, dict(r)
    assert 0.85 * r["expected"] < r["expected_sold"] < 0.95 * r["expected"], (r["expected"], r["expected_sold"])
    assert r["offer_walk"] <= r["expected_sold"] and "sold for about" in r["offer_notes"], r["offer_notes"]


def test_sold_typical_is_bounded_and_skips_red_flags():
    from app import score
    con = reset([])
    _family(con)
    t = db.now()
    for i in range(6):   # "sold" junk: half price, several with red flags
        con.execute("""INSERT INTO listings(id, source, ext_id, url, title, price, end_price, ended_at, first_seen, last_seen,
                         status, parsed, relevant, category, family, year, is_new, red_flags, reasons)
                       VALUES (?, 'facebook', ?, 'u', 'j', 7000, 7000, ?, ?, ?, 'sold', 1, 1, 'utv4', 'RZR XP 4', 2022, 0, ?, '[]')""",
                    (f"facebook:j{i}", f"j{i}", t, t, t, '["doesn\'t run"]' if i < 3 else "[]"))
    con.commit()
    assert len(score._comps(con, sold=True)["RZR XP 4"]) == 3            # red-flagged sold ones left out
    con.execute("""INSERT INTO listings(id, source, ext_id, url, title, price, first_seen, last_seen, status, parsed, relevant,
                     category, family, year, is_new, red_flags, reasons) VALUES ('facebook:me', 'facebook', 'me', 'u', 'me',
                     15000, ?, ?, 'active', 1, 1, 'utv4', 'RZR XP 4', 2022, 0, '[]', '[]')""", (t, t))
    # a 4th clean sold one at half price: enough to count, still far under asking
    con.execute("INSERT INTO listings(id, source, ext_id, url, title, price, end_price, ended_at, first_seen, last_seen, status, parsed, relevant, category, family, year, is_new, red_flags, reasons) VALUES ('facebook:j9','facebook','j9','u','j',7000,7000,?,?,?,'sold',1,1,'utv4','RZR XP 4',2022,0,'[]','[]')", (t, t, t))
    con.commit()
    score.rescore_all(con)
    r = con.execute("SELECT expected, expected_sold FROM listings WHERE id = 'facebook:me'").fetchone()
    assert r["expected_sold"] >= 0.75 * r["expected"] - 1, dict(r)       # clamped, not 45% under


def test_sold_at_asking_means_holding_not_a_second_number():
    from app import score
    con = reset([])
    _family(con)
    t = db.now()
    for i in range(6):   # sold at the same prices things are asking
        year, miles = 2021 + i % 3, 2000 + 1000 * i
        price = int(16000 * 1.08 ** (year - 2022) * (1 - 0.025 * miles / 1000))
        con.execute("""INSERT INTO listings(id, source, ext_id, url, title, price, end_price, ended_at, first_seen, last_seen,
                         status, parsed, relevant, category, family, year, miles, is_new, red_flags, reasons)
                       VALUES (?, 'facebook', ?, 'u', 's', ?, ?, ?, ?, ?, 'sold', 1, 1, 'utv4', 'RZR XP 4', ?, ?, 0, '[]', '[]')""",
                    (f"facebook:h{i}", f"h{i}", price, price, t, t, t, year, miles))
    con.execute("""INSERT INTO listings(id, source, ext_id, url, title, price, first_seen, last_seen, status, parsed, relevant,
                     category, family, year, miles, is_new, red_flags, reasons) VALUES ('facebook:me', 'facebook', 'me', 'u',
                     'me', 15500, ?, ?, 'active', 1, 1, 'utv4', 'RZR XP 4', 2022, 5000, 0, '[]', '[]')""", (t, t))
    con.commit()
    score.rescore_all(con)
    r = con.execute("SELECT * FROM listings WHERE id = 'facebook:me'").fetchone()
    assert r["sold_basis"] == "holding" and r["expected_sold"] is None, dict(r)
    assert "selling at about asking" in r["offer_notes"], r["offer_notes"]


def test_sold_estimate_from_category_ratio():
    from app import score
    con = reset([{"id": f"facebook:q{i}", "ext_id": f"q{i}", "status": "sold", "family": f"F{i}",
                  "price": 8000, "end_price": 8000, "expected": 10000, "ended_at": db.now()} for i in range(8)])
    assert score.sold_ratios(con)["utv4"] == (0.8, 8)


def test_recheck_order_suspects_first_then_hot():
    t = db.now()
    base = {"detail_fetched": 1, "last_checked": t - 10 * 3600}
    con = reset([dict(base, id="facebook:plain", ext_id="plain", score=40),
                 dict(base, id="facebook:hot", ext_id="hot", score=85),
                 dict(base, id="facebook:fresh-hot", ext_id="fresh-hot", score=90, last_checked=t - 600),
                 dict(base, id="facebook:suspect", ext_id="suspect", score=30, detail_misses=1)])
    ids = [r["id"] for r in scan.recheck_candidates(con)]
    assert ids[0] == "facebook:suspect" and ids[1] == "facebook:hot", ids
    assert ids.index("facebook:fresh-hot") > ids.index("facebook:plain") or "facebook:fresh-hot" not in ids[:2], ids


def test_gone_button():
    from fastapi.testclient import TestClient
    from app import web
    con = reset([{}])
    client().post("/api/listing/facebook:0", json={"gone": True})
    r = db.connect().execute("SELECT status, ended_at FROM listings").fetchone()
    assert r["status"] == "gone" and r["ended_at"], dict(r)


def test_parse_coerces_odd_llm_output():
    from app import parse as P
    class R:   # fake httpx response
        def __init__(self, body): self._b = body
        def raise_for_status(self): pass
        def json(self): return {"response": self._b}
    class H:
        def __init__(self, body): self.body = body
        async def post(self, *a, **k): return R(self.body)
    row = {"source": "facebook", "title": "t", "price": 1, "location": None, "seller_type": None, "description": ""}
    odd = json.dumps({"category": "utv4", "relevant": True, "family": "RZR XP 4", "trim": ["Premium", "EPS"],
                      "model": 2022, "extras": "winch", "red_flags": None, "summary": ["a", "b"]})
    p = asyncio.run(P.parse(H(odd), row))
    assert p["trim"] == "Premium, EPS" and p["model"] == "2022" and p["extras"] == '["winch"]' and p["summary"] == "a, b", p
    assert asyncio.run(P.parse(H("null"), row)) is None and asyncio.run(P.parse(H('"str"'), row)) is None


def test_search_hit_resets_misses_and_user_gone_sticks():
    con = reset([{"detail_fetched": 1, "detail_misses": 2, "status": "gone"},
                 {"id": "facebook:1", "ext_id": "1", "status": "gone", "user_gone": 1}])
    item = {"ext_id": "0", "url": "u", "title": "t0", "price": 10000, "status": "active"}
    assert scan.upsert(con, "facebook", item) is False
    r = con.execute("SELECT status, detail_misses FROM listings WHERE id='facebook:0'").fetchone()
    assert (r["status"], r["detail_misses"]) == ("active", 0), dict(r)
    scan.upsert(con, "facebook", dict(item, ext_id="1", title="t1"))
    assert con.execute("SELECT status FROM listings WHERE id='facebook:1'").fetchone()[0] == "gone"
    con.commit()


def test_never_fetched_listing_is_not_marked_gone_by_misses():
    con = reset([{"detail_fetched": 0}])
    for _ in range(scan.MAX_DETAIL_MISSES):
        scan.record_miss(con, "facebook:0")
    r = con.execute("SELECT status, detail_fetched FROM listings").fetchone()
    assert (r["status"], r["detail_fetched"]) == ("active", 1), dict(r)     # parse from the title, still live
    con.commit()


def test_caption_survives_null_price_and_long_text():
    con = reset([{"price": None, "summary": "x" * 2000, "reasons": json.dumps(["y" * 500])}])
    cap = notify.listing_caption(con.execute("SELECT * FROM listings").fetchone())
    assert "no price listed" in cap and len(cap) <= 1024 and cap.rstrip().endswith("Dashboard</a>"), (len(cap), cap[-80:])


def test_days_to_sell_ignores_sold_pull_rows():
    from app import score
    t = db.now()
    rows = [{"id": f"facebook:a{i}", "ext_id": f"a{i}", "status": "gone", "listed_at": t - 10 * 86400,
             "ended_at": t, "end_price": 1, "seen_active": 1} for i in range(5)]
    rows += [{"id": f"facebook:s{i}", "ext_id": f"s{i}", "status": "sold", "listed_at": t - 90 * 86400,
              "ended_at": t, "end_price": 1, "seen_active": 0} for i in range(5)]
    con = reset(rows)
    assert 9.5 < score.days_to_sell(con)["RZR XP 4"] < 10.5


def test_settings_validation():
    from fastapi.testclient import TestClient
    from app import web
    reset([])
    c = client()
    assert c.put("/api/settings", json={"home_lat": "45.04 -93.49"}).status_code == 400
    assert c.put("/api/settings", json={"radius_mi": ""}).status_code == 400
    assert c.put("/api/settings", json={"alert_rules": "x"}).status_code == 400
    assert c.put("/api/settings", json={"radius_mi": "150", "home_lat": "45.1", "home_lon": "-93.5"}).status_code == 200
    assert c.post("/api/appraise", json={"category": "utv4", "family": "RZR XP 4", "year": "abc"}).status_code == 400


def test_gate_default_deny_and_csrf():
    from fastapi.testclient import TestClient
    from app import web
    reset([])
    from fastapi.testclient import TestClient
    from app import web
    c = TestClient(web.app)
    assert c.get("/api/status", headers={"x-real-ip": "8.8.8.8"}).status_code == 403
    assert c.get("/api/status", headers={"x-real-ip": "10.10.10.50"}).status_code == 200
    assert c.post("/api/scan", headers={"x-real-ip": "10.10.10.50", "sec-fetch-site": "cross-site"}).status_code == 403


def test_notes_and_search():
    con = reset([{"description": "comes with a Warn winch and Boss plow", "summary": "clean unit"},
                 {"id": "facebook:1", "ext_id": "1", "title": "other", "description": "nothing special"}])
    c = client()
    assert c.post("/api/listing/facebook:1", json={"notes": "  messaged seller  "}).status_code == 200
    assert db.connect().execute("SELECT notes FROM listings WHERE id='facebook:1'").fetchone()[0] == "messaged seller"
    assert c.get("/api/search?q=boss plow").json() == ["facebook:0"]
    assert c.get("/api/search?q=messaged").json() == ["facebook:1"]
    assert c.get("/api/search?q=x").json() == []                       # too short
    assert c.post("/api/listing/facebook:1", json={"notes": ""}).status_code == 200
    assert db.connect().execute("SELECT notes FROM listings WHERE id='facebook:1'").fetchone()[0] is None


def test_digest_builds():
    from app import digest
    t = db.now()
    con = reset([{"score": 82, "price": 12000, "expected": 16000, "first_seen": t - 3600, "location": "Anoka, MN"},
                 {"id": "facebook:w", "ext_id": "w", "starred": 1, "price": 9000, "first_price": 10000, "status": "pending"},
                 {"id": "facebook:d", "ext_id": "d", "score": 90, "price": 5000, "first_seen": t - 3600, "is_dealer": 1}])
    text = digest.build(con)
    assert "4-seat UTVs" in text and "$12,000" in text and "typical $16,000" in text, text
    assert "Watching (1)" in text and "(was $10,000)" in text and "PENDING" in text, text
    assert "facebook:d" not in text and "$5,000" not in text            # dealers don't make the digest


def _insert(con, rows):
    t = db.now()
    for i, r in enumerate(rows):
        base = dict(id=f"facebook:k{i}", source="facebook", ext_id=f"k{i}", url="u", title="c", first_seen=t, last_seen=t,
                    status="active", parsed=1, relevant=1, is_new=0, red_flags="[]", reasons="[]")
        base.update(r)
        con.execute(f"INSERT INTO listings({','.join(base)}) VALUES ({','.join('?' * len(base))})", list(base.values()))
    con.commit()


def test_jet_ski_pair_priced_per_ski_with_one_trailer():
    from app import score
    con = reset([])
    fam = "Sea-Doo GTI/GTS"
    _insert(con, [dict(category="pwc", family=fam, year=2021, price=9000, units=1) for _ in range(5)]
            + [dict(category="pwc", family=fam, year=2021, price=18000, units=2)])       # a pair: $9,000 each
    comps = score._comps(con)
    assert all(c.price == 9000 for c in comps[fam]), comps
    eff = {"pwc": {"trailer": 900}}
    row = {"id": "x", "family": fam, "year": 2021, "category": "pwc", "hours": None, "miles": None}
    pair = score.expected_price(dict(row, units=2, equipment='["trailer"]'), comps, eff)
    assert pair[0] == 2 * 9000 + 900 and "2 machines" in pair[3], pair
    one = score.expected_price(dict(row, units=1, equipment='["trailer"]'), comps, eff)
    assert one[0] == 9900, one


def test_sleds_compared_on_track_length():
    from app import score
    con = reset([])
    fam = "Polaris Indy/Switchback/Rush (trail/crossover)"
    _insert(con, [dict(category="sled", family=fam, year=2020, price=8000, track_in=129) for _ in range(4)]
            + [dict(category="sled", family=fam, year=2020, price=12000, track_in=146) for _ in range(4)])
    comps = score._comps(con)
    row = {"id": "x", "family": fam, "year": 2020, "category": "sled", "miles": None, "hours": None, "equipment": "[]"}
    assert score.expected_price(dict(row, track_in=129), comps)[0] == 8000
    assert score.expected_price(dict(row, track_in=146), comps)[0] == 12000


def test_parse_pwc_and_sled_fields():
    from app import parse as P
    class R:
        def __init__(self, body): self._b = body
        def raise_for_status(self): pass
        def json(self): return {"response": self._b}
    class H:
        def __init__(self, body): self.body = body
        async def post(self, *a, **k): return R(self.body)
    row = {"source": "facebook", "title": "t", "price": 1, "location": None, "seller_type": None, "description": ""}
    sled = asyncio.run(P.parse(H(json.dumps({"category": "sled", "relevant": True, "family": "Ski-Doo Summit/Freeride (mountain)",
                                              "track_in": "154", "cc": 850, "units": 1})), row))
    assert sled["category"] == "sled" and sled["track_in"] == 154 and sled["cc"] == 850 and sled["units"] == 1, sled
    pwc = asyncio.run(P.parse(H(json.dumps({"category": "pwc", "relevant": True, "family": "Yamaha VX",
                                             "units": 2, "track_in": 137})), row))
    assert pwc["units"] == 2 and pwc["track_in"] is None, pwc


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
