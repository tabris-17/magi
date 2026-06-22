"""Stage-1 split tests: schedule math is scheduler-independent, the web process
never schedules, and the worker reschedules only when settings change.

All deterministic — no real scheduler is started, no sleeps, network untouched.
"""
from datetime import datetime

import app
import worker
from core import notifications


# ── next_runs computed from settings, no live scheduler ─────────────────────
class TestComputeNextRuns:
    def test_disabled_returns_empty(self, db, setval):
        setval("notification_portfolio_enabled", "false")
        setval("notification_portfolio_times", "08:00,13:00")
        assert app._compute_schedule_next_runs(_cfg()) == []

    def test_one_run_per_configured_time(self, db):
        cfg = {
            "notification_portfolio_enabled": "true",
            "notification_portfolio_days": "mon,tue,wed,thu,fri",
            "notification_portfolio_times": "08:00,13:00",
            "default_timezone": "Australia/Sydney",
        }
        runs = app._compute_schedule_next_runs(cfg)
        assert len(runs) == 2
        # Sorted ISO timestamps, each strictly in the future.
        assert runs == sorted(runs)
        for iso in runs:
            assert datetime.fromisoformat(iso) > datetime.now(datetime.fromisoformat(iso).tzinfo)

    def test_no_times_returns_empty(self, db):
        cfg = {"notification_portfolio_enabled": "true", "notification_portfolio_times": "  "}
        assert app._compute_schedule_next_runs(cfg) == []

    def test_bad_timezone_falls_back(self, db):
        cfg = {
            "notification_portfolio_enabled": "true",
            "notification_portfolio_times": "09:00",
            "default_timezone": "Not/AZone",
        }
        # Must not raise — falls back to the default tz.
        assert len(app._compute_schedule_next_runs(cfg)) == 1


def _cfg():
    """Read the schedule settings dict the helper expects, from the temp DB."""
    conn = app.get_db_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'notification_portfolio_%' "
            "OR key='default_timezone'"
        )
        return {row["key"]: row["value"] for row in c.fetchall()}
    finally:
        conn.close()


# ── Web process must not schedule (no double-send) ──────────────────────────
class TestWebDoesNotSchedule:
    def test_reschedule_is_noop_when_scheduler_not_running(self, db, setval):
        # Even with a fully-enabled schedule, the web process (scheduler never
        # started) must add no jobs — only the worker schedules.
        setval("notification_portfolio_enabled", "true")
        setval("notification_portfolio_times", "08:00")
        assert not app.scheduler.running
        app.reschedule_portfolio_notifications()  # must not raise
        assert [j for j in app.scheduler.get_jobs() if j.id.startswith("portfolio_notify_")] == []

    def test_schedule_route_reports_next_runs_without_scheduler(self, client, setval):
        setval("notification_portfolio_enabled", "true")
        setval("notification_portfolio_times", "08:00,13:00")
        data = client.get("/api/notifications/portfolio/schedule").get_json()
        # next_runs is derived from settings, so it's populated even though the
        # web test process has no running scheduler.
        assert len(data["next_runs"]) == 2
        assert data["enabled"] is True


# ── Worker reschedules only when the schedule fingerprint changes ───────────
class TestWorkerTick:
    def test_fingerprint_tracks_settings(self, db, setval):
        before = worker.schedule_fingerprint()
        setval("notification_portfolio_times", "07:30")
        after = worker.schedule_fingerprint()
        assert before != after

    def test_tick_reschedules_only_on_change(self, db, setval, monkeypatch):
        calls = []
        # worker.tick() calls notifications.reschedule_portfolio_notifications directly.
        monkeypatch.setattr(notifications, "reschedule_portfolio_notifications", lambda: calls.append(1))

        setval("notification_portfolio_times", "08:00")
        state = {"fingerprint": None}

        assert worker.tick(state) is True          # first tick applies the schedule
        assert worker.tick(state) is False         # unchanged → no reschedule
        setval("notification_portfolio_times", "09:00")
        assert worker.tick(state) is True          # changed → reschedules
        assert len(calls) == 2


# ── Worker publishes a heartbeat the web can read ───────────────────────────
class TestWorkerHeartbeat:
    def test_worker_heartbeat_marks_ready(self, db):
        # Mirrors the worker loop body: record a beat tagged with WORKER_VERSION.
        worker.health.record_worker_heartbeat(worker.version.WORKER_VERSION, "prod", now=5000)
        st = worker.health.worker_status(now=5005)
        assert st["ready"] is True
        assert st["version"] == worker.version.WORKER_VERSION
        assert st["env"] == "prod"
