# Deploying magi to the Mac mini

The Mac mini is the **always-on server**: it runs the whole unified app — one web
process (`serve.py`, via waitress, serving the host + every mounted function) plus
each function's background worker (currently betelgeuse's scheduler) as auto-starting
LaunchAgents. Your MacBook is the **dev machine + a client**.

```
MacBook (dev)  --rsync over SSH-->  Mac mini (server, always awake)
                                     ├─ com.magi.web              → http://<mini>.local:8080  (host + /youtube + /betelgeuse)
                                     └─ com.magi.betelgeuse-worker → betelgeuse notifications/scheduler
```

Each function's **runtime data** lives under `functions/<name>/data/` (e.g.
`functions/betelgeuse/data/portfolio.db` + backups, chart cache, logs). It is the
**live source of truth** and is never overwritten by a deploy (rsync excludes every
`functions/*/data/`, and a pre-flight gate aborts if any `*.db` would be touched).

## The deploy-all + migration pattern

`./magi deploy [TARGET] [--env dev|prod]` is the front door (wrapping
`deploy/deploy.sh`). `TARGET ∈ all (default) | host | youtube | betelgeuse` scopes the
rsync + which services restart; the steps below describe the default `all`:

1. **rsync** the whole magi tree (code only — never any function's `data/`).
2. **install deps** into the mini's venv (host + every function's requirements).
3. **migrate every function** with `migrate_all.py up --env prod` — run **before**
   restarting. Functions are isolated and each owns its migrations, so `migrate_all`
   is an orchestrator: for each function that ships a `migrate.py`, it runs that
   migrator in the function's own dir (backs the DB up first). It **aborts on the
   first failure**, so a failed migration never leads to a restart on a bad DB — the
   old process keeps serving on its untouched DB.
4. **restart** `com.magi.web` + `com.magi.betelgeuse-worker` (`launchctl kickstart`).

betelgeuse refuses to serve a DB older than its code (its boot guard → maintenance
page), which is exactly why step 3 runs first.

```bash
# one-off, locally:
cp deploy/config.example.sh deploy/config.sh   # then edit MINI_USER/HOST/REMOTE_DIR/PORT

# routine deploy (host + all functions, migrations included):
./magi deploy                  # = ./magi deploy all --env prod
./magi deploy betelgeuse       # just one function
```

## Everyday workflow (`upgrade` / `launch` / `stop`)

High-level verbs over deploy/migrate (`dev` = this machine, `prod` = the mini):

```bash
./magi upgrade dev    # pull ALL prod DBs down to local dev (local backed up first;
                      #   consistent sqlite3 .backup over SSH; READ-ONLY on prod)
./magi upgrade prod   # deploy local code to the mini, no prod DB touched (= deploy all)
./magi launch dev     # run locally, foreground (werkzeug, 127.0.0.1)
./magi launch prod    # start/(re)start the mini's web + worker (ssh; no deploy)
./magi stop dev       # stop the local dev server
./magi stop prod      # stop the mini's web + worker (ssh bootout; no deploy) — restart via launch prod
./magi workflow       # upgrade dev -> upgrade prod -> launch dev   (the whole chain)
```

`upgrade dev` is the safe **prod→dev** DB refresh (mirror prod data locally); schema
only ever goes **up** to prod via migrations, never by copying a DB file up. The
`workflow` relies on `upgrade prod`'s own restart to launch prod (no redundant
kickstart) and ends with the local dev server in the foreground. It runs with no
confirmation prompt (`--yes`/`-y` is still accepted but a no-op). The whole CLI is also
one **Claude slash command** — `/magi <args>` (e.g.
`/magi workflow`, `/magi upgrade dev`) — passing its args straight to `./magi`.

## One-time setup (first deploy)

1. **Config:** `cp deploy/config.example.sh deploy/config.sh` and edit it.
2. **Enable Remote Login** on the mini (System Settings → General → Sharing →
   *Remote Login*). Test: `ssh <user>@<mini>.local`.
3. **First push:** `./magi deploy` (the restart step will say "not
   bootstrapped yet" — expected).
4. **Seed betelgeuse's DB once** (the single legitimate dev→prod DB copy; skip to let
   the mini build a fresh one at the current schema):
   ```bash
   source deploy/config.sh
   ssh "${MINI_USER}@${MINI_HOST}" "mkdir -p '${REMOTE_DIR}/functions/betelgeuse/data'"
   scp functions/betelgeuse/data/portfolio.db \
       "${MINI_USER}@${MINI_HOST}:${REMOTE_DIR}/functions/betelgeuse/data/portfolio.db"
   ```
   After this, schema changes go through migrations only — never copy a DB file up.
5. **Bootstrap services on the mini** (venv, deps, data dirs, LaunchAgents, no-sleep):
   ```bash
   ssh <user>@<mini>.local "cd <REMOTE_DIR> && bash deploy/setup-mini.sh"
   ```
6. **Verify:** open `http://<mini>.local:8080/`.
7. **ffmpeg for YouTube (optional).** The YouTube function needs `ffmpeg`/`ffprobe` for
   high-res merges + MP3. They're **vendored** on the mini under `~/magi/data/vendor/`
   (deploy-safe: `data/` is rsync-excluded + `--delete`-protected, so it's NOT shipped
   from dev — provision it per-mini). The `com.magi.web` LaunchAgent puts
   `__ROOT__/data/vendor` first on its `PATH` (launchd's default PATH omits it). Drop a
   static arm64 build there and reload web:
   ```bash
   ssh <user>@<mini>.local 'mkdir -p ~/magi/data/vendor && cd ~/magi/data/vendor && \
     for b in ffmpeg ffprobe; do curl -fsSL -o $b.zip \
       https://ffmpeg.martin-riedl.de/redirect/latest/macos/arm64/release/$b.zip && \
       unzip -oq $b.zip && rm $b.zip && chmod +x $b; done'
   # then: ./magi launch prod  (or re-run setup-mini.sh to regenerate + reload the plist)
   ```

## Migrations day-to-day

- Author a betelgeuse migration as before, inside the function:
  `cd functions/betelgeuse && python3 migrate.py new <slug>`.
- Check/apply across all functions from the magi root:
  ```bash
  ./magi migrate status --env dev
  ./magi migrate up     --env dev      # local  (wraps migrate_all.py)
  ```
- `./magi deploy` runs `migrate_all.py up --env prod` on the mini automatically.

A new function gets the same treatment for free: drop a `migrate.py` in its package
and `migrate_all` picks it up; add a worker LaunchAgent if it needs one.
