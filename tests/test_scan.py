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
    con.executescript("DELETE FROM listings; DELETE FROM settings; DELETE FROM alert_log; DELETE FROM scorecard_log; "
                      "DELETE FROM alert_activity; DELETE FROM corrections;")   # every test starts clean
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
    # twin A alerted at $4,000; B is a cross-post at $4,000 -> skipped, even when A later drops to $3,500
    # (A's 12.5% cut re-alerts A itself; B at the old price stays a twin of the original alert)
    con = reset([{"title": "Arctic Cat HDX", "price": 4000},
                 {"id": "craigslist:2", "source": "craigslist", "ext_id": "2", "title": "Arctic Cat HDX", "price": 4000}])
    asyncio.run(alerts(con))
    assert SENT == ["facebook:0"], SENT
    con.execute("UPDATE listings SET price = 3500 WHERE id = 'facebook:0'"); con.commit()
    asyncio.run(alerts(con))
    assert SENT == ["facebook:0", "facebook:0"], SENT


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
                       VALUES (?, 'facebook', ?, 'u', ?, 7000, 7000, ?, ?, ?, 'sold', 1, 1, 'utv4', 'RZR XP 4', 2022, 0, ?, '[]')""",
                    (f"facebook:j{i}", f"j{i}", f"sold one {i}", t, t, t, '["doesn\'t run"]' if i < 3 else "[]"))
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
        base = dict(id=f"facebook:k{i}", source="facebook", ext_id=f"k{i}", url="u", title=f"c{i}", first_seen=t, last_seen=t,
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


def test_scorecard_buckets_and_snapshot():
    from app import score
    t = db.now()
    old = {"first_seen": t - 5 * 86400, "listed_at": t - 10 * 86400}
    rows = []
    for i in range(30):   # great: 15 of 30 gone; overpriced: 3 of 30 gone
        rows.append(dict(old, id=f"facebook:g{i}", ext_id=f"g{i}", score=80, status="gone" if i < 15 else "active",
                         ended_at=t if i < 15 else None))
        rows.append(dict(old, id=f"facebook:o{i}", ext_id=f"o{i}", score=30, status="sold" if i < 3 else "active",
                         ended_at=t if i < 3 else None))
    rows.append(dict(old, id="facebook:dealer", ext_id="dealer", score=90, is_dealer=1, status="gone", ended_at=t))
    rows.append({"id": "facebook:fresh", "ext_id": "fresh", "score": 90, "status": "gone", "ended_at": t})   # < 2 days up
    rows.append(dict(old, id="facebook:backlog", ext_id="backlog", score=90, status="sold", seen_active=0, ended_at=t))
    con = reset(rows)
    s = score.scorecard(con)
    top, bottom = s["buckets"][0], s["buckets"][-1]
    assert (top["n"], top["ended"], top["pct"], top["days"]) == (30, 15, 50.0, 10), top
    assert (bottom["n"], bottom["ended"], bottom["pct"]) == (30, 3, 10.0), bottom
    assert s["verdict"].startswith("Working") and "5.0x" in s["verdict"], s["verdict"]
    score.snapshot_scorecard(con); score.snapshot_scorecard(con)          # same day: one row
    assert con.execute("SELECT COUNT(*) FROM scorecard_log").fetchone()[0] == 1
    api = client().get("/api/scorecard").json()
    assert api["history"][0]["buckets"][0]["pct"] == 50.0 and api["listings"] == 60, api["history"]


def test_usage_doubt():
    from app import usage
    u = lambda desc, **k: usage.doubt(dict({"title": "t", "description": desc, "category": "utv4", "year": 2022,
                                            "is_new": 0, "miles": None, "hours": None}, **k))
    assert "since a repair" in u("brand new primary and secondary clutch replaced 60 miles ago", miles=60)
    assert "since a repair" in u("6500 miles on rebuilt motor. $7500 obo", miles=6500, year=2016)
    assert "since a repair" in u("800 miles on new top end", miles=800, category="sled", year=1998)
    assert u("pristine condition with only 36 hours on the engine", hours=36, category="pwc", year=2024) is None
    assert u("has 4,200 miles, clutch replaced 60 miles ago", miles=4200) is None          # total is stated
    assert u("only 60 miles, basically new", miles=60) is None                              # clearly stated
    assert "unusually low" in u("great machine", miles=60)                                  # 2022 with 60 mi, unexplained
    assert u("great machine", miles=60, year=2026) is None and u("nice", miles=3000) is None
    assert "60 miles since a repair" in u("clutch replaced 60 miles ago")                   # nothing recorded, still flagged
    assert u("runs great") is None and u("clutch replaced 60 miles ago", category="mower") is None


def test_doubtful_mileage_does_not_move_the_price():
    from app import score
    con = reset([])
    _family(con)
    t = db.now()
    for lid, desc in (("facebook:doubt", "clutch replaced 60 miles ago"), ("facebook:real", "only 60 miles on it")):
        con.execute("""INSERT INTO listings(id, source, ext_id, url, title, description, price, first_seen, last_seen, status,
                         parsed, relevant, category, family, year, miles, is_new, red_flags, reasons)
                       VALUES (?, 'facebook', ?, 'u', 't', ?, 15000, ?, ?, 'active', 1, 1, 'utv4', 'RZR XP 4', 2022, 60,
                               0, '[]', '[]')""", (lid, lid, desc, t, t))
    con.commit()
    score.rescore_all(con)
    d = con.execute("SELECT * FROM listings WHERE id = 'facebook:doubt'").fetchone()
    r = con.execute("SELECT * FROM listings WHERE id = 'facebook:real'").fetchone()
    assert d["usage_doubt"] and "for use" not in (d["usage_note"] or ""), dict(d)
    assert "Ask for the actual miles" in d["offer_notes"], d["offer_notes"]
    assert r["usage_doubt"] is None and "for use" in r["usage_note"] and r["expected"] > d["expected"], (r["expected"], d["expected"])


def test_distance_prefers_the_map_pin():
    from app import geo
    assert craigslist.coords('<div id="map" data-latitude="45.035486" data-longitude="-93.781070" data-accuracy="20">') == (45.035486, -93.78107)
    assert craigslist.coords('<div data-latitude="0" data-longitude="0">') == (None, None) and craigslist.coords("x") == (None, None)
    home, cache = (45.04, -93.49), {"Anoka, MN": (45.1977, -93.3872), "JUNK DEALER, MN": (None, None)}
    assert 10 < geo.distance({"lat": None, "lon": None, "location": "Anoka"}, home, cache) < 14
    assert 12 < geo.distance({"lat": 45.035486, "lon": -93.78107, "location": "JUNK DEALER"}, home, cache) < 16
    assert geo.distance({"lat": None, "lon": None, "location": "JUNK DEALER"}, home, cache) is None
    con = reset([{"location": None, "lat": 45.035486, "lon": -93.78107}])
    assert 12 < client().get("/api/listings").json()[0]["distance"] < 16


def _trailer(con, lid, price, **k):
    t = db.now()
    row = dict(id=lid, source="facebook", ext_id=lid, url="u", title=lid, price=price, first_seen=t, last_seen=t,
               status="active", parsed=1, relevant=1, category="trailer", family="Equipment / deckover", len_ft=20,
               width_ft=6.83, axles=2, gvwr_lb=10000, is_new=0, red_flags="[]", reasons="[]", year=2026)
    row.update(k)
    con.execute(f"INSERT INTO listings({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))


def test_trailer_new_price_ceiling_and_weight_class():
    from app import score
    con = reset([])
    for i, p in enumerate((9500, 9800, 10200, 9900, 10500, 9700)):          # 14K wide-bodies: a different trailer
        _trailer(con, f"big{i}", p, gvwr_lb=14000, width_ft=8.5, len_ft=20 + i % 2)
    for i in range(4):                                                       # new 10K deckovers at a dealer
        _trailer(con, f"new{i}", 7099, is_new=1, is_dealer=1, width_ft=8.5)
    _trailer(con, "newheavy", 9995, is_new=1, is_dealer=1, width_ft=8.5, gvwr_lb=None)   # unlabeled: ignored for the ceiling
    _trailer(con, "new10k", 8900, is_new=1, is_dealer=1, width_ft=8.5)                  # pricier 10K: low end still wins
    _trailer(con, "mine", 5500, miles=500, listed_at=db.now() - 4 * 86400)
    _trailer(con, "newmoney", 6800, listed_at=db.now() - 4 * 86400)
    con.commit()
    score.rescore_all(con)
    r = con.execute("SELECT * FROM listings WHERE id = 'mine'").fetchone()
    assert r["new_price"] == 7099 and r["expected"] == int(7099 * 0.85), dict(r)        # not the 14K per-foot price
    assert r["score"] < 65 and "low use" not in r["reasons"], (r["score"], r["reasons"])  # no odometer bonus on a trailer
    assert "New ones like this list around $7,099" in r["offer_notes"], r["offer_notes"]
    n = con.execute("SELECT * FROM listings WHERE id = 'newmoney'").fetchone()
    assert n["score"] <= 50 and "price of a new one" in n["reasons"], (n["score"], n["reasons"])
    assert score.gvwr_class(7000) == 1 and score.gvwr_class(9990) == 2 and score.gvwr_class(14000) == 3
    # a cheaper new trailer of the same size and class filed under another open-deck type still sets the ceiling
    _trailer(con, "newutil1", 4795, is_new=1, is_dealer=1, family="Open utility (rails / mesh sides)", len_ft=18)
    _trailer(con, "newutil2", 4995, is_new=1, is_dealer=1, family="Tilt / car hauler flatbed", len_ft=18)
    con.commit()
    score.rescore_all(con)
    r = con.execute("SELECT * FROM listings WHERE id = 'mine'").fetchone()
    assert r["new_price"] in (4795, 4995) and r["score"] <= 50 and "price of a new one" in r["reasons"], dict(r)


def test_facebook_backoff_ladder():
    con = reset([])
    st = db.settings(con)
    msg = scan.fb_backoff(con, st, walled=True, fb_found=0)
    st = db.settings(con)
    assert msg.startswith("pausing Facebook via home IP for 2 h") and st["fb_backoff_level:home"] == "1"
    assert int(st["fb_backoff_until:home"]) - db.now() > 7000
    scan.fb_backoff(con, st, walled=True, fb_found=0); st = db.settings(con)
    assert st["fb_backoff_level:home"] == "2" and int(st["fb_backoff_until:home"]) - db.now() > 14000     # 4 h
    scan.fb_backoff(con, st, walled=False, fb_found=12); st = db.settings(con)
    assert st["fb_backoff_level:home"] == "0"                                                             # good run resets
    assert scan.fb_backoff(con, st, walled=False, fb_found=0) is None


def test_facebook_routes_and_proxy():
    from app.sources.facebook import proxy_config
    from app import web
    assert proxy_config("http://jon:s3cret@proxy.torguard.org:6060") == {"server": "http://proxy.torguard.org:6060", "username": "jon", "password": "s3cret"}
    assert proxy_config("") is None and proxy_config("socks5://h:1080") == {"server": "socks5://h:1080"}
    try:
        proxy_config("ftp://x"); assert False
    except ValueError:
        pass
    assert web._mask_proxy("http://jon:s3cret@h:1") == "http://jon:********@h:1" and web._mask_proxy("http://h:1") == "http://h:1"
    now = db.now()
    assert scan.fb_routes({"fb_route": "auto", "fb_proxy": ""}) == ["home"]
    assert scan.fb_routes({"fb_route": "auto", "fb_proxy": "http://u:p@h:1"}) == ["home", "proxy"]
    assert scan.fb_routes({"fb_route": "proxy", "fb_proxy": ""}) == ["home"]              # nothing configured: fall back
    st = {"fb_route": "auto", "fb_proxy": "http://u:p@h:1", "fb_backoff_until:home": str(now + 3600)}
    assert scan.fb_pick_route(st, now) == ("proxy", 0)                                     # home blocked -> proxy
    st["fb_backoff_until:proxy"] = str(now + 7200)
    assert scan.fb_pick_route(st, now) == (None, now + 3600)                               # both blocked -> wait for soonest
    con = reset([])
    c = client()
    assert c.put("/api/settings", json={"fb_proxy": "nonsense"}).status_code == 400
    assert c.put("/api/settings", json={"fb_proxy": "http://jon:s3cret@h:1", "fb_route": "auto"}).status_code == 200
    assert c.get("/api/settings").json()["settings"]["fb_proxy"] == "http://jon:********@h:1"
    c.put("/api/settings", json={"fb_proxy": "http://jon:********@h:1"})                 # masked value echoed back: unchanged
    assert db.settings(db.connect())["fb_proxy"] == "http://jon:s3cret@h:1"
    msg = scan.fb_backoff(con, db.settings(db.connect()), walled=True, fb_found=0, route="home")
    assert "via home IP" in msg and "switching to the VPN proxy" in msg, msg


def test_deal_alerts_need_enough_savings():
    old = {"listed_at": db.now() - 3 * 3600}       # past the just-listed window, so only the deal path can send
    con = reset([dict(old, score=85, price=2800, expected=3400),                          # 18% / $600 under: not worth a flip
                 dict(old, id="facebook:1", ext_id="1", title="big", score=85, price=12000, expected=16500),  # 27% / $4,500
                 dict(old, id="facebook:2", ext_id="2", title="sold", score=85, price=12000, expected=16500,
                      expected_sold=13000)],                                                # sells for 13k: only $1,000 real savings
                alert_min_pct="20", alert_min_usd="1500")
    asyncio.run(alerts(con))
    assert SENT == ["facebook:1"], SENT
    con = reset([dict(old, score=85, price=2800, expected=3400)])                           # floors off: the old behaviour
    asyncio.run(alerts(con))
    assert SENT == ["facebook:0"], SENT


def test_listings_carry_map_points():
    con = reset([{"lat": 45.1, "lon": -93.4, "location": "Anoka, MN"},
                 {"id": "facebook:1", "ext_id": "1", "location": "Anoka, MN"},
                 {"id": "facebook:2", "ext_id": "2", "location": "Nowhere, MN"}])
    con.execute("INSERT OR REPLACE INTO geocache VALUES ('Anoka, MN', 45.1977, -93.3872)"); con.commit()
    resp = client().get("/api/listings")
    rows = {d["id"]: d for d in resp.json()}
    assert (rows["facebook:0"]["lat"], rows["facebook:0"]["approx"]) == (45.1, False)       # own pin
    assert (rows["facebook:1"]["lat"], rows["facebook:1"]["approx"]) == (45.1977, True)     # town
    assert rows["facebook:2"].get("lat") is None                 # no point: field left out (the page reads missing as null)
    assert resp.headers["X-Home"].startswith("45.")


def test_facebook_lock_is_exclusive():
    async def go():
        a = await scan.acquire_fb_lock(1)
        b = await scan.acquire_fb_lock(1)      # held by a -> None after the wait
        a.close()
        c = await scan.acquire_fb_lock(1)
        return a is not None, b, c is not None
    assert asyncio.run(go()) == (True, None, True)


def test_thin_same_year_comps_follow_the_year_trend():
    """Jon 2026-10-04: a 2022 Maverick X3 MAX got a lower typical than a 2020 because its 7 near comps were
    mostly cheap 2023s, while the 2020 (only 3 near comps) was priced off the family's year trend."""
    from app import score
    comps = [(2020, 25000), (2021, 17000), (2021, 22499), (2022, 25500), (2023, 18700), (2023, 18995),
             (2023, 21000), (2023, 24900), (2024, 25000), (2024, 31000), (2025, 29000), (2025, 33000), (2019, 16500)]
    con = reset([{"id": f"facebook:m{i}", "ext_id": f"m{i}", "family": "Maverick X3 MAX", "year": y, "price": p}
                 for i, (y, p) in enumerate(comps)])
    c = score._comps(con)
    row = {"id": "x", "family": "Maverick X3 MAX", "category": "utv4", "deck_in": None, "miles": None, "hours": None,
           "equipment": "[]"}
    t2020 = score.expected_price(dict(row, year=2020), c)
    t2022 = score.expected_price(dict(row, year=2022), c)
    assert t2022[0] >= t2020[0], (t2020, t2022)
    assert "price-by-year trend" in (t2022[3] or ""), t2022
    # vintage / trailer / "Other" families never lean on a trend
    con.execute("UPDATE listings SET family = 'Other 4-seat UTV'"); con.commit()
    other = score.expected_price(dict(row, family="Other 4-seat UTV", year=2022), score._comps(con))
    assert "trend" not in (other[3] or ""), other


