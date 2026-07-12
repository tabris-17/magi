"""Polaris's altair widgets — the 'Journal feed' widget type.

One TYPE, any number of configured instances: each instance can narrow to one tag and
an entry-date window (the params), so "a widget per tag" is just adding it twice with
different tags. The registry callable runs per request, so the tag options stay live.

Like everything in polaris this never imports the host; magi.py reads META["widgets"]
(wired in __init__.py) and namespaces the type into the registry altair consumes.
The .alt-jf-* classes are styled by theme.css's altair block.
"""
from datetime import date, timedelta

from markupsafe import escape

from . import logic

# value → (label, days). "" = no date filter; "older-365" flips the window direction.
AGE_OPTIONS = [
    ("", "Any time"),
    ("7", "Last 7 days"),
    ("30", "Last 30 days"),
    ("90", "Last 90 days"),
    ("365", "Last year"),
    ("older-365", "Older than 1 year"),
]


def widget_types():
    tags = logic.list_tags()
    return [{
        "key": "tag-feed",
        "label": "Journal feed",
        "description": "Latest journal entries — optionally one tag, optionally an entry-date window.",
        "params": [
            {"name": "tag", "label": "Tag", "type": "select", "default": "",
             "options": ([{"value": "", "label": "All entries"}] +
                         [{"value": str(t["id"]), "label": t["name"]} for t in tags])},
            {"name": "age", "label": "Entry date", "type": "select", "default": "",
             "options": [{"value": v, "label": l} for v, l in AGE_OPTIONS]},
            {"name": "limit", "label": "Entries shown", "type": "number", "default": 5},
        ],
        "render": render_tag_feed,
    }]


def _age_window(age):
    """The config's age value → (since, before) ISO bounds, evaluated at render time."""
    if not age:
        return None, None
    if age == "older-365":
        return None, (date.today() - timedelta(days=365)).isoformat()
    return (date.today() - timedelta(days=int(age))).isoformat(), None


def render_tag_feed(config):
    tag_id = (config.get("tag") or "").strip() or None
    since, before = _age_window((config.get("age") or "").strip())
    try:
        limit = int(config.get("limit") or 5)
    except ValueError:
        limit = 5

    tag_name = None
    if tag_id:
        tag = next((t for t in logic.list_tags() if str(t["id"]) == tag_id), None)
        if tag is None:
            raise RuntimeError("the configured tag no longer exists")
        tag_name = tag["name"]

    entries = logic.entries_for_widget(tag_id=tag_id, since=since, before=before, limit=limit)
    title = f"Journal · {tag_name}" if tag_name else "Journal"

    if not entries:
        return {"title": title, "html": '<div class="alt-jf-none">No matching entries.</div>'}

    parts = []
    for e in entries:
        parts.append(
            f'<div class="alt-jf-item">'
            f'<span class="alt-jf-date">{escape(e["date"])}</span>'
            f'<a class="alt-jf-title" href="/polaris/?entry={e["id"]}">'
            f'{escape(e["title"] or "(untitled)")}</a>'
            + (f'<div class="alt-jf-preview">{escape(e["preview"])}</div>' if e["preview"] else "")
            + '</div>')
    return {"title": title, "html": "".join(parts)}
