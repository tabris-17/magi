"""Polaris function — core logic (Flask-free, self-contained).

A personal journal. An entry has three parts: a title, a free-text body, and a list of
attachments. This module owns its own SQLite store (functions/polaris/data/polaris.db).

Attachment bytes live in the DB as BLOBs rather than on disk, deliberately: `deploy.sh`
excludes `functions/*/data/` from rsync and `pull-prod-dbs.sh` only mirrors `.db` files,
so a file on disk would never sync prod→dev and the journal would show broken images on
dev. Keeping bytes in polaris.db means an attachment is backed up and snapshot-consistent
with the entry that owns it, for free. Large images are downscaled client-side before
upload; ATTACH_MAX_BYTES is the backstop.

Isolation contract (like youtube/taxation/notifier): this NEVER imports the host, and
importing it touches NO filesystem or network — the schema is created lazily on the first
DB access. There is no migration engine (same as the host's own store and notifier's):
_SCHEMA is idempotent, so an existing polaris.db picks up `attachments` on next connect.
"""
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger("magi.polaris")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "polaris.db")

ATTACH_MAX_BYTES = 25 * 1024 * 1024

# Only these render inline in the browser. Everything else (incl. image/svg+xml, which can
# carry script) is served as a download, so a stored file can never execute in our origin.
INLINE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_date TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_date ON entries (entry_date DESC, id DESC);

CREATE TABLE IF NOT EXISTS attachments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id   INTEGER NOT NULL,
    filename   TEXT NOT NULL,
    mime       TEXT NOT NULL DEFAULT '',
    size       INTEGER NOT NULL DEFAULT 0,
    data       BLOB NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_att_entry ON attachments (entry_id, id);

