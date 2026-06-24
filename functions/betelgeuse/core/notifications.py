"""Telegram notifications + the recurring-notification scheduler.

Flask-free so the background worker (worker.py) can own the live scheduler. The
web layer re-exports these and calls the build/send/reschedule helpers from its
route handlers; only the worker actually starts `scheduler`.
"""
import os
import sqlite3
from datetime import datetime

import requests
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from core.config import MARKETS
from core.db import get_db_connection
from core.logging_setup import get_logger

logger = get_logger('notifications')

# Background scheduler for recurring notifications. Started by the worker only.
scheduler = BackgroundScheduler(daemon=True)


def _host_settings_db():
    """Path to magi's APP-WIDE settings DB, if betelgeuse is running inside magi.

    Telegram credentials were promoted out of betelgeuse into the host (Settings ->
    Tools -> Telegram), so the bot is shared across magi functions. Both the web
    process (magi.py sets MAGI_HOST_DB) and the standalone worker resolve it: env
    MAGI_HOST_DB -> MAGI_DATA_DIR/magi.db -> the vendored layout (<root>/data/magi.db,
    three dirs up from this file). Returns None when not found, so STANDALONE betelgeuse
    (and pytest) fall back to its own settings table and behave exactly as before.
    """
    path = os.environ.get('MAGI_HOST_DB')
    if not path:
        data_dir = os.environ.get('MAGI_DATA_DIR')
        if data_dir:
            path = os.path.join(data_dir, 'magi.db')
        else:
            path = os.path.abspath(os.path.join(
                os.path.dirname(__file__), '..', '..', '..', 'data', 'magi.db'))
    return path if path and os.path.exists(path) else None


def _read_telegram_credentials():
    """(token, chat_id) for the bot — from magi's host DB when available, else from
    betelgeuse's own settings table (standalone fallback)."""
    host_db = _host_settings_db()
    if host_db:
        conn = sqlite3.connect(host_db)
        conn.row_factory = sqlite3.Row
    else:
        conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('SELECT key, value FROM settings WHERE key IN (?,?)',
                  ('telegram_bot_token', 'telegram_chat_id'))
        cfg = {row['key']: row['value'] for row in c.fetchall()}
    finally:
        conn.close()
    return (cfg.get('telegram_bot_token') or '').strip(), (cfg.get('telegram_chat_id') or '').strip()


def send_telegram_message(text):
    """Send a message via the app-wide Telegram bot. Returns (ok, error_string).

    Credentials live in magi's host settings DB (Settings -> Tools -> Telegram) so the
    bot is shared across functions; betelgeuse is a consumer, not the host."""
    token, chat_id = _read_telegram_credentials()
    if not token or not chat_id:
        return False, 'Telegram not configured — set Bot Token and Chat ID in magi → Settings → Tools → Telegram'
    try:
        resp = requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'},
            timeout=10
        )
        data = resp.json()
        if resp.status_code == 200 and data.get('ok'):
            return True, None
        return False, data.get('description', f'HTTP {resp.status_code}')
    except Exception as e:
        return False, str(e)


def _build_portfolio_message(markets_filter=None):
    """Build the portfolio summary Telegram message string.
    Returns (message_str, total_count) — message is None if portfolio is empty."""
    if markets_filter is None:
        markets_filter = list(MARKETS.keys())
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('SELECT symbol, market, name, "group" FROM portfolio ORDER BY market, "group", symbol')
        rows = c.fetchall()
    finally:
        conn.close()

    by_market = {}
    for row in rows:
        if row['market'] in markets_filter:
            by_market.setdefault(row['market'], []).append(row)

    if not by_market:
        return None, 0

    LABELS = {'hk': '🇭🇰 HK', 'jp': '🇯🇵 Japan', 'us': '🇺🇸 US', 'crypto': '🟠 Crypto'}
    lines = ['<b>📊 Your Betelgeuse Portfolio</b>', '']
    total = 0
    for market in ['hk', 'jp', 'us', 'crypto']:
        if market not in by_market:
            continue
        items = by_market[market]
        total += len(items)
        parts = [f"{r['symbol']} ({r['group'] or 'Default'})" for r in items]
        lines.append(f"{LABELS[market]}: {', '.join(parts)}")
    lines += ['', f'<i>Total: {total} position{"s" if total != 1 else ""}</i>']
    return '\n'.join(lines), total


