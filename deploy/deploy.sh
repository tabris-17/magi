#!/usr/bin/env bash
#
# Deploy magi from this MacBook to the Mac mini.
#
#   deploy/deploy.sh [TARGET] [--env dev|prod]
#
#     TARGET   all (default) | host | youtube | betelgeuse
#     --env    migration env + (for `all`) the env passed to migrate_all
#              (default: MAGI_ENV from config.sh, else prod). The mini's SERVED
#              mode is baked into the LaunchAgents at setup time (deploy/setup-mini.sh
#              from MAGI_ENV) — normally prod; a dev-mode deployment sets MAGI_ENV=dev
#              in config.sh before the one-time setup. --env should match it.
#
#   For every target:
#     1. rsync the code, scoped to TARGET (NEVER any function's data/ — see the
#        excludes + the pre-flight *.db gate, identical for every target)
#     2. install/refresh deps in the mini's venv
#     3. migrate the affected DB(s) BEFORE restart (skipped for host/youtube)
#     4. restart only the affected LaunchAgents
#
# First-time setup is separate: after the first `all` rsync, SSH to the mini and run
# `bash deploy/setup-mini.sh` once (see deploy/README.md).
#
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

# ---- args (parsed before requiring config, so --help always works) ---------
TARGET="all"
ENV=""   # default applied from config.sh's MAGI_ENV below when --env not given
while [[ $# -gt 0 ]]; do
  case "$1" in
    all|host|youtube|betelgeuse) TARGET="$1"; shift ;;
    --env)   ENV="${2:-}"; shift 2 ;;
    --env=*) ENV="${1#--env=}"; shift ;;
    -h|--help)
      sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "deploy.sh: unknown argument '$1' (TARGET ∈ all|host|youtube|betelgeuse)" >&2; exit 2 ;;
  esac
done

if [[ ! -f "$HERE/config.sh" ]]; then
  echo "ERROR: deploy/config.sh not found. Run: cp deploy/config.example.sh deploy/config.sh  (then edit it)" >&2
  exit 1
fi
source "$HERE/config.sh"

ENV="${ENV:-${MAGI_ENV:-prod}}"   # --env wins, else config's MAGI_ENV, else prod
if [[ "$ENV" != "dev" && "$ENV" != "prod" ]]; then
  echo "deploy.sh: --env must be dev|prod (got '${ENV}')" >&2; exit 2
fi

# ---- rsync scope + options -------------------------------------------------
# One RSYNC_OPTS array so the pre-flight dry-run and the real run are byte-for-byte
# identical. Each function's runtime data (functions/*/data/ — live DBs + backups +
# caches + logs) is the source of truth on the mini and must NEVER be touched: it's
# excluded from transfer AND protected from --delete, and the pre-flight gate below
# ABORTS if any *.db would be created/updated/deleted. The protections are
# transfer-root-relative, so they hold for a whole-tree OR a scoped sub-tree sync.
RSYNC_OPTS=(
  -az --delete
  --filter='protect functions/*/data/***'
  --filter='protect data/***'
  --filter='protect *.db'
  --filter='protect *.db-wal'
  --filter='protect *.db-shm'
  --exclude='.git/'
  --exclude='__pycache__/'
  --exclude='*.pyc'
  --exclude='.venv/'
  --exclude='venv/'
  --exclude='.pytest_cache/'
  --exclude='.DS_Store'
  --exclude='functions/*/data/'
  --exclude='data/'
  --exclude='downloads/'
  --exclude='deploy/config.sh'
  --exclude='functions/*/deploy/config.sh'
)

# Scope the transfer to TARGET. SUB is the sub-tree synced; `host` syncs the whole
# tree but keeps every function untouched (excluded from transfer AND --delete).
EXTRA=()
case "$TARGET" in
  all)        SUB="" ;;
  host)       SUB=""; EXTRA=(--exclude='functions/') ;;
  youtube)    SUB="functions/youtube/" ;;
  betelgeuse) SUB="functions/betelgeuse/" ;;
esac
SRC="$ROOT/${SUB}"
DST="${MINI_USER}@${MINI_HOST}:${REMOTE_DIR}/${SUB}"

echo "==> target=${TARGET}  env=${ENV}"

# ---- pre-flight: prove no *.db will be touched -----------------------------
echo "==> pre-flight: proving no *.db will be touched on the mini"
preview="$(rsync --dry-run --itemize-changes "${RSYNC_OPTS[@]}" ${EXTRA[@]+"${EXTRA[@]}"} "$SRC" "$DST")"
if printf '%s\n' "$preview" | grep -iE '\.db([-.]|$)'; then
  echo "" >&2
  echo "ABORT: the rsync step would modify or delete a database on the mini" >&2
  echo "       (offending line(s) above). Mini DBs are the live source of truth —" >&2
  echo "       refusing to deploy. NOTHING was changed on the mini." >&2
  exit 1
fi

# ---- rsync -----------------------------------------------------------------
echo "==> rsync  ${SRC}  ->  ${MINI_USER}@${MINI_HOST}:${REMOTE_DIR}/${SUB}"
rsync "${RSYNC_OPTS[@]}" ${EXTRA[@]+"${EXTRA[@]}"} "$SRC" "$DST"

# ---- deps + migrate (BEFORE restart) + restart affected services -----------
# betelgeuse's boot guard refuses to serve an out-of-date DB (it lands on its
# maintenance page), so migrate first. The migration backs each DB up and aborts on
# the first failure — so on error we ABORT before restarting and the still-running
# old process keeps serving on its (untouched) old DB. TARGET/ENV are passed to the
# remote (quoted heredoc → no local expansion of the script body).
echo "==> install deps + migrate (${TARGET}) + restart on ${MINI_HOST}"
ssh "${MINI_USER}@${MINI_HOST}" "TARGET='${TARGET}' ENV='${ENV}' REMOTE_DIR='${REMOTE_DIR}' bash -l -s" <<'REMOTE'
  set -e
  cd "$REMOTE_DIR"
  # Rebuild a stale/broken venv (moved dir or changed system python) — venvs aren't
  # relocatable, so a `bad interpreter` pip shebang means recreate, don't reuse.
  if ! ./.venv/bin/pip --version >/dev/null 2>&1; then
    rm -rf .venv && python3 -m venv .venv
  fi
  ./.venv/bin/pip install -q -r requirements.txt -r functions/betelgeuse/requirements.txt
  case "$TARGET" in
    all)
      echo "  -> migrating all functions (backs up first)"
      ./.venv/bin/python migrate_all.py up --env "$ENV"
      SERVICES="web betelgeuse-worker worker" ;;
    betelgeuse)
      echo "  -> migrating betelgeuse (backs up first)"
      ( cd functions/betelgeuse && "$REMOTE_DIR/.venv/bin/python" migrate.py up --env "$ENV" )
      SERVICES="web betelgeuse-worker" ;;
    youtube)
      echo "  -> youtube has no migrations — skipping"
      SERVICES="web" ;;
    host)
      echo "  -> host: no function migrations — skipping"
      SERVICES="web worker" ;;   # worker.py is host code
  esac
  uid=$(id -u)
  for svc in $SERVICES; do
    launchctl kickstart -k "gui/$uid/com.magi.$svc" 2>/dev/null \
      || echo "  (com.magi.$svc not bootstrapped yet — run deploy/setup-mini.sh once)"
  done
REMOTE

echo "==> done. Access from the MacBook at:  http://${MINI_HOST}:${PORT}/"
