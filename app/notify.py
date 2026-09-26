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
    bits = [(f"{w:g}x{l:g}" if w and l else f"{l:g} ft" if l else "") + (f" {r['trailer_type']}" if r["trailer_type"] else ""),
            {1: "single axle", 2: "tandem"}.get(r["axles"], f"{r['axles']} axles" if r["axles"] else ""),
            f"{r['gvwr_lb']:,} lb" if r["gvwr_lb"] else "", "brakes" if r["brakes"] == 1 else "",
            {"yes": "✅ fits a 4-seat UTV", "maybe": "may fit a UTV", "no": "too small for a UTV"}.get(r["utv_fit"], "")]
    return " · ".join(b.strip() for b in bits if b.strip()) or None


def listing_caption(r, header: str | None = None) -> str:
    e = html.escape
    bits = ([header] if header else []) + [f"{cfg(r['category'])['emoji']} <b>{e(r['title'])}</b>",
            f"<b>${r['price']:,}</b>" + (f"  ·  typical ${r['expected']:,}" if r["expected"] else "")
            + f"  ·  score {r['score']}"]
    facts = [x for x in (
        trailer_size(r),
        r["location"],
        f"{r['deck_in']}\" deck" if r["deck_in"] else None,
        r["engine"],
        f"{r['miles']:,} mi" if r["miles"] is not None else None,
        f"{r['hours']:,} hrs" if r["hours"] is not None else None,
        "dealer" if (r["is_dealer"] == 1 or r["seller_type"] == "dealer") else "private seller",
        r["source"],
    ) if x]
    bits.append(e(" · ".join(facts)))
    if r["usage_note"]:
        bits.append(e(r["usage_note"]))
    if r["offer_aim"]:
        o = f"Offer ${r['offer_open']:,}" + (f" · aim ${r['offer_aim']:,}" if r["offer_open"] < r["offer_aim"] else "")
        bits.append(f"💬 {e(o)} · walk away above ${r['offer_walk']:,}")
    if r["summary"]:
        bits.append(e(r["summary"]))
    if r["reasons"]:
        bits.append("Why: " + e("; ".join(json.loads(r["reasons"]))))
    bits.append(f'<a href="{e(r["url"])}">Open listing</a>  ·  <a href="{e(DASHBOARD_URL)}">Dashboard</a>')
    return "\n".join(bits)


async def send_listing(http: httpx.AsyncClient, r, header: str | None = None) -> bool:
    if not enabled():
        return False
    cap = listing_caption(r, header)[:1020]
    base = f"https://api.telegram.org/bot{TOKEN}"
    try:
        return await _send(http, base, r, cap)
    except httpx.HTTPError:
        return False


async def _send(http, base, r, cap) -> bool:
    if r["image"]:
        resp = await http.post(f"{base}/sendPhoto", data={
            "chat_id": CHAT, "photo": r["image"], "caption": cap, "parse_mode": "HTML"})
        if resp.status_code == 200:
            return True
    resp = await http.post(f"{base}/sendMessage", data={
        "chat_id": CHAT, "text": cap, "parse_mode": "HTML", "disable_web_page_preview": "true"})
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
