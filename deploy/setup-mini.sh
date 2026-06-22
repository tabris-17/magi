#!/usr/bin/env bash
#
# ONE-TIME bootstrap, run ON the Mac mini (after the first rsync):
#   ssh <user>@<mini> 'cd ~/magi && bash deploy/setup-mini.sh'
#
# Creates the venv, installs deps (host + functions), makes each function's runtime
# data dirs, installs+loads the unified web + per-function worker LaunchAgents (so
# they auto-start and restart on crash/login), and disables system sleep so workers
# keep firing while the screen is locked.
#
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PY="$ROOT/.venv/bin/python"
UID_NUM="$(id -u)"

# Mode the LaunchAgents serve in. From MAGI_ENV in deploy/config.sh (default prod);
# the mini normally runs prod. Set MAGI_ENV=dev there for a dev-mode deployment.
[ -f "$HERE/config.sh" ] && source "$HERE/config.sh"
ENV="${MAGI_ENV:-prod}"

echo "==> venv + deps (host + functions)"
# venvs aren't relocatable: a moved dir (or an upgraded/removed system python) leaves
# the venv's pip shebang pointing at a path that no longer exists (`bad interpreter`).
# Probe pip itself — the thing the next line uses — and rebuild if it won't run,
# rather than reusing a broken .venv.
if ! "$ROOT/.venv/bin/pip" --version >/dev/null 2>&1; then
  echo "    (re)creating venv — missing or stale (venvs can't be moved)"
  rm -rf "$ROOT/.venv"
  python3 -m venv "$ROOT/.venv"
fi
"$ROOT/.venv/bin/pip" install -q --upgrade pip
"$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt" -r "$ROOT/functions/betelgeuse/requirements.txt"

echo "==> runtime data dirs"
mkdir -p "$ROOT/data/logs"                                   # host (launchd web stdout)
mkdir -p "$ROOT/functions/betelgeuse/data/charts" \
         "$ROOT/functions/betelgeuse/data/logs" \
         "$ROOT/functions/betelgeuse/data/backtest" \
         "$ROOT/functions/betelgeuse/data/backup"

echo "==> install + (re)load LaunchAgents"
mkdir -p "$HOME/Library/LaunchAgents"
for svc in web betelgeuse-worker; do
  dst="$HOME/Library/LaunchAgents/com.magi.$svc.plist"
  sed -e "s|__ROOT__|$ROOT|g" -e "s|__PYTHON__|$PY|g" -e "s|__ENV__|$ENV|g" \
      "$HERE/launchd/com.magi.$svc.plist" > "$dst"
  launchctl bootout   "gui/$UID_NUM/com.magi.$svc" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_NUM" "$dst"
  launchctl enable    "gui/$UID_NUM/com.magi.$svc"
done

echo "==> prevent system sleep (locking the screen stays fine)"
echo "    (needs sudo; skip/Ctrl-C if you'll set it in System Settings instead)"
sudo pmset -a sleep 0 disablesleep 1 || echo "    pmset skipped — set 'prevent sleeping' in System Settings > Lock Screen/Energy"

echo "==> done."
echo "    Web:    http://$(hostname).local:8080/"
echo "    Logs:   $ROOT/data/logs/web.{out,err}.log  +  functions/betelgeuse/data/logs/worker.{out,err}.log"
