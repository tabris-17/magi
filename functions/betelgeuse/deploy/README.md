# Deploying Betelgeuse to the Mac mini

The Mac mini is the **always-on server**: it runs the web UI (`serve.py`, via waitress)
and the background worker (`worker.py`, the scheduler) as auto-starting LaunchAgents.
Your MacBook is the **dev machine + a client** — you browse to the mini over the LAN.

```
MacBook (dev)  --rsync over SSH-->  Mac mini (server, always awake)
                                     ├─ com.betelgeuse.web     → http://<mini>.local:8000
                                     └─ com.betelgeuse.worker  → notifications/scheduler
```

The mini's **runtime data** lives under `data/` (`config.DATA_DIR` — the live
`data/portfolio.db` + backups, the chart render-cache, backtest snapshots, and logs). It is
the **live source of truth** and is never overwritten by a deploy (rsync excludes the whole
`data/` dir). Override the location with the `BETELGEUSE_DATA_DIR` env var; the default is
`<repo>/data`.

> **Upgrading an existing deploy to the `data/` layout?** See *One-time data relocation* below
> — the live DB must be moved into `data/` before the new code starts.

---

## One-time setup (first deploy)

1. **On the MacBook**, create your config:
   ```bash
   cp deploy/config.example.sh deploy/config.sh
   # edit deploy/config.sh: MINI_USER, MINI_HOST, REMOTE_DIR
   ```
2. **Enable Remote Login on the mini**: System Settings → General → Sharing → *Remote Login* (SSH) ON.
   Test from the MacBook: `ssh <user>@<mini>.local` should connect.
3. **First push** (creates the remote folder, copies code — not the DB):
   ```bash
   bash deploy/deploy.sh        # the service-restart step will say "not bootstrapped yet" — expected
   ```
4. **Seed the database once — first-time bootstrap ONLY.** This is the *single* legitimate
   dev→prod DB copy (the mini has no data yet). From the MacBook:
   ```bash
   source deploy/config.sh
   ssh "${MINI_USER}@${MINI_HOST}" "mkdir -p '${REMOTE_DIR}/data'"
   scp data/portfolio.db "${MINI_USER}@${MINI_HOST}:${REMOTE_DIR}/data/portfolio.db"
   ```
   *(Skip this to let the mini create a fresh, empty DB at the current schema — it will.)*
   > ⚠ **Never repeat this as a routine sync.** After the mini owns live data, the only
   > sanctioned DB movement is `update-from-prod.sh` (mini→MacBook). Schema changes go
   > through migrations (see *Schema migrations* below), never by copying a DB file.
5. **Bootstrap the services on the mini** (venv, deps, LaunchAgents, disable sleep):
   ```bash
   ssh <user>@<mini>.local "cd <REMOTE_DIR> && bash deploy/setup-mini.sh"
   ```
6. **Allow incoming connections** if macOS firewall prompts for `python`/`waitress` → Allow.
   (System Settings → Network → Firewall.)
7. **Verify from the MacBook**: open `http://<mini>.local:8000/`.

---

## Routine deploys (after the first)

From the MacBook:
```bash
bash deploy/deploy.sh
```
This rsyncs the code, refreshes deps, and restarts both services. The DB, venv, and chart
cache on the mini are left untouched.

> With the Claude Code skill installed you can also just say **`/deploy`** (prod) or
> **`/deploy dev`** (run locally on the MacBook).

---

## Runtime data layout (`data/` — done in v1.5.0)

Runtime data lives under `data/` (`config.DATA_DIR`): `data/portfolio.db` (live DB),
`data/charts` (render cache), `data/backtest` (training snapshots), `data/logs`, and
`data/backup` (pre-migration + pulled-from-prod DB backups). It's resolved from `__file__`
(not the CWD) and overridable via `BETELGEUSE_DATA_DIR`. The whole `data/` tree is gitignored
and rsync-excluded, so a deploy never touches it.

> **Historical note (v1.5.0 cutover — already completed, no longer in the tree):** earlier deploys
> kept the DB at the top-level `<repo>/portfolio.db`. The one-time move into `data/` was done by a
> guarded `deploy/relocate-data-once.sh` invoked from `deploy.sh` (snapshot → stop services → move →
> `setup-mini.sh` to restart). Both the script and its `deploy.sh` call were removed once prod was
> cut over. If you ever provision a *brand-new* machine, there's nothing to do — fresh installs create
> `data/` directly. Old `static/chart_*` render-cache files from before the move are harmless.

---

## Schema migrations

Schema changes are **versioned, reversible migrations** — never hand-edited DB files. Definitions
live in `migrations/00N_*.py` (git); what has run on a given DB is recorded in its
`schema_migrations` table. `core/db.py` holds `DB_SCHEMA_VERSION` (the head the code expects).

