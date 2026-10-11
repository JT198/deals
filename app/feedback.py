"""Reads the 👍 / 👎 taps under Telegram alerts (deals-feedback.timer, every 2 minutes).

A tap is a Telegram callback query; this polls getUpdates (no webhook: the box isn't reachable from
the internet), records the verdict, stars (👍) or hides (👎) the listing, and swaps the buttons for
"noted". The verdicts are what the score gets tuned against.

  python -m app.feedback           # one poll
  python -m app.feedback --print   # show the verdicts so far
"""
import asyncio
import json
import sys

import httpx

from . import db, notify

LABEL = {"+": "👍 good one", "-": "👎 not for me"}


def record(con, lid: str, verdict: str, note: str = "") -> None:
    """Store a verdict and act on it: 👍 = watch it, 👎 = hide it."""
    con.execute("INSERT INTO feedback(listing_id, verdict, ts) VALUES (?, ?, ?)", (lid, verdict, db.now()))
    if verdict == "+":
        con.execute("""UPDATE listings SET starred = 1, hidden = 0, watch_price = COALESCE(watch_price, price),
                         watch_status = COALESCE(watch_status, status) WHERE id = ?""", (lid,))
    else:
        con.execute("UPDATE listings SET hidden = 1 WHERE id = ?", (lid,))
    con.execute("INSERT INTO alert_activity(ts, listing_id, outcome, reason) VALUES (?, ?, 'feedback', ?)",
                (db.now(), lid, LABEL[verdict] + (f" ({note})" if note else "")))
    db.bump_rev(con)


def handle(con, updates: list[dict]) -> list[tuple[str, str, str, int | None]]:
    """-> [(callback_id, message_id, verdict text, chat_id)] for the taps that were applied."""
    done = []
    for u in updates:
        q = u.get("callback_query") or {}
        data = q.get("data") or ""
        msg = q.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id") or "")
        if not data.startswith("fb:") or chat != str(notify.CHAT):     # only our chat gets a say
            continue
        _, verdict, lid = data.split(":", 2)
        if verdict not in LABEL or not con.execute("SELECT 1 FROM listings WHERE id = ?", (lid,)).fetchone():
            continue
        who = (q.get("from") or {}).get("first_name") or ""
        record(con, lid, verdict, who)
        done.append((q["id"], msg.get("message_id"), LABEL[verdict], msg.get("chat", {}).get("id")))
    con.commit()
    return done


async def poll(con, http) -> int:
    base = f"https://api.telegram.org/bot{notify.TOKEN}"
    offset = int(db.settings(con).get("tg_update_offset") or 0)
    r = await http.get(f"{base}/getUpdates", params={"offset": offset, "timeout": 0,
                                                     "allowed_updates": json.dumps(["callback_query"])})
    r.raise_for_status()
    updates = r.json().get("result") or []
    if not updates:
        return 0
    done = handle(con, updates)
    for cb_id, message_id, text, chat_id in done:
        await http.post(f"{base}/answerCallbackQuery", data={"callback_query_id": cb_id, "text": f"Noted: {text}"})
        if message_id and chat_id:
            await http.post(f"{base}/editMessageReplyMarkup", data={
                "chat_id": chat_id, "message_id": message_id,
                "reply_markup": json.dumps({"inline_keyboard": [[{"text": f"{text} - noted", "callback_data": "noop"}]]})})
    con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('tg_update_offset', ?)",
                (str(updates[-1]["update_id"] + 1),))
    con.commit()
    return len(done)


def main():
    db.init()
    con = db.connect()
    if "--print" in sys.argv:
        for r in con.execute("""SELECT f.ts, f.verdict, l.title, l.price, l.score FROM feedback f
                                LEFT JOIN listings l ON l.id = f.listing_id ORDER BY f.ts DESC LIMIT 50"""):
            print(LABEL[r["verdict"]], r["score"], r["price"], r["title"])
        return
    if not notify.enabled():
        print("telegram not configured")
        return

    async def go():
        async with httpx.AsyncClient(timeout=20) as http:
            n = await poll(con, http)
            if n:
                print(f"feedback: {n} verdict(s) recorded")
    asyncio.run(go())


if __name__ == "__main__":
    main()
