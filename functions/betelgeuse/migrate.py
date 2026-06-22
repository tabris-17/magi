"""CLI face of the migration engine (see core/migrate.py).

    python3 migrate.py new     <slug> [--data]      # scaffold next migration + bump version
    python3 migrate.py status  --env dev
    python3 migrate.py up      --env prod [--to N] [--dry-run]
    python3 migrate.py down    --env dev  --to N
    python3 migrate.py history --env dev
    python3 migrate.py prune   --env prod [--keep N]

`--env dev|prod` is mandatory for consistency with app.py / worker.py (and to make "which
machine am I migrating?" explicit); the engine itself acts on `core.db.DATABASE`. This is
what `deploy/deploy.sh` runs over SSH on the mini to migrate prod *before* restarting it.
The one exception is `new`: it only *authors* a migration file (and bumps DB_SCHEMA_VERSION)
— it opens no DB, so it takes no `--env`.
"""
import argparse
import os
import re
import sys

from core import db, migrate


# ── scaffolding: `migrate.py new` ───────────────────────────────────────────────
# Creating a migration + bumping DB_SCHEMA_VERSION is ONE step here: any DB change —
# a schema change OR a one-off data augmentation — gets versioned automatically, so the
# head==const test (and the boot guard) stay honest without a separate manual bump.
_ROOT = os.path.dirname(os.path.abspath(__file__))
_MIGRATIONS_DIR = os.path.join(_ROOT, 'migrations')
_DB_PY = os.path.join(_ROOT, 'core', 'db.py')

_SCHEMA_TEMPLATE = '''"""{slug} — VERSION {version}.

Scaffolded by `migrate.py new {slug}`. Fill in up()/down(), then
`python3 migrate.py up --env dev` to apply (backs up first).
"""
from core.migrate import Irreversible

VERSION = {version}
DESCRIPTION = "TODO: summary of {slug}"


def up(c):
    # Schema change, additive by default. Examples:
    #   c.execute('ALTER TABLE portfolio ADD COLUMN foo TEXT')
    #   c.execute('CREATE TABLE IF NOT EXISTS bar (id INTEGER PRIMARY KEY)')
    raise NotImplementedError("fill in up() for migration {version}")


def down(c):
    # Revert up(); if not safely reversible, raise Irreversible(...) and rely on the
    # automatic pre-migration backup. NEVER drop portfolio/transactions.
    raise Irreversible(
        "migration {version} has no usable down(); restore the pre-migration backup")
'''

_DATA_TEMPLATE = '''"""{slug} — VERSION {version} (data migration).

Scaffolded by `migrate.py new --data {slug}`. DATA migration: up() does only INSERT/UPDATE
(no DDL). Keep it DETERMINISTIC (no network/clock/randomness) — do heavy/networked computation
on dev and bake the RESULT as literal rows so dev and prod converge to the same state. Apply
with `python3 migrate.py up --env dev` (backs up first). Never copy a dev DB up to prod.
"""
from core.migrate import Irreversible

VERSION = {version}
DESCRIPTION = "TODO: summary of {slug}"

# _ROWS = [( ... )]   # for a seed migration, bake the dev-computed result here


def up(c):
    # Pure data ops — two flavours:
    #   transform : c.execute('UPDATE portfolio SET ... WHERE ...')        # from current rows
    #   seed      : c.executemany('INSERT OR IGNORE INTO t VALUES (?,?)', _ROWS)
    raise NotImplementedError("fill in up() for data migration {version}")


def down(c):
    # Undo the data change if you can identify it; else Irreversible + restore the backup.
    raise Irreversible(
        "data migration {version} can't be reverted precisely; restore the pre-migration backup")
'''


def _slugify(s):
    """Normalize a human name into a kebab-case slug (letters/digits only)."""
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')


def _next_version(migrations_dir):
    """Highest NNN_ prefix in migrations_dir + 1 (directory-local, no imports)."""
    versions = [int(m.group(1)) for m in
                (re.match(r'(\d+)_.*\.py$', f) for f in os.listdir(migrations_dir)) if m]
    return max(versions) + 1 if versions else 2


def _render_template(version, slug, data):
    tpl = _DATA_TEMPLATE if data else _SCHEMA_TEMPLATE
    return tpl.replace('{version}', str(version)).replace('{slug}', slug)


def _bump_schema_version(version, db_py=_DB_PY):
    """Rewrite the `DB_SCHEMA_VERSION = N` line in core/db.py. True on success."""
    with open(db_py) as f:
        src = f.read()
    new_src, n = re.subn(r'(?m)^DB_SCHEMA_VERSION\s*=\s*\d+',
                         f'DB_SCHEMA_VERSION = {version}', src)
    if n != 1:
        return False
    with open(db_py, 'w') as f:
        f.write(new_src)
    return True


