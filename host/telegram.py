"""magi host Telegram service — the app-wide notification bot.

Promoted out of betelgeuse so EVERY magi function can send Telegram messages through
one shared bot. The bot token + chat id are GLOBAL host settings (data/magi.db, edited
on Settings -> Tools -> Telegram); functions are consumers. Betelgeuse reads the same
credentials from the host DB (see functions/betelgeuse/core/notifications.py) instead of
hosting its own.

Uses stdlib urllib (the host deliberately avoids the `requests` dependency — same rule
as /api/prod/health). All calls return (value, error) tuples; never raise to the route.
"""
import json
import urllib.error
import urllib.request

from host import db as hostdb

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 10


def get_config():
    """Current bot token + chat id from the host settings DB (both may be empty)."""
    return {
        "telegram_bot_token": (hostdb.get_setting("telegram_bot_token") or "").strip(),
        "telegram_chat_id": (hostdb.get_setting("telegram_chat_id") or "").strip(),
    }


def is_configured():
    cfg = get_config()
    return bool(cfg["telegram_bot_token"] and cfg["telegram_chat_id"])


def _call(token, method, *, params=None, post=False):
    """One Telegram Bot API call. Returns (data_dict, error_string)."""
    url = API.format(token=token, method=method)
    try:
        if post:
            req = urllib.request.Request(
                url, data=json.dumps(params or {}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
        else:
            req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        # Telegram returns a JSON body with a `description` even on 4xx — surface it.
        try:
            data = json.loads(e.read().decode("utf-8"))
            return None, data.get("description", f"HTTP {e.code}")
        except Exception:  # noqa: BLE001
            return None, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def send_message(text, parse_mode="HTML"):
    """Send a message via the configured bot. Returns (ok: bool, error: str|None)."""
    cfg = get_config()
    token, chat_id = cfg["telegram_bot_token"], cfg["telegram_chat_id"]
    if not token or not chat_id:
        return False, ("Telegram not configured — set Bot Token and Chat ID in "
                       "Settings → Tools → Telegram")
    data, err = _call(token, "sendMessage", post=True,
                      params={"chat_id": chat_id, "text": text, "parse_mode": parse_mode})
    if err:
        return False, err
    if data.get("ok"):
        return True, None
    return False, data.get("description", "Telegram rejected the message")


def test():
    """Send a connectivity test message. Returns (ok, error)."""
    return send_message("🐱 magi is connected! Telegram notifications are working.")


def detect_chat_id():
    """Poll getUpdates to auto-detect the most recent chat id (user must have sent the
    bot /start first). Returns (chat_id: str|None, error: str|None)."""
    token = get_config()["telegram_bot_token"]
    if not token:
        return None, "Bot token not saved yet — save it first then retry"
    data, err = _call(token, "getUpdates")
    if err:
        return None, f"Could not reach Telegram: {err}"
    results = data.get("result", [])
    if not results:
        return None, ("No messages found — open Telegram, send /start to your bot, "
                      "then click Auto-detect again")
    for update in reversed(results):
        msg = update.get("message") or update.get("channel_post")
        if msg and msg.get("chat", {}).get("id"):
            return str(msg["chat"]["id"]), None
    return None, "Could not extract chat id from recent messages"
