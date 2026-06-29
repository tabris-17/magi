"""magi host Telegram service — per-consumer notification bots.

Each consumer (magi control, betelgeuse) has its OWN bot: a `telegram_<consumer>_bot_token`
+ `telegram_<consumer>_chat_id` pair in the host settings DB (data/magi.db), edited on
Settings → Tools → Telegram → <consumer>. This module is the connection/test helper behind
those pages; the actual notification senders live in each consumer (the Notifier's
`functions/notifier/logic.py`, betelgeuse's `core/notifications.py`) and read the same keys.

Uses stdlib urllib (the host deliberately avoids the `requests` dependency — same rule
as /api/prod/health). All calls return (value, error) tuples; never raise to the route.
"""
import json
import ssl
import urllib.error
import urllib.request

from host import db as hostdb

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 10

# The consumers that own a bot (each maps to telegram_<consumer>_{bot_token,chat_id}).
CONSUMERS = ("magi", "betelgeuse")


def _ssl_context():
    """Verify TLS against the OS trust store via truststore, so a corporate/firewall
    TLS-intercepting proxy whose self-signed root CA the OS already trusts doesn't break
    api.telegram.org. Falls back to Python's default verifying context when truststore
    isn't installed. Verification is NEVER disabled. (Same pattern as the taxation fn.)"""
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001
        return None  # urlopen(context=None) uses the default verifying context


def _keys(consumer):
    return f"telegram_{consumer}_bot_token", f"telegram_{consumer}_chat_id"


def get_config(consumer):
    """One consumer's bot token + chat id from the host settings DB (both may be empty)."""
    tok_key, chat_key = _keys(consumer)
    return {
        "bot_token": (hostdb.get_setting(tok_key) or "").strip(),
        "chat_id": (hostdb.get_setting(chat_key) or "").strip(),
    }


def is_configured(consumer):
    cfg = get_config(consumer)
    return bool(cfg["bot_token"] and cfg["chat_id"])


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
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ssl_context()) as resp:
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


def send_message(consumer, text, parse_mode="HTML"):
    """Send a message via one consumer's bot. Returns (ok: bool, error: str|None)."""
    cfg = get_config(consumer)
    token, chat_id = cfg["bot_token"], cfg["chat_id"]
    if not token or not chat_id:
        return False, (f"{consumer} Telegram bot not configured — set its Bot Token and Chat "
                       f"ID in Settings → Tools → Telegram → {consumer}")
    data, err = _call(token, "sendMessage", post=True,
                      params={"chat_id": chat_id, "text": text, "parse_mode": parse_mode})
    if err:
        return False, err
    if data.get("ok"):
        return True, None
    return False, data.get("description", "Telegram rejected the message")


def test(consumer):
    """Send a connectivity test message via one consumer's bot. Returns (ok, error)."""
    return send_message(
        consumer, f"🐱 magi is connected! Telegram notifications are working ({consumer} bot).")


def detect_chat_id(consumer):
    """Poll getUpdates to auto-detect the most recent chat id for one consumer's bot (user
    must have sent that bot /start first). Returns (chat_id: str|None, error: str|None)."""
    token = get_config(consumer)["bot_token"]
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
