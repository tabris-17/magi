#!/usr/bin/env bash
#
# Pull the mini's LIVE portfolio.db down to this MacBook so dev can run on real data.
#
# Direction is mini -> MacBook ONLY. The mini's DB is read via a consistent sqlite
# snapshot (.backup) and is NEVER written — the only thing this touches on the mini
# is a throwaway file under /tmp. Locally it backs up the existing dev DB before
# atomically swapping in the snapshot, so a pull is always reversible.
#
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

if [[ ! -f "$HERE/config.sh" ]]; then
  echo "ERROR: deploy/config.sh not found. Run: cp deploy/config.example.sh deploy/config.sh  (then edit it)" >&2
  exit 1
fi
source "$HERE/config.sh"

REMOTE="${MINI_USER}@${MINI_HOST}"
TS="$(date +%Y%m%d-%H%M%S)"
SNAP="/tmp/betelgeuse-snap-${TS}.db"
# Runtime data lives under data/ (config.DATA_DIR) on BOTH machines. Stage + swap there;
# DB backups go in data/backup/ (alongside migrate.py's pre-migration snapshots).
DATA="$ROOT/data"
LOCAL_DB="$DATA/portfolio.db"
INCOMING="$DATA/portfolio.db.incoming-${TS}"
mkdir -p "$DATA" "$DATA/backup"

echo "==> snapshot mini's live DB (consistent, safe while prod is writing)"
# sqlite3 .backup is transactionally consistent even under concurrent writes.
ssh "$REMOTE" "sqlite3 '${REMOTE_DIR}/data/portfolio.db' \".backup '${SNAP}'\""

echo "==> pull snapshot  ${REMOTE}:${SNAP}  ->  ${INCOMING}"
rsync -az "${REMOTE}:${SNAP}" "$INCOMING"

if [[ -f "$LOCAL_DB" ]]; then
  BACKUP="$DATA/backup/portfolio.db.bak-${TS}"
  echo "==> back up local DB  ->  ${BACKUP}"
  cp "$LOCAL_DB" "$BACKUP"
fi

echo "==> swap in the prod snapshot (atomic mv)"
mv "$INCOMING" "$LOCAL_DB"

echo "==> clean up remote snapshot"
ssh "$REMOTE" "rm -f '${SNAP}'"

# Report row counts so the result is verifiable at a glance.
if command -v sqlite3 >/dev/null 2>&1; then
  pf="$(sqlite3 "$LOCAL_DB" 'SELECT COUNT(*) FROM portfolio;' 2>/dev/null || echo '?')"
  st="$(sqlite3 "$LOCAL_DB" 'SELECT COUNT(*) FROM settings;' 2>/dev/null || echo '?')"
  echo "==> done. local data/portfolio.db now holds prod data: ${pf} portfolio rows, ${st} settings."
else
  echo "==> done. local data/portfolio.db replaced with prod data."
fi

# ── Schema version safety (the pull is the one sanctioned prod->dev DB path) ──
# Compare the PULLED DB's schema version to what THIS checkout's code expects, so a
# downgrade-breaking gap is loud instead of silent. We read both with sqlite3/python:
#   pulled  = db_meta.version in the snapshot we just swapped in
#   code    = DB_SCHEMA_VERSION in core/db.py (the head this checkout knows)
if command -v sqlite3 >/dev/null 2>&1; then
  pulled="$(sqlite3 "$LOCAL_DB" "SELECT value FROM db_meta WHERE key='version';" 2>/dev/null || echo '')"
  code="$(cd "$ROOT" && python3 -c 'from core.db import DB_SCHEMA_VERSION; print(DB_SCHEMA_VERSION)' 2>/dev/null || echo '')"
  if [[ -n "$pulled" && -n "$code" ]]; then
    if (( pulled < code )); then
      echo ""
      echo "  ⚠ Pulled prod DB is v${pulled}; this code expects v${code}."
      echo "    Before developing, upgrade the pulled DB:  python3 migrate.py up --env dev"
      echo "    (or open Settings → Admin → Database Migrations and click Migrate up — it backs up first)."
    elif (( pulled > code )); then
      echo ""
      echo "  ‼ DANGER: pulled prod DB is v${pulled} but this code only knows v${code}." >&2
      echo "    Your checkout is BEHIND prod. Do NOT migrate down — sync/checkout newer code first." >&2
      echo "    Running dev now would refuse to start (DB newer than code)." >&2
    else
      echo "  ✓ Schema versions match (v${pulled}). Good to go."
    fi
  fi
fi
echo "    Restart dev so the running process reopens the new DB:  /deploy restart dev"
