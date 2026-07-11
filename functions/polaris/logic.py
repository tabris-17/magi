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
import glob
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone

logger = logging.getLogger("magi.polaris")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "polaris.db")

# ---- backups (see "Backups & rollback" in CLAUDE.md) -----------------------------------
# Two layers, both landing in data/backup/ (rsync-excluded + gitignored, so each box
# keeps its own):
#   * polaris-pre-vN-<stamp>.db — taken AUTOMATICALLY the first time a process opens a
#     DB stamped with a different PRAGMA user_version than this code's SCHEMA_VERSION,
#     BEFORE any schema statement runs. Kept forever (they mark exactly the moments
#     worth rolling back to). Bump SCHEMA_VERSION whenever the schema changes shape.
#   * polaris-daily-<stamp>.db — the shared magi worker's daily job (03:30), skipped
#     when the DB hasn't changed; only the newest BACKUP_KEEP_DAILY are kept.
# Rollback is manual by design: stop the app, copy a snapshot over polaris.db, start.
SCHEMA_VERSION = 2
BACKUP_DIR = os.path.join(DATA_DIR, "backup")
BACKUP_KEEP_DAILY = 14

_schema_lock = threading.Lock()
_schema_checked = False

ATTACH_MAX_BYTES = 25 * 1024 * 1024

# Only these render inline in the browser. Everything else (incl. image/svg+xml, which can
# carry script) is served as a download, so a stored file can never execute in our origin.
INLINE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_date    TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    body          TEXT NOT NULL DEFAULT '',
    reminder_date TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
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
    emoji      TEXT NOT NULL DEFAULT '',
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
    _schema_guard()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _schema_guard():
    """Once per process: if the DB file was written by a DIFFERENT schema version,
    snapshot it to backup/ BEFORE this code touches it, then apply the (idempotent)
    schema and stamp PRAGMA user_version. This is the journal's safety net for future
    schema/data-shape changes — the pre-change bytes always survive in a file you can
    manually copy back."""
    global _schema_checked
    if _schema_checked:
        return
    with _schema_lock:
        if _schema_checked:
            return
        if os.path.exists(DB_PATH):
            conn = sqlite3.connect(DB_PATH)
            try:
                stored = conn.execute("PRAGMA user_version").fetchone()[0]
            finally:
                conn.close()
            if stored != SCHEMA_VERSION:
                dest = snapshot_db(f"pre-v{SCHEMA_VERSION}")
                logger.info("polaris schema v%s -> v%s: pre-change snapshot %s",
                            stored, SCHEMA_VERSION, dest and os.path.basename(dest))
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.executescript(_SCHEMA)
            _ensure_columns(conn)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
        finally:
            conn.close()
        _schema_checked = True


def _ensure_columns(conn):
    """Additive upgrades for DBs created before a column existed (CREATE IF NOT EXISTS
    won't touch an existing table). Runs only under _schema_guard, AFTER its snapshot."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(entries)")}
    if "reminder_date" not in have:  # v2
        conn.execute("ALTER TABLE entries ADD COLUMN reminder_date TEXT NOT NULL DEFAULT ''")
    have = {r[1] for r in conn.execute("PRAGMA table_info(tags)")}
    if "emoji" not in have:          # v2
        conn.execute("ALTER TABLE tags ADD COLUMN emoji TEXT NOT NULL DEFAULT ''")


def snapshot_db(reason):
    """A consistent copy of polaris.db → data/backup/polaris-<reason>-<stamp>.db, via
    the sqlite backup API (safe against concurrent writers). None when no DB exists."""
    if not os.path.exists(DB_PATH):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"polaris-{reason}-{stamp}.db")
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


def _dailies():
    return sorted(glob.glob(os.path.join(BACKUP_DIR, "polaris-daily-*.db")))


def daily_backup():
    """The shared worker's daily job: snapshot when the DB changed since the newest
    daily, then prune dailies beyond BACKUP_KEEP_DAILY (pre-v* snapshots are never
    pruned). Returns the new snapshot path, or None when skipped."""
    if not os.path.exists(DB_PATH):
        return None
    last = _dailies()
    if last and os.path.getmtime(DB_PATH) <= os.path.getmtime(last[-1]):
        logger.info("polaris backup: unchanged since %s — skipped", os.path.basename(last[-1]))
        return None
    dest = snapshot_db("daily")
    logger.info("polaris backup: wrote %s", os.path.basename(dest))
    for old in _dailies()[:-BACKUP_KEEP_DAILY]:
        os.remove(old)
        logger.info("polaris backup: pruned %s", os.path.basename(old))
    return dest


# ---- shared-worker interface (worker.py drives this, like the Notifier) ---------------

BACKUP_JOB_ID = "polaris_daily_backup"
BACKUP_AT = (3, 30)   # daily, 03:30 in the box's local time


def schedule_fingerprint():
    """Static — the backup schedule isn't user-configurable; the worker just needs a
    stable value so it installs the job once and never churns it."""
    return f"backup@{BACKUP_AT[0]:02d}:{BACKUP_AT[1]:02d}/v{SCHEMA_VERSION}"


def reschedule(scheduler):
    """(Re)install the daily snapshot job on the shared worker's scheduler."""
    from apscheduler.triggers.cron import CronTrigger  # lazy — web imports stay cheap
    for job in list(scheduler.get_jobs()):
        if job.id == BACKUP_JOB_ID:
            job.remove()
    scheduler.add_job(daily_backup, CronTrigger(hour=BACKUP_AT[0], minute=BACKUP_AT[1]),
                      id=BACKUP_JOB_ID)
    logger.info("[polaris] daily backup scheduled %02d:%02d", *BACKUP_AT)


