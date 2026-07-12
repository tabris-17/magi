"""Altair function — core logic (Flask-free, self-contained).

Altair is magi's push feed: a single page of "widgets" (applets), each one a card
rendered by whichever function contributed it. This module owns only the LAYOUT —
which widgets the user added, their config, and their order — in its own SQLite
store (functions/altair/data/altair.db).

The widgets themselves come from the WIDGET REGISTRY, which the host injects via
set_widget_registry_resolver() (the youtube/taxation resolver pattern — altair never
imports the host or another function). Each registry entry is a widget TYPE:

    {"id": "<function>.<key>",     # namespaced by the host
     "source": "<function label>", # e.g. "Betelgeuse"
     "key", "label", "description",
     "params": [{name,label,type: select|number|text, options?, default?}, …],
     "render": callable(config: dict) -> {"html": str, "title"?: str},
     "mask"?:  callable(config: dict) -> {"html": str, "title"?: str}}

A type MAY also declare `mask` — the privacy view used while the instance's eye is
closed (e.g. the P&L with its amounts replaced by •••••). Masking happens SERVER-side:
while hidden, render_instance() only ever returns the mask output, so the real numbers
never reach the browser. A type without `mask` simply collapses when hidden (the page
shows only the card's title row and fetches nothing).

`params` drives the Add-widget form; `render` produces the card body (called guarded —
a raising widget becomes an error card, never a broken feed). A function that wants to
offer widgets in the future only has to put such a callable on its META["widgets"];
altair picks it up with zero changes here.

Isolation contract (like youtube/taxation/notifier/polaris): importing this touches NO
filesystem or network — the schema is created lazily on first DB access. No migration
engine; _SCHEMA is idempotent (same as the host's own store).
"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "altair.db")

_schema_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS widgets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    widget     TEXT NOT NULL,                 -- namespaced type id, "<function>.<key>"
    config     TEXT NOT NULL DEFAULT '{}',    -- JSON dict of param values
    position   INTEGER NOT NULL DEFAULT 0,    -- feed order, ascending
    hidden     INTEGER NOT NULL DEFAULT 0,    -- eye toggle: 1 = body not shown/rendered
    created_at TEXT NOT NULL
);
"""

# Host-injected: returns the CURRENT widget registry (fresh each call, so dynamic
# param options — e.g. polaris's tag list — stay live). None → no widgets available.
_registry_resolver = None


def set_widget_registry_resolver(fn):
    global _registry_resolver
    _registry_resolver = fn


def _registry():
    if _registry_resolver is None:
        return []
    return _registry_resolver() or []


