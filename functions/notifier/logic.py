"""Notifier function — core logic (Flask-free, self-contained).

Personal reminders: a free-text message sent to Telegram on a recurring schedule
(same day/time/timezone model as betelgeuse's portfolio notifications). This module
owns its own SQLite store and a Telegram send helper; it is the interface the shared
magi worker (worker.py at the repo root) drives on a schedule.

Isolation contract (like youtube/taxation): this NEVER imports the host. It reads the
APP-WIDE bot credentials straight from magi's settings DB file and the per-env enable
gate from the env's scope DB — as plain files, resolved by env var / relative layout —
so both the in-process web app and the standalone worker behave identically without a
host import. Importing this module touches NO filesystem or network: ensure_schema()
runs lazily on first DB access, and apscheduler/pytz/truststore are imported lazily
inside the functions that need them (keeps `import logic` cheap for the web path).
"""
import json
import logging
import os
import sqlite3
import ssl
import urllib.error
import urllib.request
from datetime import datetime

logger = logging.getLogger("magi.notifier")

# This function's own store (separate from the host DB) — created lazily at first use.
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "notifier.db")

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 10

# Telegram HTML parse-mode tags we advertise in the UI + allow in the preview.
ALLOWED_TAGS = ("b", "i", "u", "s", "code", "pre", "a", "tg-spoiler", "blockquote")

# Reminder config — a key/value settings table, defaults applied on read.
DEFAULTS = {
    "reminder_text": "",
    "reminder_enabled": "0",
    "reminder_days": "mon,tue,wed,thu,fri",
    "reminder_times": "",
    "reminder_timezone": "Australia/Sydney",
    "reminder_last_sent": "",
}


# ---- this function's own store -------------------------------------------------------

def _connect():
    """Open notifier.db, creating the dir + schema lazily (never at import)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    return conn


def get_config():
    """All reminder settings as a flat dict, defaults filled in."""
    conn = _connect()
    try:
        rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
    finally:
        conn.close()
    return {k: rows.get(k, d) for k, d in DEFAULTS.items()}


def save_config(values):
    """Upsert the given subset of reminder settings (only known keys)."""
    conn = _connect()
    try:
        for key, value in values.items():
            if key not in DEFAULTS:
                continue
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                         (key, "" if value is None else str(value)))
        conn.commit()
    finally:
        conn.close()


# ---- app-wide bot credentials + per-env enable gate (read from the host DB files) ----

def _host_data_dir():
    """magi's data/ dir (holds magi.db + magiscope.<env>.db). MAGI_DATA_DIR wins, else
    the dir of MAGI_HOST_DB, else the vendored relative layout (two dirs up from here)."""
    d = os.environ.get("MAGI_DATA_DIR")
    if d:
        return d
    host_db = os.environ.get("MAGI_HOST_DB")
    if host_db:
        return os.path.dirname(host_db)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data"))


def _env():
    return os.environ.get("MAGI_ENV", "dev")


def _read_kv(path, *keys):
    """Read keys from a magi settings DB file (read-only); {} if the file/table is absent."""
    if not os.path.exists(path):
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            q = "SELECT key, value FROM settings WHERE key IN (%s)" % ",".join("?" * len(keys))
            return {r["key"]: r["value"] for r in conn.execute(q, keys)}
        finally:
            conn.close()
    except sqlite3.Error:
        return {}


def _read_credentials():
    """The magi-control bot's own token + chat id from magi.db (Tools → Telegram → magi
    control). The Notifier has its OWN bot, separate from betelgeuse's."""
    cfg = _read_kv(os.path.join(_host_data_dir(), "magi.db"),
                   "telegram_magi_bot_token", "telegram_magi_chat_id")
    return ((cfg.get("telegram_magi_bot_token") or "").strip(),
            (cfg.get("telegram_magi_chat_id") or "").strip())


def gate_enabled():
    """Whether magi's own notifications are enabled for THIS env (Tools → Telegram → magi
    control). Reads the per-env scope DB; absent/unset → disabled (the registry default is
    OFF — opt-in)."""
    val = _read_kv(os.path.join(_host_data_dir(), f"magiscope.{_env()}.db"),
                   "telegram_magi_enabled").get("telegram_magi_enabled")
    return (val or "0") == "1"


def is_configured():
    token, chat_id = _read_credentials()
    return bool(token and chat_id)


def _ssl_context():
    """Verify TLS via the OS trust store (truststore) so a TLS-intercepting proxy doesn't
    break api.telegram.org; falls back to Python's default verifying context. Never disables
    verification. (Same pattern as host/telegram.py + the taxation fn.)"""
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001
        return None


