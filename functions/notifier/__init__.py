"""Notifier function — a self-contained Flask blueprint (mounted by the host at /notifier).

First feature: a Personal Reminder — a free-text Telegram message sent to yourself on a
recurring schedule. Sends through the APP-WIDE bot (Settings → Tools → Telegram), gated by
the per-env "magi control" enable; the shared magi worker (worker.py) fires it on schedule.

  bp    : the Flask blueprint
  META  : sidebar/dashboard metadata + the `health` callable the host aggregates
"""
from flask import Blueprint, jsonify, render_template, request

from . import logic

bp = Blueprint("notifier", __name__, url_prefix="/notifier", template_folder="templates")

ICON = (
    '<svg width="16" height="16" viewBox="0 0 16 16"><path d="M8 16a2 2 0 0 0 1.985-1.75c.017-'
    '.137-.097-.25-.235-.25h-3.5c-.138 0-.252.113-.235.25A2 2 0 0 0 8 16ZM3 5a5 5 0 0 1 10 '
    '0v2.947c0 .05.015.098.042.139l1.703 2.555A1.519 1.519 0 0 1 13.482 13H2.518a1.518 1.518 0 '
    '0 1-1.263-2.36l1.703-2.554A.255.255 0 0 0 3 7.947Zm5-3.5A3.5 3.5 0 0 0 4.5 5v2.947c0 .346-'
    '.102.683-.294.97l-1.703 2.556a.017.017 0 0 0-.002.005l.001.008.005.006.008.004.007.001h10.'
    '964l.007-.001.008-.004.005-.006.001-.008a.017.017 0 0 0-.002-.005l-1.703-2.555a1.745 1.745 '
    '0 0 1-.294-.97V5A3.5 3.5 0 0 0 8 1.5Z"/></svg>'
)

META = {
    "key": "notifier",
    "label": "Notifier",
    "description": "Send yourself recurring Telegram reminders — free text on your own schedule.",
    "icon": ICON,
    "url": "/notifier/",
    "version": "notifier-1.0.0",
}

# Keys the save route accepts, mapped to the stored reminder_* settings.
_SAVE_FIELDS = {
    "text": "reminder_text",
    "enabled": "reminder_enabled",
    "days": "reminder_days",
    "times": "reminder_times",
    "timezone": "reminder_timezone",
}


def _norm_enabled(v):
    return "1" if str(v).strip().lower() in ("1", "true", "on", "yes") else "0"


@bp.route("/")
def page():
    return render_template("notifier/page.html", active="notifier")


@bp.route("/api/reminder", methods=["GET", "POST"])
def api_reminder():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        updates = {}
        for field, key in _SAVE_FIELDS.items():
            if field in data:
                updates[key] = _norm_enabled(data[field]) if field == "enabled" else data[field]
        logic.save_config(updates)
        return jsonify(ok=True, next_runs=logic.compute_next_runs())
    cfg = logic.get_config()
    return jsonify(
        text=cfg["reminder_text"],
        enabled=cfg["reminder_enabled"] == "1",
        days=cfg["reminder_days"],
        times=cfg["reminder_times"],
        timezone=cfg["reminder_timezone"],
        last_sent=cfg["reminder_last_sent"] or None,
        next_runs=logic.compute_next_runs(cfg),
        configured=logic.is_configured(),
        gate_enabled=logic.gate_enabled(),
        allowed_tags=list(logic.ALLOWED_TAGS),
    )


@bp.route("/api/reminder/send", methods=["POST"])
def api_reminder_send():
    data = request.get_json(silent=True) or {}
    text = data.get("text")  # None → use the saved reminder_text
    ok, err = logic.send_now(text)
    if ok:
        return jsonify(success=True)
    return jsonify(error=err), 400


@bp.route("/api/health")
def api_health():
    return jsonify(logic.status())


def health_payload():
    """Function health for the host's aggregated Health page (no network)."""
    return logic.status()


META["health"] = health_payload
