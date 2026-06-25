"""Read-only SQLite browser for the host's Tools → Database page.

Introspects every magi-owned database — the host's `data/magi.db` plus each function's
own DB (currently betelgeuse's `portfolio.db`) — so all their tables/rows can be inspected
from one categorized page. Schema-agnostic (reads `sqlite_master` + `PRAGMA table_info`),
and STRICTLY read-only:

  * connections are opened with `PRAGMA query_only=ON` (the connection physically rejects
    writes — safe to point at betelgeuse's WAL DB while its worker is writing);
  * only SELECT / PRAGMA / COUNT run;
  * every table name is validated against the live `sqlite_master` whitelist before being
    interpolated into SQL (identifiers can't be parameterized).

Never imports a function — a function's DB is read as a plain file, so this stays within the
host's "functions are isolated" rule.
"""
import os
import sqlite3

from host import db as hostdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _betelgeuse_db_path():
    data_dir = os.environ.get("BETELGEUSE_DATA_DIR") or os.path.join(
        ROOT, "functions", "betelgeuse", "data")
    return os.path.join(data_dir, "portfolio.db")


# Databases surfaced on the page, in display order. A new function that ships a DB adds an
# entry here; `path` is a callable so a missing/renamed file degrades to "not found" rather
# than breaking import.
DATABASES = [
    {"key": "magi", "label": "magi", "desc": "Host settings & common store (data/magi.db)",
     "path": lambda: hostdb.DB_PATH},
    {"key": "betelgeuse", "label": "Betelgeuse",
     "desc": "Portfolio, transactions, market-data cache (portfolio.db)",
     "path": _betelgeuse_db_path},
]


def _spec(key):
    return next((d for d in DATABASES if d["key"] == key), None)


def _resolve_path(spec):
    p = spec["path"]
    return p() if callable(p) else p


def _connect(path):
    """A query-only connection — handles WAL DBs and can never mutate the file."""
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _table_names(cursor):
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' "
                   "AND name NOT LIKE 'sqlite_%' ORDER BY name")
    return [r["name"] for r in cursor.fetchall()]


def _safe(v):
    """JSON-safe cell value (BLOBs become a short placeholder)."""
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<blob {len(bytes(v))} bytes>"
    return v


def list_all():
    """Per-database table listing for the page:
    [{key,label,desc,available,error?,tables:[{name,row_count}]}]."""
    out = []
    for spec in DATABASES:
        path = _resolve_path(spec)
        entry = {"key": spec["key"], "label": spec["label"], "desc": spec["desc"]}
        if not path or not os.path.exists(path):
            out.append({**entry, "available": False, "tables": [],
                        "error": "database file not found"})
            continue
        try:
            conn = _connect(path)
            try:
                c = conn.cursor()
                tables = []
                for name in _table_names(c):
                    c.execute(f'SELECT COUNT(*) AS cnt FROM "{name}"')
                    tables.append({"name": name, "row_count": c.fetchone()["cnt"]})
            finally:
                conn.close()
            out.append({**entry, "available": True, "tables": tables})
        except Exception as e:  # noqa: BLE001
            out.append({**entry, "available": False, "tables": [], "error": str(e)})
    return out


def table_data(dbkey, name, page=1, per_page=50):
    """(payload, error) for one table — columns + a paginated page of rows."""
    spec = _spec(dbkey)
    if not spec:
        return None, f"unknown database {dbkey!r}"
    path = _resolve_path(spec)
    if not path or not os.path.exists(path):
        return None, "database file not found"
    page = max(1, page)
    per_page = min(200, max(10, per_page))
    offset = (page - 1) * per_page
    conn = _connect(path)
    try:
        c = conn.cursor()
        if name not in _table_names(c):
            return None, f"unknown table {name!r}"
        c.execute(f'PRAGMA table_info("{name}")')
        columns = [r["name"] for r in c.fetchall()]
        c.execute(f'SELECT COUNT(*) AS cnt FROM "{name}"')
        total = c.fetchone()["cnt"]
        c.execute(f'SELECT * FROM "{name}" LIMIT ? OFFSET ?', (per_page, offset))
        rows = [[_safe(v) for v in r] for r in c.fetchall()]
    finally:
        conn.close()
    return {
        "database": dbkey, "name": name, "columns": columns, "rows": rows,
        "total": total, "page": page, "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
    }, None