def send_message(text):
    """Send `text` (Telegram HTML) via the app-wide bot. Returns (ok, error).

    Gated by the per-env magi-control enable: off → not sent. Self-contained urllib send
    (no `requests`, no host import)."""
    if not gate_enabled():
        return False, ("magi notifications are disabled for this environment — enable them in "
                       "Settings → Tools → Telegram → magi control")
    token, chat_id = _read_credentials()
    if not token or not chat_id:
        return False, ("Telegram not configured — set Bot Token and Chat ID in "
                       "Settings → Tools → Telegram")
    url = API.format(token=token, method="sendMessage")
    body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ssl_context()) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return False, json.loads(e.read().decode("utf-8")).get("description", f"HTTP {e.code}")
        except Exception:  # noqa: BLE001
            return False, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    if data.get("ok"):
        return True, None
    return False, data.get("description", "Telegram rejected the message")


def send_now(text=None):
    """Send the reminder immediately (UI 'Send Now'). Uses `text` if given, else the saved
    reminder_text. Records last-sent on success. Returns (ok, error)."""
    msg = (text if text is not None else get_config()["reminder_text"]).strip()
    if not msg:
        return False, "Nothing to send — the reminder message is empty."
    ok, err = send_message(msg)
    if ok:
        save_config({"reminder_last_sent": datetime.now().isoformat()})
    return ok, err


def send_scheduled():
    """APScheduler job: send the saved reminder if enabled + non-empty. Records last-sent."""
    cfg = get_config()
    if cfg["reminder_enabled"] != "1":
        return
    text = (cfg["reminder_text"] or "").strip()
    if not text:
        logger.info("[notifier] reminder enabled but empty — skipping send")
        return
    ok, err = send_message(text)
    if ok:
        save_config({"reminder_last_sent": datetime.now().isoformat()})
        logger.info("[notifier] sent personal reminder")
    else:
        logger.error("[notifier] reminder send failed: %s", err)


# ---- schedule (same model as betelgeuse: days + up to 3 times + timezone) ------------

def _tz(name):
    import pytz
    try:
        return pytz.timezone(name)
    except Exception:  # noqa: BLE001
        return pytz.timezone("Australia/Sydney")


def _triggers(cfg):
    """Yield (slot_index, CronTrigger) for each configured time — shared by reschedule()
    and compute_next_runs() so the live worker and the UI agree."""
    from apscheduler.triggers.cron import CronTrigger
    days = (cfg.get("reminder_days") or "mon,tue,wed,thu,fri").strip()
    times = [t.strip() for t in (cfg.get("reminder_times") or "").split(",") if t.strip()][:3]
    tz = _tz(cfg.get("reminder_timezone") or "Australia/Sydney")
    for i, time_str in enumerate(times):
        try:
            hour, minute = (int(p) for p in time_str.split(":")[:2])
        except (ValueError, IndexError):
            continue
        yield i, CronTrigger(day_of_week=days, hour=hour, minute=minute, timezone=tz)


def compute_next_runs(cfg=None):
    """Upcoming fire times (ISO strings) from settings alone — no running scheduler needed
    (the worker owns the live one; the UI derives the same times deterministically)."""
    cfg = cfg or get_config()
    if cfg.get("reminder_enabled") != "1":
        return []
    now = datetime.now(_tz(cfg.get("reminder_timezone") or "Australia/Sydney"))
    runs = []
    for _, trigger in _triggers(cfg):
        nxt = trigger.get_next_fire_time(None, now)
        if nxt:
            runs.append(nxt.isoformat())
    runs.sort()
    return runs


def schedule_fingerprint():
    """Hashable snapshot of the timing settings — the worker reschedules when it changes."""
    cfg = get_config()
    return (cfg["reminder_enabled"], cfg["reminder_days"],
            cfg["reminder_times"], cfg["reminder_timezone"])


JOB_PREFIX = "notifier_reminder_"


def reschedule(scheduler):
    """(Re)install this function's jobs on the shared worker's scheduler from current
    settings. Removes our old jobs first, then adds one per configured time (when enabled)."""
    for job in list(scheduler.get_jobs()):
        if job.id.startswith(JOB_PREFIX):
            job.remove()
    cfg = get_config()
    if cfg.get("reminder_enabled") != "1":
        logger.info("[notifier] reminder disabled — no jobs scheduled")
        return
    count = 0
    for i, trigger in _triggers(cfg):
        scheduler.add_job(send_scheduled, trigger, id=f"{JOB_PREFIX}{i}",
                          replace_existing=True, misfire_grace_time=300)
        count += 1
    logger.info("[notifier] scheduled %d reminder job(s): days=%s times=%s tz=%s",
                count, cfg["reminder_days"], cfg["reminder_times"], cfg["reminder_timezone"])


def status():
    """Function health snapshot for the host's /health page (no network)."""
    cfg = get_config()
    return {
        "configured": is_configured(),
        "gate_enabled": gate_enabled(),
        "enabled": cfg["reminder_enabled"] == "1",
        "has_text": bool((cfg["reminder_text"] or "").strip()),
        "last_sent": cfg["reminder_last_sent"] or None,
        "next_run": (compute_next_runs(cfg) or [None])[0],
    }
