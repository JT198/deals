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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