CREATE TABLE IF NOT EXISTS tags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entry_tags (
    entry_id   INTEGER NOT NULL,
    tag_id     INTEGER NOT NULL,
    PRIMARY KEY (entry_id, tag_id)
);
"""


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _connect():
    """Open polaris.db, creating the dir + schema lazily (never at import)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _row(r):
    return {
        "id": r["id"],
        "date": r["entry_date"],
        "title": r["title"],
        "body": r["body"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


# ---- reads ---------------------------------------------------------------------------

PREVIEW_LEN = 110


def _strip_md(text):
    """Drop the Markdown syntax the body is stored with, so previews read as prose."""
    text = re.sub(r"^\s*#{1,3}\s+", "", text or "", flags=re.M)      # headings
    text = re.sub(r"^\s*(?:[-*]|\d+[.)])\s+", "", text, flags=re.M)  # list bullets
    text = re.sub(r"\\([\\*_`])", r"\1", text)                       # escaped literals
    return re.sub(r"\*\*|\*|`|_", "", text)                          # bold/italic/code marks


def _snippet(body, query=""):
    """A one-line preview: centered on the query's first body match, else the opening."""
    flat = " ".join(_strip_md(body).split())
    if not flat:
        return ""
    at = flat.lower().find(query.lower()) if query else -1
    if at <= PREVIEW_LEN // 2:
        head = flat[:PREVIEW_LEN]
        return head + ("…" if len(flat) > PREVIEW_LEN else "")
    start = at - PREVIEW_LEN // 3
    tail = flat[start:start + PREVIEW_LEN]
    return "…" + tail + ("…" if start + PREVIEW_LEN < len(flat) else "")


def list_entries(query="", limit=500):
    """Newest first, for the sidebar tree. Carries a `preview` instead of the full body.

    `query` is a case-insensitive substring match on the title, the body, OR the ISO date
    (so "2026-07" narrows to a month and "2026" to a year).
    """
    sql = ("SELECT e.*, (SELECT COUNT(*) FROM attachments a WHERE a.entry_id = e.id) "
           "AS att_count FROM entries e")
    args = []
    if query:
        sql += " WHERE e.title LIKE ? OR e.body LIKE ? OR e.entry_date LIKE ?"
        args = [f"%{query}%"] * 3
    sql += " ORDER BY e.entry_date DESC, e.id DESC LIMIT ?"
    args.append(int(limit))
    conn = _connect()
    try:
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        e = _row(r)
        e["preview"] = _snippet(e.pop("body"), query)
        e["attachment_count"] = r["att_count"]  # a count here; get_entry() returns the list
        out.append(e)
    return out


def get_entry(entry_id):
    """The full entry, with its attachment metadata (never the bytes) and tags."""
    conn = _connect()
    try:
        r = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if not r:
            return None
        entry = _row(r)
        entry["attachments"] = _list_attachments(conn, entry_id)
        entry["tags"] = _tag_rows(conn, entry_id)
    finally:
        conn.close()
    return entry


# ---- attachments ---------------------------------------------------------------------

def _att_row(r):
    return {
        "id": r["id"],
        "filename": r["filename"],
        "mime": r["mime"],
        "size": r["size"],
        "created_at": r["created_at"],
        "inline": r["mime"] in INLINE_MIMES,
        "url": f"/polaris/media/{r['id']}",
    }


def _list_attachments(conn, entry_id):
    rows = conn.execute(
        "SELECT id, filename, mime, size, created_at FROM attachments "
        "WHERE entry_id = ? ORDER BY id", (entry_id,))
    return [_att_row(r) for r in rows]


def list_attachments(entry_id):
    conn = _connect()
    try:
        return _list_attachments(conn, entry_id)
    finally:
        conn.close()


def add_attachment(entry_id, filename, mime, data):
    """Attach bytes to an existing entry. Raises KeyError if the entry is gone."""
    conn = _connect()
    try:
        if not conn.execute("SELECT 1 FROM entries WHERE id = ?", (entry_id,)).fetchone():
            raise KeyError(f"no entry {entry_id}")
        cur = conn.execute(
            "INSERT INTO attachments (entry_id, filename, mime, size, data, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (entry_id, filename or "file", mime or "", len(data), sqlite3.Binary(data), _now()))
        conn.commit()
        r = conn.execute(
            "SELECT id, filename, mime, size, created_at FROM attachments WHERE id = ?",
            (cur.lastrowid,)).fetchone()
    finally:
        conn.close()
    return _att_row(r)


def get_attachment(att_id):
    """(filename, mime, data) for serving — or None."""
    conn = _connect()
    try:
        r = conn.execute("SELECT filename, mime, data FROM attachments WHERE id = ?",
                         (att_id,)).fetchone()
    finally:
        conn.close()
    return (r["filename"], r["mime"], bytes(r["data"])) if r else None


def delete_attachment(att_id):
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM attachments WHERE id = ?", (att_id,))
        conn.commit()
    finally:
        conn.close()
    return cur.rowcount > 0


# ---- writes --------------------------------------------------------------------------

def save_entry(entry_id=None, date=None, title="", body=""):
    """Create (entry_id None) or update an entry. Returns the saved row.

    An empty date defaults to today; an entry with neither title nor body is still
    allowed (the UI blocks it, but the store stays dumb).
    """
    date = (date or "").strip() or _today()
    title = (title or "").strip()
    body = body or ""
    now = _now()
    conn = _connect()
    try:
        if entry_id:
            cur = conn.execute(
                "UPDATE entries SET entry_date = ?, title = ?, body = ?, updated_at = ? "
                "WHERE id = ?", (date, title, body, now, entry_id))
            if cur.rowcount == 0:
                raise KeyError(f"no entry {entry_id}")
        else:
            cur = conn.execute(
                "INSERT INTO entries (entry_date, title, body, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)", (date, title, body, now, now))
            entry_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    return get_entry(entry_id)


def delete_entry(entry_id):
    """Delete an entry, its attachments and tag links (sqlite FKs are off by default)."""
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        conn.execute("DELETE FROM attachments WHERE entry_id = ?", (entry_id,))
        conn.execute("DELETE FROM entry_tags WHERE entry_id = ?", (entry_id,))
        conn.commit()
    finally:
        conn.close()
    return cur.rowcount > 0


# ---- tags ------------------------------------------------------------------------------
# Betelgeuse-style groups, minus the guardrails: deleting a tag never checks usage — it
# just unlinks (the entries themselves are untouched). Names are unique case-insensitively.

def _tag_rows(conn, entry_id):
    rows = conn.execute(
        "SELECT t.id, t.name FROM tags t JOIN entry_tags et ON et.tag_id = t.id "
        "WHERE et.entry_id = ? ORDER BY t.name COLLATE NOCASE", (entry_id,))
    return [{"id": r["id"], "name": r["name"]} for r in rows]


def list_tags():
    """All tags with how many entries carry each — for the manager page + pickers."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT t.id, t.name, COUNT(et.entry_id) AS n FROM tags t "
            "LEFT JOIN entry_tags et ON et.tag_id = t.id "
            "GROUP BY t.id ORDER BY t.name COLLATE NOCASE")
        return [{"id": r["id"], "name": r["name"], "entry_count": r["n"]} for r in rows]
    finally:
        conn.close()


def create_tag(name):
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("tag name is empty")
    conn = _connect()
    try:
        try:
            cur = conn.execute("INSERT INTO tags (name, created_at) VALUES (?, ?)",
                               (name, _now()))
            conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError(f"tag “{name}” already exists")
        return {"id": cur.lastrowid, "name": name, "entry_count": 0}
    finally:
        conn.close()


def rename_tag(tag_id, name):
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("tag name is empty")
    conn = _connect()
    try:
        try:
            cur = conn.execute("UPDATE tags SET name = ? WHERE id = ?", (name, tag_id))
            conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError(f"tag “{name}” already exists")
        if cur.rowcount == 0:
            raise KeyError(f"no tag {tag_id}")
    finally:
        conn.close()


def delete_tag(tag_id):
    """Delete a tag and its links. Deliberately NO in-use check (see module docstring)."""
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        conn.execute("DELETE FROM entry_tags WHERE tag_id = ?", (tag_id,))
        conn.commit()
    finally:
        conn.close()
    return cur.rowcount > 0


def set_entry_tags(entry_id, tag_ids):
    """Replace an entry's tag set. Unknown tag ids are silently dropped."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM entry_tags WHERE entry_id = ?", (entry_id,))
        for tid in dict.fromkeys(int(t) for t in (tag_ids or [])):  # dedupe, keep order
            if conn.execute("SELECT 1 FROM tags WHERE id = ?", (tid,)).fetchone():
                conn.execute("INSERT INTO entry_tags (entry_id, tag_id) VALUES (?, ?)",
                             (entry_id, tid))
        conn.commit()
        return _tag_rows(conn, entry_id)
    finally:
        conn.close()


# ---- health --------------------------------------------------------------------------

def status():
    """Function health for the host's aggregated Health page (no network)."""
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n, MAX(entry_date) AS last FROM entries").fetchone()
            att = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes FROM attachments"
            ).fetchone()
        finally:
            conn.close()
        return {"db": DB_PATH, "entries": row["n"], "latest_entry": row["last"],
                "attachments": att["n"], "attachment_bytes": att["bytes"], "ok": True}
    except Exception as exc:  # noqa: BLE001
        logger.warning("polaris health failed: %s", exc)
        return {"db": DB_PATH, "ok": False, "error": str(exc)}
