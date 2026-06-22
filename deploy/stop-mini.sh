#!/usr/bin/env bash
#
# stop prod — stop the unified app on the Mac mini. The services run as KeepAlive
# LaunchAgents (a plain kill just respawns them), so this BOOTS THEM OUT (unloads
# them) — a real stop that won't auto-restart. It does NOT deploy or touch any DB.
# Bring prod back with `./magi launch prod` (it bootstraps if needed).
#
#   deploy/stop-mini.sh        (usually via:  ./magi stop prod)
#
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "$HERE/config.sh" ]]; then
  echo "ERROR: deploy/config.sh not found. Run: cp deploy/config.example.sh deploy/config.sh" >&2
  exit 1
fi
source "$HERE/config.sh"

echo "==> stop prod: stopping services on ${MINI_HOST}"
ssh "${MINI_USER}@${MINI_HOST}" 'bash -l -s' <<'REMOTE'
  set -e
  uid=$(id -u)
  for svc in web betelgeuse-worker; do
    if launchctl bootout "gui/$uid/com.magi.$svc" 2>/dev/null; then
      echo "  -> com.magi.$svc stopped (booted out)"
    else
      echo "  -- com.magi.$svc was not running"
    fi
  done
REMOTE
echo "==> done. Prod is stopped. Start it again with:  ./magi launch prod"
