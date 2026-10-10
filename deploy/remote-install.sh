#!/usr/bin/env bash
# Runs INSIDE the deals container (pushed there by deploy.sh). Installs /tmp/deals-deploy.tgz:
# tests first, then swap app/ + units + nginx, health check, and a real rollback if that fails.
set -euo pipefail
NEW=/tmp/deals-new
rm -rf "$NEW" && mkdir "$NEW" && tar -xzf /tmp/deals-deploy.tgz -C "$NEW"

if [ "${SKIP_TESTS:-}" != 1 ]; then
  echo "running tests..."
  (cd "$NEW" && /opt/deals/venv/bin/python -m tests.test_scan > /tmp/deals-test.log 2>&1) \
    || { echo "TESTS FAILED - not deploying"; grep -v "^ok " /tmp/deals-test.log | tail -25; exit 1; }
  echo "tests passed ($(grep -c '^ok ' /tmp/deals-test.log))"
fi

# keep the previous version of everything we replace
rm -rf /opt/deals/prev && mkdir -p /opt/deals/prev/units
[ -d /opt/deals/app ] && cp -a /opt/deals/app /opt/deals/prev/app
cp /etc/systemd/system/deals-*.service /etc/systemd/system/deals-*.timer /opt/deals/prev/units/ 2>/dev/null || true
cp /etc/nginx/sites-available/deals /opt/deals/prev/nginx-deals.conf

rollback() {
  echo "health check FAILED - rolling back"
  rm -rf /opt/deals/app && cp -a /opt/deals/prev/app /opt/deals/app
  cp /opt/deals/prev/units/* /etc/systemd/system/
  cp /opt/deals/prev/nginx-deals.conf /etc/nginx/sites-available/deals
  systemctl daemon-reload && nginx -t -q && systemctl reload nginx && systemctl restart deals-web
  exit 1
}

rm -rf /opt/deals/app && mv "$NEW/app" /opt/deals/app
cp "$NEW"/deploy/*.service "$NEW"/deploy/*.timer /etc/systemd/system/
cp "$NEW"/deploy/nginx-deals.conf /etc/nginx/sites-available/deals
cp "$NEW"/deploy/logrotate-deals /etc/logrotate.d/deals
cp "$NEW"/deploy/*.sh /opt/deals/ 2>/dev/null || true
systemctl daemon-reload && nginx -t -q && systemctl reload nginx && systemctl restart deals-web

ok=0
for _ in $(seq 15); do
  sleep 2
  curl -fsS -o /dev/null http://127.0.0.1/api/status && curl -fsS -o /dev/null -H "x-real-ip: 10.10.10.50" http://127.0.0.1/api/listings && ok=1 && break
done
[ "$ok" = 1 ] || rollback
systemctl restart deals-scan.timer deals-quick.timer deals-sold.timer deals-backup.timer deals-digest.timer 2>/dev/null || true
systemctl enable -q --now deals-sweep.timer
echo "deployed OK"
