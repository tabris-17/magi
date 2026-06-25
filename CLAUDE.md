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
  app version (`full_version()` → `magi-1.8.0`); `host/db.py` is the common-settings store —
  GLOBAL keys in `data/magi.db`, SCOPED keys in per-env `data/magiscope.<env>.db` (see Storage
  below; `ensure_schema()` is idempotent — no migration engine for the host yet).
  `host/telegram.py` is the **app-wide Telegram notification service** (see Telegram below);
  `host/dbtool.py` is the **read-only DB browser** behind Tools → Database (see Shared settings).
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
  This is distinct from the host's own `magi-1.8.0` in the sidebar footer.

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
  - **Settings tree promoted into the sidebar.** On the betelgeuse **Settings** page
    its admin/markets panels are surfaced as a **second-level sub-nav** under
    Betelgeuse → Settings (`header.html`, `.magi-nav-sub2` in `shell.css`): General,
    Static Data, Market Data, FX Rates, HK, Crypto — each a
    `#<hash>` deep-link resolved by `settings.html`'s existing `openPanelFromHash`
    (a `hashchange` listener handles same-page switches; a small script syncs the
    active sidebar item). **Excludes** Tools → Application Health and Tools →
    Migrations — they stay inside the Settings page's Tools workbench, not the sidebar.
    (Telegram was promoted to the host's Tools → Telegram; the Database tool was removed —
    magi's Tools → Database now browses betelgeuse's `portfolio.db` centrally.)
    When rendered **inside magi** (`nav_functions` present) the page's own in-page
    `.settings-nav` is hidden (`.settings-shell.embedded`) so the sidebar is the sole
    nav and the panel fills the width; standalone betelgeuse keeps its in-page nav.
    All template/CSS only — `app.py` stays byte-identical. (Vendored-only: re-apply
    after re-vendoring betelgeuse from prod.)

### Shared settings (the only cross-function sharing)

Settings split by **lifetime/ownership**. The shell's **Settings** sidebar section holds two
**collapsible groups** — **General** and **Tools** — each a clickable parent (linking to its
first child) that reveals its children only when you're inside it (collapse-when-active):

- **General → Config** (`/general/config`, active `config`) — **global, DB-backed app settings**
  stored in `data/magi.db`: the same value in dev and prod (e.g. the taxation RBA URL). This is
  where each function's optional **`settings_section`** is aggregated as a card (a callable on its
  `META` returning `{id, label, html}`; the function still owns its storage + save route, only the
  presentation is shared). `magi.py` route `config_page` composes them under
  `templates/general_config.html`.
- **General → Appearance** (`/settings`, active `appearance`) — the host's own look (theme) only.
- **Tools → Health** (`/health`, active `health`) — the Application Health page (below).
- **Tools → Telegram** (`/tools/telegram`, active `telegram`) — the **app-wide Telegram bot**
  config (Application Telegram below).
- **Tools → Database** (`/tools/database`, active `database`) — a **read-only DB browser**, NOT a
  settings page: it lists every magi-owned database's tables (host `data/magi.db` + each function's
  DB, categorized) and shows a table's rows on click. Powered by **`host/dbtool.py`** +
  `templates/tools_database.html` (see Database browser below).
- **The function's own page** — **env-scoped ("user profile") settings** (see below).

**Sidebar groups (collapse-when-active).** Both **General** (children Config, Appearance) and
**Tools** (children Health, Telegram, Database) are gated by `active` in `base.html`
(`{% if active in ['config','appearance'] %}` / `['health','telegram','database']`); the parent
links to its first child. **Betelgeuse** likewise shows its pages only on betelgeuse pages. Children
are text-only, drawn with CSS tree connectors (`.magi-nav-sub .magi-nav-item::before/::after` in
`shell.css` — a vertical rail + an elbow tick per child, last child = `└`). betelgeuse's
`header.html` mirrors the two parents (General, Tools) as plain links into the host shell — you're
never on a host `/general/*` or `/tools/*` page while inside betelgeuse, so they don't expand there.

**APP SETTING vs USER PROFILE (the rule for where a setting lives).** A **global** setting
(one value, same dev/prod — e.g. `theme`, `taxation_rba_url`) is an *app setting* → it goes in
the DB and is edited centrally (Appearance for theme, **General → Config** for the rest, via
`settings_section`). An **env-scoped** setting (a separate value per environment — e.g.
`youtube_download_dir`) is a **user profile / per-machine** thing → it is **NOT** shown in
central settings or General → Config; it is **displayed and edited directly on the owning
function's own page** (the YouTube download folder is edited on `/youtube/` — its "Save to"
field has a **Save** button that persists `youtube_download_dir` for the box's *own* env via
`POST /api/settings {key,value}` with **no `env`**, so it writes the running env; blank clears
it to the default). **When the user calls a setting environment-specific (dev/prod), default to
this: edit it on the function page, don't add it to General → Config.**

