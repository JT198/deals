#!/usr/bin/env bash
# Push code + units to the deals LXC (ct 116 on pveai). Never touches /opt/deals/.env or data/.
# Goes through the Proxmox host (pct push/exec) so it works even if the LXC's LAN IP is unreachable.
# The container-side steps live in deploy/remote-install.sh (tests, swap, health check, rollback).
#   ./deploy.sh            # run the test suite in the container first (a few minutes)
#   ./deploy.sh --no-test  # skip it
set -euo pipefail
PVE=root@10.10.10.251
CT=116
cd "$(dirname "$0")"
python3 -m py_compile app/*.py app/sources/*.py tests/*.py
SKIP=0; [ "${1:-}" = "--no-test" ] && SKIP=1
tar --exclude=__pycache__ -czf - app deploy tests | ssh "$PVE" "cat > /tmp/deals-deploy.tgz && pct push $CT /tmp/deals-deploy.tgz /tmp/deals-deploy.tgz && rm /tmp/deals-deploy.tgz"
ssh "$PVE" "pct exec $CT -- env SKIP_TESTS=$SKIP bash -c 'tar -xzf /tmp/deals-deploy.tgz -C /tmp deploy/remote-install.sh && bash /tmp/deploy/remote-install.sh'"
