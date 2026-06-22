# Plan — dev/prod modes, per-function versioning, unified deploy & start, shell-aligned betelgeuse dark mode

Status: **proposed** · Author: Claude · Date: 2026-06-22

This plan covers nine related changes that finish turning magi into a single
front end with first-class dev/prod awareness, consistent per-function
versioning, and a single deploy/start path. It folds in the leftover **M4** work
(retire the legacy `server.py` SPA) where it overlaps requirement #4.

> Scope note: work *inside* `functions/betelgeuse/` is governed by that package's
> own `CLAUDE.md`. **Its pytest suite (264 tests, run from that dir) must stay
> green.** We change version *labels* and dark-theme *token values* there, never
> the `WEB_VERSION`/`WORKER_VERSION` constants a test pins, and never schema.

---

## Decisions to confirm (sensible defaults chosen; easy to flip)

1. **Cat emoji** = `🐱` (U+1F431) — the exact cat betelgeuse already uses (`app.py`
   Telegram test message). Shown for **both** dev and prod; the *mode* is conveyed
   by color + label, not by a different animal. (Req #2 says "same cat for dev and
   prod".)
2. **Dev brand color** = GitHub-danger red. The `M` avatar gradient flips from
   blue→purple (`#2f81f7→#8957e5`) to **red→pink (`#f85149→#db61a2`)** in dev, plus
   a red version label and a red `🐱 dev` chip. Prod keeps the blue avatar with a
   muted `🐱 prod` chip. (Req #3 — explicitly delegated to me.)
3. **betelgeuse version label mapping** (Req #6): `WEB_VERSION` → **`betelgeuse-app-<x.y.z>`**
   (the Flask web app), `WORKER_VERSION` → **`betelgeuse-server-<x.y.z>`** (the
   background worker/scheduler "server"). Constants keep their names; we add display
   helpers so tests stay green.
4. **youtube version** = **`yd-1.0.0`** (Req #5). New per-function field
   `META["version"]` is a free-form display string each function owns.
5. **Retire** (Req #4): delete the root **`start.sh`** *and* the legacy
   **`server.py` + `static/index.html`** stdlib SPA (the M4 retirement). The only
   web entrypoints become `magi.py` (dev) and `serve.py` (prod). The betelgeuse
   **worker** is a background process, not a "front end", so it stays as its own
   LaunchAgent.

---

## Req #1 + #2 + #3 — dev/prod mode surfaced in the shell (cat + dev red)

**Today:** `MAGI_ENV` (`dev`|`prod`) already exists and is read in
`magi.load_betelgeuse_wsgi()` to start the mounted betelgeuse in the right mode,
but the **host shell never exposes it** — host pages don't know the env, and the
sidebar looks identical in dev and prod. betelgeuse's own context processor
already injects `app_env` into its templates (used only to rewrite the page title
to `Betelgeuse-Dev`).

**Goal:** the magi sidebar shows the current mode everywhere (host pages *and*
betelgeuse pages), with a `🐱` chip and — in dev — a red brand.

### Plumbing

- **`magi.py`**: read the env once at module load:
  `APP_ENV = os.environ.get("MAGI_ENV", "dev")`. Add it to the context processor
  alongside `nav_functions` / `app_version`:
  `return {"nav_functions": FUNCTIONS, "app_version": full_version(), "app_env": APP_ENV}`.
  (Optional: a tiny `host/runtime.py` to own `app_env()` so the host doesn't read
  `os.environ` inline — mirrors betelgeuse's `core/runtime.py`. Low priority.)
- **`/api/settings`** GET payload: add `"env": APP_ENV` so function pages (which
  fill the version label from this endpoint via `shell.js`) can also reflect the
  mode without a server round-trip in markup.

### Shell markup (two render sites — keep in lockstep)

The sidebar is rendered in **two** places and both must get the indicator:

1. **`templates/base.html`** (host pages + youtube via `{% extends %}`): set the
   env on the root element server-side — `<html lang="en" data-env="{{ app_env }}">`
   — and add a `🐱` mode chip in the `.magi-account` block (e.g. under
   `Control panel`). Keep `M` as the avatar letter.
2. **`functions/betelgeuse/templates/header.html`** (betelgeuse pages): it already
   has `app_env` and an inline script. Extend that script to also
   `document.documentElement.setAttribute('data-env', '{{ app_env }}')`, and add the
   same `🐱` chip markup in its `.magi-account` block. (header.html is the
   betelgeuse-side copy of the sidebar — by design it duplicates base.html's chrome.)

### Shell CSS (single shared file)

All dev/prod styling goes in **`static/shell.css`** (loaded by host *and*
betelgeuse), so it's defined once:

```css
/* mode chip in the account block */
.magi-env { display:inline-flex; align-items:center; gap:5px; font-size:11px;
            color:var(--fg-muted); }
.magi-env .magi-env-dot { /* optional */ }

/* DEV: distinctive red brand on the left panel */
[data-env="dev"] .magi-avatar {
  background: linear-gradient(135deg, #f85149, #db61a2);  /* red→pink */
}
[data-env="dev"] .magi-env { color: var(--danger-fg); font-weight:600; }
[data-env="dev"] .magi-ver { color: var(--danger-fg); }       /* red version footer */
[data-env="dev"] .magi-sidebar { border-right-color: var(--danger-border); }
```

The chip text is environment-driven in markup: `🐱 dev` vs `🐱 prod`. No JS needed
on host pages (server-rendered `data-env`); betelgeuse pages set `data-env` via the
one-line script above so the same CSS applies.

**Files:** `magi.py`, `templates/base.html`,
`functions/betelgeuse/templates/header.html`, `static/shell.css`.

---

## Req #5 + #6 — per-function versioning with prefixes

**Today:** only the host version (`magi-1.0.0`, `host/version.py`) is surfaced (in
the sidebar footer + `/api/settings`). Functions have no declared version in their
META. betelgeuse internally has `WEB_VERSION`/`WORKER_VERSION` (both `1.5.4`,
`core/version.py`) but they aren't shown in the magi shell.

**Goal:** every function declares its own versioned identifier with a short prefix,
and the shell surfaces it.

### Function contract addition

- Add an optional **`version`** key to each function's `META` (a display string the
  function owns). The host treats it as opaque.
- **youtube** (`functions/youtube/__init__.py`): `META["version"] = "yd-1.0.0"`.
- **betelgeuse** (`magi.py`'s `BETELGEUSE_META`): compose from betelgeuse's own
  constants — `"betelgeuse-app-1.5.4 · betelgeuse-server-1.5.4"` (see Req #6 for the
  helpers that build these). Import the helpers from the package so the string
  tracks the constants instead of being hardcoded in two places.

### Surfacing

- **`templates/home.html`** dashboard cards: render each function's `version` as a
  small muted line on its card (only when present).
- **`/api/settings`** GET: include a `functions` array of `{key, label, version}` so
  function pages / the settings page can show versions too.
- The sidebar **footer** keeps the **host** version (`magi-1.0.0`) — that's the
  shell version, distinct from any function's.

**Files:** `functions/youtube/__init__.py`, `magi.py`, `templates/home.html`,
(read) `templates/settings.html`.

---

## Req #6 — betelgeuse `betelgeuse-app-x` / `betelgeuse-server-x`

**Today:** `functions/betelgeuse/core/version.py` exports `WEB_VERSION = "1.5.4"`
and `WORKER_VERSION = "1.5.4"`. `app.py` surfaces `web_version` raw in its context
processor + `/api/health`. A betelgeuse test pins these constants.

**Goal:** present them as `betelgeuse-app-<x>` (web) and `betelgeuse-server-<x>`
(worker), without renaming the constants (keeps the suite green).

- In **`core/version.py`** add display helpers (keep `WEB_VERSION`/`WORKER_VERSION`
  intact):
  ```python
  APP_NAME = "betelgeuse"
  def app_version_string():    return f"{APP_NAME}-app-{WEB_VERSION}"       # betelgeuse-app-1.5.4
  def server_version_string(): return f"{APP_NAME}-server-{WORKER_VERSION}" # betelgeuse-server-1.5.4
  ```
- Surface them: betelgeuse `/api/health` and its context processor can additionally
  emit the formatted strings; `BETELGEUSE_META["version"]` in `magi.py` is built from
  these helpers.
- **Tests:** add/extend a betelgeuse unit test asserting the two helper strings (the
  package convention requires new logic to ship with tests). Run `python3 -m pytest`
  from `functions/betelgeuse/` — must stay green.

**Files:** `functions/betelgeuse/core/version.py`,
`functions/betelgeuse/app.py` (surface), a betelgeuse test, `magi.py` (consume).

---

## Req #4 — retire `start.sh`; single front end only

**Today:**
- Root **`start.sh`** boots the **legacy** `server.py` (stdlib SPA on :8800) and
  opens a browser — a pre-migration relic.
- **`server.py`** + **`static/index.html`** are the old single-file SPA, already
  slated for retirement in **M4**.

**Goal:** one front end. Delete `start.sh`, `server.py`, and `static/index.html`.
The only ways to run magi become:
- dev: `python3 magi.py` (werkzeug, `127.0.0.1:8080`)
- prod: `python3 serve.py --env prod` (waitress, LaunchAgent `com.magi.web`)

The betelgeuse **worker** (`com.magi.betelgeuse-worker`) stays — it's background
infrastructure, not a front end, and the host never starts it in-process.

- Grep for stragglers referencing the deleted files (README, docs, deploy) and
  update them.
- This completes **M4**'s "retire the legacy `server.py` + `static/index.html`"
  item; update the Migration-state section accordingly (Req #9).

**Files:** delete `start.sh`, `server.py`, `static/index.html`; update `README.md`.

---

## Req #7 — deploy tooling: `deploy all` / `deploy <app>` / dev|prod

**Today:** `deploy/deploy.sh` is **deploy-all only** and **hardcodes `--env prod`**
(`migrate_all.py up --env prod`, restarts both LaunchAgents). `migrate_all.py`
already takes `{status|up|down} --env dev|prod` and already discovers per-function
`migrate.py`.

**Goal:** `deploy/deploy.sh [all|<app-name>] [--env dev|prod]`.

### Argument parsing

- `TARGET` positional, default `all`. Accept `all`, `host`, and each function key
  (`youtube`, `betelgeuse`). Validate against the known set.
- `--env dev|prod`, default `prod` (matches current behavior).

### Per-target behavior

- **`all`** — current behavior, but env-parameterized: rsync whole tree → deps →
  `migrate_all.py up --env <env>` → kickstart `com.magi.web` +
  `com.magi.betelgeuse-worker`.
- **`<app-name>`** — narrow the rsync to that function's subtree
  (`functions/<app>/`) **plus** shared host files when the app needs them, run only
  that function's migration (`cd functions/<app> && python3 migrate.py up --env <env>`
  if it ships one; youtube has none → skip), and restart only the affected services
  (the web app always; that function's worker if it has one). Reuse the **same
  `RSYNC_OPTS` data-protection guards** (the `*.db` pre-flight gate, `data/`
  excludes) — never weaken them for a scoped deploy.
- **`host`** — shell/templates/static + `magi.py`/`serve.py` only; restart
  `com.magi.web`. No function migrations.

### dev|prod mode

- The chosen `--env` flows to `migrate_all.py`/`migrate.py` **and** is what the
  deployed `com.magi.web` serves. The web plist currently hardcodes `--env prod`
  (`deploy/launchd/com.magi.web.plist`); for a dev-mode deploy, either template the
  env into the plist at setup or expose `MAGI_ENV` via the plist's
  `EnvironmentVariables`. Document that prod deploys stay `prod`; `dev` is for a
  staging/loopback run.
- Keep the **migrate-before-restart** ordering (betelgeuse refuses to serve a
  mismatched DB) and the abort-on-first-failure semantics for every target.

**Files:** `deploy/deploy.sh`, `deploy/README.md`, possibly
`deploy/launchd/com.magi.web.plist` + `deploy/setup-mini.sh` (env templating).
`migrate_all.py` already supports the needed flags — no change expected.

> "gradle" in the request is read as the **deploy scripts** (this repo has no
> Gradle; deployment is the bash tooling under `deploy/`).

---

## Req #8 — betelgeuse dark mode == main app dark mode

**Today:** betelgeuse content colors are tokenized to `--bt-*` vars in
**`functions/betelgeuse/static/betelgeuse-theme.css`**, and the file's design rule
is *"dark value == betelgeuse's ORIGINAL color"* — i.e. dark mode is its old
**slate-blue** palette (`--bt-bg:#0f172a`, `--bt-surface:#1e293b`,
`--bt-accent:#3b82f6`). The magi shell's dark mode is **GitHub-dark / neutral
gray** (`--canvas:#0d1117`, `--surface:#161b22`, `--accent:#2f81f7`, in
`static/shell.css`). So betelgeuse's content looks blue while the shell around it
looks gray.

**Goal:** retune the **dark** `--bt-*` values to the magi shell palette so
betelgeuse content blends with the shell. Light mode unchanged. This deliberately
**breaks the "dark == original" invariant** — update the file's header comment to
say dark now mirrors the magi shell tokens.

### Token remap (dark block only)

Map each `--bt-*` to the nearest magi/GitHub-dark token:

| `--bt-*` | old (slate-blue) | new (shell / GitHub-dark) |
|---|---|---|
| `--bt-bg` | `#0f172a` | `#0d1117` (`--canvas`) |
| `--bt-surface` | `#1e293b` | `#161b22` (`--surface`) |
| `--bt-bg-deep` | `#0d1929` | `#010409` |
| `--bt-surface-3` | `#334155` | `#21262d` (`--surface-2`) |
| `--bt-fg-bright` | `#f1f5f9` | `#f0f6fc` |
| `--bt-fg` | `#e2e8f0` | `#e6edf3` (`--fg`) |
| `--bt-fg-2` | `#cbd5e1` | `#c9d1d9` |
| `--bt-fg-muted` | `#94a3b8` | `#7d8590` (`--fg-muted`) |
| `--bt-fg-subtle` | `#64748b` | `#6e7681` (`--fg-subtle`) |
| `--bt-fg-faint` | `#475569` | `#484f58` |
| `--bt-line-strong` | `#4b5563` | `#30363d` (`--border`) |
| `--bt-accent` | `#3b82f6` | `#2f81f7` (`--accent`) |
| `--bt-accent-2` | `#60a5fa` | `#58a6ff` |
| `--bt-accent-3` | `#93c5fd` | `#79c0ff` |
| `--bt-accent-4` | `#bfdbfe` | `#a5d6ff` |
| `--bt-up` / `-strong` / `-soft` / `-teal` | greens | `#3fb950` / `#2ea043` / `#56d364` / `#3fb950` |
| `--bt-green` / `-deep` | `#10b981` / `#059669` | `#2ea043` / `#238636` |
| `--bt-down` / `-strong` / `-soft` | reds | `#f85149` / `#da3633` / `#ff7b72` |
| `--bt-warn` / `-strong` / `-soft` / `-2` | ambers | `#d29922` / `#bb8009` / `#e3b341` / `#d29922` |

And the `*-rgb` overlay triples (alpha kept inline) to match:
`--bt-bg-rgb:13,17,23` · `--bt-surface-rgb:22,27,34` · `--bt-accent-rgb:47,129,247`
· `--bt-accent2-rgb:88,166,255` · `--bt-slate-rgb:125,133,144` ·
`--bt-up-rgb:46,160,67` · `--bt-up2-rgb:63,185,80` · `--bt-up3-rgb:86,211,100` ·
`--bt-down-rgb:248,81,73` · `--bt-down2-rgb:218,54,51` · `--bt-warn-rgb:187,128,9` ·
`--bt-warn2-rgb:210,153,34` · `--bt-subtle-rgb:110,118,129`.

### Caveats / what stays literal

- **Only `<style>` blocks were tokenized.** A few **JS-driven status colors** and
  the `.btn*` gradients in `header.html` are still literal slate-blue (by design, per
  M3b). To fully match the shell, the dev should also retune those literals:
  `header.html`'s `.btn-primary`/`.btn-secondary` blue gradients (`#3b82f6→#60a5fa`)
  → the shell's blue, and any inline `#3b82f6`/`#0f172a` JS color assignments. Audit
  with `grep -n "#3b82f6\|#0f172a\|#1e293b\|#60a5fa" functions/betelgeuse`.
- **No script change:** `scripts/tokenize_betelgeuse.py` maps literal→token *names*;
  values live in `betelgeuse-theme.css`. Re-copying betelgeuse from prod + re-running
  tokenize is still safe **as long as `betelgeuse-theme.css` is not overwritten** (it's
  a magi-side file). Note this in the script's/CLAUDE's guidance.
- betelgeuse's own `CLAUDE.md` "Styling" section still documents the old dark palette
  as canonical — update that note (or point it at the shell tokens) so future work
  there doesn't reintroduce slate-blue.

**Files:** `functions/betelgeuse/static/betelgeuse-theme.css` (dark block + header
comment), `functions/betelgeuse/templates/header.html` (`.btn*` literals),
audit/patch JS color literals, `functions/betelgeuse/CLAUDE.md` (Styling note).

---

## Req #9 — update `CLAUDE.md`

Update the **root** `/Users/kai/Documents/development/magi/CLAUDE.md`:

- **Running** — drop any implication that `start.sh`/`server.py` are usable; state
  the two entrypoints (`magi.py` dev, `serve.py --env prod`). Remove the "Legacy
  (`server.py`)" block.
- **Architecture / Theming / Versioning** — document: the host now exposes
  `app_env` (dev|prod) to the shell; the `🐱` mode chip + dev red brand
  (`[data-env="dev"]` in `shell.css`); per-function `META["version"]` (youtube
  `yd-1.0.0`); the host footer = host version, function versions on the dashboard.
- **Function contract** — add `version` to the listed META keys.
- **Deploy & migrations** — document `deploy.sh [all|<app>] [--env dev|prod]`.
- **Migration state** — mark **M4 done** (legacy SPA retired) and add a short note
  for the dev/prod-shell + versioning + shell-aligned betelgeuse dark work.
- Update **`functions/betelgeuse/CLAUDE.md`** Styling note (Req #8) and add the
  `betelgeuse-app`/`betelgeuse-server` version label convention.

---

## Suggested implementation order

1. **Plumbing first** — `magi.py` env exposure + `/api/settings` `env`/`functions`
   (unblocks #1/#5).
2. **Shell UI** — `base.html` + `header.html` chip & `data-env`; `shell.css` dev
   styles (#1/#2/#3).
3. **Versioning** — youtube META, betelgeuse `core/version.py` helpers + test,
   `BETELGEUSE_META["version"]`, `home.html` cards (#5/#6).
4. **betelgeuse dark theme** — `betelgeuse-theme.css` dark remap + `header.html`
   `.btn` literals + JS color audit (#8). Run betelgeuse pytest.
5. **Retire** — delete `start.sh` / `server.py` / `static/index.html`; fix refs (#4).
6. **Deploy** — `deploy.sh` targets + env flag; README + plist/setup as needed (#7).
7. **Docs** — both `CLAUDE.md`s (#9).

## Verification

- `python3 magi.py` then load `/`, `/youtube/`, `/betelgeuse/`, `/settings`:
  - prod-ish (`MAGI_ENV=prod`) → blue `M`, `🐱 prod` chip; betelgeuse content
    matches the gray shell in dark mode.
  - `MAGI_ENV=dev python3 magi.py` → red `M`, `🐱 dev` chip, red version footer —
    on host *and* betelgeuse pages.
  - Toggle light/dark via Appearance → betelgeuse stays legible both modes.
  - Dashboard cards show `yd-1.0.0` and the betelgeuse app/server versions; footer
    shows `magi-1.0.0`.
- `cd functions/betelgeuse && python3 -m pytest` → **green**.
- `deploy/deploy.sh youtube --env prod` and `deploy/deploy.sh all` **dry-run** paths:
  confirm the `*.db` pre-flight gate still aborts on any DB touch.
```
