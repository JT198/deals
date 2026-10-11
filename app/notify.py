"""Telegram alerts (same homelab bot/chat as the update alerts)."""
import html
import json
import os

import httpx

from .categories import cfg

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://deals.lan")


def enabled() -> bool:
    return bool(TOKEN and CHAT)


def trailer_size(r) -> str | None:
    """'7x16 enclosed · tandem · 7,000 lb · brakes · fits a 4-seat UTV'"""
    if r["category"] != "trailer":
        return None
    w, l = r["width_ft"], r["len_ft"]
    kind = "equipment" if r["trailer_type"] == "deckover" and w and w < 8 else r["trailer_type"]
    bits = [(f"{w:g}x{l:g}" if w and l else f"{l:g} ft" if l else "") + (f" {kind}" if kind else ""),
            {1: "single axle", 2: "tandem"}.get(r["axles"], f"{r['axles']} axles" if r["axles"] else ""),
            f"{r['gvwr_lb']:,} lb" if r["gvwr_lb"] else "", "brakes" if r["brakes"] == 1 else "",
            {"yes": "✅ fits a 4-seat UTV", "maybe": "may fit a UTV", "no": "too small for a UTV"}.get(r["utv_fit"], "")]
    return " · ".join(b.strip() for b in bits if b.strip()) or None


def seller_message(r) -> str:
    """A ready-to-send opener (same wording as the dashboard's Copy message button)."""
    name = r["model"] or r["family"] or "it"
    what = (f"{r['year']} " if r["year"] and not str(name).startswith(str(r["year"])) else "") + str(name)
    truck = " I'll bring my truck." if r["category"] == "trailer" else ""
    dealer = r["is_dealer"] == 1 or r["seller_type"] == "dealer"
    listed = r["listed_at"] or r["first_seen"] or 0
    fresh = listed > __import__("time").time() - 86400
    great = (r["deal_pct"] or 0) >= 0.15
    ask = f" Would you take ${r['offer_open']:,}?" if r["offer_open"] and r["price"] and r["offer_open"] < r["price"] else ""
    if dealer:
        return f"Hi, is the {what} still available? What's your best out-the-door price? I'm ready to buy this week."
    if great or (fresh and not r["offer_rough"]):
        return f"Hi! Is the {what} still available? I'm ready to buy - cash, and I can come see it today or tomorrow.{truck}{ask}"
    return f"Hi, is the {what} still available? I'm a serious buyer with cash and can pick it up this week.{truck}{ask}"


def buttons(r) -> dict:
    """Inline 👍 / 👎 under an alert: feedback.py reads the taps."""
    return {"inline_keyboard": [[{"text": "👍 Good one - watch it", "callback_data": f"fb:+:{r['id']}"},
                                 {"text": "👎 Not for me", "callback_data": f"fb:-:{r['id']}"}]]}


def listing_caption(r, header: str | None = None) -> str:
    e = html.escape
    price = f"${r['price']:,}" if r["price"] else "no price listed"
    bits = ([header] if header else []) + [f"{cfg(r['category'])['emoji']} <b>{e(r['title'][:120])}</b>",
            f"<b>{price}</b>" + (f"  ·  typical ${r['expected']:,}" if r["expected"] else "")
            + (f"  ·  score {r['score']}" if r["score"] is not None else "")]
    facts = [x for x in (
        trailer_size(r),
        r["location"],
        f"{r['deck_in']}\" deck" if r["deck_in"] else None,
        f"pair - {r['units']} for one price" if (r["units"] or 1) > 1 else None,
        f"{r['track_in']}\" track" if r["track_in"] else None,
        r["engine"],
        "miles unclear - ask" if r["usage_doubt"] else None,
        f"{r['miles']:,} mi" if r["miles"] is not None and not r["usage_doubt"] and r["category"] != "trailer" else None,
        f"{r['hours']:,} hrs" if r["hours"] is not None and not r["usage_doubt"] else None,
        "dealer" if (r["is_dealer"] == 1 or r["seller_type"] == "dealer") else "private seller",
        r["source"],
    ) if x]
    bits.append(e(" · ".join(facts)))
    if r["usage_note"]:
        bits.append(e(r["usage_note"]))
    if r["offer_aim"]:
        o = f"Offer ${r['offer_open']:,}" + (f" · aim ${r['offer_aim']:,}" if r["offer_open"] < r["offer_aim"] else "")
        bits.append(f"💬 {e(o)} · walk away above ${r['offer_walk']:,}" + (" (rough - few comps)" if r["offer_rough"] else ""))
    if r["summary"]:
        bits.append(e(r["summary"][:300]))
    if r["reasons"]:
        bits.append("Why: " + e("; ".join(json.loads(r["reasons"]))[:200]))
    if r["status"] == "active":      # long-press to copy, paste into Messenger / the Craigslist reply
        bits.append(f"✉️ <i>{e(seller_message(r))}</i>")
    links = f'<a href="{e(r["url"])}">Open listing</a>  ·  <a href="{e(DASHBOARD_URL)}">Dashboard</a>'
    body = "\n".join(bits)
    # Telegram caption limit is 1024; trim the text, never the links or a tag
    room = 1000 - len(links)
    if len(body) > room:
        body = body[:room].rsplit("\n", 1)[0]
    return body + "\n" + links


async def send_listing(http: httpx.AsyncClient, r, header: str | None = None, ask: bool = True) -> bool:
    """ask=True adds the 👍 / 👎 buttons (deal and just-listed alerts; not watch updates)."""
    if not enabled():
        return False
    base = f"https://api.telegram.org/bot{TOKEN}"
    try:
        cap = listing_caption(r, header)
        return await _send(http, base, r, cap, json.dumps(buttons(r)) if ask else None)
    except Exception:      # a malformed row must never take the whole alert stage down
        return False


async def _send(http, base, r, cap, markup: str | None = None) -> bool:
    extra = {"reply_markup": markup} if markup else {}
    if r["image"]:
        resp = await http.post(f"{base}/sendPhoto", data={
            "chat_id": CHAT, "photo": r["image"], "caption": cap, "parse_mode": "HTML", **extra})
        if resp.status_code == 200:
            return True
    resp = await http.post(f"{base}/sendMessage", data={
        "chat_id": CHAT, "text": cap, "parse_mode": "HTML", "disable_web_page_preview": "true", **extra})
    return resp.status_code == 200


async def send_text(http: httpx.AsyncClient, text: str) -> bool:
    if not enabled():
        return False
    try:
        resp = await http.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data={
            "chat_id": CHAT, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"})
    except httpx.HTTPError:
        return False
    return resp.status_code == 200
