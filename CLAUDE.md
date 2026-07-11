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
./magi launch dev -d       # …or --detached: own session, survives this shell/agent/CI
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
(`start.sh` / `server.py` / `static/index.html`) has been **retired** (M4). Two background
worker processes run separately (see Deploy): betelgeuse's own worker and the **shared magi
worker** (`worker.py`, host-native function jobs — currently the Notifier).

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
  app version (`full_version()` → `magi-1.13.0`); `host/db.py` is the common-settings store —
  GLOBAL keys in `data/magi.db`, SCOPED keys in per-env `data/magiscope.<env>.db` (see Storage
  below; `ensure_schema()` is idempotent — no migration engine for the host yet).
  `host/telegram.py` is the **per-consumer Telegram bot service** (see Telegram below);
  `host/dbtool.py` is the **read-only DB browser** behind Tools → Database (see Shared settings).
- **`functions/<name>/`** — a self-contained function package. Nothing here
  imports the host or another function. Each contributes a **`META`** dict
  (`key`, `label`, `description`, `icon` SVG, `url`, and an optional **`version`**
  display string) the host reads for the sidebar/dashboard. Two styles (see Function
  contract): a lightweight **blueprint** (youtube) or a **mounted WSGI sub-app**
  (betelgeuse).

  **Per-function versioning.** Each function owns a `META["version"]` with its own
  short prefix — youtube → **`yd-1.2.0`**; taxation → **`tax-1.0.0`**; notifier →
  **`notifier-1.0.0`**; polaris → **`polaris-1.5.0`**; betelgeuse → **`betelgeuse-app-<x>` ·
  `betelgeuse-server-<x>`** (composed in `magi.py` from betelgeuse's
  `core.version.app_version_string()`/`server_version_string()`, which wrap
  `WEB_VERSION`/`WORKER_VERSION`). The host treats the string as opaque, shows it on
  the dashboard card (`home.html`, `.card .v`) and in `/api/settings` (`functions[]`).
  This is distinct from the host's own `magi-1.13.0` in the sidebar footer.

### Function contract

Two styles, both registered in the `FUNCTIONS` list in **`magi.py`** and both
**URL-prefixed** (the shared `/settings` and `/` belong to the host):

- **Blueprint function** (lightweight, e.g. youtube): `__init__.py` exports `bp`
  (Flask Blueprint with its own `url_prefix` + `template_folder`) and `META`. Its
  templates live in `functions/<name>/templates/<name>/…` and `{% extends
  "base.html" %}` (Flask finds the host's `base.html`). Flask-free logic goes in a
  sibling module (e.g. `functions/youtube/logic.py`). Register with
  `app.register_blueprint(bp)`. A function with several pages may declare
  **`META["subnav"] = [{key,label,url[,icon]}]`** — `base.html` renders them as a
  collapse-when-active sub-nav under the function's sidebar item (the Settings-groups
  pattern; `key` is what each page passes as `active`). polaris (Journal, Tags) is the
  first user. Betelgeuse's `header.html` needs no change: the subnav only shows while
  ON one of those pages, and you're never on a host function page inside betelgeuse.
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
- **Tools → Telegram** (`/tools/telegram`, active `telegram`) — a short **overview**; each consumer
  owns its **own bot**, configured on a **second-level sub-nav** (`.magi-nav-sub2`) page: **magi
  control** (`/tools/telegram/magi`, active `telegram-magi`) on top, then **betelgeuse**
  (`/tools/telegram/betelgeuse`, active `telegram-betelgeuse`). Each consumer page (shared
  `templates/tools_telegram_consumer.html`) has its **bot** (`telegram_<consumer>_bot_token` secret +
  `telegram_<consumer>_chat_id`, global; Test + Auto-detect) **plus** a per-env enable toggle
  (`telegram_<consumer>_enabled`, scoped — a dev + a prod toggle). See Application Telegram below.
- **Tools → Database** (`/tools/database`, active `database`) — a **read-only DB browser**, NOT a
  settings page: it lists every magi-owned database's tables (host `data/magi.db` + each function's
  DB, categorized) and shows a table's rows on click. Powered by **`host/dbtool.py`** +
  `templates/tools_database.html` (see Database browser below).
- **The function's own page** — **env-scoped ("user profile") settings** (see below).

