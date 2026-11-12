#!/usr/bin/env bash
# Nightly consistent copy of the SQLite DB (VACUUM INTO works while WAL writers are active). Keeps 14 days.
# Written to a temp name and moved into place, so a second run on the same day replaces the copy instead of failing.
set -euo pipefail
mkdir -p /opt/deals/data/backup
out=/opt/deals/data/backup/deals-$(date +%F).db
rm -f "$out.tmp"
/opt/deals/venv/bin/python -c "import sqlite3, sys; sqlite3.connect('/opt/deals/data/deals.db').execute(\"VACUUM INTO ?\", (sys.argv[1],))" "$out.tmp"
mv -f "$out.tmp" "$out"
find /opt/deals/data/backup -name 'deals-*.db' -mtime +14 -delete
