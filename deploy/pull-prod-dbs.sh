#!/usr/bin/env bash
#
# upgrade dev — copy ALL prod databases from the Mac mini down to this machine's
# local dev tree, so dev mirrors prod data (settings + portfolio, etc).
#
#   deploy/pull-prod-dbs.sh        (usually via:  ./magi upgrade dev)
#
# This OVERWRITES the local dev DBs — so each is backed up first (timestamped
# .bak next to it). It only READS prod (a consistent `sqlite3 .backup` snapshot
# over SSH, no locking risk), never writes to the mini. This prod->dev direction
# is the safe one; never copy a dev DB UP to prod (schema goes up via migrations).
#
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

if [[ ! -f "$HERE/config.sh" ]]; then
  echo "ERROR: deploy/config.sh not found. Run: cp deploy/config.example.sh deploy/config.sh" >&2
  exit 1
fi
source "$HERE/config.sh"
REMOTE="${MINI_USER}@${MINI_HOST}"

# Every prod DB, as a path relative to the magi root (same layout on both machines).
# A new function with its own DB joins by adding its data/<name>.db here.
DBS=(
  "data/magi.db"                              # host: GLOBAL settings (theme, telegram, …)
  "data/magiscope.prod.db"                    # host: PROD-scoped settings (prod = source of truth)
  "functions/betelgeuse/data/portfolio.db"    # betelgeuse: portfolio + transactions
  "functions/notifier/data/notifier.db"       # notifier: personal reminder text + schedule
  "functions/polaris/data/polaris.db"         # polaris: journal entries
  "functions/altair/data/altair.db"           # altair: widget-feed layout
)
# NOTE: data/magiscope.dev.db is deliberately NOT listed — dev OWNS its scoped settings,
# so a deploy never overwrites them. magiscope.prod.db is mirrored down so dev can SEE
# prod's scoped values without touching dev's own.

stamp="$(date +%Y%m%d-%H%M%S)"
echo "==> upgrade dev: pulling prod DBs from ${REMOTE}:${REMOTE_DIR}"

for rel in "${DBS[@]}"; do
  remote_db="${REMOTE_DIR}/${rel}"
  local_db="${ROOT}/${rel}"

  if ! ssh "$REMOTE" "test -f '$remote_db'"; then
    echo "  -- skip ${rel} (not present on prod yet)"
    continue
  fi

  mkdir -p "$(dirname "$local_db")"
  if [[ -f "$local_db" ]]; then
    cp "$local_db" "${local_db}.bak-${stamp}"
    echo "  -- backed up local ${rel} -> ${rel}.bak-${stamp}"
  fi

  # Consistent snapshot on the mini (handles WAL/active writers), then fetch it.
  snap="/tmp/magi-pull-$(basename "$rel").${stamp}"
  ssh "$REMOTE" "sqlite3 '$remote_db' \".backup '$snap'\""
  scp -q "${REMOTE}:${snap}" "$local_db"
  ssh "$REMOTE" "rm -f '$snap'"
  echo "  -> pulled ${rel}"
done

echo "==> done. Local dev now mirrors prod data. (Backups: *.bak-${stamp})"