**Sidebar groups (collapse-when-active).** Both **General** (children Config, Appearance) and
**Tools** (children Health, Telegram, Database) are gated by `active` in `base.html`
(`{% if active in ['config','appearance'] %}` / Tools shows for `health`/`database` + the three
telegram values); the parent links to its first child. **Telegram** itself is a parent: when on any
telegram page it reveals a `.magi-nav-sub2` with **magi control** + **betelgeuse**. **Betelgeuse** likewise shows its pages only on betelgeuse pages. Children
are **GitHub-styled** (the Primer ActionList look, adopted magi-wide): each child carries its own
16px octicon and simply indents in 24px steps (`padding-left` 32/56/72 for sub/sub2/sub3 — **no
tree rails**); the **active item** wears a subtle fill + weight 600 + a **detached 4px accent
pill** on the sidebar's left edge (`.magi-nav-item.active::before`), at every depth. Rows are
Primer metrics: 32px (6px pad + 20px line), 14px/400 labels, 8px icon gap, muted icons
(`--fg-muted`, active → `--fg`). Third-level items (Telegram's consumers, betelgeuse's settings
panels) stay text-only, like GitHub's expanded children. betelgeuse's
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
the two scope DBs → `hostdb.scope_db_path('dev'|'prod')`, betelgeuse → its `portfolio.db`, notifier →
its `notifier.db`).
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
(the per-consumer `telegram_<consumer>_bot_token`) — those are **excluded from `all_settings()`** so the value is NOT
broadcast in the `/api/settings` GET payload (every function page reads that); the owning page
renders it server-side instead.

### Application Telegram (per-consumer notification bots)

**Each consumer owns its OWN Telegram bot** — there is NO shared bot. Surfaced at **Settings →
Tools → Telegram** (`/tools/telegram`, `templates/tools_telegram.html` = a short **overview** linking
the consumers) with a `.magi-nav-sub2` of per-consumer pages: **magi control** + **betelgeuse**, each
`templates/tools_telegram_consumer.html`. A consumer page configures **its own bot** (Bot Token +
Chat ID + Test + Auto-detect) **and** a per-env enable toggle.

**`host/telegram.py`** is the connection/test service, **parameterized by consumer**:
`get_config(consumer)`/`is_configured(consumer)`/`send_message(consumer, text)`/`test(consumer)`/
`detect_chat_id(consumer)` (`CONSUMERS = ("magi","betelgeuse")`) — all over **`urllib`** (stdlib; the
host does NOT depend on `requests`). Each consumer's creds are **global host settings**
`telegram_<consumer>_bot_token` (secret) + `telegram_<consumer>_chat_id` in `host/db.py`'s registry.
Host routes (`magi.py`): `GET /tools/telegram` (overview), `tools_telegram_magi`/`_betelgeuse`
(render bot creds server-side — token is secret), `POST /api/telegram/{test,detect-chat-id}` (take
`{consumer}` in the JSON body); **bot creds + enables save via `POST /api/settings`** (keys are
registered, so no extra endpoint).

**Migration:** the old single shared bot (`telegram_bot_token`/`telegram_chat_id`) was split; a
one-time idempotent backfill in `host/db.ensure_schema()` (`_backfill_consumer_telegram`) seeds BOTH
consumers from the old shared creds when present, so existing notifications keep working until the
user points a consumer at a different bot.

**Per-consumer, per-env enable gates.** Each consumer also has a **scoped** enable
(`telegram_<consumer>_enabled`, one value per env in `magiscope.<env>.db`; magi defaults OFF/opt-in,
betelgeuse defaults ON to preserve behavior) — a dev + a prod toggle on the consumer page (each `POST
/api/settings {key,value,env}`). Each **consumer checks its own gate + reads its own bot creds at send
time** for the running env: the **Notifier** (magi control) in `functions/notifier/logic` (reads
`telegram_magi_*`); **betelgeuse** in its vendored `core/notifications` (reads `telegram_betelgeuse_*`).

