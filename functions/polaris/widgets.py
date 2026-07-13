"""Polaris's altair widgets — the 'Journal feed' widget type.

One TYPE, any number of configured instances: each instance can narrow to one tag and
an entry-date window (the params), so "a widget per tag" is just adding it twice with
different tags. The registry callable runs per request, so the tag options stay live.

The card SIZE (altair injects it as config["_size"]) decides how much each entry gets:
1x4 = compact date+title rows, 2x4 = plus the one-line prose preview, 4x4 = the body
rendered as REAL markdown (via _md_to_html below — a one-way, display-only Python
renderer for polaris's deliberately closed subset; the round-trip converter stays
static/polaris-md.js and is NOT reimplemented here).

Like everything in polaris this never imports the host; magi.py reads META["widgets"]
(wired in __init__.py) and namespaces the type into the registry altair consumes.
The .alt-jf-* classes are styled by theme.css's altair block.
"""
import re
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
        "default_size": "1x4",
        "render": render_tag_feed,
    }]


# ---- markdown → html, display-only ----------------------------------------------------
# Renders polaris's CLOSED markdown subset (h1-h3, ul, ol, p; **bold** *italic* `code`)
# for the widget's 4x4 view. One-way only: entries are stored as markdown and the
# store/round-trip logic never touches this. Same hard-won escape invariant as
# polaris-md.js: HTML-escape first, then park \\ \* \_ \` behind sentinels BEFORE the
# bold/italic/code regexes run, and restore them as their literal characters last —
# otherwise a body like `sync\_dell` grows an <em> boundary.

MD_EXCERPT_CHARS = 700   # per-entry cap in the 4x4 view (cut at a line boundary)

_MD_SENTINELS = (("\\\\", "\x00"), ("\\*", "\x01"), ("\\_", "\x02"), ("\\`", "\x03"))


def _md_inline(text):
    s = str(escape(text))
    for lit, tok in _MD_SENTINELS:
        s = s.replace(lit, tok)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", s)
    for (_, tok), ch in zip(_MD_SENTINELS, "\\*_`"):
        s = s.replace(tok, ch)
    return s


def _md_to_html(md, max_chars=MD_EXCERPT_CHARS):
    """(html, truncated). Each stored line is a block (that's how polaris writes them);
    consecutive bullet/numbered lines group into one list."""
    md = (md or "").strip()
    truncated = len(md) > max_chars
    if truncated:
        cut = md.rfind("\n", 0, max_chars)
        md = md[:cut if cut > 0 else max_chars]

    out, list_tag = [], None

    def close_list():
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    for line in md.split("\n"):
        line = line.strip()
        if not line:
            close_list()
            continue
        m = re.match(r"(#{1,3})\s+(.*)", line)
        if m:
            close_list()
            out.append(f"<h{len(m.group(1))}>{_md_inline(m.group(2))}</h{len(m.group(1))}>")
            continue
        m = re.match(r"[-*]\s+(.*)", line)
        if not m:
            m = re.match(r"\d+[.)]\s+(.*)", line)
            tag = "ol" if m else None
        else:
            tag = "ul"
        if tag:
            if list_tag != tag:
                close_list()
                out.append(f"<{tag}>")
                list_tag = tag
            out.append(f"<li>{_md_inline(m.group(1))}</li>")
            continue
        close_list()
        out.append(f"<p>{_md_inline(line)}</p>")
    close_list()
    return "".join(out), truncated


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

    # the card size decides each entry's depth: 1x4 compact rows, 2x4 + prose preview,
    # 4x4 + the body as rendered markdown (bounded — the card scrolls past that)
    size = config.get("_size") or "1x4"
    parts = []
    for e in entries:
        body = ""
        if size == "4x4":
            md_html, truncated = _md_to_html(e.get("body", ""))
            if md_html:
                body = (f'<div class="alt-jf-md">{md_html}</div>'
                        + ('<div class="alt-jf-more">…</div>' if truncated else ""))
        elif size != "1x4" and e["preview"]:
            body = f'<div class="alt-jf-preview">{escape(e["preview"])}</div>'
        parts.append(
            f'<div class="alt-jf-item">'
            f'<span class="alt-jf-date">{escape(e["date"])}</span>'
            f'<a class="alt-jf-title" href="/polaris/?entry={e["id"]}">'
            f'{escape(e["title"] or "(untitled)")}</a>'
            + body + '</div>')
    return {"title": title, "html": "".join(parts)}
