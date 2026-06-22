"""DB schema migration engine — the single source of truth for moving a SQLite
`portfolio.db` between schema versions.

Two stores, by design (see CLAUDE.md "Schema versioning"):
  * migration **definitions** live as files in `migrations/` — git-versioned, the
    reviewable "what changed" history, shipped to prod via rsync.
  * the applied **ledger** lives in the `schema_migrations` table inside *each* DB —
    per-instance "what actually ran here, and when" (dev and prod each carry their own).

`db_meta.version` is a derived cache of `current_version()` (= MAX ledger version), kept
in sync here so `/api/health` and the Database Tool keep showing it.

This module is Flask-free. It reads `core.db.DATABASE` *dynamically* (via the `db` module
object, never a bound import) so the test suite's monkeypatched temp-DB path is honoured.
"""
import glob
import importlib
import os
import pkgutil
import sqlite3
from collections import namedtuple
from datetime import datetime

from core import db


class Irreversible(Exception):
    """Raised by a migration's down() when it cannot be safely reversed.
    The automatic pre-migration backup is the rollback path in that case."""


# version: target schema version. name: module filename. up/down: callables(cursor).
Migration = namedtuple('Migration', 'version name description up down')


def _irreversible(msg):
    def _down(_cursor):
        raise Irreversible(msg)
    return _down


def discover():
    """Load every valid migration module from `migrations/`, sorted ascending by VERSION."""
    import migrations as _pkg
    out = []
    for _finder, modname, _ispkg in pkgutil.iter_modules(_pkg.__path__):
        mod = importlib.import_module(f'migrations.{modname}')
        if not hasattr(mod, 'VERSION') or not hasattr(mod, 'up'):
            continue
        down = getattr(mod, 'down', None) or _irreversible(
            f'{modname} has no down(); restore the pre-migration backup to roll back')
        out.append(Migration(int(mod.VERSION), modname, getattr(mod, 'DESCRIPTION', ''),
                             mod.up, down))
    out.sort(key=lambda m: m.version)
    return out


def head_version(migrations=None):
    """Highest schema version known to the code (== DB_SCHEMA_VERSION; verified by a test)."""
    migs = migrations if migrations is not None else discover()
    return max((m.version for m in migs), default=0)


# ── infra / ledger ───────────────────────────────────────────────────────────

def ensure_infra(cursor):
    """Create the framework's own tables if absent. These are infrastructure (like a
    version stamp), NOT versioned migrations — the ledger can't migrate itself in —
    so they are created idempotently in both init_db() branches and by the CLI."""
    cursor.execute('''CREATE TABLE IF NOT EXISTS db_meta (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT,
        applied_at TEXT DEFAULT CURRENT_TIMESTAMP)''')


def _table_exists(conn, name):
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def current_version(conn):
    """Current schema version = MAX applied ledger version (0 when none/absent)."""
    if not _table_exists(conn, 'schema_migrations'):
        return 0
    row = conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _sync_db_meta_version(conn):
    """Mirror current_version() into db_meta.version (the fast display pointer)."""
    v = current_version(conn)
    conn.execute("INSERT OR REPLACE INTO db_meta (key, value, updated_at) "
                 "VALUES ('version', ?, datetime('now'))", (str(v),))
    return v


def bootstrap_ledger(conn, migrations=None):
    """Backfill the ledger for a DB that predates this framework (no-op once it has rows).

    A freshly-pulled prod DB, or today's existing v2 DBs, carry a `db_meta.version` stamp
    but an empty `schema_migrations`. Trust that stamp: mark every migration with VERSION
    <= the stamped version as already-applied, instead of re-running history. Then re-sync
    db_meta.version from the ledger. Returns the resulting current version.
    """
    migs = migrations if migrations is not None else discover()
    ensure_infra(conn.cursor())
    if conn.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0] > 0:
        return _sync_db_meta_version(conn)
    row = conn.execute("SELECT value FROM db_meta WHERE key='version'").fetchone()
    if row and str(row[0]).isdigit():
        base = int(row[0])
    elif _table_exists(conn, 'portfolio'):
        base = head_version(migs)   # populated but unstamped -> assume current reality
    else:
        base = 0
    for m in migs:
        if m.version <= base:
            conn.execute('INSERT OR IGNORE INTO schema_migrations (version, name, description) '
                         'VALUES (?,?,?)', (m.version, m.name, f'{m.description} (bootstrapped)'))
    conn.commit()
    return _sync_db_meta_version(conn)


# ── backups ──────────────────────────────────────────────────────────────────

def _backup_dir():
    """Directory pre-migration backups live in: a `backup/` subfolder beside the DB.

    Derived from `db.DATABASE` (read dynamically) so the test suite's monkeypatched
    temp-DB path is honoured AND prod backups land under `<DATA_DIR>/backup` — kept out
    of the DB's own directory so they don't clutter it."""
    return os.path.join(os.path.dirname(db.DATABASE) or '.', 'backup')


