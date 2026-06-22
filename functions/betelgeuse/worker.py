"""Betelgeuse background worker — the "server" half of the app.

Runs the recurring-notification scheduler (and, later, prefetch jobs) in a process
that is independent of the Flask web server. Run it on an always-awake machine:

    python3 worker.py

Notifications then fire on schedule even with no browser/UI open. Note that the web
process (`app.py`) deliberately does NOT start the scheduler, so exactly one process
schedules and notifications are never double-sent.

This module imports only the Flask-free `core` layer — never `app` — so the worker
carries no web dependency.

Scheduler config lives in the `settings` table (the source of truth). The web UI just
writes those settings; this worker polls them every POLL_INTERVAL_SEC and reschedules
its in-memory jobs when they change — loose coupling through the DB, no cross-process RPC.
"""
import sys
import time
from datetime import datetime

from core import db, health, migrate, notifications, runtime, version
from core.marketdata import (
    tracked_prefetch, tracked_backload, record_job_next_run,
    pending_rebuild_requests, clear_rebuild_request, run_rebuild, get_backload_start_date,
)
from core.logging_setup import configure_logging, get_logger

logger = get_logger('worker')

POLL_INTERVAL_SEC = 30
PREFETCH_INTERVAL_MIN = 15
# Deep-history backfill runs more often than the freshness prefetch needs to, but it's
# idempotent and _needs_backfill-gated: once an instrument's history reaches the start
# date it's skipped (a cheap MIN(timestamp) check, no network), so the hourly tick only
# re-fetches genuine laggards. yfinance tolerates this easily — even all-instruments-need-
# backfill is a few dozen requests/hour. Hourly lets symbols that hit a transient empty/
# throttled deep fetch catch up within hours instead of a day+.
BACKLOAD_INTERVAL_MIN = 60

# Settings keys that affect job *timing* (markets are read at send time, not here).
_SCHEDULE_KEYS = (
    'notification_portfolio_enabled',
    'notification_portfolio_days',
    'notification_portfolio_times',
    'default_timezone',
)


def schedule_fingerprint():
    """Return a hashable snapshot of the schedule-timing settings.

    Two equal fingerprints mean the live jobs are still correct; a change means we
    must reschedule. Reading from the DB on each tick keeps the worker in sync with
    edits made in the web UI without any direct process-to-process signalling.
    """
    conn = db.get_db_connection()
    try:
        c = conn.cursor()
        placeholders = ','.join('?' * len(_SCHEDULE_KEYS))
        c.execute(f'SELECT key, value FROM settings WHERE key IN ({placeholders})', _SCHEDULE_KEYS)
        cfg = {row['key']: row['value'] for row in c.fetchall()}
    finally:
        conn.close()
    return tuple(cfg.get(k) for k in _SCHEDULE_KEYS)


def tick(state):
    """One poll: reschedule jobs iff the schedule settings changed since last tick.

    `state` is a mutable dict carrying the last 'fingerprint'. Returns True if it
    rescheduled (useful for tests / logging).
    """
    fp = schedule_fingerprint()
    if fp != state.get('fingerprint'):
        state['fingerprint'] = fp
        notifications.reschedule_portfolio_notifications()
        logger.info("[worker] schedule changed -> rescheduled jobs %s", fp)
        return True
    return False


def dispatch_rebuilds(scheduler):
    """Pick up any queued cache-rebuild requests and run each as a one-shot job on the
    scheduler's threadpool.

    The request flag is cleared on dispatch so a long-running rebuild isn't scheduled
    twice on the next poll; run_rebuild writes live progress + the final status back to
    the DB for the web UI to poll. Running on the threadpool (never inline in this poll
    loop) keeps the heartbeat alive while a rebuild runs.
    """
    for market, provider in pending_rebuild_requests():
        clear_rebuild_request(market)
        scheduler.add_job(
            run_rebuild, 'date',
            args=[market, provider, get_backload_start_date()],
            id=f'market_data_rebuild_{market}',
            replace_existing=True,
        )
        logger.info("[worker] dispatched rebuild: %s/%s", market, provider)


