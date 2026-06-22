"""Worker liveness via a DB heartbeat — Flask-free, depends only on core.db.

The worker is a separate process (the "server" half). Rather than cross-process RPC,
it records a heartbeat into the `settings` table every poll (the same loose,
DB-mediated coupling the scheduler already uses). The web reads that heartbeat to
report whether the worker is up on the Application Health page.

`now` is injectable on both functions so tests are deterministic (no real clock).
"""
import time

from core import db

# settings keys (the worker writes these; the web reads them)
WORKER_HEARTBEAT_KEY = 'worker_heartbeat'            # epoch seconds, as a string
WORKER_VERSION_KEY = 'worker_running_version'        # version of the *running* worker
WORKER_ENV_KEY = 'worker_running_env'                # dev|prod of the *running* worker
WORKER_STARTED_KEY = 'worker_started_at'             # epoch seconds the worker process started

# A heartbeat older than this means the worker is considered down. The worker beats
# every 30s (POLL_INTERVAL_SEC), so 90s tolerates two missed beats before alarming.
WORKER_STALE_AFTER_SEC = 90


def record_worker_heartbeat(version, env, now=None):
    """Upsert the worker's heartbeat + the version/env it is running as."""
    ts = int(now if now is not None else time.time())
    conn = db.get_db_connection()
    try:
        c = conn.cursor()
        c.executemany(
            'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
            [
                (WORKER_HEARTBEAT_KEY, str(ts)),
                (WORKER_VERSION_KEY, str(version)),
                (WORKER_ENV_KEY, str(env)),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def record_worker_start(now=None):
    """Record the worker process's start time (epoch seconds), once at startup.

    Separate from the per-tick heartbeat: the heartbeat answers "is it alive?", this
    answers "when did it last restart?" — useful when a dev instance is inspecting prod.
    """
    ts = int(now if now is not None else time.time())
    conn = db.get_db_connection()
    try:
        c = conn.cursor()
        c.execute(
            'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
            (WORKER_STARTED_KEY, str(ts)),
        )
        conn.commit()
    finally:
        conn.close()


def worker_status(now=None):
    """Report worker liveness from the heartbeat.

    Returns {version, env, ready, last_seen (epoch s | None), age_seconds (| None),
    started_at (epoch s | None)}.
    `ready` is True only when a heartbeat exists and is within WORKER_STALE_AFTER_SEC.
    """
    now_ts = now if now is not None else time.time()
    conn = db.get_db_connection()
    try:
        c = conn.cursor()
        c.execute(
            'SELECT key, value FROM settings WHERE key IN (?, ?, ?, ?)',
            (WORKER_HEARTBEAT_KEY, WORKER_VERSION_KEY, WORKER_ENV_KEY, WORKER_STARTED_KEY),
        )
        vals = {row['key']: row['value'] for row in c.fetchall()}
    finally:
        conn.close()

    hb = vals.get(WORKER_HEARTBEAT_KEY)
    last_seen = int(hb) if hb and hb.isdigit() else None
    age = (now_ts - last_seen) if last_seen is not None else None
    ready = age is not None and age <= WORKER_STALE_AFTER_SEC
    started = vals.get(WORKER_STARTED_KEY)
    return {
        'version': vals.get(WORKER_VERSION_KEY),
        'env': vals.get(WORKER_ENV_KEY),
        'ready': ready,
        'last_seen': last_seen,
        'age_seconds': age,
        'started_at': int(started) if started and started.isdigit() else None,
    }
