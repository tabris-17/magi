#!/usr/bin/env bash
#
# ONE-TIME bootstrap, run ON the Mac mini (after the first rsync):
#   ssh <user>@<mini> 'cd ~/betelgeuse && bash deploy/setup-mini.sh'
#
# Creates the venv, installs deps, installs+loads the web & worker LaunchAgents
# (so they auto-start and restart on crash / login), and disables system sleep
# so the worker keeps firing while the screen is locked.
#
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PY="$ROOT/.venv/bin/python"
UID_NUM="$(id -u)"

echo "==> venv + deps"
[ -d "$ROOT/.venv" ] || python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/pip" install -q --upgrade pip
"$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt"

echo "==> runtime data dirs (config.DATA_DIR = $ROOT/data)"
mkdir -p "$ROOT/data/charts" "$ROOT/data/logs" "$ROOT/data/backtest" "$ROOT/data/backup"

echo "==> install + (re)load LaunchAgents"
mkdir -p "$HOME/Library/LaunchAgents"
for svc in web worker; do
  dst="$HOME/Library/LaunchAgents/com.betelgeuse.$svc.plist"
  sed -e "s|__ROOT__|$ROOT|g" -e "s|__PYTHON__|$PY|g" \
      "$HERE/launchd/com.betelgeuse.$svc.plist" > "$dst"
  launchctl bootout   "gui/$UID_NUM/com.betelgeuse.$svc" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_NUM" "$dst"
  launchctl enable    "gui/$UID_NUM/com.betelgeuse.$svc"
done

echo "==> prevent system sleep (locking the screen stays fine)"
echo "    (needs sudo; skip/Ctrl-C if you'll set it in System Settings instead)"
sudo pmset -a sleep 0 disablesleep 1 || echo "    pmset skipped — set 'prevent sleeping' in System Settings > Lock Screen/Energy"

echo "==> done."
echo "    Web:    http://$(hostname).local:8000/"
echo "    Logs:   $ROOT/data/logs/{web,worker}.{out,err}.log"