def test_trim_levels():
    from app.equipment import trim_level
    t = lambda fam, title, desc="", trim=None: trim_level({"family": fam, "title": title, "description": desc, "trim": trim})
    assert t("Maverick X3 MAX", "2021 Can-Am Maverick X3 MAX X RS Turbo RR") == "top_trim"
    assert t("Maverick X3 MAX", "2022 Maverick X3 Max", "DS for sale, 2100 miles") == "base_trim"
    assert t("Maverick X3 (2-seat)", "2023 Maverick X3 DS Turbo RR") is None          # RR: the middle
    assert t("Maverick X3 MAX", "2022 Maverick X3 Max", "x" * 400 + " upgraded to x ds wheels") is None   # too deep
    assert t("RZR Pro XP 4", "Polaris RZR Pro XP 4", "Valley Power and Sport, Rochester") is None
    assert t("RZR Pro XP 4", "2024 Polaris RZR Pro XP 4 Sport") == "base_trim"
    assert t("RZR Pro XP 4", "2024 Polaris pro xp 4", trim="Sport") == "base_trim"
    assert t("Commander MAX", "2022 Commander MAX XT-P 1000R") == "top_trim"
    assert t("Commander MAX", "2024 Commander MAX XT 1000R") == "base_trim"
    assert t("Ranger Crew 1000", "Ranger Crew NorthStar Ultimate") is None            # cab editions = equipment


