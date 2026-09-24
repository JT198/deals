#!/usr/bin/env bash
# Push code + units to the deals LXC (116, 10.10.10.82). Never touches /opt/deals/.env or data/.
set -euo pipefail
HOST=root@10.10.10.82
cd "$(dirname "$0")"
python3 -m py_compile app/*.py app/sources/*.py
rsync -a --delete --exclude __pycache__ app/ "$HOST":/opt/deals/app/
scp -q deploy/*.service deploy/*.timer "$HOST":/etc/systemd/system/
scp -q deploy/nginx-deals.conf "$HOST":/etc/nginx/sites-available/deals
scp -q deploy/logrotate-deals "$HOST":/etc/logrotate.d/deals
ssh "$HOST" 'systemctl daemon-reload && nginx -t -q && systemctl reload nginx && systemctl restart deals-web && sleep 2 && curl -fsS -o /dev/null http://127.0.0.1/api/status && echo "deployed OK"'
