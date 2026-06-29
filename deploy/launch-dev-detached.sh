#!/usr/bin/env bash
#
# launch dev, DETACHED — start the local dev server in its OWN session so it
# outlives the shell (or agent/CI background task) that launched it.
#
#   deploy/launch-dev-detached.sh        (via: ./magi launch dev --detached
#                                          or:  ./magi workflow --detached)
#
# Why this exists: `serve.py --env dev` is a foreground werkzeug process with no
# supervisor (unlike prod, which launchd KeepAlive-restarts). A plain foreground
# launch dies with its parent — fine in your own terminal, but a background task
# (an agent turn, a CI step) gets reaped at teardown, taking the server with it.
# Here we start the server as a session leader (Python's start_new_session, the
# portable setsid — macOS has no setsid binary), reparented to init, so a
# process-group kill on the launcher never reaches it. Stop it with `./magi stop dev`.
#
# Idempotent: stops any existing dev server first (avoids a port clash), then waits
# until the new one answers before returning. Logs + pid go under data/ (gitignored).
#
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

PORT="${MAGI_PORT:-8080}"
HOSTBIND="${MAGI_HOST:-127.0.0.1}"
LOG="${MAGI_DEV_LOG:-$ROOT/data/dev-server.log}"
PIDFILE="$ROOT/data/dev-server.pid"
mkdir -p "$ROOT/data"

# Replace any running dev server so we don't fight over the port.
if pkill -f "serve.py --env dev" 2>/dev/null; then
  echo "==> stopped an existing dev server"
  # give the old one a moment to release the socket
  for _ in $(seq 1 20); do
    pgrep -f "serve.py --env dev" >/dev/null 2>&1 || break
    sleep 0.25
  done
fi

echo "==> launching dev (detached) — log: ${LOG}"
# Start serve.py in a NEW SESSION (start_new_session=True == setsid): detached from
# this script's process group + controlling terminal, reparented to init on our exit.
pid="$(
  MAGI_PORT="$PORT" MAGI_HOST="$HOSTBIND" \
  python3 - "$ROOT" "$LOG" "$PIDFILE" <<'PY'
import os, sys, subprocess
root, log, pidfile = sys.argv[1], sys.argv[2], sys.argv[3]
logf = open(log, "ab", buffering=0)
proc = subprocess.Popen(
    [sys.executable, os.path.join(root, "serve.py"), "--env", "dev"],
    stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
    start_new_session=True,            # detach: new session/process group
    cwd=root,
)
with open(pidfile, "w") as f:
    f.write(str(proc.pid))
print(proc.pid)
PY
)"

# Wait for it to actually answer (or surface the log if it dies on startup).
url="http://${HOSTBIND}:${PORT}/"
for _ in $(seq 1 60); do
  if curl -s -o /dev/null "$url" 2>/dev/null; then
    echo "==> dev is up: ${url}  (pid ${pid}, stop with ./magi stop dev)"
    exit 0
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "ERROR: dev server exited during startup — last log lines:" >&2
    tail -n 20 "$LOG" >&2 || true
    exit 1
  fi
  sleep 0.5
done

echo "ERROR: dev server did not answer ${url} within 30s — see ${LOG}" >&2
tail -n 20 "$LOG" >&2 || true
exit 1
