#!/usr/bin/env bash
#
# launch prod — (re)start the unified app on the Mac mini. The mini serves via
# LaunchAgents (RunAtLoad + KeepAlive), so this just kickstarts them; it does NOT
# deploy code or touch any database. Use `./magi upgrade prod` (deploy) to ship code.
#
#   deploy/kickstart-mini.sh        (usually via:  ./magi launch prod)
#
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "$HERE/config.sh" ]]; then
  echo "ERROR: deploy/config.sh not found. Run: cp deploy/config.example.sh deploy/config.sh" >&2
  exit 1
fi
source "$HERE/config.sh"

echo "==> launch prod: starting services on ${MINI_HOST}"
ssh "${MINI_USER}@${MINI_HOST}" 'bash -l -s' <<'REMOTE'
  set -e
  uid=$(id -u)
  for svc in web betelgeuse-worker worker; do
    label="com.magi.$svc"
    plist="$HOME/Library/LaunchAgents/$label.plist"
    if launchctl print "gui/$uid/$label" >/dev/null 2>&1; then
      launchctl kickstart -k "gui/$uid/$label" && echo "  -> $label (re)started"
    elif [ -f "$plist" ]; then
      # was stopped (booted out) — load it back; RunAtLoad starts it.
      launchctl bootstrap "gui/$uid" "$plist" && launchctl enable "gui/$uid/$label" \
        && echo "  -> $label started (bootstrapped)"
    else
      echo "  !! $label not installed — run deploy/setup-mini.sh once" >&2
    fi
  done
REMOTE
echo "==> done. Prod: http://${MINI_HOST}:${PORT}/"
