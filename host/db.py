"""magi host settings DB — a tiny SQLite store for COMMON (cross-function) settings.

This is the host's own store, separate from every function's DB (federated model:
each function owns its settings; the host owns global ones like the theme). Lives at
<root>/data/magi.db (override with MAGI_DATA_DIR). Schema is a key/value `settings`
table + a `meta` table stamping schema + app version; `ensure_schema()` is idempotent
(create-if-missing), so a restart after deploy brings it up without a migration step.
"""
import os
import sqlite3

from host.version import full_version

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("MAGI_DATA_DIR") or os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "magi.db")
SCHEMA_VERSION = 1

# Known common settings + their allowed values (None = any string). The API only
# accepts keys listed here, so the host store stays a deliberate, validated surface.
ALLOWED = {
    "theme": {"dark", "light", "system"},
}
DEFAULTS = {
    "theme": "dark",
}


def _connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema():
    """Create the tables if missing and stamp the schema + app version. Idempotent."""
    conn = _connect()
    try:
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                  (str(SCHEMA_VERSION),))
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('app_version', ?)",
                  (full_version(),))
        conn.commit()
    finally:
        conn.close()


def get_setting(key, default=None):
    conn = _connect()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is not None:
            return row["value"]
        return default if default is not None else DEFAULTS.get(key)
    finally:
        conn.close()


def set_setting(key, value):
    conn = _connect()
    try:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
    finally:
        conn.close()


def all_settings():
    """Stored settings merged over DEFAULTS, so the client always gets a full set."""
    merged = dict(DEFAULTS)
    conn = _connect()
    try:
        for r in conn.execute("SELECT key, value FROM settings").fetchall():
            merged[r["key"]] = r["value"]
    finally:
        conn.close()
    return merged


def is_valid(key, value):
    if key not in ALLOWED:
        return False
    allowed = ALLOWED[key]
    return allowed is None or value in allowed
