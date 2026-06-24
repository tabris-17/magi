# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

magi is a personal, fully local web **control panel** for a single machine. It's
a hub: each capability is a self-contained **function** selectable from a
GitHub-style sidebar. Everything runs locally; nothing is sent to third parties
beyond the requests a function itself makes.

The app is mid-migration toward a unified Flask host (see "Migration state").

## Running

`./magi` is the single CLI — one front door, with **high-level dev↔prod workflow**
verbs over **low-level** steps (`dev` = this machine, `prod` = the Mac mini):

```bash
# high-level workflow (see Deploy & migrations)
./magi upgrade dev         # copy ALL prod DBs down to local dev (local backed up first)
./magi upgrade prod        # deploy local code to the mini, no DB touched (= deploy all)
./magi launch dev          # run locally, foreground (werkzeug, 127.0.0.1)
./magi launch prod         # start/(re)start the app on the mini (ssh; no deploy)
./magi stop dev            # stop the local dev server
./magi stop prod           # stop the app on the mini (bootout its LaunchAgents; no deploy)
./magi workflow            # upgrade dev → upgrade prod → launch dev (the whole chain)

# low-level
./magi run --env dev       # dev:  werkzeug → http://127.0.0.1:8080 (this machine only)
./magi run --env prod      # prod: waitress, binds 0.0.0.0 (the mode the mini serves)
./magi deploy [TARGET] [--env dev|prod]      # → deploy/deploy.sh (see Deploy)
./magi migrate {status|up|down} [--env …]    # → migrate_all.py
./magi --help              # usage
```

Each subcommand passes its remaining args straight through to the tool it wraps
(`serve.py` / `deploy/deploy.sh` / `migrate_all.py` / `deploy/pull-prod-dbs.sh` /
`deploy/kickstart-mini.sh` / `deploy/stop-mini.sh` / `deploy/workflow.sh`), so every
flag those accept works unchanged. `MAGI_PORT` (default 8080) and `MAGI_HOST` override the bind. The whole CLI
is also a single **Claude slash command** (`.claude/commands/magi.md`): `/magi <args>`
passes its args straight to `./magi` (e.g. `/magi workflow`, `/magi upgrade dev`).

- **`serve.py`** is the launcher engine: `--env dev` → werkzeug on `127.0.0.1`,
  `--env prod` → waitress on `0.0.0.0` (default env is **prod**, for the LaunchAgent /
  bare runs). It sets `MAGI_ENV` **before** importing `magi`, so the mounted betelgeuse
  function starts in the right mode. The `com.magi.web` LaunchAgent calls `serve.py`
  directly (so launchd supervises the real server process); humans use `./magi run`.
- **`python3 magi.py`** still works as a dev shortcut — it just **delegates** to
  `serve.py` (defaults to `--env dev`), so the launch logic lives in one place.

Requirements: `pip install -r requirements.txt` (Flask + waitress + yt-dlp) **plus**
each function's deps (`pip install -r functions/betelgeuse/requirements.txt`).
`ffmpeg` is needed for the YouTube function.

There is **one front end** — the magi host. The old stdlib single-file SPA
(`start.sh` / `server.py` / `static/index.html`) has been **retired** (M4). The only
other process is betelgeuse's background worker, run separately (see Deploy).

### dev / prod mode

