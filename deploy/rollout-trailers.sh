#!/usr/bin/env bash
# One-off: add the trailer category without flooding Telegram. Run detached on the LXC.
cd /opt/deals && set -a && . ./.env && set +a
systemctl stop deals-scan.timer deals-quick.timer
while systemctl is-active -q deals-scan.service deals-quick.service deals-scan-now.service; do sleep 10; done
venv/bin/python -m app.scan --force --quiet              # new trailer searches run first (never run before)
# trailers found by UTV searches before this category existed were dropped as irrelevant - re-read them
venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect("data/deals.db")
n = c.execute("UPDATE listings SET parsed = 0 WHERE relevant = 0 AND status != 'gone' AND lower(title) LIKE '%trailer%'").rowcount
c.commit(); print("re-reading", n, "old trailer listings")
PY
for i in 1 2 3 4; do
  venv/bin/python -m app.scan --backfill --no-search
  left=$(venv/bin/python -c "import sqlite3;print(sqlite3.connect('data/deals.db').execute(\"select count(*) from listings where parsed=0 and status!='gone'\").fetchone()[0])")
  echo "$(date) round $i unparsed=$left"; [ "$left" -eq 0 ] && break
done
systemctl start deals-scan.timer deals-quick.timer
echo "$(date) trailer rollout done"