def _type(widget_id):
    return next((t for t in _registry() if t.get("id") == widget_id), None)


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _connect():
    """Open altair.db, creating the dir + schema lazily (never at import)."""
    with _schema_lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        # column-add guard for DBs created before the eye toggle (polaris's pattern —
        # no migration engine; an existing altair.db picks the column up on connect)
        have = {r["name"] for r in conn.execute("PRAGMA table_info(widgets)")}
        if "hidden" not in have:
            conn.execute("ALTER TABLE widgets ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
            conn.commit()
    return conn


# ---- widget types (what the Add-widget gallery offers) --------------------------------

def available_types():
    """The registry, minus the render/mask callables — JSON-safe for the page."""
    out = []
    for t in _registry():
        entry = {k: v for k, v in t.items() if k not in ("render", "mask")}
        entry["maskable"] = callable(t.get("mask"))
        out.append(entry)
    return out


# ---- widget instances (the user's configured feed) ------------------------------------

def _instance_row(r, types_by_id):
    t = types_by_id.get(r["widget"])
    try:
        config = json.loads(r["config"]) or {}
    except ValueError:
        config = {}
    return {
        "id": r["id"],
        "widget": r["widget"],
        "config": config,
        "position": r["position"],
        "hidden": bool(r["hidden"]),
        # a maskable widget renders a •••••-masked body while hidden, instead of collapsing
        "maskable": bool(t and callable(t.get("mask"))),
        # display metadata from the live registry; a widget whose provider vanished
        # stays in the feed as known:False (removable, renders an error card)
        "known": t is not None,
        "label": t["label"] if t else r["widget"],
        "source": t["source"] if t else "",
    }


def list_instances():
    """The configured feed, in display order."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM widgets ORDER BY position, id").fetchall()
    finally:
        conn.close()
    types_by_id = {t["id"]: t for t in _registry()}
    return [_instance_row(r, types_by_id) for r in rows]


def add_instance(widget_id, config=None):
    """Add one widget to the end of the feed. Unknown type → ValueError."""
    t = _type(widget_id)
    if t is None:
        raise ValueError(f"unknown widget: {widget_id}")
    if not isinstance(config, dict):
        config = {}
    # keep only declared params (stringified — they come from form fields anyway)
    declared = {p["name"] for p in t.get("params", [])}
    config = {k: str(v) for k, v in config.items() if k in declared}
    conn = _connect()
    try:
        pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM widgets").fetchone()[0]
        cur = conn.execute(
            "INSERT INTO widgets (widget, config, position, created_at) VALUES (?,?,?,?)",
            (widget_id, json.dumps(config), pos, _now()))
        conn.commit()
        row = conn.execute("SELECT * FROM widgets WHERE id = ?", (cur.lastrowid,)).fetchone()
    finally:
        conn.close()
    return _instance_row(row, {t["id"]: t})


def set_hidden(instance_id, hidden):
    """Persist a widget's eye toggle (True = body hidden). False for a missing id."""
    conn = _connect()
    try:
        cur = conn.execute("UPDATE widgets SET hidden = ? WHERE id = ?",
                           (1 if hidden else 0, instance_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def remove_instance(instance_id):
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM widgets WHERE id = ?", (instance_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def reorder(ids):
    """Persist a new feed order: position = index in `ids`. Unknown ids are ignored;
    instances not listed keep their old position (they sort after the reordered ones
    only by whatever position they already had)."""
    conn = _connect()
    try:
        for pos, instance_id in enumerate(ids):
            conn.execute("UPDATE widgets SET position = ? WHERE id = ?",
                         (pos, int(instance_id)))
        conn.commit()
    finally:
        conn.close()


# ---- rendering -------------------------------------------------------------------------

def render_instance(instance_id):
    """Render one configured widget → {ok, title, html} or {ok: False, title, error}.

    Always returns a dict (never raises for a widget failure): the feed shows an error
    card for a broken/vanished widget instead of breaking the page — same guarded-call
    philosophy as the host's /api/health.
    """
    conn = _connect()
    try:
        r = conn.execute("SELECT * FROM widgets WHERE id = ?", (instance_id,)).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    t = _type(r["widget"])
    if t is None:
        return {"ok": False, "title": r["widget"],
                "error": "this widget's provider is no longer available"}
    try:
        config = json.loads(r["config"]) or {}
    except ValueError:
        config = {}
    hidden = bool(r["hidden"])
    if hidden and not callable(t.get("mask")):
        # no privacy view — while hidden this instance renders NOTHING (the page
        # collapses the card client-side and shouldn't even ask; belt-and-braces)
        return {"ok": True, "masked": True, "title": t["label"], "html": ""}
    try:
        out = (t["mask"] if hidden else t["render"])(config) or {}
        return {"ok": True, "masked": hidden,
                "title": out.get("title") or t["label"],
                "html": out.get("html", "")}
    except Exception as exc:  # noqa: BLE001 — one widget must never break the feed
        return {"ok": False, "title": t["label"], "error": str(exc)}


# ---- health ----------------------------------------------------------------------------

def status():
    """Function health for the host's aggregated Health page (no network)."""
    instances = list_instances()
    return {
        "ok": True,
        "widgets": len(instances),
        "unknown": sum(1 for i in instances if not i["known"]),
        "types_available": len(available_types()),
        "db": os.path.exists(DB_PATH),
    }