def _backup_path(frm, to):
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    name = f'{os.path.basename(db.DATABASE)}.premigrate-v{frm}-to-v{to}-{ts}'
    return os.path.join(_backup_dir(), name)


def backup_db(frm, to):
    """Transactionally-consistent snapshot of the live DB before a migration. Returns path."""
    os.makedirs(_backup_dir(), exist_ok=True)
    path = _backup_path(frm, to)
    src = db.get_db_connection()
    try:
        dest = sqlite3.connect(path)
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()
    return path


def prune_backups(keep=5):
    """Delete the oldest premigrate backups beyond `keep` (by mtime). Returns removed paths."""
    pattern = os.path.join(_backup_dir(), f'{os.path.basename(db.DATABASE)}.premigrate-*')
    backups = sorted(glob.glob(pattern), key=os.path.getmtime)
    removed = []
    victims = backups[:-keep] if keep > 0 else backups
    for p in victims:
        try:
            os.remove(p)
            removed.append(p)
        except OSError:
            pass
    return removed


# ── planning / status / gate ───────────────────────────────────────────────────

def plan(conn, target=None, migrations=None):
    """(direction, [Migration,...]) to move from current_version to target.

    direction ∈ 'up' | 'down' | 'none'. For 'down' the steps are ordered highest-first
    (each is reversed in turn)."""
    migs = migrations if migrations is not None else discover()
    cur = current_version(conn)
    target = head_version(migs) if target is None else int(target)
    if target == cur:
        return ('none', [])
    if target > cur:
        return ('up', sorted([m for m in migs if cur < m.version <= target],
                             key=lambda m: m.version))
    return ('down', sorted([m for m in migs if target < m.version <= cur],
                          key=lambda m: m.version, reverse=True))


def gate_state(conn, migrations=None):
    """OK (versions match) | NEEDS_UP (DB older than code) | DB_NEWER (DB ahead of code)."""
    cur = current_version(conn)
    head = head_version(migrations)
    if cur == head:
        return 'OK'
    return 'NEEDS_UP' if cur < head else 'DB_NEWER'


def status(conn, migrations=None):
    """Snapshot for the CLI / web panel: current, head, gate, and the pending list."""
    migs = migrations if migrations is not None else discover()
    cur = current_version(conn)
    pending = [{'version': m.version, 'name': m.name, 'description': m.description}
               for m in migs if m.version > cur]
    return {'current': cur, 'head': head_version(migs),
            'gate': gate_state(conn, migs), 'pending': pending}


def history(conn):
    """The applied ledger, oldest-first (drives the panel's history view)."""
    if not _table_exists(conn, 'schema_migrations'):
        return []
    rows = conn.execute('SELECT version, name, description, applied_at '
                        'FROM schema_migrations ORDER BY version').fetchall()
    return [{'version': r[0], 'name': r[1], 'description': r[2], 'applied_at': r[3]} for r in rows]


# ── the runner ─────────────────────────────────────────────────────────────────

def apply(conn, target=None, migrations=None, do_backup=True, prune=True, keep=5):
    """Move the DB to `target` (default head). Backs up first (unless do_backup=False, e.g.
    a fresh empty build). Each step runs in its OWN transaction; on the first error we stop,
    leaving the DB at the last good version with the backup intact. Returns a result dict
    `{from, to, direction, steps, backup, pruned[, error]}`.
    """
    migs = migrations if migrations is not None else discover()
    cur = current_version(conn)
    direction, steps = plan(conn, target, migs)
    result = {'from': cur, 'to': cur, 'direction': direction,
              'steps': [], 'backup': None, 'pruned': []}
    if direction == 'none' or not steps:
        return result
    if do_backup:
        result['backup'] = backup_db(cur, head_version(migs) if target is None else int(target))

    prev_iso = conn.isolation_level
    conn.isolation_level = None        # manual transaction control (DDL-safe)
    c = conn.cursor()
    try:
        for m in steps:
            try:
                c.execute('BEGIN')
                if direction == 'up':
                    m.up(c)
                    c.execute('INSERT OR REPLACE INTO schema_migrations '
                              "(version, name, description, applied_at) VALUES (?,?,?, datetime('now'))",
                              (m.version, m.name, m.description))
                else:
                    m.down(c)
                    c.execute('DELETE FROM schema_migrations WHERE version=?', (m.version,))
                _sync_db_meta_version(conn)
                c.execute('COMMIT')
                result['steps'].append({'version': m.version, 'name': m.name, 'direction': direction})
            except Exception as e:
                c.execute('ROLLBACK')
                result['error'] = f'{m.name}: {e}'
                break
    finally:
        conn.isolation_level = prev_iso
    result['to'] = current_version(conn)
    if prune and 'error' not in result:
        result['pruned'] = prune_backups(keep)
    return result
