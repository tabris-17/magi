#!/usr/bin/env bash
#
# Deploy Betelgeuse from this MacBook to the Mac mini server.
#   1. rsync the code (NEVER the DB/venv/caches — see excludes below)
#   2. install/refresh deps in the mini's venv
#   3. restart the web + worker LaunchAgents
#
# First-time setup is separate: after the first rsync, SSH to the mini and run
# `bash deploy/setup-mini.sh` once (and seed the DB — see deploy/README.md).
#
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

if [[ ! -f "$HERE/config.sh" ]]; then
  echo "ERROR: deploy/config.sh not found. Run: cp deploy/config.example.sh deploy/config.sh  (then edit it)" >&2
  exit 1
fi
source "$HERE/config.sh"

SRC="$ROOT/"
DST="${MINI_USER}@${MINI_HOST}:${REMOTE_DIR}/"

# rsync options live in ONE array so the pre-flight dry-run below and the real
# run are byte-for-byte identical — the safety check can never drift from what
# actually runs.
#
# The mini's runtime data (data/ — the live portfolio.db + backups, chart cache,
# backtest snapshots, logs) is the source of truth and must NEVER be touched.
# Enforcement, strongest first:
#   1. PRE-FLIGHT GATE (further down) — the real guarantee. Runs this exact arg set
#      as a --dry-run and ABORTS if the preview would create/update/delete ANY
#      portfolio.db path, for any reason (removed exclude, added --delete-excluded,
#      a new flag). Works on every rsync flavor (verified on macOS openrsync).
#   2. `--exclude` — the whole data/ dir (and the legacy top-level DB/sidecars/backups)
#      is never transferred, and is protected from --delete too (as long as nobody adds
#      --delete-excluded, which layer 1 would catch regardless).
#   3. `--filter 'protect ...'` — best-effort extra that blocks deletion even under
#      --delete-excluded, but ONLY on GNU rsync; macOS openrsync ignores it. Do not
#      rely on it — layer 1 is what actually holds. Kept for GNU-rsync hosts.
RSYNC_OPTS=(
  -az --delete
  --filter='protect data/***'
  --filter='protect portfolio.db'
  --filter='protect portfolio.db.*'
  --filter='protect *.db-wal'
  --filter='protect *.db-shm'
  --exclude='.git/'
  --exclude='__pycache__/'
  --exclude='*.pyc'
  --exclude='.venv/'
  --exclude='venv/'
  --exclude='data/'
  --exclude='portfolio.db'
  --exclude='portfolio.db.*'
  --exclude='*.db-wal'
  --exclude='*.db-shm'
  --exclude='static/chart_*'
  --exclude='deploy/config.sh'
  --exclude='.pytest_cache/'
)

echo "==> pre-flight: proving portfolio.db will NOT be touched"
# Itemized dry-run: any send/update/delete of a portfolio.db path would show here.
preview="$(rsync --dry-run --itemize-changes "${RSYNC_OPTS[@]}" "$SRC" "$DST")"
if printf '%s\n' "$preview" | grep -i 'portfolio\.db'; then
  echo "" >&2
  echo "ABORT: the rsync step would modify or delete portfolio.db on the mini" >&2
  echo "       (offending line(s) shown above). The mini's DB is the live source" >&2
  echo "       of truth — refusing to deploy. NOTHING was changed on the mini." >&2
  exit 1
fi

echo "==> rsync  $ROOT  ->  ${MINI_USER}@${MINI_HOST}:${REMOTE_DIR}"
rsync "${RSYNC_OPTS[@]}" "$SRC" "$DST"

# Migrate prod's DB to the schema the just-rsynced code expects, BEFORE restarting the
# services. The new code's boot guard refuses to run against an out-of-date DB, so a
# restart without this step would just land on the maintenance page. migrate.py backs the
# DB up first and applies any pending migrations; if it fails we ABORT before restarting,
# so the still-running old process keeps serving on the (untouched) old DB. Note: the old
# process keeps running on the migrated DB for the few seconds until kickstart below —
# fine for additive changes (the steady state for this app).
echo "==> install deps + migrate DB + restart services on ${MINI_HOST}"
ssh "${MINI_USER}@${MINI_HOST}" "bash -lc '
  set -e
  cd ${REMOTE_DIR}
  [ -d .venv ] || python3 -m venv .venv
  ./.venv/bin/pip install -q -r requirements.txt
  echo \"  -> migrating prod DB (backs up first)\"
  ./.venv/bin/python migrate.py up --env prod
  uid=\$(id -u)
  launchctl kickstart -k gui/\$uid/com.betelgeuse.web    2>/dev/null || echo \"  (web service not bootstrapped yet — run deploy/setup-mini.sh once)\"
  launchctl kickstart -k gui/\$uid/com.betelgeuse.worker 2>/dev/null || echo \"  (worker service not bootstrapped yet — run deploy/setup-mini.sh once)\"
'"

echo "==> done. Access from the MacBook at:  http://${MINI_HOST}:${PORT}/"
