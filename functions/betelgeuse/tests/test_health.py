"""Worker-heartbeat health logic (core.health) — temp DB, deterministic clock.

`now` is injected explicitly so freshness is tested without sleeping or touching
the real wall clock.
"""
from core import health


class TestWorkerStatus:
    def test_no_heartbeat_is_down(self, db):
        st = health.worker_status(now=1000)
        assert st["ready"] is False
        assert st["version"] is None
        assert st["last_seen"] is None
        assert st["age_seconds"] is None

    def test_fresh_heartbeat_is_ready(self, db):
        health.record_worker_heartbeat("1.0", "dev", now=1000)
        st = health.worker_status(now=1010)  # 10s later
        assert st["ready"] is True
        assert st["version"] == "1.0"
        assert st["env"] == "dev"
        assert st["last_seen"] == 1000
        assert st["age_seconds"] == 10

    def test_stale_heartbeat_is_down(self, db):
        health.record_worker_heartbeat("1.0", "prod", now=1000)
        # Older than WORKER_STALE_AFTER_SEC -> down, but version/env still reported.
        st = health.worker_status(now=1000 + health.WORKER_STALE_AFTER_SEC + 1)
        assert st["ready"] is False
        assert st["version"] == "1.0"
        assert st["env"] == "prod"

    def test_exactly_at_threshold_is_ready(self, db):
        health.record_worker_heartbeat("1.0", "dev", now=1000)
        st = health.worker_status(now=1000 + health.WORKER_STALE_AFTER_SEC)
        assert st["ready"] is True

    def test_heartbeat_is_upserted_not_duplicated(self, db):
        health.record_worker_heartbeat("1.0", "dev", now=1000)
        health.record_worker_heartbeat("1.1", "dev", now=2000)
        st = health.worker_status(now=2005)
        assert st["version"] == "1.1"
        assert st["last_seen"] == 2000
