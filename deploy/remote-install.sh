#!/usr/bin/env bash
# Runs INSIDE the deals container (pushed there by deploy.sh). Installs /tmp/deals-deploy.tgz:
# tests first, then swap app/ + units + nginx, verify the NEW release answers, and roll everything
# back if any step after the swap fails (set -e + the ERR trap: a bad nginx config, a service that
# won't start, a health check that fails - all of them restore the previous release).
# Paths are overridable so tests/test_deploy.sh can run this against a scratch tree.
set -Eeuo pipefail   # -E: the ERR trap fires inside functions too
ROOT=${DEALS_ROOT:-/opt/deals}
UNITS=${DEALS_UNITS:-/etc/systemd/system}
NGINX_CONF=${DEALS_NGINX_CONF:-/etc/nginx/sites-available/deals}
LOGROTATE=${DEALS_LOGROTATE:-/etc/logrotate.d/deals}
TGZ=${DEALS_TGZ:-/tmp/deals-deploy.tgz}
NEW=${DEALS_NEW:-/tmp/deals-new}
HEALTH=${DEALS_HEALTH:-http://127.0.0.1}
PY=${DEALS_PY:-$ROOT/venv/bin/python}
rm -rf "$NEW" && mkdir -p "$NEW" && tar -xzf "$TGZ" -C "$NEW"

if [ "${SKIP_TESTS:-}" != 1 ]; then
  echo "running tests..."
  (cd "$NEW" && "$PY" -m tests.test_scan > /tmp/deals-test.log 2>&1) \
    || { echo "TESTS FAILED - not deploying"; grep -v "^ok " /tmp/deals-test.log | tail -25; exit 1; }
  echo "tests passed ($(grep -c '^ok ' /tmp/deals-test.log))"
fi

# keep the previous version of everything we replace
rm -rf "$ROOT/prev" && mkdir -p "$ROOT/prev/units"
[ -d "$ROOT/app" ] && cp -a "$ROOT/app" "$ROOT/prev/app"
cp "$UNITS"/deals-*.service "$UNITS"/deals-*.timer "$ROOT/prev/units/" 2>/dev/null || true
[ -f "$NGINX_CONF" ] && cp "$NGINX_CONF" "$ROOT/prev/nginx-deals.conf"

RELEASE=$(date +%Y%m%d-%H%M%S)
echo "$RELEASE" > "$NEW/app/RELEASE"

install_release() {       # $1 = directory holding app/, units in $2, nginx conf $3
  rm -rf "$ROOT/app" && cp -a "$1/app" "$ROOT/app"
  cp "$2"/* "$UNITS/"
  cp "$3" "$NGINX_CONF"
  systemctl daemon-reload
  nginx -t -q
  systemctl reload nginx
  systemctl restart deals-web
}

rollback() {
  trap - ERR
  echo "install FAILED - rolling back to the previous release"
  set +e
  install_release "$ROOT/prev" "$ROOT/prev/units" "$ROOT/prev/nginx-deals.conf"
  exit 1
}
trap rollback ERR

mkdir -p "$NEW/units" && cp "$NEW"/deploy/*.service "$NEW"/deploy/*.timer "$NEW/units/"
cp "$NEW"/deploy/logrotate-deals "$LOGROTATE"
cp "$NEW"/deploy/*.sh "$ROOT/" 2>/dev/null || true
install_release "$NEW" "$NEW/units" "$NEW/deploy/nginx-deals.conf"

# the running service must be THIS release (not the old process still answering), and the feed must build
ok=0
for _ in $(seq 15); do
  sleep "${DEALS_HEALTH_SLEEP:-2}"
  if curl -fsS "$HEALTH/api/status" | grep -q "\"release\":\"$RELEASE\"" \
     && curl -fsS -o /dev/null -H "x-real-ip: 10.10.10.50" "$HEALTH/api/listings"; then ok=1; break; fi
done
[ "$ok" = 1 ] || rollback
trap - ERR
systemctl restart deals-scan.timer deals-quick.timer deals-sold.timer deals-backup.timer deals-digest.timer 2>/dev/null || true
systemctl enable -q --now deals-sweep.timer
echo "deployed OK ($RELEASE)"
