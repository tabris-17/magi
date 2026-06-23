"""magi host settings DB — a tiny SQLite store for COMMON (cross-function) settings.

This is the host's own store, separate from every function's DB (federated model:
each function owns its settings; the host owns global ones like the theme). Lives at
<root>/data/magi.db (override with MAGI_DATA_DIR). Schema is a key/value `settings`
table + a `meta` table stamping schema + app version; `ensure_schema()` is idempotent
(create-if-missing), so a restart after deploy brings it up without a migration step.

Settings are either GLOBAL (one value, e.g. theme) or ENV-SCOPED (a separate value per
environment, e.g. youtube_download_dir). Env-scoped values share the one key/value table
via a `<key>@<env>` storage key — so dev and prod keep DISTINCT values even though
`./magi upgrade dev` copies the whole magi.db prod->dev. This needs no schema change and
no migration engine (which the host deliberately doesn't have).
"""
import os
import sqlite3

from host.version import full_version

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("MAGI_DATA_DIR") or os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "magi.db")
SCHEMA_VERSION = 1

# The environments an env-scoped setting can hold values for.
ENVS = ("dev", "prod")
# This host's mode — which env-scoped value get_setting/all_settings resolve by default.
ENV = os.environ.get("MAGI_ENV", "dev")

# The host's known common settings. Each entry declares: `allowed` values (None = any
# string), a `default`, and whether it is `scoped` (a separate value per environment).
# The API only accepts keys listed here, so the store stays a deliberate, validated
# surface.
SETTINGS = {
    "theme": {"allowed": {"dark", "light", "system"}, "default": "dark", "scoped": False},
    "youtube_download_dir": {"allowed": None, "default": None, "scoped": True},
    # The taxation function's RBA source spreadsheet URL (global; same dev/prod).
    "taxation_rba_url": {
        "allowed": None,
        "default": "https://www.rba.gov.au/statistics/tables/xls-hist/2023-current.xls",
        "scoped": False,
    },
}

# Derived views kept for any external reference (the registry above is the source of truth).
ALLOWED = {k: v["allowed"] for k, v in SETTINGS.items()}
DEFAULTS = {k: v["default"] for k, v in SETTINGS.items() if v["default"] is not None}


def _is_scoped(key):
    spec = SETTINGS.get(key)
    return bool(spec and spec.get("scoped"))


def _default(key):
    spec = SETTINGS.get(key)
    return spec["default"] if spec else None


def _storage_key(key, env):
    """The row key a setting is stored under: `<key>@<env>` for env-scoped keys (so
    dev/prod coexist in one DB), the bare key for global ones."""
    return f"{key}@{env}" if _is_scoped(key) else key


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


def get_setting(key, default=None, env=None):
    """Resolve a setting's value. Env-scoped keys read the `<key>@<env>` row (env
    defaults to this host's ENV); global keys read the bare key. Falls back to the
    caller's default, then the registry default."""
    skey = _storage_key(key, env or ENV)
    conn = _connect()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (skey,)).fetchone()
        if row is not None:
            return row["value"]
        return default if default is not None else _default(key)
    finally:
        conn.close()


def set_setting(key, value, env=None):
    """Upsert a setting. Env-scoped keys write the `<key>@<env>` row (env defaults to
    this host's ENV); an empty value on a scoped key clears it (so it falls back to the
    default / env var). Global keys ignore `env`."""
    skey = _storage_key(key, env or ENV)
    conn = _connect()
    try:
        if _is_scoped(key) and not (value or "").strip():
            conn.execute("DELETE FROM settings WHERE key = ?", (skey,))
        else:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (skey, value))
        conn.commit()
    finally:
        conn.close()


def all_settings(env=None):
    """The current-env-resolved settings as a flat {key: value} map (env-scoped keys
    collapsed to their bare name), merged over defaults — the shell reads `theme` here."""
    env = env or ENV
    conn = _connect()
    try:
        rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings").fetchall()}
    finally:
        conn.close()
    return {key: rows.get(_storage_key(key, env), spec["default"])
            for key, spec in SETTINGS.items()}


def env_config(key=None):
    """Per-env values for env-scoped settings: {key: {dev: val, prod: val}} (the
    side-by-side payload for the settings page). Pass `key` to scope to one setting."""
    conn = _connect()
    try:
        rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings").fetchall()}
    finally:
        conn.close()
    keys = [key] if key else [k for k in SETTINGS]
    return {k: {e: rows.get(_storage_key(k, e), SETTINGS[k]["default"]) for e in ENVS}
            for k in keys if _is_scoped(k)}


def is_valid(key, value):
    spec = SETTINGS.get(key)
    if spec is None:
        return False
    allowed = spec["allowed"]
    return allowed is None or value in allowed
