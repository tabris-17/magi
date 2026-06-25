"""magi host settings store — split into GLOBAL + per-ENV SCOPED SQLite files so deploy
can treat them differently.

  * data/magi.db           — GLOBAL settings (one value, same dev/prod: theme, telegram,
                             taxation URL). PROD is the source of truth; `./magi upgrade dev`
                             copies it prod->dev.
  * data/magiscope.dev.db  — ENV-SCOPED ("user profile" / per-machine) settings for the DEV
                             environment. Dev OWNS this and it is NEVER synced, so a deploy
                             can't wipe a value you set on dev.
  * data/magiscope.prod.db — the same, for PROD. Prod is the source of truth; `upgrade dev`
                             mirrors it prod->dev so dev can SEE prod's scoped values, while
                             dev's own keep living in magiscope.dev.db.

`MAGI_ENV` picks which scope DB a scoped key resolves against by default; passing `env=...`
targets a specific one (the dev/prod side-by-side path). Scoped keys are stored under the
BARE key in their per-env file — the file *is* the env, so there's no `<key>@<env>` suffix
anymore. `ensure_schema()` is idempotent. No migration engine (the host deliberately has none).
"""
import os
import sqlite3

from host.version import full_version

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("MAGI_DATA_DIR") or os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "magi.db")  # GLOBAL settings live here
SCHEMA_VERSION = 2

# The environments a scoped setting can hold values for (one scope DB each).
ENVS = ("dev", "prod")
# This host's mode — which scope DB get_setting/all_settings resolve against by default.
ENV = os.environ.get("MAGI_ENV", "dev")


def scope_db_path(env):
    """Per-environment scoped-settings DB: data/magiscope.<env>.db."""
    return os.path.join(DATA_DIR, f"magiscope.{env}.db")


# The host's known common settings. Each entry declares: `allowed` values (None = any
# string), a `default`, and whether it is `scoped` (lives in the per-env scope DB). The
# API only accepts keys listed here, so the store stays a deliberate, validated surface.
SETTINGS = {
    "theme": {"allowed": {"dark", "light", "system"}, "default": "dark", "scoped": False},
    "youtube_download_dir": {"allowed": None, "default": None, "scoped": True},
    # YouTube download options — scoped "0"/"1" toggles, remembered per dev/prod and used
    # as the default checkbox state on the YouTube page (a per-download toggle still
    # overrides for that one download).
    "youtube_date_prefix": {"allowed": {"0", "1"}, "default": "1", "scoped": True},
    "youtube_write_meta": {"allowed": {"0", "1"}, "default": "1", "scoped": True},
    # The taxation function's RBA source spreadsheet URL (global; same dev/prod).
    "taxation_rba_url": {
        "allowed": None,
        "default": "https://www.rba.gov.au/statistics/tables/xls-hist/2023-current.xls",
        "scoped": False,
    },
    # App-wide Telegram notification bot (global; same token/chat across functions).
    # `secret` keeps the token out of the broadcast /api/settings payload (it's read
    # server-side on the Tools -> Telegram page instead). chat_id is not a secret.
    "telegram_bot_token": {"allowed": None, "default": None, "scoped": False, "secret": True},
    "telegram_chat_id": {"allowed": None, "default": None, "scoped": False},
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


def _path_for(key, env):
    """Which DB file a setting lives in: a scoped key -> its env's scope DB; a global
    key -> magi.db."""
    return scope_db_path(env or ENV) if _is_scoped(key) else DB_PATH


def _connect(path):
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_settings_table(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")


def ensure_schema():
    """Create magi.db (global) + both magiscope.<env>.db files if missing; stamp magi.db's
    meta. Also drop any legacy `<key>@<env>` rows from magi.db (scoped values moved to the
    per-env scope DBs). Idempotent."""
    conn = _connect(DB_PATH)
    try:
        _ensure_settings_table(conn)
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('app_version', ?)",
                     (full_version(),))
        # Legacy cleanup: scoped values used to live here as `<key>@<env>`; they're now in
        # magiscope.<env>.db. Purge the stale rows (intentionally discarded — re-entered per box).
        conn.execute("DELETE FROM settings WHERE key LIKE '%@dev' OR key LIKE '%@prod'")
        conn.commit()
    finally:
        conn.close()
    for env in ENVS:
        conn = _connect(scope_db_path(env))
        try:
            _ensure_settings_table(conn)
            conn.commit()
        finally:
            conn.close()


def get_setting(key, default=None, env=None):
    """Resolve a setting's value from its file (scoped -> the env's scope DB, env defaults
    to this host's ENV; global -> magi.db). Falls back to the caller's default, then the
    registry default."""
    conn = _connect(_path_for(key, env))
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is not None:
            return row["value"]
        return default if default is not None else _default(key)
    finally:
        conn.close()


def set_setting(key, value, env=None):
    """Upsert a setting into its file. An empty value on a scoped key clears it (so it falls
    back to the default / env var). `env` selects the scope DB (defaults to this host's ENV);
    global keys ignore it."""
    conn = _connect(_path_for(key, env))
    try:
        if _is_scoped(key) and not (value or "").strip():
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
    finally:
        conn.close()


def _read_all(path):
    if not os.path.exists(path):
        return {}
    conn = _connect(path)
    try:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings").fetchall()}
    finally:
        conn.close()


def all_settings(env=None):
    """The env-resolved settings as a flat {key: value} map, merged over defaults — the shell
    reads `theme` here. Global keys come from magi.db; scoped keys from the env's scope DB.
    Secret keys are excluded (not broadcast in the /api/settings payload)."""
    glob = _read_all(DB_PATH)
    scoped = _read_all(scope_db_path(env or ENV))
    out = {}
    for key, spec in SETTINGS.items():
        if spec.get("secret"):
            continue
        src = scoped if spec.get("scoped") else glob
        out[key] = src.get(key, spec["default"])
    return out


def env_config(key=None):
    """Per-env values for scoped settings: {key: {dev: val, prod: val}} (the side-by-side
    payload). Each env's value comes from its own scope DB. Pass `key` to scope to one."""
    per_env = {e: _read_all(scope_db_path(e)) for e in ENVS}
    keys = [key] if key else list(SETTINGS)
    return {k: {e: per_env[e].get(k, SETTINGS[k]["default"]) for e in ENVS}
            for k in keys if _is_scoped(k)}


def is_valid(key, value):
    spec = SETTINGS.get(key)
    if spec is None:
        return False
    allowed = spec["allowed"]
    return allowed is None or value in allowed