def test_trim_moves_typical():
    from app import score
    con = reset([])
    _family(con)
    con.execute("UPDATE listings SET equipment = '[]'"); con.commit()
    comps = score._comps(con)
    eff = {"utv4": {"cab": 0.08, "heat": 0.05, "ac": 0.03, "plow": 600, "trailer": 1500, "base_trim": -0.08, "top_trim": 0.06}}
    row = {"id": "x", "family": "RZR XP 4", "year": 2022, "category": "utv4", "deck_in": None, "miles": None, "hours": None}
    mid = score.expected_price(dict(row, equipment="[]"), comps, eff)
    low = score.expected_price(dict(row, equipment='["base_trim"]'), comps, eff)
    top = score.expected_price(dict(row, equipment='["top_trim"]'), comps, eff)
    assert low[0] < mid[0] < top[0], (low, mid, top)
    assert "lower trim" in low[3] and "top trim" in top[3], (low[3], top[3])


def test_price_band_search_url_and_splits():
    from app import sweep
    from app.sources.facebook import search_url
    u = search_url("plymouth-mn", "jet ski", 100, price=(2000, 3499))
    assert "&minPrice=2000&maxPrice=3499" in u and "minPrice" not in search_url("plymouth-mn", "jet ski", 100)
    bands = sweep.default_bands("pwc")
    assert bands[0] == (500, 1999) and bands[-1][1] == 29999
    assert all(bands[i][1] + 1 == bands[i + 1][0] for i in range(len(bands) - 1))          # no gaps, no overlap
    assert sweep.split(2000, 3499) == ((2000, 2750), (2751, 3499))
    assert sweep.split(2000, 2150) is None                                                 # too narrow to split