**Betelgeuse is a consumer with its own bot.** Its Telegram config panel was removed
(`functions/betelgeuse/templates/settings.html` + the `header.html` settings sub-nav); its
`core/notifications.send_telegram_message` reads betelgeuse's **own per-consumer bot** from the host
DB (`telegram_betelgeuse_bot_token`/`_chat_id`). `_host_settings_db()` resolves magi's `data/magi.db`
(env **`MAGI_HOST_DB`**, which `magi.py` sets for the in-process web process → `MAGI_DATA_DIR` → the
vendored relative layout three dirs up), so **both** the mounted web app and the standalone **worker**
(which never imports the host) send through betelgeuse's bot; standalone betelgeuse / pytest (no host
DB found) fall back to its own settings table (`telegram_bot_token`/`telegram_chat_id`) unchanged (265
tests stay green). Betelgeuse keeps its portfolio-specific message building + scheduler — only the
credential/send primitive is host-resolved. Its `send_telegram_message` is also **gated** on the
host's per-env `telegram_betelgeuse_enabled` (`_betelgeuse_notifications_enabled()` reads
`magiscope.<env>.db`, resolved ONLY from `MAGI_DATA_DIR`/`MAGI_HOST_DB` — no relative fallback, so
pytest/standalone are never gated; unset → ON). The `com.magi.betelgeuse-worker` plist sets
`MAGI_ENV`+`MAGI_DATA_DIR` so the worker reads the right env's gate. **Vendored-only:** re-apply these
`notifications.py` / `settings.html` / `header.html` edits after re-vendoring betelgeuse from prod
(like the prefix/tokenize scripts). `header.html`'s magi-side edits now also include the
**octicons on its page sub-nav** (Overview→graph, My Portfolio→briefcase, Groups→stack,
Notifications→bell, Settings→gear, Training→mortar-board, Tracker→history) and the gear on the
mirrored General link.

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

### The UI scale contract (buttons, inputs, type) — REUSE, don't reinvent

Colors have tokens; **sizes have this contract.** Every new page/component drifted before
this was written down (six pages each invented their own "small button"), so: **never write
a bespoke `padding`/`font-size` for a control that already has a primitive.** Read this
before adding any button, input, or heading, and prefer the closest existing class over a
new one.

- **Buttons — there is exactly ONE primitive: `.btn`** (`9px 16px`, `14px`, weight 500),
  with **`.primary`** (green, for the single main action) and **`.sm`** (`6px 12px`, `13px`,
  for toolbar/secondary actions like "Add files"). A page class may set **layout only**
  (`margin-left:auto`, grid placement) — **never** its own padding/font/background. Need a
  size that doesn't exist? Add a **modifier to `.btn`** in the "generic UI bits" block of
  `theme.css`, don't fork it per page.
- **Inputs:** `input[type=text]` = `9px 12px`, `14px`, radius 6. Match it for any text-ish
  control (search boxes, URL fields). **Specificity trap:** that base rule is `(0,1,1)`, so a
  single-class `.foo` `(0,1,0)` **loses** to it — override with two classes
  (`.pol-toolbar .pol-search`), or your padding is silently ignored (this bit polaris).