# job name → APScheduler job id, for publishing next-run times to the UI.
_TRACKED_JOBS = {'prefetch': 'market_data_prefetch', 'backload': 'market_data_backload'}


def publish_next_runs(scheduler):
    """Mirror each recurring job's next scheduled fire time into the DB so the UI can
    show 'next reload in …'. Cheap; called every poll."""
    for name, job_id in _TRACKED_JOBS.items():
        job = scheduler.get_job(job_id)
        nxt = job.next_run_time.isoformat() if job and job.next_run_time else None
        record_job_next_run(name, nxt)


def main(env):
    configure_logging('worker', env)
    db.init_db()
    # Refuse to start against a DB whose schema ≠ this code's. The worker has no UI to
    # migrate through and must never run jobs against a mismatched schema, so it hard-exits
    # (the explicit, backed-up fix is `migrate.py up --env <env>`). Prod is normally
    # migrated by deploy before the worker restarts, so this only fires on a real gap.
    conn = db.get_db_connection()
    try:
        gate = migrate.gate_state(conn)
        cur, head = migrate.current_version(conn), migrate.head_version()
    finally:
        conn.close()
    if gate != 'OK':
        logger.critical("[worker] FATAL: DB is v%s but this code expects v%s (%s).",
                        cur, head, gate)
        if gate == 'NEEDS_UP':
            logger.critical("[worker]   Run:  python3 migrate.py up --env %s   (backs up first)", env)
        else:
            logger.critical("[worker]   The DB is NEWER than this code — deploy newer code or "
                            "restore a backup; never downgrade live data.")
        logger.critical("[worker] Refusing to start.")
        sys.exit(1)
    notifications.scheduler.start()
    # Schedule the market-data prefetch job; next_run_time=now() fires it immediately
    # so the cache is warm before the first Overview page load.
    notifications.scheduler.add_job(
        tracked_prefetch, 'interval',
        minutes=PREFETCH_INTERVAL_MIN,
        id='market_data_prefetch',
        next_run_time=datetime.now(),
        replace_existing=True,
    )
    # Deep-history backfill — low frequency, idempotent, _needs_backfill-gated. Fires
    # once at startup too so a newly-added instrument starts filling without waiting 6h.
    notifications.scheduler.add_job(
        tracked_backload, 'interval',
        minutes=BACKLOAD_INTERVAL_MIN,
        id='market_data_backload',
        next_run_time=datetime.now(),
        replace_existing=True,
    )
    # Stamp the start time once (for "when did the worker last restart?"), then beat
    # immediately so the web shows "ready" without waiting a full poll.
    health.record_worker_start()
    health.record_worker_heartbeat(version.WORKER_VERSION, env)
    state = {'fingerprint': None}
    tick(state)  # apply the initial schedule
    dispatch_rebuilds(notifications.scheduler)  # pick up any rebuild queued while down
    publish_next_runs(notifications.scheduler)  # seed the UI's "next reload" times
    logger.info("[worker] started (%s, v%s); polling every %ss (Ctrl-C to stop)",
                env, version.WORKER_VERSION, POLL_INTERVAL_SEC)
    try:
        while True:
            time.sleep(POLL_INTERVAL_SEC)
            # Refresh the heartbeat every poll so the web can detect a hung/stopped worker.
            health.record_worker_heartbeat(version.WORKER_VERSION, env)
            tick(state)
            dispatch_rebuilds(notifications.scheduler)
            publish_next_runs(notifications.scheduler)
    except (KeyboardInterrupt, SystemExit):
        notifications.scheduler.shutdown()
        logger.info("[worker] stopped")


if __name__ == '__main__':
    main(runtime.parse_env_arg())
