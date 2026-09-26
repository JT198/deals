#!/usr/bin/env bash
# Nightly consistent copy of the SQLite DB (VACUUM INTO works while WAL writers are active). Keeps 14 days.
set -euo pipefail
mkdir -p /opt/deals/data/backup
/opt/deals/venv/bin/python -c "import sqlite3, datetime; sqlite3.connect('/opt/deals/data/deals.db').execute(f\"VACUUM INTO '/opt/deals/data/backup/deals-{datetime.date.today()}.db'\")"
find /opt/deals/data/backup -name 'deals-*.db' -mtime +14 -delete
