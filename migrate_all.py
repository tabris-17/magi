#!/usr/bin/env python3
"""magi migration pattern — run EVERY function's migrations in one command.

Mirrors betelgeuse's deploy-time `migrate.py up` step, lifted to the magi host so a
single deploy can migrate the whole app. Functions are isolated and each owns its
migrations, so this is an ORCHESTRATOR, not a unified migration store: for every
function that ships a `migrate.py`, it runs that migrator **as a subprocess in the
function's own directory**, so the function's imports/paths resolve exactly as they
do standalone (and the vendored copy stays in sync with its prod source).

Functions without migrations (e.g. youtube) are skipped. On the first function that
fails, it aborts non-zero and runs no further migrators — so a deploy that calls
`migrate_all.py up --env prod` before restarting never half-migrates then restarts.

    python3 migrate_all.py status --env dev
    python3 migrate_all.py up     --env prod [--dry-run]
    python3 migrate_all.py down   --env dev  --to N

The action + any flags after it are passed straight through to each function's
`migrate.py` (so `--env`, `--to`, `--dry-run`, … work as documented there).
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
FUNCTIONS_DIR = os.path.join(ROOT, "functions")


def discover():
    """Every function package that ships a migrate.py, in stable order."""
    out = []
    if not os.path.isdir(FUNCTIONS_DIR):
        return out
    for name in sorted(os.listdir(FUNCTIONS_DIR)):
        d = os.path.join(FUNCTIONS_DIR, name)
        if os.path.isfile(os.path.join(d, "migrate.py")):
            out.append((name, d))
    return out


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2

    action, passthrough = argv[0], argv[1:]
    funcs = discover()
    if not funcs:
        print("migrate_all: no functions ship migrations — nothing to do.")
        return 0

    print(f"migrate_all: '{action}' across {len(funcs)} function(s): "
          f"{', '.join(n for n, _ in funcs)}\n")
    for name, d in funcs:
        cmd = [sys.executable, "migrate.py", action, *passthrough]
        print(f"==> [{name}] {' '.join(cmd)}  (cwd={os.path.relpath(d, ROOT)})")
        rc = subprocess.run(cmd, cwd=d).returncode
        if rc != 0:
            print(f"\n!! [{name}] migrate.py {action} failed (rc={rc}) — aborting; "
                  f"no further functions migrated.", file=sys.stderr)
            return rc
        print()
    print("migrate_all: all functions migrated OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
