"""Polaris logic-layer battery — the Python store that the JS converter tests don't cover.

Covers entry CRUD + search, previews, tags (unique/partial/unlink), entry↔tag links,
attachments (blobs + inline gate), and the backup/schema-guard safety net that guarantees
a manual-rollback copy survives every schema-version change. No dev server, no network.
"""
import glob
import os
import sqlite3
import time

from functions.polaris import logic


# ---- entries ---------------------------------------------------------------------------

def test_create_defaults_date_to_today_and_allows_empty():
    e = logic.save_entry()                       # no title, no body, no date
    assert e["id"] > 0
    assert e["date"] == logic._today()
    assert e["title"] == "" and e["body"] == ""
    assert e["reminder"] is None
    assert e["attachments"] == [] and e["tags"] == []


def test_create_preserves_explicit_date_and_reminder():
    e = logic.save_entry(date="2026-07-05", title=" Hi ", body="b", reminder="2026-07-20")
    assert e["date"] == "2026-07-05"
    assert e["title"] == "Hi"                     # trimmed
    assert e["reminder"] == "2026-07-20"


def test_update_changes_fields():
    e = logic.save_entry(title="one")
    u = logic.save_entry(entry_id=e["id"], title="two", body="body", reminder="2026-08-01")
    assert u["id"] == e["id"]
    assert u["title"] == "two" and u["body"] == "body"
    assert u["reminder"] == "2026-08-01"


def test_update_missing_raises_keyerror():
    try:
        logic.save_entry(entry_id=99999, title="x")
    except KeyError as exc:
        assert exc.args[0] == "no entry 99999"
    else:
        assert False, "expected KeyError"


def test_reminder_blank_clears_to_none():
    e = logic.save_entry(title="t", reminder="2026-07-20")
    u = logic.save_entry(entry_id=e["id"], reminder="")
    assert u["reminder"] is None


def test_get_entry_missing_returns_none():
    assert logic.get_entry(4242) is None


