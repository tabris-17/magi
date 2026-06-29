#!/usr/bin/env bash
#
# Full release workflow, one shot:
#
#   1. upgrade dev   — pull all prod DBs down to local   (pull-prod-dbs.sh)
#   2. upgrade prod  — deploy local code to the mini, no DB touch, and the deploy's
#                      own restart step LAUNCHES prod      (deploy.sh all --env prod)
#   3. launch dev    — run the app locally in the foreground (serve.py --env dev)
#
# Prod is (re)started by step 2's restart, so there's no separate kickstart here
# (use `./magi launch prod` for a standalone prod restart). Step 3 blocks — Ctrl-C
# to stop the local server.
#
#   deploy/workflow.sh [--yes] [--detached]    (usually via:  ./magi workflow)
#     --yes        skip the confirmation prompt (for non-interactive runs)
#     --detached   step 3 launches dev in its OWN session and RETURNS, instead of
#                  blocking in the foreground — so the server survives this shell
#                  (or an agent/CI background task). Stop it with `./magi stop dev`.
#
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

YES=0
DETACHED=0
for arg in "$@"; do
  case "$arg" in
    --yes|-y)      YES=1 ;;
    --detached|-d) DETACHED=1 ;;
    *) echo "workflow: unknown option '$arg' (try: --yes, --detached)" >&2; exit 2 ;;
  esac
done

if [[ ! -f "$HERE/config.sh" ]]; then
  echo "ERROR: deploy/config.sh not found. Run: cp deploy/config.example.sh deploy/config.sh" >&2
  exit 1
fi
source "$HERE/config.sh"

cat <<INFO
==> magi full workflow
      from : this machine (dev)
      to   : ${MINI_USER}@${MINI_HOST}:${REMOTE_DIR}  (prod)
    steps:
      1) upgrade dev   — OVERWRITE local DBs with prod's (local backed up first)
      2) upgrade prod  — deploy code to the mini (no prod DB touched) + restart it
      3) launch dev    — run locally in the foreground (Ctrl-C to stop)
INFO

if [[ "$YES" -ne 1 ]]; then
  read -r -p "Proceed? [y/N] " ans
  [[ "$ans" == "y" || "$ans" == "Y" ]] || { echo "aborted."; exit 1; }
fi

echo; echo "########## 1/3  upgrade dev ##########"
"$HERE/pull-prod-dbs.sh"

echo; echo "########## 2/3  upgrade prod (deploy + launch prod) ##########"
"$HERE/deploy.sh" all --env prod

if [[ "$DETACHED" -eq 1 ]]; then
  echo; echo "########## 3/3  launch dev (detached) ##########"
  exec "$HERE/launch-dev-detached.sh"
else
  echo; echo "########## 3/3  launch dev (foreground) ##########"
  exec python3 "$ROOT/serve.py" --env dev
fi