**Database browser (`host/dbtool.py`).** Read-only, schema-agnostic introspection over a `DATABASES`
registry (each entry: `key`, `label`, `desc`, lazily-resolved `path`; magi global → `hostdb.DB_PATH`,
the two scope DBs → `hostdb.scope_db_path('dev'|'prod')`, betelgeuse → its `portfolio.db`).
`list_all()` → per-DB `{available, tables:[{name,row_count}]}`;
`table_data(dbkey, name, page, per_page)` → columns + a page of rows. Connections open with
**`PRAGMA query_only=ON`** (physically reject writes — safe to point at betelgeuse's live WAL DB),
run only SELECT/PRAGMA, and **validate the table name against the live `sqlite_master` whitelist**
before interpolating it. It reads a function's DB as a plain file — never imports the function.
Routes: `GET /api/db/tables`, `GET /api/db/<dbkey>/table/<name>`; the page server-renders the table
list and fetches rows on click. A new function's DB shows up by adding a `DATABASES` entry.

**Storage — THREE DB files (`host/db.py`).** `SETTINGS` is the single registry
(`allowed`/`default`/`scoped` per key); `/api/settings` (`GET` → `{version, env, prod_url, envs,
env_config, functions, settings}`, `POST {key,value[,env]}` → upsert, validated against the registry)
is the one endpoint. Settings are split by file so **deploy can treat them differently**:
- **`data/magi.db`** — GLOBAL keys (one value, same dev/prod). **Prod is the source of truth**;
  `./magi upgrade dev` copies it prod→dev.
- **`data/magiscope.dev.db`** / **`data/magiscope.prod.db`** — SCOPED keys, **one file per env**
  (stored under the **bare** key — the file *is* the env, no more `<key>@<env>` suffix).
  **`magiscope.dev.db` is dev-owned and NEVER synced** (not in `pull-prod-dbs`'s `DBS`), so a
  deploy can't wipe a dev value; `magiscope.prod.db` is mirrored prod→dev (prod = source of truth)
  so dev can see prod's scoped values while keeping its own.

`_path_for(key, env)` routes each key to its file; `get_setting`/`set_setting(key, value, env=…)`
resolve against `MAGI_ENV` by default (a `env=` arg targets a specific scope DB — the dev/prod
side-by-side path). `all_settings()` is the env-resolved flat map (global from `magi.db` + scoped
from the env's scope DB; the shell reads `theme`); `env_config()` returns `{key:{dev,prod}}` read
from the two scope DBs. `ensure_schema()` creates all three + purges any legacy `<key>@<env>` rows
from `magi.db`. **No migration engine** (the host doesn't have one). The host injects a **resolver**
into the otherwise self-contained
function (`magi.py` → `youtube_logic.set_download_dir_resolver`), so the function reads the
env-scoped setting **without importing the host** (precedence: setting → `MAGI_YOUTUBE_DIR` env
→ hardcoded default; see Conventions). A registry entry may also be flagged **`secret: True`**
(`telegram_bot_token`) — those are **excluded from `all_settings()`** so the value is NOT
broadcast in the `/api/settings` GET payload (every function page reads that); the owning page
renders it server-side instead.

### Application Telegram (the app-wide notification bot)

A single **Telegram bot** is shared by all functions, surfaced at **Settings → Tools → Telegram**
(`/tools/telegram`, `templates/tools_telegram.html`). **`host/telegram.py`** is the service:
`get_config()`/`is_configured()`, `send_message(text)` → `(ok, err)`, `test()`, and
`detect_chat_id()` — all over **`urllib`** (stdlib; the host does NOT depend on `requests`). The
bot token + chat id are **global host settings** (`telegram_bot_token` secret + `telegram_chat_id`
in `host/db.py`'s registry), edited on the Tools → Telegram page. The host routes (`magi.py`):
`GET /tools/telegram` (renders config server-side), `POST /api/telegram/test`,
`POST /api/telegram/detect-chat-id`; **save reuses `POST /api/settings`** (the keys are registered,
so no extra endpoint). The sidebar **Telegram** child sits next to **Database** under **Tools**.

**Betelgeuse is a consumer, not the host.** Its Telegram config panel was removed
(`functions/betelgeuse/templates/settings.html` + the `header.html` settings sub-nav); its
`core/notifications.send_telegram_message` now reads the **host** credentials. `_host_settings_db()`
resolves magi's `data/magi.db` (env **`MAGI_HOST_DB`**, which `magi.py` sets for the in-process web
process → `MAGI_DATA_DIR` → the vendored relative layout three dirs up), so **both** the mounted web
app and the standalone **worker** (which never imports the host) send through the one app-wide bot;
standalone betelgeuse / pytest (no host DB found) fall back to its own settings table unchanged
(265 tests stay green). Betelgeuse keeps its portfolio-specific message building + scheduler — only
the credential/send primitive is centralized. **Vendored-only:** re-apply these `notifications.py` /
`settings.html` / `header.html` edits after re-vendoring betelgeuse from prod (like the prefix/
tokenize scripts).

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
if ffmpeg missing; betelgeuse from worker-liveness + schema-gate). Health is the **first item
under Settings → Tools** (`base.html`); betelgeuse pages reach it via the Tools parent link
(`header.html`). `APP_START_TIME` (module load) is the host's `started_at`.

### Theming

`data-theme` (`dark`/`light`) on `<html>` selects the variable block in **`shell.css`**.
Persistence is **two-layer**: the **host DB is the source of truth** (`theme` in
`data/magi.db`, via `/api/settings`); **`localStorage` (`magi-theme`) is a no-flash
cache** read by the inline pre-paint script (in `base.html` and betelgeuse's
`header.html`). `shell.js` reconciles them: on load `syncFromServer()` pulls the DB
value (and fills the version label), and on change `applyTheme(pref, true)` writes
through to the DB. `system` resolves via `matchMedia`. **New UI must use the theme
tokens** (`var(--fg)`, `var(--surface)`, `var(--accent)`, …), never hardcoded colors.

**Definition of done for UI work:** a layout/color/theming change isn't finished until
it's **verified legible and consistent in BOTH themes** (and, in future, mobile widths) —
a hardcoded color is correct in at most one theme. The **`/look-and-feel` skill** is the
how-to (token maps, the known breakages like betelgeuse's inline-literal debt, and the
screenshot-both-themes recipe); run it after any non-trivial UI change. (Advisory, like
"tests green = done" — not enforced; the skill is also auto-discoverable from its own
description.)

**Versioning:** `host/version.py` → `full_version()` = `magi-1.8.0` (the host/shell
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
- **`./magi upgrade dev`** → `deploy/pull-prod-dbs.sh` — copies prod DBs in the script's
  `DBS` array down to local dev (host `data/magi.db` + `data/magiscope.prod.db` +
  `functions/betelgeuse/data/portfolio.db` — extend it when a new function ships a DB),
  backing up each local copy first. **`data/magiscope.dev.db` is deliberately NOT in `DBS`** —
  dev owns its scoped settings, so a deploy never overwrites them (the whole point of the
  three-DB split; see Storage above). It pulls a **consistent `sqlite3 .backup` snapshot** over
  SSH and is READ-ONLY against prod (missing remote DBs are skipped). This **prod→dev** direction
  is the safe one; schema still goes **up** to prod only via migrations, never by copying a DB up.
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
- **Telegram promoted to app-wide — done.** Betelgeuse's Telegram bot was lifted to a host
  service (`host/telegram.py`, Settings → Tools → Telegram); betelgeuse is now a consumer that
  reads the host credentials. See **Application Telegram** above.
- **M4 (remaining)** — fold betelgeuse settings in as a contributed `settings_section`
  (the legacy-SPA retirement half of M4 is now done, above; Telegram is now promoted whole to
  the host rather than surfaced via `settings_section`).

## Conventions worth knowing

- The YouTube function is **machine-local**. The active download dir is
  `logic.current_download_dir()`, precedence: the **env-scoped `youtube_download_dir`
  host setting** (injected by the host via `set_download_dir_resolver` — set per-env via the
  **Save** button on the YouTube page's "Save to" field) → `MAGI_YOUTUBE_DIR` env →
  `DEFAULT_DOWNLOAD_DIR` (a path under `/Users/kai`); a per-request `dest` still overrides.
  The two download **toggles** (`youtube_date_prefix`, `youtube_write_meta`) are likewise
  **env-scoped host settings** ("0"/"1", default "1") — the YouTube page pre-fills the
  checkboxes from them (read straight off `/api/settings`) and saves on change (`POST
  /api/settings`, no `env` → running env); the per-download checkbox state still wins for
  that download. `video-links.txt` is written into
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