def _scaffold(slug, data=False, migrations_dir=_MIGRATIONS_DIR, db_py=_DB_PY):
    """Create migrations/NNN_<slug>.py AND bump DB_SCHEMA_VERSION to NNN. Returns exit code."""
    clean = _slugify(slug)
    if not clean:
        print('error: slug must contain letters or digits', file=sys.stderr)
        return 1
    version = _next_version(migrations_dir)
    fname = f'{version:03d}_{clean.replace("-", "_")}.py'
    path = os.path.join(migrations_dir, fname)
    if os.path.exists(path):
        print(f'error: {path} already exists', file=sys.stderr)
        return 1
    with open(path, 'w') as f:
        f.write(_render_template(version, clean, data))
    bumped = _bump_schema_version(version, db_py)
    print(f'created {"data" if data else "schema"} migration  migrations/{fname}  (VERSION {version})')
    if bumped:
        print(f'bumped DB_SCHEMA_VERSION -> {version} in core/db.py')
    else:
        print(f'WARNING: could not bump DB_SCHEMA_VERSION; set it to {version} in {db_py} by hand',
              file=sys.stderr)
    print('next: fill in up()/down(), then  python3 migrate.py up --env dev')
    return 0


def _print_status(conn):
    st = migrate.status(conn)
    print(f"DB schema: v{st['current']}   code head: v{st['head']}   gate: {st['gate']}")
    if st['pending']:
        print(f"  {len(st['pending'])} pending migration(s):")
        for p in st['pending']:
            print(f"    -> v{p['version']:<3} {p['name']}  — {p['description']}")
    else:
        print("  up to date — nothing pending.")
    if st['gate'] == 'DB_NEWER':
        print("  WARNING: the DB is NEWER than this code. Deploy newer code or restore a "
              "backup; never downgrade live data.", file=sys.stderr)


def _print_result(res):
    if res.get('direction') == 'none' or not res.get('steps'):
        if 'error' in res:
            print(f"  ERROR: {res['error']}", file=sys.stderr)
            return
        print(f"already at v{res['to']} — nothing to do.")
        return
    print(f"migrated v{res['from']} -> v{res['to']} ({res['direction']}); "
          f"{len(res['steps'])} step(s): " + ", ".join(f"v{s['version']}" for s in res['steps']))
    if res.get('backup'):
        print(f"  backup: {res['backup']}")
    if res.get('pruned'):
        print(f"  pruned {len(res['pruned'])} old backup(s).")
    if res.get('error'):
        print(f"  ERROR: {res['error']} (stopped at v{res['to']}; restore the backup above "
              "to roll back).", file=sys.stderr)


def main(argv=None):
    # --env lives on a shared parent so it can be given after the subcommand
    # (e.g. `migrate.py up --env prod`), matching app.py / worker.py ergonomics.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--env', required=True, choices=['dev', 'prod'],
                        help='runtime mode (mandatory): which instance you are migrating')

    p = argparse.ArgumentParser(description='Betelgeuse DB migrations')
    sub = p.add_subparsers(dest='cmd', required=True)
    nw = sub.add_parser('new', help='scaffold the next migration file + bump DB_SCHEMA_VERSION '
                                    '(authoring only — no --env, touches no DB)')
    nw.add_argument('slug', help='short name, e.g. add-price-alerts')
    nw.add_argument('--data', action='store_true',
                    help='scaffold a DATA migration (INSERT/UPDATE seed/backfill), not schema DDL')
    sub.add_parser('status', parents=[common], help='show current/head version + pending list')
    up = sub.add_parser('up', parents=[common], help='apply pending migrations (default: to head)')
    up.add_argument('--to', type=int, default=None, help='target version (default: head)')
    up.add_argument('--dry-run', action='store_true', help='show the plan without applying')
    dn = sub.add_parser('down', parents=[common], help='revert migrations down to --to')
    dn.add_argument('--to', type=int, required=True, help='target version to downgrade to')
    sub.add_parser('history', parents=[common], help='show the applied-migration ledger')
    pr = sub.add_parser('prune', parents=[common], help='delete old pre-migration backups')
    pr.add_argument('--keep', type=int, default=5, help='how many backups to keep (default 5)')
    args = p.parse_args(argv)

    if args.cmd == 'new':          # pure authoring — never opens/creates a DB, so no --env
        return _scaffold(args.slug, data=args.data)

    db.init_db()   # ensure infra + bootstrap the ledger on an existing DB (never migrates)
    conn = db.get_db_connection()
    try:
        if args.cmd == 'status':
            _print_status(conn)
            return 0
        if args.cmd == 'history':
            rows = migrate.history(conn)
            if not rows:
                print("(no migrations recorded)")
            for r in rows:
                print(f"v{r['version']:<3} {r['applied_at']}  {r['name']}  — {r['description']}")
            return 0
        if args.cmd == 'prune':
            removed = migrate.prune_backups(args.keep)
            print(f"pruned {len(removed)} backup(s); kept the {args.keep} newest.")
            for r in removed:
                print(f"  rm {r}")
            return 0
        if args.cmd == 'up':
            if args.dry_run:
                direction, steps = migrate.plan(conn, args.to)
                if not steps:
                    print("[dry-run] nothing to do.")
                else:
                    print(f"[dry-run] would {direction}: " +
                          ", ".join(f"v{m.version}({m.name})" for m in steps))
                return 0
            res = migrate.apply(conn, target=args.to)
            _print_result(res)
            return 1 if 'error' in res else 0
        if args.cmd == 'down':
            res = migrate.apply(conn, target=args.to)
            _print_result(res)
            return 1 if 'error' in res else 0
    finally:
        conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
