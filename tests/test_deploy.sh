#!/usr/bin/env bash
# Exercises deploy/remote-install.sh against a scratch tree with stub systemctl / nginx / curl, so a
# deploy failure of any kind is proven to restore the previous release.   bash tests/test_deploy.sh
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
export DEALS_ROOT=$T/opt DEALS_UNITS=$T/units DEALS_NGINX_CONF=$T/nginx.conf DEALS_LOGROTATE=$T/logrotate \
       DEALS_TGZ=$T/deploy.tgz DEALS_NEW=$T/new DEALS_HEALTH=http://stub DEALS_HEALTH_SLEEP=0 SKIP_TESTS=1 \
       DEALS_PY=/bin/true STUB=$T/stub
mkdir -p "$T/bin" "$T/src/app" "$T/src/deploy"
reset_tree() {   # the installed ("old") release, fresh for every case
  rm -rf "$T/opt" "$T/units" "$T/new"; mkdir -p "$T/opt/app" "$T/units"
  echo old > "$T/opt/app/marker"; echo "old unit" > "$T/units/deals-web.service"; echo "old nginx" > "$T/nginx.conf"
}
echo new > "$T/src/app/marker"; echo "new unit" > "$T/src/deploy/deals-web.service"; echo "new timer" > "$T/src/deploy/deals-scan.timer"
echo "new nginx" > "$T/src/deploy/nginx-deals.conf"; echo "lr" > "$T/src/deploy/logrotate-deals"
tar -czf "$T/deploy.tgz" -C "$T/src" app deploy
# stubs: STUB file says which step fails; curl answers with the release the "running service" reports
cat > "$T/bin/systemctl" <<'S'
#!/bin/bash
[ "$(cat "$STUB" 2>/dev/null)" = "restart-fails" ] && [ "$1" = restart ] && [ "$2" = deals-web ] && exit 1
exit 0
S
cat > "$T/bin/nginx" <<'S'
#!/bin/bash
[ "$(cat "$STUB" 2>/dev/null)" = "nginx-bad" ] && exit 1
exit 0
S
cat > "$T/bin/curl" <<'S'
#!/bin/bash
case "$(cat "$STUB" 2>/dev/null)" in
  old-process) echo '{"release":"20200101-000000"}';;      # the old service is still the one answering
  *) rel=$(cat "$DEALS_ROOT/app/RELEASE" 2>/dev/null || echo none); echo "{\"release\":\"$rel\"}";;
esac
S
chmod +x "$T/bin/"*; export PATH=$T/bin:$PATH

run() { reset_tree; echo "$1" > "$STUB"; bash "$HERE/deploy/remote-install.sh" > "$T/out" 2>&1 && echo 0 || echo $?; }
check_old() { [ "$(cat "$T/opt/app/marker")" = old ] && [ "$(cat "$T/units/deals-web.service")" = "old unit" ] && [ "$(cat "$T/nginx.conf")" = "old nginx" ]; }

fail() { echo "FAIL: $1"; echo "--- installer output:"; cat "$T/out"; echo "--- marker=$(cat "$T/opt/app/marker") unit=$(cat "$T/units/deals-web.service") nginx=$(cat "$T/nginx.conf")"; exit 1; }
[ "$(run ok)" = 0 ] && [ "$(cat "$T/opt/app/marker")" = new ] && grep -q "deployed OK" "$T/out" && echo "ok  clean install" || fail "clean install"
[ "$(run nginx-bad)" = 1 ] && check_old && echo "ok  bad nginx config -> rolled back" || fail "bad nginx config"
[ "$(run restart-fails)" = 1 ] && check_old && echo "ok  service won't restart -> rolled back" || fail "service restart"
[ "$(run old-process)" = 1 ] && check_old && echo "ok  old process still answering -> rolled back" || fail "old process"
echo "deploy tests passed"