def test_delete_entry_and_cascade():
    e = logic.save_entry(title="doomed")
    tag = logic.create_tag("t")
    logic.set_entry_tags(e["id"], [tag["id"]])
    logic.add_attachment(e["id"], "a.png", "image/png", b"12345")
    assert logic.delete_entry(e["id"]) is True
    assert logic.get_entry(e["id"]) is None
    # attachments + tag links go too (sqlite FKs are off; delete_entry sweeps them)
    conn = sqlite3.connect(logic.DB_PATH)
    assert conn.execute("SELECT COUNT(*) FROM attachments WHERE entry_id=?", (e["id"],)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM entry_tags WHERE entry_id=?", (e["id"],)).fetchone()[0] == 0
    conn.close()
    # the tag itself survives (only the link was removed)
    assert any(t["id"] == tag["id"] for t in logic.list_tags())


def test_delete_missing_entry_false():
    assert logic.delete_entry(123) is False


# ---- listing + search ------------------------------------------------------------------

def test_list_newest_first_by_date_then_id():
    a = logic.save_entry(date="2026-07-01", title="a")
    b = logic.save_entry(date="2026-07-03", title="b")
    c = logic.save_entry(date="2026-07-02", title="c")
    order = [e["id"] for e in logic.list_entries()]
    assert order == [b["id"], c["id"], a["id"]]


def test_list_carries_preview_not_body_plus_att_count():
    e = logic.save_entry(title="t", body="# Head\n- item **bold**")
    logic.add_attachment(e["id"], "x.png", "image/png", b"aa")
    row = logic.list_entries()[0]
    assert "body" not in row                       # the list is lightweight
    assert row["attachment_count"] == 1
    assert "#" not in row["preview"] and "**" not in row["preview"]
    assert "Head" in row["preview"] and "item" in row["preview"]


def test_search_matches_title_body_or_date():
    logic.save_entry(date="2026-07-05", title="Groceries", body="milk and eggs")
    logic.save_entry(date="2026-08-05", title="Other", body="nothing here")
    assert {e["title"] for e in logic.list_entries(query="groceries")} == {"Groceries"}
    assert {e["title"] for e in logic.list_entries(query="eggs")} == {"Groceries"}
    assert {e["title"] for e in logic.list_entries(query="2026-07")} == {"Groceries"}
    assert {e["title"] for e in logic.list_entries(query="2026")} == {"Groceries", "Other"}
    assert logic.list_entries(query="zzz-no-match") == []


# ---- markdown-stripping for previews ---------------------------------------------------

def test_strip_md_drops_syntax():
    out = logic._strip_md("# Title\n- item\n1. num\n**bold** *it* `code` \\* lit")
    for junk in ("#", "**", "`", "- ", "1. "):
        assert junk not in out
    for word in ("Title", "item", "num", "bold", "it", "code", "lit"):
        assert word in out


def test_snippet_centres_on_match_and_handles_empty():
    long = "start " + "x " * 60 + "NEEDLE tail"
    s = logic._snippet(long, "NEEDLE")
    assert "NEEDLE" in s and s.startswith("…")
    assert logic._snippet("", "x") == ""
    assert logic._snippet("   \n  ", "") == ""


# ---- tags ------------------------------------------------------------------------------

def test_create_tag_normalises_and_dedupes():
    t = logic.create_tag("  Deep   Work  ", emoji="book")
    assert t["name"] == "Deep Work"               # inner whitespace collapsed
    assert t["emoji"] == "book" and t["entry_count"] == 0
    for dup in ("Deep Work", "deep work"):        # unique, case-insensitive
        try:
            logic.create_tag(dup)
        except ValueError:
            pass
        else:
            assert False, f"expected duplicate {dup!r} to be rejected"


def test_create_tag_empty_name_rejected():
    for bad in ("", "   "):
        try:
            logic.create_tag(bad)
        except ValueError:
            pass
        else:
            assert False, "expected empty name to raise"


def test_list_tags_counts_and_sorts():
    work = logic.create_tag("work")
    home = logic.create_tag("home")
    e = logic.save_entry(title="t")
    logic.set_entry_tags(e["id"], [work["id"]])
    names = [t["name"] for t in logic.list_tags()]
    assert names == ["home", "work"]              # sorted NOCASE
    counts = {t["name"]: t["entry_count"] for t in logic.list_tags()}
    assert counts == {"home": 0, "work": 1}


def test_update_tag_partial():
    t = logic.create_tag("old", emoji="book")
    logic.update_tag(t["id"], name="new")         # emoji left alone
    row = next(x for x in logic.list_tags() if x["id"] == t["id"])
    assert row["name"] == "new" and row["emoji"] == "book"
    logic.update_tag(t["id"], emoji="star")       # name left alone
    row = next(x for x in logic.list_tags() if x["id"] == t["id"])
    assert row["name"] == "new" and row["emoji"] == "star"
    assert logic.update_tag(t["id"]) is None       # nothing to change → no-op


def test_update_tag_conflict_and_missing():
    logic.create_tag("taken")
    t = logic.create_tag("free")
    try:
        logic.update_tag(t["id"], name="taken")
    except ValueError:
        pass
    else:
        assert False, "expected rename-to-duplicate to raise"
    try:
        logic.update_tag(999, name="whatever")
    except KeyError as exc:
        assert exc.args[0] == "no tag 999"
    else:
        assert False, "expected KeyError for missing tag"


def test_delete_tag_unlinks_without_usage_check():
    t = logic.create_tag("temp")
    e = logic.save_entry(title="t")
    logic.set_entry_tags(e["id"], [t["id"]])
    assert logic.delete_tag(t["id"]) is True       # no in-use guard, by design
    assert logic.get_entry(e["id"])["tags"] == []  # link gone, entry intact
    assert logic.delete_tag(t["id"]) is False       # already gone


def test_set_entry_tags_dedupes_and_drops_unknown():
    a = logic.create_tag("a")
    b = logic.create_tag("b")
    e = logic.save_entry(title="t")
    rows = logic.set_entry_tags(e["id"], [b["id"], a["id"], b["id"], 99999])
    # unknown 99999 dropped; b + a kept, deduped; result sorted by name
    assert [r["name"] for r in rows] == ["a", "b"]
    # replacing wipes the old set
    rows = logic.set_entry_tags(e["id"], [a["id"]])
    assert [r["name"] for r in rows] == ["a"]


# ---- attachments -----------------------------------------------------------------------

def test_add_attachment_metadata_and_inline_gate():
    e = logic.save_entry(title="t")
    png = logic.add_attachment(e["id"], "pic.png", "image/png", b"\x89PNG-bytes")
    assert png["size"] == len(b"\x89PNG-bytes")
    assert png["inline"] is True
    assert png["url"] == f"/polaris/media/{png['id']}"
    svg = logic.add_attachment(e["id"], "x.svg", "image/svg+xml", b"<svg/>")
    assert svg["inline"] is False                  # svg never renders inline (XSS gate)


def test_add_attachment_to_missing_entry_raises():
    try:
        logic.add_attachment(555, "f", "image/png", b"x")
    except KeyError as exc:
        assert exc.args[0] == "no entry 555"
    else:
        assert False, "expected KeyError"


def test_get_and_delete_attachment_roundtrips_bytes():
    e = logic.save_entry(title="t")
    a = logic.add_attachment(e["id"], "f.png", "image/png", b"rawbytes")
    fn, mime, data = logic.get_attachment(a["id"])
    assert (fn, mime, data) == ("f.png", "image/png", b"rawbytes")
    assert logic.delete_attachment(a["id"]) is True
    assert logic.get_attachment(a["id"]) is None
    assert logic.delete_attachment(a["id"]) is False


def test_list_attachments_ordered():
    e = logic.save_entry(title="t")
    first = logic.add_attachment(e["id"], "1", "image/png", b"a")
    second = logic.add_attachment(e["id"], "2", "image/png", b"b")
    assert [a["id"] for a in logic.list_attachments(e["id"])] == [first["id"], second["id"]]


# ---- backups + schema guard (the rollback safety net) ----------------------------------

def test_snapshot_none_when_no_db():
    assert logic.snapshot_db("x") is None


def test_snapshot_creates_consistent_copy():
    logic.save_entry(title="keep")
    dest = logic.snapshot_db("manual")
    assert dest and os.path.exists(dest)
    conn = sqlite3.connect(dest)
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1
    conn.close()


def test_schema_guard_snapshots_pre_change_bytes_on_version_bump():
    logic.save_entry(title="before-bump")          # creates DB stamped at SCHEMA_VERSION
    # simulate an older on-disk schema version, then force the guard to re-run
    conn = sqlite3.connect(logic.DB_PATH)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    logic._schema_checked = False

    logic.save_entry(title="after-bump")            # next access triggers the guard

    snaps = glob.glob(os.path.join(logic.BACKUP_DIR, f"polaris-pre-v{logic.SCHEMA_VERSION}-*.db"))
    assert snaps, "expected a pre-change snapshot"
    # the snapshot holds the PRE-change state (one entry), the DB has both now
    snap = sqlite3.connect(snaps[0])
    assert snap.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1
    snap.close()
    conn = sqlite3.connect(logic.DB_PATH)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == logic.SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 2
    conn.close()


def test_schema_guard_adds_missing_v2_columns():
    # a v1-shaped DB: entries without reminder_date, tags without emoji
    os.makedirs(logic.DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(logic.DB_PATH)
    conn.executescript(
        "CREATE TABLE entries (id INTEGER PRIMARY KEY AUTOINCREMENT, entry_date TEXT NOT NULL,"
        " title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',"
        " created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
        "CREATE TABLE tags (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " name TEXT NOT NULL UNIQUE COLLATE NOCASE, created_at TEXT NOT NULL);"
        "PRAGMA user_version = 1;")
    conn.commit()
    conn.close()
    logic._schema_checked = False

    e = logic.save_entry(title="x", reminder="2026-07-20")   # needs reminder_date column
    assert logic.get_entry(e["id"])["reminder"] == "2026-07-20"
    t = logic.create_tag("y", emoji="book")                  # needs emoji column
    assert next(x for x in logic.list_tags() if x["id"] == t["id"])["emoji"] == "book"


def test_daily_backup_writes_then_skips_when_unchanged():
    assert logic.daily_backup() is None            # no DB yet
    logic.save_entry(title="a")
    first = logic.daily_backup()
    assert first and os.path.exists(first)
    assert logic.daily_backup() is None            # DB unchanged since last daily → skip


def test_daily_backup_prunes_to_keep():
    logic.save_entry(title="seed")
    os.makedirs(logic.BACKUP_DIR, exist_ok=True)
    old = time.time() - 86400
    for i in range(BACKUP_OVERFLOW := logic.BACKUP_KEEP_DAILY + 6):
        p = os.path.join(logic.BACKUP_DIR, f"polaris-daily-20200101-{i:06d}.db")
        open(p, "wb").close()
        os.utime(p, (old, old))                    # older than the (now) DB
    logic.save_entry(entry_id=1, title="touch")    # make the DB newer than every fake
    logic.daily_backup()                           # writes one, prunes the oldest
    kept = glob.glob(os.path.join(logic.BACKUP_DIR, "polaris-daily-*.db"))
    assert len(kept) == logic.BACKUP_KEEP_DAILY


# ---- shared-worker interface -----------------------------------------------------------

def test_schedule_fingerprint_is_stable_and_versioned():
    fp = logic.schedule_fingerprint()
    assert fp == logic.schedule_fingerprint()
    assert str(logic.SCHEMA_VERSION) in fp


def test_reschedule_installs_the_job_idempotently():
    class FakeJob:
        def __init__(self, jid, sched):
            self.id, self._s = jid, sched
        def remove(self):
            self._s.jobs.remove(self)

    class FakeSched:
        def __init__(self):
            self.jobs = []
        def get_jobs(self):
            return list(self.jobs)
        def add_job(self, fn, trigger, id):        # noqa: A002 - matches apscheduler's kw
            self.jobs.append(FakeJob(id, self))

    s = FakeSched()
    logic.reschedule(s)
    logic.reschedule(s)                            # re-run must not duplicate the job
    ids = [j.id for j in s.get_jobs()]
    assert ids == [logic.BACKUP_JOB_ID]


# ---- health ----------------------------------------------------------------------------

def test_status_reports_counts_and_last_backup():
    e = logic.save_entry(date="2026-07-09", title="t")
    logic.add_attachment(e["id"], "f", "image/png", b"1234567")
    logic.snapshot_db("daily")
    st = logic.status()
    assert st["ok"] is True
    assert st["entries"] == 1
    assert st["latest_entry"] == "2026-07-09"
    assert st["attachments"] == 1
    assert st["attachment_bytes"] == 7
    assert st["schema_version"] == logic.SCHEMA_VERSION
    assert st["backups"] >= 1 and st["last_backup"].startswith("polaris-")