**The app refuses to run against a mismatched DB.** On a version gap the worker exits and the web
process serves a maintenance page until you migrate — so migrating is a deliberate step, not a
silent startup side effect.

```bash
python3 migrate.py status  --env dev|prod    # current vs head + pending list
python3 migrate.py up       --env dev|prod    # apply pending (backs up first, prunes old backups)
python3 migrate.py down     --env dev  --to N # revert to vN (backup first; some steps irreversible)
python3 migrate.py history  --env dev|prod    # the applied ledger
```

- **Dev** can also use **Settings → Admin → Database Migrations** (buttons + history).
- **Prod** is migrated automatically by `deploy/deploy.sh` (it runs `migrate.py up --env prod`
  **before** restarting services, and aborts the deploy if the migration fails). The web panel is
  read-only on prod.
- Every up/down takes a timestamped `portfolio.db.premigrate-*` backup first; if a migration errors,
  the DB is left at the last good version and you can restore that backup.

**Adding a migration:** create `migrations/00N_<slug>.py` (numeric prefix == target `VERSION`) with
`VERSION`, `DESCRIPTION`, `up(cursor)`, and `down(cursor)` (or `raise Irreversible(...)`), then bump
`DB_SCHEMA_VERSION` in `core/db.py` to match. Back up `portfolio.db` before testing locally.

---

## Quick reference — all six actions

Web + worker are managed together (the "client" is just a browser pointing at the web server).
With the Claude Code skill, say the command in the table; the bare commands follow.

| Action       | Dev (MacBook)              | Prod (mini)                          |
|--------------|----------------------------|--------------------------------------|
| Push         | — (use restart)            | `/deploy`                            |
| Restart      | `/deploy restart dev`      | `/deploy restart` (no code push)     |
| Stop         | `/deploy stop dev`         | `/deploy stop`                       |
| Pull prod DB | `/update-from-prod`        | —                                    |
| Migrate DB   | `migrate.py up --env dev` / Settings panel | done by `/deploy` (before restart)   |

- **Push to prod** = rsync + restart both services (`bash deploy/deploy.sh`).
- **Restart dev** = kill this repo's `app.py`/`worker.py`, relaunch both in the background
  **with the mandatory mode flag**: `python3 app.py --env dev` + `python3 -u worker.py --env dev`.
- **Restart prod (only)** = `launchctl kickstart -k` both LaunchAgents (see below).
- **Stop dev** = `pkill -f "$(pwd).*app.py"; pkill -f "$(pwd).*worker.py"`.
- **Stop prod** = `launchctl bootout` both LaunchAgents (see below).
- **Pull prod DB** = `bash deploy/update-from-prod.sh` — snapshot the mini's live DB and swap it
  into local (backs up the old local DB first). The mini's DB is never written.

> **Runtime mode is mandatory.** Every entrypoint requires `--env dev|prod` and exits without it.
> Dev launches pass `--env dev`; the prod LaunchAgents pass `--env prod` via their plists. After
> deploying this change the first time, **re-run `bash deploy/setup-mini.sh` on the mini once** so
> the LaunchAgents pick up the new `--env prod` argument.

## Managing the services (on the mini)

```bash
uid=$(id -u)
launchctl kickstart -k gui/$uid/com.betelgeuse.web      # restart web
launchctl kickstart -k gui/$uid/com.betelgeuse.worker   # restart worker
launchctl print gui/$uid/com.betelgeuse.worker          # status
launchctl bootout gui/$uid/com.betelgeuse.web           # stop web
launchctl bootout gui/$uid/com.betelgeuse.worker        # stop worker
tail -f data/logs/worker.out.log                        # watch the scheduler
```

---

## Notes & gotchas

- **LaunchAgents run in your login session.** Keep the mini **logged in** (enable automatic
  login: System Settings → Users & Groups → Automatic login) so the services come back after a
  reboot. The screen can be **locked** — locking doesn't stop processes; only *system sleep* does,
  which `setup-mini.sh` disables.
- **Never** add `--delete-excluded` to the rsync or un-exclude `data/` / `portfolio.db` — that would
  clobber the mini's live data with your dev copy. (The deploy's pre-flight gate aborts on any
  `portfolio.db` line as a backstop.)
- **Two-way data:** the mini owns the live data. To run dev on real data, pull it down with
  `bash deploy/update-from-prod.sh` (mini→MacBook only; it backs up your local DB first and warns
  on a schema-version gap). Never copy a DB the other way except the one-time bootstrap (step 4).
- **Remote (off-LAN) access** isn't set up here (LAN-only by choice). If you want it later, install
  Tailscale on both Macs and use the mini's Tailscale name instead of `<mini>.local`.