`MAGI_ENV` (`dev`|`prod`) is the host's mode — set by `./magi run --env …` (via
`serve.py`); `python3 magi.py` defaults it to `dev`. `magi.py` exposes it as `APP_ENV`, injected into the
shell via the context processor (`app_env`) and returned by `/api/settings` (`env`).
The sidebar account block shows a `🐱 <env>` chip (the cat is betelgeuse's), and **dev
wears a red brand** — `[data-env="dev"]` in `shell.css` puts a **red ring** around the
`.magi-avatar` + reddens the version label/sidebar edge, so a dev tab is never mistaken
for prod. The `.magi-avatar` is the magi brand icon (`/static/icon-512.png` as a
`background`, no more text "M"); the dev cue is a ring (was a red gradient fill). Host
pages set `data-env` server-side on `<html>`; betelgeuse pages set it via the pre-paint
script in `header.html` (it already gets `app_env`).

**Open-prod link.** In dev, the account block also renders a one-click **`open prod ↗`**
link (under the chip, accent-colored) pointing at the **same path** on the prod box —
`{{ prod_url.rstrip('/') }}{{ request.path }}`. The target is `PROD_URL` in `magi.py`
(`MAGI_PROD_URL` env override; default the mini's browser-resolvable Bonjour URL
`http://wklin3s-mac-mini.local:8080/` — NOT the `Macmini` SSH alias; set empty to hide
it). It's injected via the context processor (`prod_url`) and returned by `/api/settings`.
Rendered only on host shell pages (`base.html`) and only in dev; betelgeuse pages have
their own dev↔prod awareness (its `dev ▸ prod` title bar — see its CLAUDE.md).

## Architecture (the unified host)

The host owns **only** the shell and the shared settings page; each function is
isolated.

- **`magi.py`** — Flask host. Serves `/` (dashboard), `/settings` (shared), and
  `/health` (the aggregated Application Health page; see Application Health),
  and wires functions two ways: lightweight ones (youtube) are **blueprints**
  registered on the host Flask app; heavier formerly-standalone apps (betelgeuse)
  are mounted **unchanged** as WSGI sub-apps under a prefix via
  `DispatcherMiddleware` (so the vendored copy stays byte-identical to prod). The
  module must NOT be importable as `app` — that name belongs to betelgeuse's own
  `app.py`, which is imported with `functions/betelgeuse/` on `sys.path[0]`. Holds
  the `FUNCTIONS` list; a context processor injects `nav_functions` to build the
  sidebar. The server runs via `werkzeug.run_simple(..., threaded=True)` because
  the top-level WSGI object is a `DispatcherMiddleware`, not a Flask app.
- **`templates/`** + **`static/`** — the shell. `base.html` (sidebar markup,
  theme pre-paint script, content block), `home.html`, `settings.html`. CSS is
  split: **`shell.css`** = theme tokens + the sidebar/topbar chrome, with all
  classes **`magi-`-prefixed** and no body/global rules, so it can be loaded by a
  mounted sub-app without bleeding into its styles; **`theme.css`** = the host's
  own page components (cards, buttons, youtube, settings). Host pages load both;
  mounted sub-apps load only `shell.css`. **`shell.js`** = theme + mobile drawer +
  the Appearance picker. Shell DOM IDs are `magiSidebar` / `magiBackdrop` /
  `magiMenuBtn`.
- **`host/`** — the host's own package (named `host`, NOT `core`, to avoid colliding
  with betelgeuse's `core` when its dir is on `sys.path`). `host/version.py` holds the
  app version (`full_version()` → `magi-1.3.0`); `host/db.py` is the common-settings
  SQLite store at `data/magi.db` (key/value `settings` + a `meta` table stamping schema
  + app version; `ensure_schema()` is idempotent — no migration engine for the host yet).
- **`functions/<name>/`** — a self-contained function package. Nothing here
  imports the host or another function. Each contributes a **`META`** dict
  (`key`, `label`, `description`, `icon` SVG, `url`, and an optional **`version`**
  display string) the host reads for the sidebar/dashboard. Two styles (see Function
  contract): a lightweight **blueprint** (youtube) or a **mounted WSGI sub-app**
  (betelgeuse).

  **Per-function versioning.** Each function owns a `META["version"]` with its own
  short prefix — youtube → **`yd-1.0.0`**; betelgeuse → **`betelgeuse-app-<x>` ·
  `betelgeuse-server-<x>`** (composed in `magi.py` from betelgeuse's
  `core.version.app_version_string()`/`server_version_string()`, which wrap
  `WEB_VERSION`/`WORKER_VERSION`). The host treats the string as opaque, shows it on
  the dashboard card (`home.html`, `.card .v`) and in `/api/settings` (`functions[]`).
  This is distinct from the host's own `magi-1.3.0` in the sidebar footer.

### Function contract

Two styles, both registered in the `FUNCTIONS` list in **`magi.py`** and both
**URL-prefixed** (the shared `/settings` and `/` belong to the host):

- **Blueprint function** (lightweight, e.g. youtube): `__init__.py` exports `bp`
  (Flask Blueprint with its own `url_prefix` + `template_folder`) and `META`. Its
  templates live in `functions/<name>/templates/<name>/…` and `{% extends
  "base.html" %}` (Flask finds the host's `base.html`). Flask-free logic goes in a
  sibling module (e.g. `functions/youtube/logic.py`). Register with
  `app.register_blueprint(bp)`.
- **Mounted sub-app** (a formerly-standalone Flask app, e.g. betelgeuse): mounted
  unchanged via `DispatcherMiddleware` at its prefix. It keeps its OWN templates
  and renders the magi shell itself — it includes the shared `/static/shell.css`
  + `/static/shell.js` and emits the `magi-*` sidebar markup (see
  `functions/betelgeuse/templates/header.html`), with its pages as a sub-nav under
  the function. Its sidebar **function list is host-driven, NOT hardcoded**:
  `header.html` loops over the same `nav_functions` (+ `app_version`, `prod_url`)
  that `base.html` does, injected into the sub-app via a context processor
  registered on `betel_app` in `magi.py` (kept there so betelgeuse's own `app.py`
  stays byte-identical to prod). So a new function appears in betelgeuse's sidebar
  for free, at the same level as youtube/taxation — don't re-hardcode the list (the
  loop guards `nav_functions or []` so betelgeuse rendered standalone in its own
  pytest degrades gracefully). Because it doesn't extend `base.html`, it adds its own
  `body{padding-left:260px}` to clear the fixed sidebar. Its hardcoded absolute
  URLs must carry the prefix (`scripts/prefix_betelgeuse.py`).

### Shared settings (the only cross-function sharing)

Settings split by **lifetime/ownership**, surfaced at three places in the shell's
**Settings** sidebar section:

- **`/settings` (Appearance)** — the host's own look (theme) only. Nothing else.
- **`/tools/database` (Settings → Tools → Database)** — **global, DB-backed app settings**
  stored in `data/magi.db`: the same value in dev and prod (e.g. the taxation RBA URL).
  This is where each function's optional **`settings_section`** is aggregated (a callable on
  its `META` returning `{id, label, html}`; the function still owns its storage + save route,
  only the presentation is shared). `magi.py` route `tools_database` composes them under
  `templates/tools_database.html`. The sidebar shows a **Tools** parent (`base.html` AND
  betelgeuse's `header.html`, for parity) with a **Database** sub-nav item underneath.
  **Sidebar sub-navs expand only for the active section** (collapse-when-inactive): a parent
  reveals its children only when you're inside it — **Tools** shows **Database** only on
  `/tools/database` (gated `{% if active == 'database' %}` in `base.html`; absent from
  `header.html`, since you're never on that page while inside betelgeuse), and **Betelgeuse**
  shows its pages only on betelgeuse pages. Children are text-only and drawn with CSS tree
  connectors (`.magi-nav-sub .magi-nav-item::before/::after` in `shell.css` — a vertical rail
  + an elbow tick per child, last child = `└`), shared by both render sites.
- **The function's own page** — **env-scoped ("user profile") settings** (see below).

**APP SETTING vs USER PROFILE (the rule for where a setting lives).** A **global** setting
(one value, same dev/prod — e.g. `theme`, `taxation_rba_url`) is an *app setting* → it goes in
the DB and is edited centrally (Appearance for theme, **Tools → Database** for the rest, via
`settings_section`). An **env-scoped** setting (a separate value per environment — e.g.
`youtube_download_dir`) is a **user profile / per-machine** thing → it is **NOT** shown in
central settings or Tools → Database; it is **displayed and edited directly on the owning
function's own page** (the YouTube download folder is edited on `/youtube/` — its "Save to"
field has a **Save** button that persists `youtube_download_dir` for the box's *own* env via
`POST /api/settings {key,value}` with **no `env`**, so it writes the running env; blank clears
it to the default). **When the user calls a setting environment-specific (dev/prod), default to
this: edit it on the function page, don't add it to Tools → Database.**

**Storage (`host/db.py`).** `SETTINGS` is the single registry (`allowed`/`default`/`scoped`
per key); `/api/settings` (`GET` → `{version, env, prod_url, envs, env_config, functions,
settings}`, `POST {key,value[,env]}` → upsert, validated against the registry) is the one
endpoint. Env-scoped values share the one key/value table via a **`<key>@<env>` storage key**,
so dev and prod keep DISTINCT values even though `./magi upgrade dev` copies the whole `magi.db`
prod→dev — **no schema change, no migration engine** (which the host doesn't have).
`get_setting`/`set_setting(key, value, env=…)` resolve against `MAGI_ENV` by default;
`all_settings()` is the current-env-resolved flat map (the shell reads `theme` from it);
`env_config()` returns `{key:{dev,prod}}` (still in the `/api/settings` payload, no longer
rendered on a central page). The host injects a **resolver** into the otherwise self-contained
function (`magi.py` → `youtube_logic.set_download_dir_resolver`), so the function reads the
env-scoped setting **without importing the host** (precedence: setting → `MAGI_YOUTUBE_DIR` env
→ hardcoded default; see Conventions).

### Application Health (the second cross-function aggregation)

A dedicated **Health** sidebar page (`/health`, `templates/health.html`) aggregates the
runtime status of the host **and every function**, lifting betelgeuse's "Application
Health + dev-knows-prod" concept to the host. A function opts in with a **`health`
callable on its `META`** (parallel to `settings_section`); it returns an opaque dict the
host tags with the function's `label`/`version`. The host endpoints (`magi.py`):
- **`GET /api/health`** — `{host:{name,version,env,server_time,started_at,ok}, functions:[
  {key,label,version,ok,health|error}]}`. Each function's `health()` is called **guarded**
  (`ok=False`+`error` on raise; `ok=None` = no reporting) so one function can't break the
  page. youtube → `{ffmpeg, download_dir}`; betelgeuse → its **own `/api/health` reused
  unchanged**, called in-process via `betel_app.test_client()` (a 503 in maintenance still
  returns JSON, surfaced as-is — no betelgeuse code change, its tests stay green).
- **`GET /api/prod/health`** — on **dev** only, probes `PROD_URL + /api/health` server-side
  with **`urllib.request`** (stdlib — do NOT add `requests`), 3s timeout →
  `{configured, reachable, base_url, probed_at, health|error}`; mirrors betelgeuse's probe.
The page polls both every 60s and color-codes cards client-side (host green; youtube amber
if ffmpeg missing; betelgeuse from worker-liveness + schema-gate). The Health nav link is
emitted on host pages (`base.html`) AND betelgeuse pages (`header.html`) for parity.
`APP_START_TIME` (module load) is the host's `started_at`.

### Theming

`data-theme` (`dark`/`light`) on `<html>` selects the variable block in **`shell.css`**.
Persistence is **two-layer**: the **host DB is the source of truth** (`theme` in
`data/magi.db`, via `/api/settings`); **`localStorage` (`magi-theme`) is a no-flash
cache** read by the inline pre-paint script (in `base.html` and betelgeuse's
`header.html`). `shell.js` reconciles them: on load `syncFromServer()` pulls the DB
value (and fills the version label), and on change `applyTheme(pref, true)` writes
through to the DB. `system` resolves via `matchMedia`. **New UI must use the theme
tokens** (`var(--fg)`, `var(--surface)`, `var(--accent)`, …), never hardcoded colors.

**Versioning:** `host/version.py` → `full_version()` = `magi-1.3.0` (the host/shell
version, distinct from a function's own — see Per-function versioning above). Shown in
the sidebar footer (`#magiVersion`; server-rendered on host pages, JS-filled from
`/api/settings` on function pages) and returned by `/api/settings`. Bump it on
host-level changes. Function versions sit on the dashboard cards, not the footer.

**The dev/prod chip + red brand** live in `shell.css` keyed off `[data-env]` (see
"dev / prod mode" above) — both render sites (`base.html`, betelgeuse's `header.html`)
emit the `🐱 <env>` chip and set `data-env`.

### SSE downloads (YouTube function)

`/youtube/api/download` streams progress as Server-Sent Events. `logic.run_download`
spawns a worker thread that runs yt-dlp and pushes `(event, data)` tuples onto a
`queue.Queue`; it yields them (an internal `__end__` sentinel ends the stream),
and the route wraps each into an SSE frame via a `stream_with_context` generator.
Run the host `threaded=True` so streaming + concurrent requests work.

## Deploy & migrations (the deploy-all pattern)

magi deploys as ONE app to the always-on Mac mini, mirroring betelgeuse's original
pattern lifted to the host. Functions stay isolated — each owns its own migrations
and (if needed) its own worker process; the host orchestrates. `./magi deploy …` and
`./magi migrate …` are the CLI front doors for the two scripts below (args pass
straight through).

- **`deploy/deploy.sh [TARGET] [--env dev|prod]`** (`./magi deploy …`) — `TARGET ∈ all (default) | host |
  youtube | betelgeuse`. rsync scoped to the target (never any `functions/*/data/`; the
  **same** pre-flight `--dry-run` `*.db` gate + data protections run for every target,
  transfer-root-relative) → install host + function deps → **migrate the affected DB(s)
  BEFORE restart** (`all` → `migrate_all.py up`; `betelgeuse` → its own `migrate.py up`;
  `host`/`youtube` → none) → `launchctl kickstart` only the affected services (`web`
  always; `betelgeuse-worker` for `all`/`betelgeuse`). `--env` selects the **migration**
  env (default: `MAGI_ENV` from `config.sh`, else prod); `host` syncs the whole tree but
  excludes/`--delete`-protects `functions/`. Config in `deploy/config.sh` (git-ignored;
  copy from `config.example.sh`). The mini's **served** mode is baked into the
  LaunchAgents at setup (the plists' `__ENV__`, from `MAGI_ENV` — normally prod);
  `serve.py` also falls back to `MAGI_ENV` when launched without `--env`.
- **`migrate_all.py`** — the migration pattern. Discovers every function shipping a
  `migrate.py` and runs it **as a subprocess in the function's own dir** (so paths/
  imports resolve standalone); aborts on the first failure so a deploy never restarts
  on a half-migrated DB. `python3 migrate_all.py {status|up|down} --env dev|prod`
  (or `./magi migrate …`; action + flags pass through to each function's `migrate.py`). youtube has no
  migrations → skipped. Author migrations inside the function as before
  (`cd functions/betelgeuse && python3 migrate.py new <slug>`).
- **`serve.py`** — the launcher engine (`./magi run` / `python3 magi.py` delegate to
  it; the `com.magi.web` plist calls it directly). Sets `MAGI_ENV` then serves
  `magi.application`: `--env dev` → werkzeug on `127.0.0.1`, `--env prod` → waitress on
  `0.0.0.0` (default env prod). **`deploy/setup-mini.sh`** — one-time bootstrap on the
  mini (venv, deps, data dirs, install/load the two LaunchAgents in
  `deploy/launchd/`, disable sleep). See `deploy/README.md`.
- The betelgeuse **worker** runs as its own LaunchAgent (`com.magi.betelgeuse-worker`)
  with `WorkingDirectory` inside the package — never started by the web process.
- A new function joins for free: drop a `migrate.py` (picked up by `migrate_all`) and,
  if it needs background work, a worker plist.

**High-level workflow verbs** (over the above; `dev` = this machine, `prod` = the mini):
- **`./magi upgrade dev`** → `deploy/pull-prod-dbs.sh` — copies **all** prod DBs (host
  `data/magi.db` + `functions/betelgeuse/data/portfolio.db`, listed in the script's
  `DBS` array — extend it when a new function ships a DB) down to local dev, backing up
  each local copy first. It pulls a **consistent `sqlite3 .backup` snapshot** over SSH
  and is READ-ONLY against prod. This **prod→dev** direction is the safe one; schema
  still goes **up** to prod only via migrations, never by copying a DB file up.
- **`./magi upgrade prod`** = `deploy all --env prod` — ship code to the mini, no prod
  DB touched (the deploy's data protections guarantee it), then it restarts prod.
- **`./magi launch prod`** → `deploy/kickstart-mini.sh` — ssh-start the mini's
  `com.magi.web` + `com.magi.betelgeuse-worker` (no deploy, no DB touch). **Bootstrap-
  if-needed**, so it recovers from a `stop prod`: kickstarts a loaded service, else
  `bootstrap`s+`enable`s the plist back (RunAtLoad starts it), else warns to run
  `setup-mini.sh`. **`launch dev`** = `run --env dev` (local foreground).
- **`./magi stop dev`** — kills the local dev server (`pkill -f "serve.py --env dev"`),
  no script. **`./magi stop prod`** → `deploy/stop-mini.sh` — ssh-`bootout`s the mini's
  `com.magi.web` + `com.magi.betelgeuse-worker` (a real unload — they're KeepAlive, so a
  kill would just respawn); no deploy, no DB touch. Restart with `./magi launch prod`.
- **`./magi workflow`** → `deploy/workflow.sh` — `upgrade dev` → `upgrade prod` →
  `launch dev` (foreground). Prod is (re)started by the deploy step, so it doesn't
  re-kickstart. `--yes` skips the confirm. The whole CLI is also one Claude slash
  command — `/magi <args>` (`.claude/commands/magi.md`).

## Migration state

Absorbing the betelgeuse portfolio app (a separate, production Flask app at
`../betelgeuse`) into magi as a function. Decisions made: **prefix every
function**; **copy betelgeuse in while keeping its production deploy stable**.
Direction is inverted from the name — betelgeuse is the mature platform; magi is
the shell/brand. End state: one Flask app, functions as isolated blueprints
(betelgeuse keeps its own `core/`, `worker.py`, `migrations/`, `deploy/`, `data/`
inside `functions/betelgeuse/`), with only settings shared.

- **M1 — done.** Unified host + shell + `theme.css`/`shell.js`, YouTube ported to
  `functions/youtube/`, shared settings with Appearance.
- **M2 — done.** betelgeuse copied to `functions/betelgeuse/` (data rules: kept
  `portfolio.db` + `backup/` + `logs/`; `charts/`/`backtest/` are empty dir trees).
  Mounted at **`/betelgeuse`** via `DispatcherMiddleware`. Its hardcoded absolute
  URLs are prefixed by **`scripts/prefix_betelgeuse.py`** (re-run after re-copying
  from prod; idempotent); the 3 server-side `/charts/` URLs use `request.script_root`
  (so they stay correct standalone too). `url_for`/static get the prefix free via
  SCRIPT_NAME. betelgeuse pages still use their OWN header/theme (M3 unifies that).
  The worker is NOT run by the host (separate process; don't start the in-process
  scheduler). `functions/betelgeuse/CLAUDE.md` governs work inside that package —
  **its `pytest` suite must stay green** (264 tests; run from that dir).
- **M3a — done.** betelgeuse's `header.html` now renders the magi sidebar (its
  pages are a sub-nav under the "Betelgeuse" function; the function label is
  "Betelgeuse"). Theme tokens + sidebar chrome were extracted to **`static/shell.css`**
  with **`magi-`-prefixed classes** (betelgeuse already uses `.nav-item`/`.backdrop`/
  `.badge`, so prefixing avoids collisions); host pages load `shell.css` + `theme.css`,
  betelgeuse pages load only `shell.css` (no body/global bleed). `header.html` keeps
  the `.btn*` base + static-data nudge; dropped the prod-probe/dev-chip/`toggleMenu`.
  A `body{padding-left:260px}` offset (desktop) clears the fixed sidebar.
- **M3b — done.** betelgeuse's content colors are tokenized to `var(--bt-*)` defined
  in **`functions/betelgeuse/static/betelgeuse-theme.css`** (loaded by `header.html`),
  applied by **`scripts/tokenize_betelgeuse.py`** (re-run after re-copying from prod;
  idempotent). **Dark mode now mirrors the magi shell's GitHub-dark palette** (the
  `--bt-*` dark values map to `--canvas`/`--surface`/`--accent`/… — the old slate-blue
  theme is gone; see the dark-align note below); light values are chosen equivalents.
  Key trick: rgba overlays keep their alpha inline over a base triple —
  `rgba(var(--bt-slate-rgb), 0.1)`. The map in the script MUST match the token names in
  the CSS (a referenced-but-undefined `--bt-*` = an invisible color). Light mode is
  legible app-wide but not pixel-designed; polish as needed.
- **Dark-align (betelgeuse dark == shell dark) — done.** `betelgeuse-theme.css` dark
  values were retuned to the shell palette, and `tokenize_betelgeuse.py` gained a
  **second pass**: pass 1 tokenizes `<style>` blocks (as before); **pass 2 recolors the
  same slate-blue palette to its GitHub-dark LITERAL equivalent everywhere else** —
  inline `style="…"`, JS-built CSS strings (incl. `static/*.js`), and URL-encoded hex
  in data-URIs — which can't safely reference CSS vars. `header.html` is hand-authored
  (excluded from both passes; its `.btn` hues were aligned by hand). Re-run the script
  after re-copying betelgeuse from prod. Update betelgeuse's own CLAUDE Styling note
  alongside (it documented the old slate-blue dark palette).
- **Deploy/migrate — done.** Unified deploy + `migrate_all.py` + prod `serve.py` +
  LaunchAgents (see "Deploy & migrations" above); now `deploy.sh [TARGET] [--env]`.
- **dev/prod shell + versioning + retire-legacy — done.** Host exposes `APP_ENV`; the
  `🐱 <env>` chip + dev red brand (`[data-env]`); per-function `META["version"]`
  (youtube `yd-1.0.0`, betelgeuse `betelgeuse-app`/`-server`); the legacy `start.sh` /
  `server.py` / `static/index.html` SPA removed (the M4 retirement).
- **M4 (remaining)** — fold betelgeuse settings in as a contributed `settings_section`
  (the legacy-SPA retirement half of M4 is now done, above).

## Conventions worth knowing

- The YouTube function is **machine-local**. The active download dir is
  `logic.current_download_dir()`, precedence: the **env-scoped `youtube_download_dir`
  host setting** (injected by the host via `set_download_dir_resolver` — set per-env on
  `/settings`) → `MAGI_YOUTUBE_DIR` env → `DEFAULT_DOWNLOAD_DIR` (a path under
  `/Users/kai`); a per-request `dest` still overrides. `video-links.txt` is written into
  whatever folder the videos land in (`metadata_file(dest)`). The
  download dir is created **lazily at download time** (`os.makedirs(dest, …)` inside
  `run_download`) — importing the module must NOT touch the filesystem, or a path that
  isn't writable on the host (e.g. `/Users/kai` on the `wklin3` mini) crashes the whole
  unified app at import and KeepAlive crash-loops `com.magi.web`. If the unified app
  ever deploys to the betelgeuse mini, decide whether this function appears there.
- The YouTube function appends one line per download to `video-links.txt` in the
  save dir, format `YYYY/MM/DD: <slug> <url> <title>` with blank lines between
  days — `append_metadata()` preserves that exact format.
- The **Taxation** function (a blueprint, like youtube) downloads + parses the RBA daily
  FX `.xls` and answers "A$1 = USD/GBP/HKD for date D" (nearest prior business day on a
  weekend/holiday). Source URL is the **global `taxation_rba_url` host setting** (injected
  via `set_rba_url_resolver`, editable on `/settings`; falls back to `MAGI_RBA_URL` env →
  `DEFAULT_RBA_URL`). Reads the old binary `.xls` with **`xlrd`** (openpyxl can't);
  currency columns are matched by the `A$1=<CCY>` header, not fixed indices. The download
  uses a **`truststore`** SSL context (verifies against the OS trust store) so a TLS-
  intercepting proxy doesn't break it; verification is never disabled. Parsed data is cached
  in `functions/taxation/data/rba-cache.xls` (rsync-excluded + gitignored → regenerated per
  box) with a ~12h TTL + a manual Refresh; **importing the module touches no network/FS**
  (same crash-loop lesson as youtube).
- The host binds to `127.0.0.1`. For phone/LAN access bind `0.0.0.0` (trusted
  networks only).
