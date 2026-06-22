"""Migration engine tests (core/migrate.py).

Deterministic: the `db` fixture points core.db.DATABASE at a temp file, so backups land
in tmp and never touch the real portfolio.db. Synthetic migrations exercise up/down/gate
without needing a real v3 schema change in the repo.
"""
import glob
import os

# Aliased: the `db` pytest fixture (temp DB) would otherwise shadow the core.db module
# inside any test that takes `db` as a parameter.
from core import db as core_db, migrate


# ── synthetic migrations (a reversible v3 + an irreversible v3) ──────────────
def _up_alerts(c):
    c.execute("CREATE TABLE price_alerts (id INTEGER PRIMARY KEY, note TEXT)")


def _down_alerts(c):
    c.execute("DROP TABLE price_alerts")


# A synthetic migration ONE PAST the real head, so these engine tests exercise up/down/gate
# without hard-coding the repo's current head (adding a real migration must not break them).
def _synthetic(reversible=True):
    v = migrate.head_version() + 1
    down = _down_alerts if reversible else migrate._irreversible("no going back")
    return migrate.Migration(v, f"{v:03d}_price_alerts", "add price_alerts", _up_alerts, down)


def _with_next(reversible=True):
    return migrate.discover() + [_synthetic(reversible)]


# ── discovery / head ─────────────────────────────────────────────────────────
class TestDiscovery:
    def test_baseline_discovered(self, db):
        names = [m.name for m in migrate.discover()]
        assert "002_baseline" in names

    def test_head_matches_constant(self, db):
        # Adding a migration without bumping DB_SCHEMA_VERSION (or vice-versa) is a bug.
        assert migrate.head_version() == core_db.DB_SCHEMA_VERSION


# ── current_version / gate ─────────────────────────────────────────────────────
class TestGate:
    def test_fresh_is_ok(self, conn):
        assert migrate.current_version(conn) == core_db.DB_SCHEMA_VERSION
        assert migrate.gate_state(conn) == "OK"

    def test_needs_up_when_code_ahead(self, conn):
        # DB at real head; the synthetic list is one ahead -> NEEDS_UP
        assert migrate.gate_state(conn, _with_next()) == "NEEDS_UP"

    def test_db_newer_refuses(self, conn):
        # Pretend the DB reached head+1 but the live code only knows head -> DB_NEWER
        ahead = migrate.head_version() + 1
        conn.execute("INSERT INTO schema_migrations (version, name) VALUES (?, 'x')", (ahead,))
        conn.commit()
        assert migrate.gate_state(conn) == "DB_NEWER"


# ── status / plan ──────────────────────────────────────────────────────────────
class TestStatusPlan:
    def test_status_lists_pending(self, conn):
        head = migrate.head_version()
        st = migrate.status(conn, _with_next())
        assert st["current"] == head and st["head"] == head + 1 and st["gate"] == "NEEDS_UP"
        assert [p["version"] for p in st["pending"]] == [head + 1]

    def test_plan_up_and_down(self, conn):
        head = migrate.head_version()
        assert migrate.plan(conn, head + 1, _with_next())[0] == "up"
        # at head already -> downgrade target == head is a no-op
        assert migrate.plan(conn, head, _with_next())[0] == "none"


# ── apply: up / down round-trip + backup ────────────────────────────────────────
class TestApply:
    def test_up_applies_and_backs_up(self, conn):
        head = migrate.head_version()
        res = migrate.apply(conn, target=None, migrations=_with_next())
        assert res["from"] == head and res["to"] == head + 1 and res["direction"] == "up"
        assert migrate._table_exists(conn, "price_alerts")
        assert migrate.current_version(conn) == head + 1
        assert core_db.get_db_meta("version") == str(head + 1)
        assert res["backup"] and os.path.exists(res["backup"])
        # ledger row written
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=?",
                            (head + 1,)).fetchone()[0] == 1

    def test_down_reverts_and_drops_ledger_row(self, conn):
        head = migrate.head_version()
        migrate.apply(conn, target=head + 1, migrations=_with_next())
        res = migrate.apply(conn, target=head, migrations=_with_next())
        assert res["direction"] == "down" and res["to"] == head
        assert not migrate._table_exists(conn, "price_alerts")
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=?",
                            (head + 1,)).fetchone()[0] == 0
        assert core_db.get_db_meta("version") == str(head)

    def test_irreversible_down_errors_and_leaves_db_intact(self, conn):
        head = migrate.head_version()
        migrate.apply(conn, target=head + 1, migrations=_with_next(reversible=False))
        res = migrate.apply(conn, target=head, migrations=_with_next(reversible=False))
        assert "error" in res
        # DB untouched: still at head+1 with the table present
        assert migrate.current_version(conn) == head + 1
        assert migrate._table_exists(conn, "price_alerts")

    def test_up_noop_when_current(self, conn):
        res = migrate.apply(conn, target=migrate.head_version())   # already at head
        assert res["direction"] == "none" and not res["steps"] and res["backup"] is None


# ── bootstrap_ledger (the freshly-pulled-prod case) ──────────────────────────────
class TestBootstrap:
    def test_backfills_from_version_stamp_without_rerunning(self, conn):
        # Simulate a prod DB pulled in: real v2 schema, version stamped, but no ledger rows.
        conn.execute("DELETE FROM schema_migrations")
        conn.execute("INSERT OR REPLACE INTO db_meta (key, value) VALUES ('version', '2')")
        conn.commit()
        migrate.bootstrap_ledger(conn)
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        assert sorted(r[0] for r in rows) == [2]
        assert migrate.current_version(conn) == 2

    def test_noop_when_ledger_already_populated(self, conn):
        before = conn.execute("SELECT version, applied_at FROM schema_migrations").fetchall()
        migrate.bootstrap_ledger(conn)
        after = conn.execute("SELECT version, applied_at FROM schema_migrations").fetchall()
        assert before == after   # untouched