- **Type scale** (don't invent intermediates like 14.5px / 21px):
  page title `22px/600` (`.view-title`) · section heading `17px/600` (`.settings-h`,
  `.ntf-title`, `.pol-title`) · card title `15px/600` · **body `14px`** · secondary `13px` ·
  uppercase section label `12px/700` (`.ntf-section-label`, `.pol-section-label`) ·
  micro/tabular counts `11–11.5px`. A per-item title inside a page is a **section heading**
  (17px), never a second `<h1>`. **Rich-text *content* headings** (inside polaris's editor) are
  their own sub-scale: `h1 20 / h2 17 / h3 15`, all weight 600.
- **Surfaces/rhythm:** `.card` padding `16px` (a dense editor may go to `18–20px`), radius
  `10px` cards / `6–8px` controls. **Shell sidebar** = Primer ActionList: 32px rows, 14px/400,
  16px muted icons, 8px gap (see Sidebar groups); **in-page trees** (polaris's year/month pane)
  are denser: ~26px rows at 12.5px.
- **Before adding a component:** `grep` `theme.css` for an existing rule playing the same
  role and reuse it. Shared primitives live in the **"generic UI bits"** block; page-specific
  classes go in that page's block and should carry layout + color-role, not new metrics.
- **Known debt** (one-offs predating `.btn.sm`; migrate opportunistically, verifying both
  themes): `.ntf-fbtn`, `.ntf-day`, `.db-tbtn`, `.health-refresh`, `.btn-env-save`.

**Definition of done for UI work:** a layout/color/theming change isn't finished until
it's **verified legible and consistent in BOTH themes** (and, in future, mobile widths) —
a hardcoded color is correct in at most one theme — **and it matches the scale contract
above**. The cheap check: screenshot the new page and an existing one (e.g. `/youtube/`) at
the same viewport and confirm the headings, buttons and inputs line up. The
**`/look-and-feel` skill** is the how-to (token maps, the scale audit, the known breakages
like betelgeuse's inline-literal debt, and the screenshot-both-themes recipe); run it after
any non-trivial UI change. (Advisory, like "tests green = done" — not enforced; the skill is
also auto-discoverable from its own description.)

**Versioning:** `host/version.py` → `full_version()` = `magi-1.13.0` (the host/shell
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
  mini (venv, deps, data dirs, install/load the **three** LaunchAgents in
  `deploy/launchd/`, disable sleep). See `deploy/README.md`.
- The betelgeuse **worker** runs as its own LaunchAgent (`com.magi.betelgeuse-worker`)
  with `WorkingDirectory` inside the package — never started by the web process.
- **The shared magi worker (`worker.py` + `com.magi.worker`) — the app-wide background-job
  runner for HOST-NATIVE functions.** One always-on process owns an APScheduler and fires
  host-native functions' scheduled jobs (currently the **Notifier**'s personal reminders).
  Generalized over a `SCHEDULABLE` list — each module exposes `schedule_fingerprint()` +
  `reschedule(scheduler)`; the worker polls every 30s and reschedules on change (DB-driven,
  no cross-process RPC — betelgeuse's worker pattern lifted to the host). `WorkingDirectory`
  is the magi root (so `from functions.* import` resolves); the plist sets `MAGI_ENV` +
  `MAGI_DATA_DIR` so the function send-helpers find the app-wide bot creds + per-env enable
  gate. It **never imports betelgeuse** — betelgeuse keeps its OWN worker (it also does
  market-data/FX/rebuilds and stays byte-identical to prod). `deploy.sh` kickstarts `worker`
  on the `all` + `host` targets; `setup-mini.sh`/`kickstart-mini.sh`/`stop-mini.sh` manage all
  three services. Needs `APScheduler`+`pytz` (in `requirements.txt`, pinned to betelgeuse's).
- A new function joins for free: drop a `migrate.py` (picked up by `migrate_all`); if it needs
  background work, either ship its own worker plist OR (host-native) add its logic module to
  the shared worker's `SCHEDULABLE` list — no new process.

**High-level workflow verbs** (over the above; `dev` = this machine, `prod` = the mini):
- **`./magi upgrade dev`** → `deploy/pull-prod-dbs.sh` — copies prod DBs in the script's
  `DBS` array down to local dev (host `data/magi.db` + `data/magiscope.prod.db` +
  `functions/betelgeuse/data/portfolio.db` + `functions/notifier/data/notifier.db` — extend
  it when a new function ships a DB),
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
  `setup-mini.sh`. **`launch dev`** = `run --env dev` (local foreground). Add
  **`--detached`/`-d`** (`./magi launch dev --detached` → `deploy/launch-dev-detached.sh`)
  to start dev in its **own session** (Python `start_new_session` — the portable `setsid`;
  macOS has no `setsid` binary), reparented to init so it **survives the launching shell /
  an agent or CI background task** (a plain foreground launch dies with its parent — dev has
  no `KeepAlive`, unlike prod). It stops any running dev server first (port-clash guard),
  waits until the port answers, logs to `data/dev-server.log` (pid in `data/dev-server.pid`,
  both gitignored). Stop it with `./magi stop dev` (its `pkill` matches the detached one too).
- **`./magi stop dev`** — kills the local dev server (`pkill -f "serve.py --env dev"`),
  no script. **`./magi stop prod`** → `deploy/stop-mini.sh` — ssh-`bootout`s the mini's
  `com.magi.web` + `com.magi.betelgeuse-worker` (a real unload — they're KeepAlive, so a
  kill would just respawn); no deploy, no DB touch. Restart with `./magi launch prod`.
- **`./magi workflow`** → `deploy/workflow.sh` — `upgrade dev` → `upgrade prod` →
  `launch dev`. Prod is (re)started by the deploy step, so it doesn't re-kickstart.
  **Step 3 always stops a running dev server first** (`pkill -f "serve.py --env dev"` +
  waits for the port to free), so it never collides — e.g. a surviving detached server from
  a previous run. **No confirmation prompt** — it runs straight away (the banner shows what it
  will do); `--yes`/`-y` is accepted but a no-op (back-compat). **`--detached`/`-d`** makes step 3
  launch dev detached (via `launch-dev-detached.sh`) and **return** instead of blocking — so the
  whole chain survives a non-interactive caller. The whole CLI is also one Claude slash command —
  `/magi <args>` (`.claude/commands/magi.md`).

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
  The three download **toggles** (`youtube_date_prefix`, `youtube_write_meta` — default "1";
  `youtube_quicktime` — default "0") are likewise
  **env-scoped host settings** ("0"/"1") — the YouTube page pre-fills the
  checkboxes from them (read straight off `/api/settings`) and saves on change (`POST
  /api/settings`, no `env` → running env); the per-download checkbox state still wins for
  that download. **`youtube_quicktime`** re-encodes the finished video to HEVC via ffmpeg's
  hardware `hevc_videotoolbox` (`logic.transcode_to_quicktime`, `-tag:v hvc1` + audio→AAC)
  so QuickTime can play it — 4K YouTube is VP9/AV1, which QuickTime can't decode. It's a
  **no-op for already-H.264/HEVC** files, skipped for audio-only/MP3, needs ffmpeg, and on
  any failure keeps the original (the download already succeeded). Big downloads also use
  ranged `http_chunk_size` + effectively-unlimited retries in `run_download` to survive
  YouTube dropping the connection mid-stream (the "N bytes read, M more expected"
  IncompleteRead). `video-links.txt` is written into
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
- The **Notifier** function (a blueprint, `/notifier/`) sends **personal reminders** — free-text
  Telegram messages on a recurring schedule. It's the first consumer of the **shared magi worker**
  (above): `logic.schedule_fingerprint()`/`reschedule()`/`send_scheduled()` are the worker
  interface; the same `logic.send_message()` powers the page's **Send Now**. It owns
  `functions/notifier/data/notifier.db` (a key/value `settings` table — `reminder_text`/`enabled`/
  `days`/`times`/`timezone`/`last_sent`; **no migration engine, lazy idempotent `ensure_schema()`**,
  like the host's own store). Like youtube/taxation it **never imports the host**: it reads its OWN
  magi-control bot creds (`telegram_magi_bot_token`/`_chat_id`) from `magi.db` and the per-env
  `telegram_magi_enabled` gate from
  `magiscope.<env>.db` as plain files (resolved via `MAGI_DATA_DIR`/`MAGI_HOST_DB`/relative), and
  sends over **urllib + truststore** (no `requests`). The schedule model + UI mirror betelgeuse's
  Notifications page but in the magi shell/theme tokens. **Importing touches no network/FS** (lazy
  schema; apscheduler/pytz imported lazily inside the schedule fns). The reminder fires only when
  **magi control** is enabled for the running env (Tools → Telegram → magi control).
- The **Polaris** function (a blueprint, `/polaris/`) is a **personal journal**. An entry has
  **three sections: title, body, attachments**. The body is a **rich-text editor
  (`contenteditable`) that STORES Markdown** — `static/polaris-md.js` (`PolarisMD.mdToHtml` /
  `htmlToMd`) converts on load/save, so entries stay plain, greppable, portable text (the `LIKE`
  search + tree snippets read them directly; `logic._strip_md()` keeps previews prose).
  It is a **deliberately CLOSED subset** — blocks `h1/h2/h3`, `ul`, `ol`, `p`; inline
  `**bold**`, `*italic*`, `` `code` `` — nothing else. That closure is what makes the
  md → html → md round-trip stable, so **paste is forced to plain text** and `htmlToMd()` only
  ever emits those shapes (a browser's `<div>`/`&nbsp;`/`<b>` soup is normalized). No WYSIWYG
  bundle, no Markdown dependency (the repo has no npm/bundler); `polaris-md.js` also exports for
  `node`, so the round-trip is unit-testable — **test it there before touching the converter**.
  Entries open **read-only (view mode)** — Edit/Save/Discard: `setMode('view'|'edit')`
  drives everything off a `.pol-editor.viewing` class + per-control flags; **Discard restores
  the `loaded` server-state snapshot** (date/title/body/tags, confirm when dirty); a mid-edit
  autosave (`saveEntry(allowEmpty=true)`, used when attaching to an unsaved entry) must NOT
  flip back to view mode. Deep links: `?entry=<id>` opens in view, `&mode=edit` jumps into edit.
  **Tags** (betelgeuse-style groups, minus the guardrails): defined on **`/polaris/tags`**
  (`active='polaris-tags'`, a `META["subnav"]` page), any number per entry, unique
  case-insensitively; **deleting a tag never checks usage** — it just unlinks (`tags` +
  `entry_tags` junction; `delete_entry` cleans the junction too, sqlite FKs are OFF). The entry
  POST takes an optional `tags: [ids]` (replaces the set; unknown ids silently dropped);
  `GET/POST/DELETE /polaris/api/tags[/<id>]` is the manager API.
  The page is a **page-level search toolbar** over a
  two-pane body: a **year → month → entry tree** on the left (collapsible, counts per node,
  newest first; open nodes persist in `localStorage['polaris-expanded']`, and the current
  year+month open on a first visit) and the editor on the right (Cmd/Ctrl+S saves, `/`
  focuses search, word count, unsaved-changes guard). **Search** matches the title, the body,
  OR the ISO date (so `2026-07` narrows to a month), force-opens the tree, shows a match
  count, and renders a `<mark>`-highlighted snippet centered on each body match. The page
  state is **deep-linkable** — `?q=<search>` and `?entry=<id>`, kept in step by `syncUrl()` via
  `history.replaceState`. **`init()` must read `location.search` BEFORE calling `newEntry()`**,
  which calls `syncUrl()` and would otherwise wipe the params it is about to parse. Like youtube/taxation/notifier
  it **never imports the host**: it owns `functions/polaris/data/polaris.db` (an `entries`
  + `attachments` tables; **no migration engine**, lazy idempotent `_SCHEMA` in `_connect()`,
  like the host's own store — an existing DB picks up new tables on next connect).
  Routes: `GET/POST /polaris/api/entries` (list — carries a `preview` snippet + `attachment_count`,
  not the full body — / create+update), `GET|DELETE /polaris/api/entries/<id>`,
  `POST /polaris/api/entries/<id>/attachments` (multipart), `DELETE /polaris/api/attachments/<id>`,
  `GET /polaris/media/<id>`, `GET /polaris/api/health`. **Importing touches no network/FS** (same crash-loop lesson as
  youtube). Its DB is in `pull-prod-dbs.sh`'s `DBS`, so `./magi upgrade dev` mirrors prod's
  journal down (prod = source of truth; local is backed up first) — **write entries on prod**,
  or a pull overwrites dev's.
  - **Attachment bytes are BLOBs in `polaris.db`, on purpose.** `deploy.sh` excludes
    `functions/*/data/` from rsync and `pull-prod-dbs.sh` only mirrors `.db` files, so files on
    disk would never sync prod→dev (broken images on dev). In the DB they are backed up and
    snapshot-consistent with their entry for free. Cost: DB size — images are **downscaled
    client-side** (canvas → webp, longest edge 1600px, only over ~1.5MB) and `ATTACH_MAX_BYTES`
    (25MB) is the backstop. If you ever paste many large originals, move to disk + add an rsync
    path. `delete_entry()` removes the entry's attachments explicitly (sqlite FKs are OFF by
    default per connection). **`GET /polaris/media/<id>` serves only `INLINE_MIMES`
    (png/jpeg/gif/webp) inline; everything else — notably `image/svg+xml`, which can carry
    script — is forced to `as_attachment`, plus `nosniff`**, so a stored file can never execute
    in the app's origin.
  - **CSS gotcha:** `theme.css`'s global `input[type=text]` rule has specificity (0,1,1), so a
    single-class `.pol-*` selector (0,1,0) **loses** to it — the search box and the borderless
    title need two-class selectors (`.pol-toolbar .pol-search`, `.pol-editor .pol-title`) or
    they silently inherit the global padding/border. Same trap for any new host page component.
- The host binds to `127.0.0.1`. For phone/LAN access bind `0.0.0.0` (trusted
  networks only).

## Response Style

magi is **born from betelgeuse** — it inherits betelgeuse's sign-off convention (originally in
`functions/betelgeuse/CLAUDE.md`). **Always end every response with a random cat flourish** — vary it
every time, a single short line at the very end. Draw from:

- Cat face: 🐱 🐈 🐈‍⬛ 😺 😸 😹 😻 😼 😽 🙀 😿 😾
- Paw / body: 🐾 🐾🐾 🦶
- ASCII: `=^.^=` `ฅ^•ﻌ•^ฅ` `(=^･ω･^=)` `/>  フ` `(∪.∪ )...zzz`
- Sounds: "meow!", "purrrr~", "mrrrow?", "nya~", "*chirp*", "*slow blink*"
- Combos like `🐾 purrr~` or `ฅ^•ﻌ•^ฅ meow!`
