# Deal Finder

Private dashboard + Telegram alerts for underpriced used machines near Plymouth, MN: 4-seat and
2-seat UTVs, ATVs, 3-wheelers, zero-turn mowers, trailers, jet skis and snowmobiles. Alerts are
filtered by a per-category buy box (Setup).

- **Host:** LXC 116 `deals` on pveai, 10.10.10.82 (Ubuntu 24.04, 2c/3G). Code in `/opt/deals`, venv, SQLite `data/deals.db`.
- **Sources:** Facebook Marketplace (logged-out headless Chromium, no account) + Craigslist (`sna` category, search_distance from home zip).
- **Pipeline** (`app/scan.py`, `deals-scan.timer` every 20 min during `active_hours`): search -> item pages for new hits -> LLM parse
  (qwen on .76 via Ollama, `think:false`) -> score vs our own comps (used, same family, +/-1 yr, ~8%/yr) -> Telegram alert when score >= threshold.
- **Dashboard:** `deals-web` (uvicorn 127.0.0.1:8000) behind nginx :80. Tunnel traffic (from CF-Gateway 10.10.10.5) must carry a
  Cloudflare Access email in `ALLOWED_EMAILS`; LAN is trusted.
- **Deploy:** `./deploy.sh` (never touches `.env` or data).
- **Manual scan:** `systemctl start deals-scan-now` (ignores active hours; alerts). Catch-up without alerts: `cd /opt/deals && set -a && . ./.env && venv/bin/python -m app.scan --backfill`.
- **Logs:** `/var/log/deals-scan.log`, `journalctl -u deals-web`. Run history on the Setup tab.