# ── `migrate.py new` scaffolding (the CLI authoring path) ────────────────────────
# Import the root CLI module (distinct from core.migrate). All scaffold tests target
# tmp dirs / a tmp db.py copy, so they never create a real migration or bump the repo.
import migrate as cli   # noqa: E402  (top-level migrate.py, not core.migrate)


class TestScaffold:
    def test_slugify(self):
        assert cli._slugify("Add Price Alerts!") == "add-price-alerts"
        assert cli._slugify("  foo__bar  ") == "foo-bar"

    def test_next_version_scans_dir(self, tmp_path):
        (tmp_path / "002_baseline.py").write_text("VERSION = 2\n")
        (tmp_path / "__init__.py").write_text("")          # ignored (no NNN_ prefix)
        assert cli._next_version(str(tmp_path)) == 3

    def test_render_template_compiles(self):
        for data in (False, True):
            src = cli._render_template(3, "add-foo", data)
            assert "VERSION = 3" in src
            compile(src, "<scaffold>", "exec")             # valid Python

    def test_data_template_is_data_flavoured(self):
        src = cli._render_template(7, "seed-x", data=True)
        assert "data migration" in src                     # flavour marker
        assert "INSERT OR IGNORE" in src                   # seed hint present
        assert "VERSION = 7" in src

    def test_scaffold_writes_file_and_bumps_version(self, tmp_path):
        migdir = tmp_path / "migrations"
        migdir.mkdir()
        (migdir / "002_baseline.py").write_text("VERSION = 2\n")
        db_py = tmp_path / "db.py"
        db_py.write_text("X = 0\nDB_SCHEMA_VERSION = 2\nY = 1\n")
        rc = cli._scaffold("add price alerts", data=False,
                           migrations_dir=str(migdir), db_py=str(db_py))
        assert rc == 0
        created = migdir / "003_add_price_alerts.py"
        assert created.exists()
        compile(created.read_text(), "<f>", "exec")        # scaffold is valid Python
        assert "VERSION = 3" in created.read_text()
        assert "DB_SCHEMA_VERSION = 3" in db_py.read_text()  # bumped in lock-step

    def test_scaffold_refuses_when_target_exists(self, tmp_path, monkeypatch):
        # The os.path.exists guard is defensive (normal flow can't collide, since
        # _next_version always advances past existing files). Pin the version to force it.
        migdir = tmp_path / "migrations"
        migdir.mkdir()
        db_py = tmp_path / "db.py"
        db_py.write_text("DB_SCHEMA_VERSION = 2\n")
        monkeypatch.setattr(cli, "_next_version", lambda d: 3)
        (migdir / "003_dup.py").write_text("VERSION = 3\n")   # pre-existing target
        assert cli._scaffold("dup", migrations_dir=str(migdir), db_py=str(db_py)) == 1
        assert "DB_SCHEMA_VERSION = 2" in db_py.read_text()   # not bumped on refusal

    def test_scaffold_rejects_empty_slug(self, tmp_path):
        migdir = tmp_path / "migrations"
        migdir.mkdir()
        db_py = tmp_path / "db.py"
        db_py.write_text("DB_SCHEMA_VERSION = 2\n")
        assert cli._scaffold("!!!", migrations_dir=str(migdir), db_py=str(db_py)) == 1


# ── 003 backfill — the pure added-date → close lookup (network path not unit-tested) ─────
class TestBackfillMonitorMigration:
    def _mod(self):
        import importlib
        return importlib.import_module('migrations.003_backfill_monitor_from_added_date')

    @staticmethod
    def _ms(y, mo, d):
        from datetime import datetime, timezone
        return int(datetime(y, mo, d, tzinfo=timezone.utc).timestamp() * 1000)

    def test_close_on_or_before(self):
        m = self._mod()
        series = [(self._ms(2026, 1, 10), 10.0),
                  (self._ms(2026, 1, 12), 12.0),
                  (self._ms(2026, 1, 20), 20.0)]
        assert m._close_on_or_before(series, '2026-01-15') == 12.0   # last on/before the 15th
        assert m._close_on_or_before(series, '2026-01-12') == 12.0   # exact day included
        assert m._close_on_or_before(series, '2026-01-25') == 20.0   # after last -> last close
        assert m._close_on_or_before(series, '2026-01-05') is None   # predates the history
        assert m._close_on_or_before(series, 'not-a-date') is None
        assert m._close_on_or_before([], '2026-01-15') is None

    def test_is_a_data_migration(self):
        m = self._mod()
        assert m.VERSION == 3 and 'monitor_price' in m.DESCRIPTION


# ── prune_backups ───────────────────────────────────────────────────────────────
class TestPrune:
    def test_keeps_newest_n(self, db):
        # craft 6 backup files in the backup/ subdir; prune to keep 2
        bdir = migrate._backup_dir()
        os.makedirs(bdir, exist_ok=True)
        base = os.path.basename(core_db.DATABASE)
        for i in range(6):
            p = os.path.join(bdir, f"{base}.premigrate-v2-to-v3-2026010{i}-000000")
            open(p, "w").close()
            os.utime(p, (i, i))   # ascending mtime
        removed = migrate.prune_backups(keep=2)
        remaining = glob.glob(os.path.join(bdir, f"{base}.premigrate-*"))
        assert len(remaining) == 2 and len(removed) == 4
        # the two newest (highest mtime) survive
        survivors = sorted(os.path.basename(p) for p in remaining)
        assert survivors[-1].endswith("20260105-000000")
