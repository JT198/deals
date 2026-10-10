"""Morning digest: one Telegram message with what happened since yesterday.

  python -m app.digest          # send (deals-digest.timer, 07:00)
  python -m app.digest --print  # just print it

Top new private deals per enabled category, price drops and status changes on watched listings,
and a one-line pulse. Calm by design: this is what to look at over coffee, not another alert.
"""
import asyncio
import html
import sys
import time

import httpx

from . import buybox, db, notify
from .categories import CATEGORIES

PER_CATEGORY = 2
MIN_SCORE = 60


def build(con) -> str:
    e = html.escape
    st = db.settings(con)
    rules = db.alert_rules(st)
    since = db.now() - 24 * 3600
    lines = [f"☕ <b>Deal Finder - {time.strftime('%A %b %-d')}</b>"]

    # seen_active keeps the sold-pull backlog (listings first seen already sold) out of these counts
    # backlog = older listings the deep sweep dug up: found yesterday, but not new on the market
    new_total = con.execute("SELECT COUNT(*) FROM listings WHERE relevant = 1 AND seen_active = 1 AND backlog = 0 AND first_seen >= ?",
                            (since,)).fetchone()[0]
    gone_total = con.execute("SELECT COUNT(*) FROM listings WHERE relevant = 1 AND seen_active = 1 AND ended_at >= ?",
                             (since,)).fetchone()[0]
    lines.append(f"{new_total} new listings yesterday, {gone_total} sold or removed.")

    any_deal = False
    radius = int(st.get("radius_mi") or 100)
    dist = buybox.distance_fn(con, st)
    for cat, cfg in CATEGORIES.items():
        rule = rules.get(cat, {})
        if not rule.get("digest", True):
            continue
        rows = con.execute(
            """SELECT * FROM listings
               WHERE relevant = 1 AND category = ? AND status = 'active' AND hidden = 0 AND first_seen >= ?
                 AND backlog = 0 AND score >= ? AND COALESCE(is_dealer, 0) = 0 AND COALESCE(seller_type, '') != 'dealer'
                 AND COALESCE(is_new, 0) = 0
               ORDER BY score DESC""", (cat, since, MIN_SCORE)).fetchall()
        rows = [r for r in rows if (cat != "trailer" or r["utv_fit"] == "yes" or r["score"] >= cfg.get("fit_gate", 100))
                and buybox.fits(rule, r, dist(r), radius)][:PER_CATEGORY]
        if not rows:
            continue
        any_deal = True
        lines.append(f"\n{cfg['emoji']} <b>{e(cfg['label'])}</b>")
        for r in rows:
            typ = f" (typical ${r['expected']:,})" if r["expected"] else ""
            where = f" · {e(r['location'])}" if r["location"] else ""
            lines.append(f"• <a href=\"{e(r['url'])}\">{e(r['title'][:60])}</a> - <b>${r['price']:,}</b>{typ}{where} · score {r['score']}")
    if not any_deal:
        lines.append("\nNo new private listings scored 60+ yesterday.")

    watched = con.execute(
        """SELECT title, url, price, first_price, status, score FROM listings WHERE starred = 1
           ORDER BY score DESC""").fetchall()
    if watched:
        lines.append(f"\n⭐ <b>Watching ({len(watched)})</b>")
        for r in watched[:8]:
            was = r["first_price"] if r["first_price"] and r["first_price"] > (r["price"] or 0) else None
            state = {"pending": " · PENDING", "sold": " · SOLD", "gone": " · removed"}.get(r["status"], "")
            drop = f" (was ${was:,})" if was else ""
            price = f"${r['price']:,}" if r["price"] else "no price"
            lines.append(f"• <a href=\"{e(r['url'])}\">{e(r['title'][:50])}</a> - {price}{drop}{state}")

    # pulse: how yesterday's new listings were priced, and how fast things are going
    pulse = con.execute(
        """SELECT COUNT(*), AVG(deal_pct) FROM listings WHERE relevant = 1 AND first_seen >= ? AND deal_pct IS NOT NULL
             AND COALESCE(is_new, 0) = 0 AND backlog = 0 AND COALESCE(is_dealer, 0) = 0""", (since,)).fetchone()
    if pulse[0] and pulse[0] >= 5:
        vs = -pulse[1] * 100
        lines.append(f"\nMarket: yesterday's used listings came in {'+' if vs > 0 else ''}{vs:.0f}% vs typical asking.")
    lines.append(f'\n<a href="{e(notify.DASHBOARD_URL)}">Open the dashboard</a>')
    return "\n".join(lines)


async def send(text: str) -> bool:
    async with httpx.AsyncClient(timeout=30) as http:
        return await notify.send_text(http, text)


def main():
    db.init()
    con = db.connect()
    text = build(con)
    if "--print" in sys.argv:
        print(text)
        return
    ok = asyncio.run(send(text))
    print("digest sent" if ok else "digest FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
