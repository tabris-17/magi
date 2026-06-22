"""Versioned, reversible DB schema migrations.

Each module here is one migration step and exposes:

    VERSION       int  — the schema version this migration brings the DB *to*
    DESCRIPTION   str  — one-line human summary (shown in /migrate status + the panel)
    up(cursor)         — apply the change
    down(cursor)       — revert it, OR `raise core.migrate.Irreversible(msg)` when it
                         cannot be safely reversed (the pre-migration backup is then the
                         rollback path).

**Convention:** the numeric filename prefix equals `VERSION`. The one exception is the
baseline (`002_baseline.py`, VERSION 2): numbering starts at 002 because v1 predates this
framework, so the baseline folds the whole pre-framework schema into a single frozen step.
Add the next change as `003_<slug>.py` (VERSION 3) and bump `DB_SCHEMA_VERSION` in core/db.py.

Definitions live here (git — the reviewable "what changed" history); the per-DB record of
what has actually been applied lives in the `schema_migrations` table. See core/migrate.py.
"""