def _record_portfolio_sent():
    """Persist the last-sent timestamp after a successful portfolio notification."""
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)',
                  ('notification_portfolio_last_sent', datetime.now().isoformat()))
        conn.commit()
    finally:
        conn.close()


def send_scheduled_portfolio_notification():
    """Called by APScheduler — builds and sends the portfolio summary on schedule."""
    logger.info("[scheduler] Sending scheduled portfolio notification at %s", datetime.now())
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('SELECT value FROM settings WHERE key=?', ('notification_portfolio_markets',))
        row = c.fetchone()
        markets_str = row['value'] if row else ''
    finally:
        conn.close()
    markets = [m.strip() for m in markets_str.split(',') if m.strip()] if markets_str else list(MARKETS.keys())

    message, total = _build_portfolio_message(markets)
    if message is None:
        logger.info("[scheduler] No portfolio items — skipping send")
        return
    ok, err = send_telegram_message(message)
    if ok:
        _record_portfolio_sent()
        logger.info("[scheduler] Sent portfolio notification (%d positions)", total)
    else:
        logger.error("[scheduler] Send failed: %s", err)


def _compute_schedule_next_runs(cfg):
    """Next fire time for each configured send time, computed straight from the
    cron settings (no running scheduler needed).

    The live scheduler runs in the worker process, so the web process can't read
    job.next_run_time off it. CronTrigger.get_next_fire_time() is deterministic
    given the schedule, so we derive the same upcoming runs from settings alone —
    keeping the Notifications UI accurate regardless of which process schedules.
    """
    if cfg.get('notification_portfolio_enabled') != 'true':
        return []
    times_str = (cfg.get('notification_portfolio_times') or '').strip()
    if not times_str:
        return []
    days_str = cfg.get('notification_portfolio_days') or 'mon,tue,wed,thu,fri'
    tz_name = cfg.get('default_timezone') or 'Australia/Sydney'
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.timezone('Australia/Sydney')

    now = datetime.now(tz)
    runs = []
    for time_str in [t.strip() for t in times_str.split(',') if t.strip()][:3]:
        try:
            parts = time_str.split(':')
            hour, minute = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            continue
        trigger = CronTrigger(day_of_week=days_str, hour=hour, minute=minute, timezone=tz)
        nxt = trigger.get_next_fire_time(None, now)
        if nxt:
            runs.append(nxt.isoformat())
    runs.sort()
    return runs


def reschedule_portfolio_notifications():
    """Read schedule settings from DB and update APScheduler jobs.

    No-op unless this process owns a running scheduler — in the split web/worker
    setup only the worker starts one, so calling this from the web process (e.g.
    on settings save) harmlessly returns and the worker picks the change up via
    its DB-driven reschedule loop.
    """
    if not scheduler.running:
        return
    # Remove old jobs
    for job in list(scheduler.get_jobs()):
        if job.id.startswith('portfolio_notify_'):
            job.remove()

    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('SELECT key, value FROM settings WHERE key IN (?,?,?,?)',
                  ('notification_portfolio_enabled', 'notification_portfolio_days',
                   'notification_portfolio_times', 'default_timezone'))
        cfg = {row['key']: row['value'] for row in c.fetchall()}
    finally:
        conn.close()

    if cfg.get('notification_portfolio_enabled') != 'true':
        logger.info("[scheduler] Portfolio notifications disabled — no jobs scheduled")
        return

    times_str = cfg.get('notification_portfolio_times', '').strip()
    if not times_str:
        return

    days_str = cfg.get('notification_portfolio_days', 'mon,tue,wed,thu,fri')
    tz_name = cfg.get('default_timezone', 'Australia/Sydney')
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.timezone('Australia/Sydney')

    times = [t.strip() for t in times_str.split(',') if t.strip()]
    for i, time_str in enumerate(times[:3]):
        try:
            parts = time_str.split(':')
            hour, minute = int(parts[0]), int(parts[1])
            scheduler.add_job(
                send_scheduled_portfolio_notification,
                CronTrigger(day_of_week=days_str, hour=hour, minute=minute, timezone=tz),
                id=f'portfolio_notify_{i}',
                replace_existing=True,
                misfire_grace_time=300
            )
            logger.info("[scheduler] Scheduled: %s @ %s %s", days_str, time_str, tz_name)
        except Exception as e:
            logger.error("[scheduler] Error scheduling at %s: %s", time_str, e)
