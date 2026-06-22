"""SQLite connection + schema init. The single source of truth for the DB path.

Tests monkeypatch `core.db.DATABASE` to point at a throwaway file, and because
`get_db_connection`/`init_db` read the module global, that redirect is honoured
everywhere the connection is opened.
"""
import os
import sqlite3

from core import config

# The SQLite database file. Resolved from config.DB_PATH (the runtime data root,
# config.DATA_DIR) so it never depends on the current working directory. Tests
# monkeypatch this module global to a throwaway tmp file; migrate.py reads it
# dynamically, so its backups follow the DB into the data dir automatically.
DATABASE = config.DB_PATH


def _ensure_db_dir():
    """Make sure the directory holding the DB exists (so sqlite can create the file)."""
    d = os.path.dirname(DATABASE)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)

# The schema version the code expects. Bump this in lock-step with adding a new
# migrations/00N_*.py file (its VERSION must equal this; a test enforces head==const).
# core.migrate compares the DB's applied version against this to gate startup and to
# plan up/down migrations. It is NOT applied silently on startup — see init_db().
DB_SCHEMA_VERSION = 4
_DB_SCHEMA_DESCRIPTION = (
    "portfolio (+ monitor_price, trigger_price, bought), settings, static-data "
    "caches (hk_securities, coingecko_coins, us_securities), OHLCV caches "
    "(crypto_ohlcv, stock_ohlcv), transactions (buy/sell ledger keyed by market+symbol)."
)


def get_db_connection():
    """Get database connection"""
    _ensure_db_dir()
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    # Wait up to 5s for a competing writer (the worker) instead of failing
    # immediately with "database is locked".
    conn.execute('PRAGMA busy_timeout=5000')
    return conn


def get_db_meta(key, default=None):
    """Read a single value from db_meta. Returns default when the key is absent."""
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('SELECT value FROM db_meta WHERE key=?', (key,))
        row = c.fetchone()
        return row['value'] if row else default
    finally:
        conn.close()


def set_db_meta(key, value):
    """Upsert a key/value in db_meta, updating updated_at to now."""
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO db_meta (key, value, updated_at) "
            "VALUES (?, ?, datetime('now'))",
            (key, value)
        )
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Ensure the DB exists with its framework infra in place. **Does NOT migrate an
    existing populated DB** — that is the explicit, backed-up job of `core.migrate`
    (run via `migrate.py` / the dev panel), gated by the boot version-check.

    - **Fresh DB** (file absent): build the schema by running every migration from scratch
      (baseline → head) and stamp the ledger. Nothing to lose, so no backup/guard.
    - **Existing DB**: only ensure `db_meta` + `schema_migrations` exist and bootstrap the
      ledger from the version stamp (no schema mutation). A version mismatch is surfaced by
      the entrypoints' boot guard, not silently patched here.
    """
    from core import migrate  # lazy: core.migrate imports core.db, so avoid an import cycle

    _ensure_db_dir()
    fresh = not os.path.exists(DATABASE)
    conn = sqlite3.connect(DATABASE)
    try:
        # WAL lets the web process and the background worker share this DB file
        # concurrently (single writer, many readers) — a persistent file property.
        conn.execute('PRAGMA journal_mode=WAL')
        c = conn.cursor()
        migrate.ensure_infra(c)
        # description is INSERT OR IGNORE: seed only when absent, preserving a user edit.
        c.execute("INSERT OR IGNORE INTO db_meta (key, value) VALUES ('description', ?)",
                  (_DB_SCHEMA_DESCRIPTION,))
        conn.commit()
        if fresh:
            # Build from migrations so a brand-new DB is identical to running them in order.
            migrate.apply(conn, target=None, do_backup=False, prune=False)
        else:
            # Existing DB: join the framework without re-running history; re-sync the
            # display version from the ledger. The boot guard handles any mismatch.
            migrate.bootstrap_ledger(conn)
        conn.commit()
    finally:
        conn.close()