def _row(r):
    return {
        "id": r["id"],
        "date": r["entry_date"],
        "title": r["title"],
        "body": r["body"],
        "reminder": r["reminder_date"] or None,
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

def save_entry(entry_id=None, date=None, title="", body="", reminder=None):
    """Create (entry_id None) or update an entry. Returns the saved row.

    An empty date defaults to today; an entry with neither title nor body is still
    allowed (the UI blocks it, but the store stays dumb). `reminder` is an optional
    ISO date ("" / None clears it) — just a stored field for now, nothing fires.
    """
    date = (date or "").strip() or _today()
    title = (title or "").strip()
    body = body or ""
    reminder = (reminder or "").strip()
    now = _now()
    conn = _connect()
    try:
        if entry_id:
            cur = conn.execute(
                "UPDATE entries SET entry_date = ?, title = ?, body = ?, reminder_date = ?, "
                "updated_at = ? WHERE id = ?", (date, title, body, reminder, now, entry_id))
            if cur.rowcount == 0:
                raise KeyError(f"no entry {entry_id}")
        else:
            cur = conn.execute(
                "INSERT INTO entries (entry_date, title, body, reminder_date, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (date, title, body, reminder, now, now))
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
        "SELECT t.id, t.name, t.emoji FROM tags t JOIN entry_tags et ON et.tag_id = t.id "
        "WHERE et.entry_id = ? ORDER BY t.name COLLATE NOCASE", (entry_id,))
    return [{"id": r["id"], "name": r["name"], "emoji": r["emoji"]} for r in rows]


def list_tags():
    """All tags with how many entries carry each — for the manager page + pickers."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT t.id, t.name, t.emoji, COUNT(et.entry_id) AS n FROM tags t "
            "LEFT JOIN entry_tags et ON et.tag_id = t.id "
            "GROUP BY t.id ORDER BY t.name COLLATE NOCASE")
        return [{"id": r["id"], "name": r["name"], "emoji": r["emoji"],
                 "entry_count": r["n"]} for r in rows]
    finally:
        conn.close()


def create_tag(name, emoji=""):
    name = " ".join((name or "").split())
    emoji = (emoji or "").strip()
    if not name:
        raise ValueError("tag name is empty")
    conn = _connect()
    try:
        try:
            cur = conn.execute("INSERT INTO tags (name, emoji, created_at) VALUES (?, ?, ?)",
                               (name, emoji, _now()))
            conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError(f"tag “{name}” already exists")
        return {"id": cur.lastrowid, "name": name, "emoji": emoji, "entry_count": 0}
    finally:
        conn.close()


def update_tag(tag_id, name=None, emoji=None):
    """Rename and/or re-emoji a tag (None = leave that field alone)."""
    sets, args = [], []
    if name is not None:
        name = " ".join(name.split())
        if not name:
            raise ValueError("tag name is empty")
        sets.append("name = ?"); args.append(name)
    if emoji is not None:
        sets.append("emoji = ?"); args.append(emoji.strip())
    if not sets:
        return
    conn = _connect()
    try:
        try:
            cur = conn.execute(f"UPDATE tags SET {', '.join(sets)} WHERE id = ?",
                               (*args, tag_id))
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
        backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "polaris-*.db")))
        return {"db": DB_PATH, "entries": row["n"], "latest_entry": row["last"],
                "attachments": att["n"], "attachment_bytes": att["bytes"],
                "schema_version": SCHEMA_VERSION, "backups": len(backups),
                "last_backup": os.path.basename(backups[-1]) if backups else None,
                "ok": True}
    except Exception as exc:  # noqa: BLE001
        logger.warning("polaris health failed: %s", exc)
        return {"db": DB_PATH, "ok": False, "error": str(exc)}
