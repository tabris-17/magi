#!/usr/bin/env python3
"""magi shared worker — the app-wide background-job runner for host-native functions.

One always-on process (the `com.magi.worker` LaunchAgent) owns an APScheduler and fires
host-native functions' scheduled jobs — currently the Notifier's personal reminders. It
mirrors betelgeuse's worker pattern (read the DB-backed schedule, reschedule when it
changes — loose coupling through the DB, no cross-process RPC) but **generalized**: each
schedulable function exposes `schedule_fingerprint()` + `reschedule(scheduler)` and is
listed in `SCHEDULABLE` below, so a new host-native function with background work plugs in
by adding its logic module here.

Betelgeuse keeps its OWN worker (`com.magi.betelgeuse-worker`) — it also does market-data
prefetch / FX / cache rebuilds and stays byte-identical to prod; this shared worker only
runs host-native functions' jobs (it never imports betelgeuse).

    python3 worker.py --env dev|prod
"""
import logging
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
POLL_INTERVAL_SEC = 30
logger = logging.getLogger("magi.worker")


def parse_env():
    """--env dev|prod, else MAGI_ENV, else dev."""
    env = os.environ.get("MAGI_ENV", "dev")
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--env" and i + 1 < len(args):
            env = args[i + 1]
        elif a.startswith("--env="):
            env = a.split("=", 1)[1]
    if env not in ("dev", "prod"):
        print(f"worker.py: --env must be dev|prod (got {env!r})", file=sys.stderr)
        sys.exit(2)
    return env


def configure_logging():
    os.makedirs(os.path.join(ROOT, "data", "logs"), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger("magi")
    root.setLevel(logging.INFO)
    if not root.handlers:
        stream = logging.StreamHandler(sys.stdout)  # captured by launchd's StandardOutPath
        stream.setFormatter(fmt)
        root.addHandler(stream)
        fileh = logging.FileHandler(os.path.join(ROOT, "data", "logs", "worker.app.log"))
        fileh.setFormatter(fmt)
        root.addHandler(fileh)


def main():
    env = parse_env()
    os.environ["MAGI_ENV"] = env                              # before importing function logic
    # Where the function send-helpers find the app-wide bot creds (magi.db) + the per-env
    # enable gate (magiscope.<env>.db). The plist sets this too; default for bare runs.
    os.environ.setdefault("MAGI_DATA_DIR", os.path.join(ROOT, "data"))
    configure_logging()

    # Register host-native functions that want scheduling. Each module must expose
    # schedule_fingerprint() + reschedule(scheduler). Add new ones here.
    from functions.notifier import logic as notifier_logic
    SCHEDULABLE = [("notifier", notifier_logic)]

    from apscheduler.schedulers.background import BackgroundScheduler
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.start()

    fingerprints = {}
    for name, mod in SCHEDULABLE:
        try:
            mod.reschedule(scheduler)
            fingerprints[name] = mod.schedule_fingerprint()
        except Exception:  # noqa: BLE001
            logger.exception("[%s] initial schedule failed", name)

    logger.info("magi worker started (env=%s) — managing: %s",
                env, ", ".join(n for n, _ in SCHEDULABLE))

    try:
        while True:
            time.sleep(POLL_INTERVAL_SEC)
            for name, mod in SCHEDULABLE:
                try:
                    fp = mod.schedule_fingerprint()
                    if fp != fingerprints.get(name):
                        logger.info("[%s] schedule changed — rescheduling", name)
                        mod.reschedule(scheduler)
                        fingerprints[name] = fp
                except Exception:  # noqa: BLE001
                    logger.exception("[%s] reschedule tick failed", name)
    except (KeyboardInterrupt, SystemExit):
        logger.info("magi worker stopping")
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