def test_sweep_finds_old_listings_quietly_and_splits_full_bands():
    from app import sweep
    con = reset([], active_hours="0-24")
    con.execute("DELETE FROM sweep_queue")
    con.execute("UPDATE searches SET enabled = 0 WHERE query != 'jet ski'")
    con.commit()
    now = db.now()
    calls = []

    def item(i, price, age_h):
        return {"ext_id": str(i), "url": "u", "title": f"ski {i}", "price": price, "location": "Hudson, WI",
                "listed_at": now - age_h * 3600, "status": "active"}

    class FakeFB:
        wall = False

        def __init__(self, pw, proxy=None, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def search(self, q, loc, radius, sort="newest", scrolls=None, sold=False, price=None):
            calls.append((q, price))
            if FakeFB.wall or price is None:
                return [] if FakeFB.wall else [item(999, 1, 1)]
            if price in ((500, 1999), (500, 1250)):        # a full page of listings 40 days old
                return [item(100 + i, 900, 24 * 40) for i in range(15)]
            if price == (2000, 3499):
                return [item(1, 3000, 24 * 40), item(2, 2500, 2)]   # one 40 days old, one posted 2 h ago
            return []

    class FakePW:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    async def no_pause():
        pass

    sweep.Facebook, sweep.async_playwright, sweep.pause = FakeFB, lambda: FakePW(), no_pause
    asyncio.run(sweep.run())
    st = sweep.status(con)
    ran = sweep.DAY_SEARCHES                                                   # one of them was full and split in two
    assert st["round"] == 1 and st["searches"] == 8 + 2 and st["todo"] == st["searches"] - ran, st
    assert calls[:2] == [("jet ski", (500, 1999)), ("jet ski", (2000, 3499))], calls
    rows = {r["ext_id"]: r["backlog"] for r in con.execute("SELECT ext_id, backlog FROM listings")}
    assert rows["1"] == 1 and rows["2"] == 0 and rows["100"] == 1, rows      # old = backlog; 2 h old = a normal find
    halves = [(r["lo"], r["hi"]) for r in con.execute("SELECT lo, hi FROM sweep_queue WHERE lo < 2000 AND state = 'todo'")]
    assert halves == [(500, 1250), (1251, 1999)], halves

    # an old find scoring like a great deal is recorded, not sent; a later score jump alerts as usual
    con.execute("""UPDATE listings SET parsed = 1, relevant = 1, category = 'pwc', score = 90, is_dealer = 0,
                     red_flags = '[]', reasons = '[]' WHERE ext_id IN ('1', '2')""")
    con.commit()
    asyncio.run(alerts(con))
    assert SENT == ["facebook:2"], SENT
    assert con.execute("SELECT alerted_score FROM listings WHERE ext_id = '1'").fetchone()[0] == 90
    con.execute("UPDATE listings SET score = 100, price = 2700 WHERE ext_id = '1'")   # a 10% price cut: alerts after all
    con.commit()
    SENT.clear()
    asyncio.run(alerts(con))
    assert SENT == ["facebook:1"], SENT

    # Facebook answering with nothing: the bands go back on the list and Facebook is paused
    before = sweep.status(con)["todo"]
    FakeFB.wall = True
    asyncio.run(sweep.run())
    assert sweep.status(con)["todo"] == before
    assert int(db.settings(con)["fb_backoff_until:home"]) > db.now()
    FakeFB.wall = False

    # next round starts from the bands the last one ended with, not the defaults
    con.execute("DELETE FROM settings WHERE key LIKE 'fb_backoff%'")
    con.execute("UPDATE sweep_queue SET state = 'done' WHERE state = 'todo'")
    con.commit()
    calls.clear()
    asyncio.run(sweep.run())
    st = sweep.status(con)
    assert st["round"] == 2 and st["searches"] == 9, st
    assert calls[:2] == [("jet ski", (500, 1250)), ("jet ski", (1251, 1999))], calls
    # a full page of listings we already have is not worth splitting again
    assert con.execute("SELECT state FROM sweep_queue WHERE lo = 500").fetchone()[0] == "done"
    # "not seen lately" waits for a full sweep round: 5 days normally, longer after a slow round
    assert scan.stale_after({}) == 5 * 86400
    assert scan.stale_after({"sweep_round_secs": str(6 * 86400)}) == 9 * 86400
    assert scan.stale_after({"sweep_round_started": str(db.now() - 30 * 86400)}) == 14 * 86400
    assert con.execute("SELECT backlog FROM listings WHERE ext_id = '100'").fetchone()[0] == 1   # flag is kept
    assert con.execute("SELECT COUNT(*) FROM sweep_queue WHERE round = 1").fetchone()[0] == 0
    con.execute("UPDATE searches SET enabled = 1")
    con.commit()


def test_description_arriving_late_gets_the_ad_read_again():
    con = reset([{"parsed": 1, "description": None, "detail_fetched": 0},
                 {"id": "facebook:1", "ext_id": "1", "parsed": 1, "description": "already read", "detail_fetched": 1},
                 {"id": "facebook:2", "ext_id": "2", "parsed": 1, "relevant": 0, "detail_fetched": 0},
                 {"id": "facebook:3", "ext_id": "3", "parsed": 0, "relevant": None, "detail_fetched": 0, "backlog": 1}])
    scan.apply_detail(con, "facebook:0", {"description": "2021 RZR XP 4, 900 miles", "status": "active"}, 10000)
    scan.apply_detail(con, "facebook:1", {"description": "edited text", "status": "active"}, 10000)
    con.commit()
    got = {r["id"]: r["parsed"] for r in con.execute("SELECT id, parsed FROM listings")}
    assert got["facebook:0"] == 0 and got["facebook:1"] == 1, got
    # item pages still to read: not the ad the model already dropped; fresh finds before backlog
    con.execute("UPDATE listings SET detail_fetched = 0, parsed = 1 WHERE id = 'facebook:0'")
    con.commit()
    assert [r["id"] for r in scan.pending_details(con, 10)] == ["facebook:0", "facebook:3"]


def test_review_fixes_2026_10_05():
    """Codex review of the deep-sweep commits: eight findings, one regression each."""
    from unittest.mock import AsyncMock, patch
    from app import score, sweep, web
    from app.equipment import detect

    # 1. alert markers record the row as it was sent, even if another lane changed it meanwhile
    con = reset([{"score": 75, "price": 10000, "listed_at": db.now() - 3 * 3600}])

    async def mutate_during_send(http, row, header=None):
        other = db.connect()
        other.execute("UPDATE listings SET price = 7000, score = 90 WHERE id = ?", (row["id"],))
        other.commit(); other.close()
        return True
    with patch.object(notify, "send_listing", mutate_during_send):
        asyncio.run(alerts(con))
    m = con.execute("SELECT alerted_score, alerted_price FROM listings").fetchone()
    assert (m["alerted_score"], m["alerted_price"]) == (75, 10000), dict(m)
    assert con.execute("SELECT price FROM alert_log").fetchone()[0] == 10000
    asyncio.run(alerts(con))                       # the price cut / score jump still alerts
    assert SENT == ["facebook:0"], SENT

    # 2. a paused Facebook never makes Facebook listings stale; Craigslist results don't speak for Facebook
    con = reset([{"last_seen": db.now() - 6 * 86400, "detail_fetched": 1},
                 {"id": "craigslist:1", "ext_id": "1", "source": "craigslist", "last_seen": db.now() - 6 * 86400,
                  "detail_fetched": 1}],
                active_hours="0-24", **{"fb_backoff_until:home": str(db.now() + 7200)})
    item = {"ext_id": "newcl", "url": "https://x.test/i", "title": "cl", "price": 12000, "status": "active"}

    class Lock:
        def close(self):
            pass
    with patch.object(scan, "acquire_fb_lock", AsyncMock(return_value=Lock())), \
         patch.object(scan.craigslist, "search", AsyncMock(return_value=[item])), \
         patch.object(scan.craigslist, "detail", AsyncMock(return_value={"status": "active"})), \
         patch.object(scan, "parse_pending", AsyncMock()), patch.object(scan.geo, "fill", AsyncMock()), \
         patch.object(scan, "send_alerts", AsyncMock(return_value=0)), patch.object(scan.asyncio, "sleep", AsyncMock()):
        asyncio.run(scan.run(force=True))
    got = {r["id"]: r["status"] for r in con.execute("SELECT id, status FROM listings")}
    assert got["facebook:0"] == "active" and got["craigslist:1"] == "gone", got

    # 3. a decimal in a category price limit is stored whole and doesn't crash the alert stage
    con = reset([{"listed_at": db.now() - 3 * 3600, "price": 15000}])
    r = client().put("/api/settings", json={"alert_rules": {"utv4": {"enabled": True, "max_price": "12000.0", "min_year": ""}}})
    assert r.status_code == 200 and db.alert_rules(db.settings(con))["utv4"]["max_price"] == "12000"
    asyncio.run(alerts(con))
    assert SENT == []                              # $15,000 is over the $12,000 limit
    assert client().put("/api/settings", json={"alert_rules": {"utv4": {"max_price": "lots"}}}).status_code == 400

    # 4. equipment is detected again after a late description changes the ad
    con = reset([{"title": "2022 RZR XP 4", "description": None, "equipment": None, "detail_fetched": 0}])
    _family(con)
    score.rescore_all(con)
    before = con.execute("SELECT expected FROM listings WHERE id = 'facebook:0'").fetchone()[0]
    scan.apply_detail(con, "facebook:0", {"description": "Full cab with heat and AC, comes with trailer", "status": "active"}, 10000)
    con.commit()
    assert con.execute("SELECT parsed FROM listings WHERE id = 'facebook:0'").fetchone()[0] == 0

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": json.dumps({"category": "utv4", "relevant": True, "family": "RZR XP 4", "year": 2022})}

    class HTTP:
        async def post(self, url, json, **kw):
            return Resp()
    asyncio.run(scan.parse_pending(con, HTTP(), 10, []))
    score.rescore_all(con)
    row = con.execute("SELECT equipment, expected FROM listings WHERE id = 'facebook:0'").fetchone()
    assert "cab" in json.loads(row["equipment"]) and row["expected"] > before, dict(row)

    # 5. malformed model output uses up the retry budget instead of looking like an outage
    con = reset([{"id": f"facebook:{i}", "ext_id": str(i), "title": f"ad{i}", "parsed": 0, "detail_fetched": 1,
                  "first_seen": db.now() - i} for i in range(4)])

    class Bad:
        def __init__(self, bad):
            self.bad = bad

        def raise_for_status(self):
            pass

        def json(self):
            return {"response": json.dumps({"category": "utv4", "relevant": True, "family": [] if self.bad else "RZR XP 4"})}

    class HTTP2:
        async def post(self, url, json, **kw):
            return Bad("Title: ad3\n" not in json["prompt"])
    errors = []
    asyncio.run(scan.parse_pending(con, HTTP2(), 4, errors, concurrency=1))
    assert not any("Ollama down" in e for e in errors), errors
    assert con.execute("SELECT SUM(parsed) FROM listings").fetchone()[0] == 4      # healthy ad3 and the 3 bad ones (family dropped)
    assert con.execute("SELECT family FROM listings WHERE id = 'facebook:3'").fetchone()[0] == "RZR XP 4"
    assert con.execute("SELECT family FROM listings WHERE id = 'facebook:0'").fetchone()[0] is None

    # 6b. a cross-post never prices itself through its twin: both copies value the same
    con = reset([{"title": "2022 RZR XP 4 twin", "price": 20000, "first_price": 20000},
                 {"id": "facebook:1", "ext_id": "1", "title": "2022 RZR XP 4 twin", "price": 20000, "first_price": 20000}])
    _insert(con, [dict(category="utv4", family="RZR XP 4", year=2022, price=p) for p in (15000, 15500, 16000)])
    score.rescore_all(con)
    twins = [dict(r) for r in con.execute("SELECT expected, comps, score FROM listings WHERE title LIKE '%twin' ORDER BY id")]
    assert twins[0] == twins[1] and twins[0]["comps"] == 3, twins

    # 6. eight copies of one listing are one comp
    con = reset([{"title": "2022 RZR XP 4 bargain", "price": 13000, "first_price": 13000}])
    _insert(con, [dict(category="utv4", family="RZR XP 4", year=2022, title="2022 RZR XP 4 Loaded", price=20000) for _ in range(8)])
    score.rescore_all(con)
    row = con.execute("SELECT comps FROM listings WHERE id = 'facebook:0'").fetchone()
    assert (row["comps"] or 0) <= 1, dict(row)

    # 7. disabling a search parks its sweep jobs; deleting it drops them
    con = reset([], active_hours="0-24")
    con.execute("DELETE FROM sweep_queue")
    con.execute("UPDATE searches SET enabled = (query IN ('ranger crew', 'jet ski', 'rzr xp 4'))")
    con.commit()
    sweep.start_round(con, 1)
    con.execute("UPDATE searches SET enabled = 0 WHERE query = 'ranger crew'")
    con.execute("DELETE FROM searches WHERE query = 'jet ski'")
    con.commit()
    calls = []

    class FakePW:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    class FakeFB(FakePW):
        def __init__(self, *a, **kw):
            pass

        async def search(self, q, *a, **kw):      # one result each, so the empty-run canary doesn't fire
            calls.append(q)
            return [{"ext_id": "s1", "url": "u", "title": "sweep find", "price": 9000, "status": "active"}]
    with patch.object(sweep, "async_playwright", return_value=FakePW()), patch.object(sweep, "Facebook", FakeFB), \
         patch.object(sweep, "pause", AsyncMock()), patch.object(scan, "acquire_fb_lock", AsyncMock(return_value=Lock())):
        asyncio.run(sweep.run())
    assert calls and all(q == "rzr xp 4" for q in calls), calls
    assert con.execute("SELECT COUNT(*) FROM sweep_queue WHERE query = 'jet ski'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM sweep_queue WHERE query = 'ranger crew' AND state = 'todo'").fetchone()[0] > 0
    con.execute("UPDATE searches SET enabled = 1"); con.commit()

    # 8. trailer fields left over from a category switch don't send a UTV through trailer pricing
    con = reset([]); _family(con)
    web._MARKET_CACHE.clear()
    body = {"category": "utv4", "family": "RZR XP 4", "year": 2022, "condition": "good", "miles": 1000}
    normal = client().post("/api/appraise", json=body).json()
    stale = client().post("/api/appraise", json=body | {"len_ft": 16, "axles": 2}).json()
    assert not normal["rough"] and stale["typical"] == normal["typical"] and not stale["rough"], (normal, stale)


def test_route_rechecked_after_lock_wait_and_inf_limits():
    """Opus review 2026-10-05: a pause set while a lane waits for the Facebook lock must stand; inf/nan limits are a 400."""
    from unittest.mock import AsyncMock, patch
    from app import sweep
    con = reset([], active_hours="0-24")
    con.execute("DELETE FROM sweep_queue")
    con.execute("UPDATE searches SET enabled = (query = 'jet ski')")
    con.commit()
    calls = []

    class Lock:
        closed = False

        def close(self):
            Lock.closed = True

    async def lock_after_block(wait, **kw):  # the full scan got blocked while the sweep waited
        c2 = db.connect()
        c2.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('fb_backoff_until:home', ?)", (str(db.now() + 7200),))
        c2.commit(); c2.close()
        return Lock()

    class FakeFB:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def search(self, q, *a, **kw):
            calls.append(q)
            return []
    with patch.object(scan, "acquire_fb_lock", lock_after_block), patch.object(sweep, "Facebook", FakeFB), \
         patch.object(sweep, "pause", AsyncMock()):
        asyncio.run(sweep.run())
    assert calls == [] and Lock.closed, calls
    assert int(db.settings(con)["fb_backoff_until:home"]) - db.now() > 7000      # the scan's pause is untouched
    con.execute("UPDATE searches SET enabled = 1"); con.commit()

    for bad in ("inf", "nan", "1e999"):
        r = client().put("/api/settings", json={"alert_rules": {"utv4": {"max_price": bad}}})
        assert r.status_code == 400, (bad, r.status_code)


def test_quick_lane_goes_first_and_far_padding_doesnt_split():
    from unittest.mock import AsyncMock, patch
    from app import sweep, geo
    # the sweep waits while the fast lane is waiting; the fast lane gets the lock
    async def race():
        held = await scan.acquire_fb_lock(5)
        q = asyncio.create_task(scan.acquire_fb_lock(10, quick=True))
        await asyncio.sleep(0.2)
        s = asyncio.create_task(scan.acquire_fb_lock(10, yield_to_quick=True))
        await asyncio.sleep(0.2)
        held.close()
        got = await q
        assert not s.done()
        got.close()
        lock = await s                       # marker gone once the fast lane has the lock: the sweep follows
        lock.close()
    asyncio.run(race())

    con = reset([], active_hours="0-24")
    con.execute("DELETE FROM sweep_queue")
    con.execute("UPDATE searches SET enabled = (query = 'jet ski')")
    con.execute("INSERT OR REPLACE INTO geocache VALUES ('Ames, IA', 42.03, -93.62)")
    con.commit()
    far = [{"ext_id": f"f{i}", "url": "u", "title": f"ski {i}", "price": 900, "location": "Ames, IA",
            "listed_at": db.now() - 86400 * 30, "status": "active"} for i in range(15)]

    class FakeFB:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def search(self, q, *a, price=None, **kw):
            return far if price == (500, 1999) else [far[0]]

    class Lock:
        def close(self):
            pass
    with patch.object(scan, "acquire_fb_lock", AsyncMock(return_value=Lock())), patch.object(sweep, "Facebook", FakeFB), \
         patch.object(sweep, "pause", AsyncMock()):
        asyncio.run(sweep.run())
    assert con.execute("SELECT state FROM sweep_queue WHERE lo = 500").fetchone()[0] == "done"   # full, but all far away
    assert con.execute("SELECT COUNT(*) FROM listings WHERE location = 'Ames, IA'").fetchone()[0] == 15  # still kept
    con.execute("UPDATE searches SET enabled = 1"); con.commit()

    # geocoder prefers the town over a county of the same name
    calls = []

    class R:
        status_code = 200

        def __init__(self, hit):
            self.hit = hit

        def raise_for_status(self):
            pass

        def json(self):
            return self.hit

    class H:
        async def get(self, url, params):
            calls.append(params.get("featureType"))
            return R([{"lat": "45.42", "lon": "-94.05"}] if params.get("featureType") else [{"lat": "47.56", "lon": "-95.37"}])
    with patch.object(geo.asyncio, "sleep", AsyncMock()):
        assert asyncio.run(geo.lookup(H(), "Clearwater, MN")) == (45.42, -94.05) and calls == ["settlement"]


def test_buy_box():
    from app import buybox, digest
    t = db.now() - 3 * 3600
    base = dict(listed_at=t, score=90, price=12000, expected=20000, year=2021, miles=1500, family="RZR XP 4")
    con = reset([dict(base),                                                                     # inside the box
                 dict(base, id="facebook:1", ext_id="1", title="old", year=2018),                 # too old
                 dict(base, id="facebook:2", ext_id="2", title="far", location="Ames, IA"),       # too far
                 dict(base, id="facebook:3", ext_id="3", title="worn", miles=9000),               # too many miles
                 dict(base, id="facebook:4", ext_id="4", title="unknown use", miles=None),        # unknown miles pass
                 dict(base, id="facebook:5", ext_id="5", title="ranger", family="Ranger Crew 1000"),   # not a picked model
                 dict(base, id="facebook:6", ext_id="6", title="doubt", miles=9000,            # 9,000 since a rebuild: at least that
                      usage_doubt="the ad only mentions 9,000 miles since a repair")],
                alert_min_pct="20", alert_min_usd="1500")
    con.execute("INSERT OR REPLACE INTO geocache VALUES ('Ames, IA', 42.03, -93.62)")
    con.commit()
    rule = {"enabled": True, "fresh": False, "digest": True, "models": ["RZR XP 4", "Maverick X3 MAX"], "min_year": "2020",
            "max_price": "", "max_miles": "3000", "max_hours": "", "within_mi": "150"}
    r = client().put("/api/settings", json={"alert_rules": {"utv4": rule}})
    assert r.status_code == 200, r.text
    assert db.alert_rules(db.settings(con))["utv4"]["models"] == ["RZR XP 4", "Maverick X3 MAX"]
    assert client().put("/api/settings", json={"alert_rules": {"utv4": dict(rule, models=["Toro TITAN"])}}).status_code == 400
    asyncio.run(alerts(con))
    assert sorted(SENT) == ["facebook:0", "facebook:4"], SENT
    # Ames is ~110 mi away: inside a 150 mi box, outside a 100 mi one
    assert buybox.fits(dict(rule, within_mi="100"), con.execute("SELECT * FROM listings WHERE id='facebook:2'").fetchone(), 110, 100) is False
    # instant off: no Telegram alert, but the digest still lists what's inside the box
    con.execute("UPDATE listings SET alerted_score = NULL, fresh_alerted = NULL"); con.execute("DELETE FROM alert_log"); con.commit()
    client().put("/api/settings", json={"alert_rules": {"utv4": dict(rule, enabled=False)}})
    SENT.clear()
    asyncio.run(alerts(con))
    assert SENT == [], SENT
    con.execute("UPDATE listings SET backlog = 1 WHERE id = 'facebook:4'"); con.commit()   # an old sweep find isn't news
    text = digest.build(con)
    assert ">unknown use<" not in text and "6 new listings" in text, text
    assert ">t0<" in text and ">old<" not in text and ">far<" not in text and ">ranger<" not in text, text


def test_long_lanes_step_aside_for_the_fast_lane_and_listings_feed_is_cached():
    # the full scan holds the lock; the fast lane starts waiting; the full scan's next step hands it over
    async def race():
        full = await scan.acquire_fb_lock(5)
        order = []

        async def quick():
            lk = await scan.acquire_fb_lock(30, quick=True)
            order.append("quick"); await asyncio.sleep(0.3); lk.close()
        q = asyncio.create_task(quick())
        await asyncio.sleep(0.5)
        assert scan.quick_waiting()
        await scan.step_aside(full)           # gives the lock up, waits for the fast lane, takes it back
        order.append("full again")
        await q
        full.close()
        return order
    assert asyncio.run(race()) == ["quick", "full again"]
    assert not scan.quick_waiting()

    con = reset([{"miles": None, "notes": None}])
    c = client()
    r = c.get("/api/listings")
    assert r.status_code == 200 and r.headers["etag"] and "miles" not in r.json()[0], r.json()[0]
    assert c.get("/api/listings", headers={"if-none-match": r.headers["etag"]}).status_code == 304
    assert c.post("/api/listing/facebook:0", json={"notes": "call him"}).status_code == 200
    r2 = c.get("/api/listings", headers={"if-none-match": r.headers["etag"]})
    assert r2.status_code == 200 and r2.json()[0]["notes"] == "call him"


def test_review_batch_2026_10_09():
    """Full-codebase review: stale handling, wall guard, title-only re-reads, price-cut re-alerts,
    the hourly Facebook budget, the sold pull, and the trailer new-price ceiling."""
    from unittest.mock import AsyncMock, patch
    from app import score
    old = db.now() - 10 * 86400
    # a. a Facebook machine nobody has re-seen is NOT marked gone by the calendar; junk and Craigslist still are
    con = reset([{"last_seen": old, "detail_fetched": 1},
                 {"id": "facebook:1", "ext_id": "1", "last_seen": old, "detail_fetched": 1, "relevant": 0},
                 {"id": "craigslist:2", "ext_id": "2", "source": "craigslist", "last_seen": old, "detail_fetched": 1}],
                active_hours="0-24")
    item = {"ext_id": "newcl", "url": "https://x.test/i", "title": "cl", "price": 12000, "status": "active"}

    class Lock:
        def close(self):
            pass
    with patch.object(scan, "acquire_fb_lock", AsyncMock(return_value=None)), \
         patch.object(scan.craigslist, "search", AsyncMock(return_value=[item])), \
         patch.object(scan.craigslist, "detail", AsyncMock(return_value={"status": "active"})), \
         patch.object(scan, "parse_pending", AsyncMock()), patch.object(scan.geo, "fill", AsyncMock()), \
         patch.object(scan, "send_alerts", AsyncMock(return_value=0)), patch.object(scan.asyncio, "sleep", AsyncMock()):
        asyncio.run(scan.run(force=True))
    got = {r["id"]: r["status"] for r in con.execute("SELECT id, status FROM listings")}
    assert got["facebook:0"] == "active" and got["facebook:1"] == "gone" and got["craigslist:2"] == "gone", got
    assert [r["id"] for r in scan.stale_candidates(con, 5)] == ["facebook:0"]      # its page gets checked instead

    # b. most of a batch failing to load is a wall, not a batch of removals
    con = reset([{"id": f"facebook:{i}", "ext_id": str(i), "detail_fetched": 1} for i in range(6)])

    class FB:
        async def detail(self, ext_id):
            return {"status": "active"} if ext_id == "0" else None
    errors = []
    with patch.object(scan.asyncio, "sleep", AsyncMock()):
        asyncio.run(scan.fb_details(con, FB(), con.execute("SELECT id, ext_id, price FROM listings").fetchall(), errors))
    assert any("login wall" in e for e in errors), errors
    assert con.execute("SELECT SUM(detail_misses) FROM listings").fetchone()[0] == 0

    # c. backlog finds wait a day for their page before a title-only parse; title-only dismissals are re-read later
    now = db.now()
    con = reset([{"parsed": 0, "detail_fetched": 0, "backlog": 1, "first_seen": now - 2 * 3600},
                 {"id": "facebook:1", "ext_id": "1", "parsed": 0, "detail_fetched": 0, "backlog": 1, "first_seen": now - 25 * 3600},
                 {"id": "facebook:2", "ext_id": "2", "parsed": 0, "detail_fetched": 0, "backlog": 0, "first_seen": now - 2 * 3600}])

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": json.dumps({"category": "none", "relevant": False})}

    class HTTP:
        async def post(self, url, json, **kw):
            return Resp()
    asyncio.run(scan.parse_pending(con, HTTP(), 10, []))
    rows = {r["id"]: (r["parsed"], r["title_only"]) for r in con.execute("SELECT id, parsed, title_only FROM listings")}
    assert rows == {"facebook:0": (0, 0), "facebook:1": (1, 1), "facebook:2": (1, 1)}, rows
    assert {r["id"] for r in scan.title_only_candidates(con, 5)} == {"facebook:1", "facebook:2"}
    assert scan.pending_details(con, 5) == [] or all(r["id"] == "facebook:0" for r in scan.pending_details(con, 5))

    # d. an alerted listing alerts again on a real price cut, not on comp drift alone
    con = reset([{"score": 92, "price": 10000, "alerted_score": 92, "alerted_price": 10000, "listed_at": now - 3 * 3600}])
    asyncio.run(alerts(con))
    assert SENT == [], SENT                                                   # nothing changed
    con.execute("UPDATE listings SET score = 100"); con.commit()              # comps moved, price didn't
    asyncio.run(alerts(con))
    assert SENT == [], SENT
    con.execute("UPDATE listings SET price = 9000"); con.commit()             # 10% cut
    asyncio.run(alerts(con))
    assert SENT == ["facebook:0"], SENT

    # e. the hourly Facebook budget is shared and stops the long lanes
    con = reset([{"id": f"facebook:{i}", "ext_id": str(i), "detail_fetched": 0, "parsed": 0} for i in range(3)])
    b = scan.FBBudget(con, reserve=scan.FB_HOURLY_BUDGET - 2)
    assert b.left() == 2
    b.record(); b.record()
    assert b.left() == 0

    class Ok:
        async def detail(self, ext_id):
            return {"status": "active", "description": "x"}
    errors = []
    with patch.object(scan.asyncio, "sleep", AsyncMock()):
        asyncio.run(scan.fb_details(con, Ok(), scan.pending_details(con, 10), errors, budget=b))
    assert con.execute("SELECT SUM(detail_fetched) FROM listings").fetchone()[0] == 0 and "budget" in errors[0], errors

    # f. the sold pull only sends watch alerts
    con = reset([{"score": 95, "price": 5000, "expected": 9000, "listed_at": now - 3 * 3600}])
    asyncio.run(scan.send_alerts(con, None, db.settings(con), watch_only=True))
    assert SENT == []
    asyncio.run(alerts(con))
    assert SENT == ["facebook:0"]

    # g. a used tandem with no stated GVWR is not capped by cheap single-axle new trailers
    fam = "Open utility (rails / mesh sides)"
    def comp(i, price, axles, gvwr):
        return score.Comp((f"n{i}", 2026, price, None, None, None, 16.0, axles, frozenset(), None, None, gvwr, 7.0, (f"n{i}", price)))
    me = {"category": "trailer", "family": fam, "len_ft": 16.0, "axles": 2, "gvwr_lb": None, "width_ft": 7.0}
    singles = {fam: [comp(1, 3295, 1, 2990), comp(2, 3495, 1, 2990), comp(3, 3395, 1, None)]}
    assert score.new_price_for(me, singles)[0] is None
    tandems = {fam: [comp(1, 5300, 2, 7000), comp(2, 5600, 2, 7000), comp(3, 5900, 2, 9990)]}
    assert score.new_price_for(me, tandems) == (5300, 3)
    assert score.new_price_for(me, {fam: tandems[fam][:2]})[0] is None               # fewer than 3: no ceiling


def test_review_followups_2026_10_09():
    """The smaller review findings: twins by seller, just-listed needs a posting time, non-runners aren't
    comps, equipment wording, since-repair limits, JSON-only writes, validation, cache change counter."""
    from app import buybox, equipment, score
    now = db.now()
    # twin suppression: same title + price from a different seller in another town is its own machine
    con = reset([{"title": "2021 Polaris Ranger 1000", "price": 12000, "location": "Anoka, MN", "listed_at": now - 3 * 3600},
                 {"id": "facebook:1", "ext_id": "1", "title": "2021 Polaris Ranger 1000", "price": 12000,
                  "location": "Rochester, MN", "listed_at": now - 3 * 3600},
                 {"id": "craigslist:2", "ext_id": "2", "source": "craigslist", "title": "2021 Polaris Ranger 1000",
                  "price": 12000, "location": "anoka", "listed_at": now - 3 * 3600}])
    asyncio.run(alerts(con))
    assert sorted(SENT) == ["facebook:0", "facebook:1"], SENT       # the Craigslist copy (other site) is the twin
    # just-listed: unknown posting time is not "just listed"
    con = reset([{"score": 60, "listed_at": None, "first_seen": now - 600}], alert_threshold="90")
    con.execute("UPDATE settings SET value = ? WHERE key = 'alert_rules'",
                (json.dumps({"utv4": {"enabled": True, "fresh": True, "digest": True}}),))
    con.commit()
    asyncio.run(alerts(con))
    assert SENT == [], SENT
    # a non-runner's asking price is not a comp
    con = reset([])
    _insert(con, [dict(category="utv4", family="RZR XP 4", year=2022, price=p, red_flags=f) for p, f in
                  ((16000, "[]"), (15500, "[]"), (4000, '["doesn\'t run"]'))])
    assert len(score._comps(con)["RZR XP 4"]) == 2
    # equipment wording
    det = lambda text: equipment.detect({"category": "utv4", "title": "", "description": text, "extras": "[]", "summary": None, "trim": None, "family": "RZR XP 4"})
    assert "cab" in det("full cab enclosure, no cab heater") and "heat" not in det("full cab enclosure, no cab heater")
    assert "plow" not in det("plow ready with mount installed") and "plow" in det("comes with a 72 inch plow")
    assert "trailer" not in det("will deliver with my trailer") and "trailer" not in det("ramps and trailer tie downs")
    assert "trailer" in det("comes with trailer and cover")
    # since-repair miles are a lower bound, so a max still applies; "unusually low" is skipped
    rule = {"max_miles": "3000"}
    assert buybox.fits(rule, {"family": "x", "year": 2022, "price": 1, "miles": 9000, "hours": None,
                              "usage_doubt": "the ad only mentions 9,000 miles since a repair"}, None, 100) is False
    assert buybox.fits(rule, {"family": "x", "year": 2022, "price": 1, "miles": 9000, "hours": None,
                              "usage_doubt": "9,000 miles is unusually low for a 2022"}, None, 100) is True
    # writes must be JSON (a cross-site form post can't be); validation; duplicate search; cache counter
    con = reset([{}])
    c = client()
    assert c.post("/api/listing/facebook:0", data="starred=1", headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 403
    assert c.put("/api/settings", json={"active_hours": "23-6"}).status_code == 400
    assert c.put("/api/settings", json={"active_hours": "6-23"}).status_code == 200
    assert c.put("/api/settings", json={"home_zip": "02134"}).status_code == 200
    assert db.settings(db.connect())["home_zip"] == "02134"
    assert c.post("/api/appraise", json={"category": "utv4", "family": "RZR XP 4", "year": "inf"}).status_code == 400
    assert c.post("/api/searches", json={"query": "ranger crew", "category": "atv"}).status_code == 409
    e1 = c.get("/api/listings").headers["etag"]
    assert c.post("/api/listing/facebook:0", json={"starred": True}).status_code == 200
    e2 = c.get("/api/listings").headers["etag"]
    assert e2 != e1 and c.get("/api/listings").json()[0]["starred"] == 1                  # an edit alone refreshes the feed
    assert c.post("/api/listing/facebook:0", json={"starred": False}).status_code == 200
    assert c.get("/api/listings").headers["etag"] == e1                                    # same data again: same ETag is right


def test_codex_round3_2026_10_09():
    """Codex review of e734d1f: feed revision from every writer, Gone survives a page check,
    a lane that yielded to the fast lane checks its own route, and the installer's rollback."""
    import subprocess
    from app import score
    # a rescore that changes only a note / offer still refreshes the cached feed
    con = reset([{"score": 80, "price": 10000}])
    c = client()
    e1 = c.get("/api/listings").headers["etag"]
    con.execute("UPDATE listings SET usage_doubt = 'the ad only mentions 60 miles since a repair' WHERE id = 'facebook:0'")
    con.commit()
    score.rescore_all(con)
    assert c.get("/api/listings").headers["etag"] != e1
    # a manual Gone is not undone by a page check that was already in flight
    con.execute("UPDATE listings SET status = 'gone', user_gone = 1 WHERE id = 'facebook:0'"); con.commit()
    scan.apply_detail(con, "facebook:0", {"status": "active", "description": "still up"}, 10000); con.commit()
    assert con.execute("SELECT status FROM listings WHERE id = 'facebook:0'").fetchone()[0] == "gone"
    scan.apply_detail(con, "facebook:0", {"status": "sold"}, 10000); con.commit()
    assert con.execute("SELECT status FROM listings WHERE id = 'facebook:0'").fetchone()[0] == "sold"
    # the home-IP browser stops when the home route got paused while it was stepping aside, even with a proxy configured
    con = reset([], fb_proxy="http://u:p@h:1", fb_route="auto")

    async def paused_meanwhile():
        lock = await scan.acquire_fb_lock(5)
        marker = os.path.join(scan.LOCK_DIR, scan.QUICK_WAITING)
        open(marker, "w").close()                    # the fast lane is waiting...

        async def fast_lane_gets_walled():
            await asyncio.sleep(0.3)
            c2 = db.connect()
            c2.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('fb_backoff_until:home', ?)", (str(db.now() + 7200),))
            c2.commit(); c2.close()
            os.remove(marker)                        # ...ran, got walled, paused the home route, and finished
        asyncio.create_task(fast_lane_gets_walled())
        try:
            await scan.step_aside(lock, "home")
            return "continued"
        except scan.SkipFacebook:
            return "stopped"
        finally:
            lock.close()
    assert asyncio.run(paused_meanwhile()) == "stopped"
    # the installer: every failure after the swap restores the previous release
    r = subprocess.run(["bash", os.path.join(os.path.dirname(__file__), "test_deploy.sh")], capture_output=True, text=True)
    assert r.returncode == 0 and "deploy tests passed" in r.stdout, r.stdout + r.stderr


def test_why_corrections_stages_and_alert_activity():
    from app import score, web
    now = db.now()
    # why: the comps behind the price, each lined up to this machine; the median of that column is the typical
    con = reset([{"title": "2022 RZR XP 4 mine", "price": 13000, "miles": 1500, "year": 2022, "listed_at": now - 3 * 3600}])
    _family(con)
    web._MARKET_CACHE.clear()
    score.rescore_all(con)
    c = client()
    w = c.get("/api/listing/facebook:0/why").json()
    me = con.execute("SELECT expected, comps FROM listings WHERE id = 'facebook:0'").fetchone()
    assert w["asking"]["expected"] == me["expected"] and len(w["asking"]["comps"]) == me["comps"] >= 4, w["asking"]["method"]
    adj = sorted(x["adjusted"] for x in w["asking"]["comps"])
    assert all(x["url"] and x["year"] for x in w["asking"]["comps"]) and "within 1 model year" in w["asking"]["method"]
    assert abs(adj[len(adj) // 2] - me["expected"]) <= max(1, 0.15 * me["expected"])   # (trend blend may move it a little)

    # corrections: applied now, kept through a re-parse, equipment overlay, and clearable
    before = me["expected"]
    r = c.post("/api/correct/facebook:0", json={"miles": 9000, "red_flags": ["needs engine work"], "equipment": ["cab", "heat"]})
    assert r.status_code == 200, r.text
    row = con.execute("SELECT miles, red_flags, equipment, expected, corrected FROM listings, (SELECT 1 corrected) WHERE id = 'facebook:0'").fetchone()
    assert row["miles"] == 9000 and json.loads(row["red_flags"]) == ["needs engine work"] and json.loads(row["equipment"]) == ["cab", "heat"]
    assert row["expected"] != before
    assert c.get("/api/listings").json()[0]["corrected"] == 1

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": json.dumps({"category": "utv4", "relevant": True, "family": "RZR XP 4", "year": 2022, "miles": 1500})}

    class HTTP:
        async def post(self, url, json, **kw):
            return Resp()
    con.execute("UPDATE listings SET parsed = 0 WHERE id = 'facebook:0'"); con.commit()     # the seller edited the ad
    asyncio.run(scan.parse_pending(con, HTTP(), 5, []))
    score.rescore_all(con)
    row = con.execute("SELECT miles, red_flags, equipment, usage_doubt FROM listings WHERE id = 'facebook:0'").fetchone()
    assert row["miles"] == 9000 and json.loads(row["red_flags"]) == ["needs engine work"] and json.loads(row["equipment"]) == ["cab", "heat"]
    assert c.post("/api/correct/facebook:0", json={"equipment": ["jetpack"]}).status_code == 400
    assert c.post("/api/correct/facebook:0", json={"family": "Toro TITAN"}).status_code == 200    # moves category too
    assert con.execute("SELECT category FROM listings WHERE id = 'facebook:0'").fetchone()[0] == "mower"
    assert c.post("/api/correct/facebook:0", json={}).status_code == 200                           # clear: re-read the ad
    assert con.execute("SELECT COUNT(*) FROM corrections").fetchone()[0] == 0
    assert con.execute("SELECT parsed FROM listings WHERE id = 'facebook:0'").fetchone()[0] == 0

    # stages: a stage stars the listing; purchased / passed stop the watch
    con = reset([{}])
    c = client()
    assert c.post("/api/listing/facebook:0", json={"stage": "contacted"}).status_code == 200
    r = con.execute("SELECT stage, starred, watch_price FROM listings").fetchone()
    assert (r["stage"], r["starred"], r["watch_price"]) == ("contacted", 1, 10000)
    assert c.post("/api/listing/facebook:0", json={"stage": "purchased"}).status_code == 200
    assert con.execute("SELECT starred FROM listings").fetchone()[0] == 0
    assert c.post("/api/listing/facebook:0", json={"stage": "bought"}).status_code == 400

    # alert activity: a skip is explained once a day, a send is logged
    con = reset([{"score": 90, "price": 12000, "expected": 20000, "listed_at": now - 3 * 3600},
                 {"id": "facebook:1", "ext_id": "1", "title": "far", "score": 90, "price": 12000, "expected": 20000,
                  "location": "Ames, IA", "listed_at": now - 3 * 3600}], alert_min_pct="20", alert_min_usd="1500")
    con.execute("INSERT OR REPLACE INTO geocache VALUES ('Ames, IA', 42.03, -93.62)")
    con.execute("UPDATE settings SET value = ? WHERE key = 'alert_rules'",
                (json.dumps({"utv4": {"enabled": True, "fresh": False, "digest": True, "within_mi": "100"}}),)); con.commit()
    asyncio.run(alerts(con)); asyncio.run(alerts(con))
    acts = [dict(a) for a in client().get("/api/alerts").json()]
    got = {(a["listing_id"], a["outcome"]): a["reason"] for a in acts}
    assert got[("facebook:0", "sent")].startswith("deal alert") and got[("facebook:1", "skipped")] == "outside the buy box", got
    assert sum(a["listing_id"] == "facebook:1" for a in acts) == 1          # once, not every run


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
