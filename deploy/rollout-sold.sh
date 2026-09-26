#!/usr/bin/env bash
# One-off: first Facebook "Sold" pull + read the backlog. Full scans and the fast lane keep running.
cd /opt/deals && set -a && . ./.env && set +a
venv/bin/python -m app.scan --sold --backfill --force
for i in 1 2 3 4 5 6; do
  left=$(venv/bin/python -c "import sqlite3;print(sqlite3.connect('data/deals.db').execute(\"select count(*) from listings where status='sold' and (detail_fetched=0 or parsed=0)\").fetchone()[0])")
  echo "$(date) sold backlog: $left"; [ "$left" -eq 0 ] && break
  venv/bin/python -m app.scan --sold --backfill --no-search --force
done
systemctl enable --now deals-sold.timer
echo "$(date) sold rollout done"
