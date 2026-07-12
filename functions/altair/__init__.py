"""Altair function — a self-contained Flask blueprint (mounted by the host at /altair).

magi's push feed: a single page of widgets contributed by other functions through the
host-injected widget registry (see logic.py). The page renders each configured widget
as a card; an Edit mode adds drag-to-reorder, remove, and an Add-widget gallery driven
by each widget type's param schema. Never imports the host.

  bp    : the Flask blueprint
  META  : sidebar/dashboard metadata + the `health` callable the host aggregates
"""
from flask import Blueprint, jsonify, render_template, request

from . import logic

bp = Blueprint("altair", __name__, url_prefix="/altair", template_folder="templates")

# octicons rss-16 — a feed
ICON = (
    '<svg width="16" height="16" viewBox="0 0 16 16"><path d="M2.002 2.725a.75.75 0 0 1 '
    '.797-.699C8.79 2.42 13.58 7.21 13.974 13.201a.75.75 0 0 1-1.497.098 10.502 10.502 0 0 '
    '0-9.776-9.776.747.747 0 0 1-.699-.798ZM2.84 7.05h-.002a7.002 7.002 0 0 1 6.113 '
    '6.111.75.75 0 0 1-1.49.178 5.503 5.503 0 0 0-4.8-4.8.75.75 0 0 1 .179-1.489ZM2 13a1 1 '
    '0 1 1 2 0 1 1 0 0 1-2 0Z"/></svg>'
)

META = {
    "key": "altair",
    "label": "Altair",
    "description": "Your push feed — widgets from every function, arranged your way.",
    "icon": ICON,
    "url": "/altair/",
    "version": "altair-1.1.0",
}


@bp.route("/")
def page():
    return render_template("altair/page.html", active="altair")


@bp.route("/api/feed")
def api_feed():
    """Everything the page needs in one shot: the configured feed + the widget types
    the Add-widget gallery can offer (param schemas included, render callables not)."""
    return jsonify(widgets=logic.list_instances(), types=logic.available_types())


@bp.route("/api/widgets", methods=["POST"])
def api_add_widget():
    data = request.get_json(silent=True) or {}
    try:
        instance = logic.add_instance(data.get("widget", ""), data.get("config"))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(widget=instance)


@bp.route("/api/widgets/order", methods=["POST"])
def api_order():
    ids = (request.get_json(silent=True) or {}).get("ids")
    if not isinstance(ids, list):
        return jsonify(error="ids must be a list"), 400
    try:
        logic.reorder([int(i) for i in ids])
    except (TypeError, ValueError):
        return jsonify(error="ids must be integers"), 400
    return jsonify(ok=True)


@bp.route("/api/widgets/<int:instance_id>", methods=["POST", "DELETE"])
def api_widget(instance_id):
    if request.method == "DELETE":
        if not logic.remove_instance(instance_id):
            return jsonify(error="not found"), 404
        return jsonify(ok=True)
    # POST — partial update; today that's only the eye toggle
    data = request.get_json(silent=True) or {}
    if "hidden" not in data:
        return jsonify(error="nothing to update"), 400
    if not logic.set_hidden(instance_id, bool(data["hidden"])):
        return jsonify(error="not found"), 404
    return jsonify(ok=True, hidden=bool(data["hidden"]))


@bp.route("/api/widgets/<int:instance_id>/render")
def api_render_widget(instance_id):
    """One widget's card body. Always 200 for an existing instance — a failing widget
    returns {ok:false, error} and the page shows it as an error card, so one broken
    provider can never take down the feed."""
    out = logic.render_instance(instance_id)
    if out is None:
        return jsonify(error="not found"), 404
    return jsonify(out)


@bp.route("/api/health")
def api_health():
    return jsonify(logic.status())


def health_payload():
    """Function health for the host's aggregated Health page (no network)."""
    return logic.status()


META["health"] = health_payload
