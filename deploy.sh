#!/usr/bin/env bash
# Push code + units to the deals LXC (ct 116 on pveai). Never touches /opt/deals/.env or data/.
# Goes through the Proxmox host (pct push/exec) so it works even if the LXC's LAN IP is unreachable.
set -euo pipefail
PVE=root@10.10.10.251
CT=116
cd "$(dirname "$0")"
python3 -m py_compile app/*.py app/sources/*.py
tar --exclude=__pycache__ -czf - app deploy | ssh "$PVE" "cat > /tmp/deals-deploy.tgz && pct push $CT /tmp/deals-deploy.tgz /tmp/deals-deploy.tgz && rm /tmp/deals-deploy.tgz"
ssh "$PVE" "pct exec $CT -- bash -c '
  set -e; rm -rf /tmp/deals-new && mkdir /tmp/deals-new && tar -xzf /tmp/deals-deploy.tgz -C /tmp/deals-new
  rm -rf /opt/deals/app.prev; [ -d /opt/deals/app ] && mv /opt/deals/app /opt/deals/app.prev
  mv /tmp/deals-new/app /opt/deals/app
  cp /tmp/deals-new/deploy/*.service /tmp/deals-new/deploy/*.timer /etc/systemd/system/
  cp /tmp/deals-new/deploy/nginx-deals.conf /etc/nginx/sites-available/deals
  cp /tmp/deals-new/deploy/logrotate-deals /etc/logrotate.d/deals
  cp /tmp/deals-new/deploy/*.sh /opt/deals/ 2>/dev/null || true
  systemctl daemon-reload && nginx -t -q && systemctl reload nginx && systemctl restart deals-web
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do sleep 2; curl -fsS -o /dev/null http://127.0.0.1/api/status && ok=1 && break; done
  if [ "\${ok:-}" != 1 ]; then
    echo "health check FAILED - rolling back"; rm -rf /opt/deals/app; mv /opt/deals/app.prev /opt/deals/app
    systemctl restart deals-web; exit 1
  fi
  systemctl restart deals-scan.timer deals-quick.timer deals-sold.timer deals-backup.timer 2>/dev/null || true
  echo deployed OK'"
