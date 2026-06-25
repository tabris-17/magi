# CLAUDE.md

Guidance for Claude Code when working in this repo.

## Required Reading (before chart/pattern/backtesting work)

- [claude/reading-aastocks-charts.md](claude/reading-aastocks-charts.md) — reading an AA Stocks chart visually.
- [claude/technical-patterns.md](claude/technical-patterns.md) — patterns this app tracks (e.g. Breakthrough).

## Project Overview

Betelgeuse is a Flask stock + crypto tracker (markets: HK, JP, US, crypto) with portfolio
management, price/performance snapshots, a buy/sell transaction ledger, a **Training** page
(chart-pattern labelling for backtesting), and a master-detail **Settings** page (per-market URL
config + Admin). Storage is SQLite (WAL). Charts: AA Stocks (HK), yfinance PNGs (JP/US), CoinGecko
PNGs (crypto).

## Running

```bash
pip install -r requirements.txt
python3 app.py --env dev     # http://localhost:8000/  (--env is mandatory: dev|prod)
python3 worker.py --env dev  # background scheduler + market-data prefetch
```

## Architecture

**`app.py` is the Flask backend** — routes, chart caching, static-data download. Shared logic lives
in `core/` (`db.py`, `config.py`, `marketdata.py`, `health.py`, `version.py`). The **worker**
(`worker.py`) is the only process that schedules jobs (notifications, market-data prefetch); never
start the scheduler in `app.py`.

**Templates** include `{% include 'header.html' %}` for the shared hamburger nav: `/` (**Overview**
home), `/portfolio`, `/notifications`, `/groups`, `/settings`, flyout **Test → Training**
(`/training`) + **Tracker (legacy)** (`/tracker`). **`header.html` is the single source of truth for
the top bar AND the app-wide button base** — its `<style>` (included on every page) defines `header`/
`header h1` plus the canonical `.btn` / `.btn-primary` / `.btn-secondary` / `.btn-danger` /
`.btn-success` classes. **Never redefine `header`/`header h1` or `.btn` sizing in a page's own
`<style>`**; mark up buttons as `class="btn btn-primary"` (etc.) and they inherit. For a deliberately
smaller/contextual button use a *higher-specificity* selector (e.g. `.actions .btn`, `.btn-delete`)
so it wins on specificity, not source order. Pages are otherwise self-contained. The legacy card-grid
Tracker is `index.html` at `/tracker`; `/crypto` renders it with the crypto tab preselected (deep-link only).

**Two shared front-end assets** (`<script src>`-included; the only shared CSS is header.html's inline
`<style>` above — no other shared external JS/CSS):
- `static/symbol-field.js` — SymbolField component.
- `static/instrument-detail.js` — InstrumentDetail component.

### Home — Portfolio Overview (`/`)

`overview.html`, route `index()`. KPI strip (4th card = **PnL (base)** from `/api/portfolio/pnl`)
above a **two-tab** body: **Watch List** (default) + **My Portfolio**. Tab state is remembered in
`sessionStorage`; the KPI strip is shared above both.

**Watch List tab** — consolidated register of **every** instrument across all markets
(`/api/portfolio`): sticky toolbar (search, market chips), collapsible per-market sortable tables —
**always grouped Market › Group** (the old Group-by toggle was removed). Columns (`COLS`, exact order):
**`# · Instrument · Price · Bought · 7D · 30D · 1Y · Monitor · Trigger · Added`**.
- **Instrument** = name (truncated, ellipsis) over `(SYMBOL)` — one fixed-layout cell. Held rows
  also carry a `HELD: <net_qty>` pill here (see Rows below).
- **Price** = current price + 1D % (side by side, colored by sign) over the muted 1D price.
- **Bought** = unrealized P/L % (colored, primary) over the weighted-avg cost (muted, secondary) —
  **same layout/format as 7D/30D/1Y** (reuses `.perf`/`.perf-pct`/`.perf-hist`). Muted N/A when not
  bought; a neutral `—` on the % line while the current price isn't cached yet.
- **7D/30D/1Y** = % change over the historical price at that lookback.
- **Monitor/Trigger** = formatted `item.monitor_price` / `item.trigger_price`, rendered in the same
  muted secondary-price style as the perf/Bought price lines; muted N/A when unset.
- **Rows** — blue-**neutral zebra striping** that RESETS per group (parity comes from the
  within-group index passed to `instRowHtml`, NOT `:nth-child`; kept desaturated/slate so it stays
  distinct from the blue `:hover`). Instruments you currently hold (`net_qty` = buys − sells > 0, or
  tagged `bought` with no transactions) get **gold "held" markers**: a left rail (inset shadow on the
  `#` cell) + a `HELD: <net_qty>` pill (thousand-separated). Fully-sold (`net_qty == 0`) rows show no
  marker.
- Price/perf come from `/api/portfolio/performance` (async, cache-only). N/A has two flavours:
  cache-not-warmed (N/A + `*` + footnote; warmed by opening the instrument or its ▸ chart) vs. true
  N/A. Row click → `InstrumentDetail.open(market, symbol)`. Each row's **▸ expander** reveals
  **Notes + an inline mini-chart** (lazy, cached in `state.chartHtmlCache`). Grid font sizes are
  driven by CSS vars `--grid-font` / `--grid-sub` on `table {}` — use them, don't hardcode.

**My Portfolio tab** — dynamic, **grouped by market only** (held markets: HK/JP/US/crypto), driven
by `/api/portfolio/pnl`. Two **analytics charts** sit on top (pure inline SVG/CSS — no chart lib): an
**allocation pie** (each slice = a holding's/market's value as % of total; legend shows notional +
weight%) and a **diverging P&L bar** (gains green→right, losses red→left, length ∝ |pnl|). A single
**By Instrument / By Market** toggle re-renders **both** together (`state.pnlChartMode`, sessionStorage).
Below, per-market sections: each header carries an aggregate **PnL pill** (signed, base) + holdings
count + **Total in base currency** (bracketed). Rows (held instruments, **no gold rail** — all are
held) are `# · Instrument · Qty · Avg Cost · Price · Value (base) · PnL (local) · PnL (base)` — Avg
Cost/Price native; Value + both PnL columns rounded (`fmtMoney(…,{dp:0})`). The per-market section is
single-currency, so `compute_pnl` returns `currency` + `*_local` amounts AND a `weight` (value as % of
total, FX-invariant) per holding AND per market (grand total stays base-only — mixed currencies). The
header **Total** and each row's **Value** also show that allocation `weight`. Rows carry the same **▸
expander** as the Watch List (inline combined notes + lazy mini-chart; shared `state.expanded` /
`chartHtmlCache` / `loadExpandedCharts`); a multi-group instrument's notes are merged DISTINCT in
`_holdings_with_basis`. Row click → `InstrumentDetail.open`. See **FX & Base-Currency P&L** for the
math/provider.

The shared inline-expander chart (`loadExpandedCharts`, used by both tabs) now dispatches on **all**
providers: **HK** aastocks small-gif · **US/JP** yfinance PNG (`/api/stock/<m>/<s>/chart/30`) · **crypto**
coingecko PNG — cached in `state.chartHtmlCache`, guarded by `chartRenderToken`.

### SymbolField component (shared)

`static/symbol-field.js`, `window.SymbolField` + `window.SYMBOL_RULES`. **Single source of truth for
client-side symbol formatting** — never re-implement HK/JP normalization or crypto hints at call
sites. `SYMBOL_RULES[market]`: `placeholder`, `live(v)` (caret-safe upper-case), `normalize(v)`
(mirrors `normalize_symbol()`), `hint(v)`, optional async `suggest(v)` (US board chips). Normalizes
on blur/Enter, shows `→ canonical` preview, amber warning for crypto > 3 chars, clickable US chips.

`new SymbolField(inputEl, opts)`: `opts.getMarket()` (required), `onResolve(sym,mkt)`,
`onEnter(sym,mkt)`, `placeholder`, `selectOnFocus`, `.refresh()`/`.resolveNow()`/`.destroy()`. Used
in the Portfolio add-form Symbol box (`onResolve` → name + Monitor-price prefill) and Tracker search
(`onEnter` → `dispatchSearch()`). Back any new symbol-entry point with this.

### InstrumentDetail component (shared)

`static/instrument-detail.js`, `window.InstrumentDetail` — `open(market,symbol)`, `close()`,
`PROVIDERS`, `MARKET_META`. The **single front-end entry point for "show me everything about this
instrument"**. Injects its own DOM + `.idv-*` styles once. **Do not re-implement the detail/chart
popup — call `InstrumentDetail.open()`.**

Data from **`GET /api/instrument/<market>/<symbol>`** (`instrument_detail()`, single source of
truth): canonical `symbol`, `name`, `provider`, `in_portfolio`, `portfolio` row when tracked (carries
monitor/trigger/bought + derived bought_price; else null), market-specific `reference`: **HK**
`{lot_size,category,sub_category}`, **US** `{base_symbol,boards[],etf}`, **crypto**
`{coin_id,market_cap_rank}`, **JP** `{}`. Works for non-portfolio symbols too.

Modal renders: header, **quote hero** (last price + currency + 24h delta + "as of … · source"),
reference cards, **Performance** panel (1D/7D/30D/1Y from `/api/instrument/<m>/<s>/performance`),
**Notes**, **Charts** dispatched on `provider` (`PROVIDERS = {hk:'aastocks', jp:'yfinance',
us:'yfinance', crypto:'coingecko'}`): `aastocks` → four `url_template_*` images with Copy URL;
`yfinance` → 30/90/180/365-day grid via `/api/stock/<m>/<s>/chart/<period>`; `coingecko` →
7/30/90/365-day grid via `/api/crypto/<id>/chart/<period>`; `null` → placeholder. **Chart provider
≠ data provider**: HK charts use AA Stocks but HK price/perf come from yfinance (see Market Data).

### Tracker market tabs — shared layout (MUST OBEY)

**All four tabs (HK/JP/US/crypto) share one identical layout, look, and usage. Keep them uniform
unless explicitly asked to differentiate.** A single generic `renderMarketPanel()` in `index.html`
drives every market — no per-market layout branch. Shell: left = Group dropdown
(`/api/groups/<market>`) + Search; right = card grid (`/api/market/<market>`) filtered by group;
click/search → `InstrumentDetail.open()`. **Apply new markets/features through the generic path —
never fork per market.**

Market-specific bits live in one place — `MARKET_CHART_PROVIDER` in `index.html` (`hk:'aastocks',
jp:'yfinance', us:'yfinance', crypto:'coingecko'`; **keep in sync with `PROVIDERS` in
instrument-detail.js**). In-card thumbnail (`applySmallCharts`) dispatches on it; the detail view
reuses `InstrumentDetail`; the search box is a SymbolField recreated each render. Change-% badge =
**HK only**.

**HK chart provider is runtime-configurable** (Settings → Markets → HK → *Chart Style*). The
`hk_chart_provider` setting (`aastocks` default | `yfinance`) selects how HK charts render
**everywhere** — Tracker cards, the Overview expanders, and the detail modal — while HK price/perf
stay yfinance-backed regardless (the toggle is *chart-only*). Single source of truth:
`app.get_hk_chart_provider()` resolves it (invalid/unset → `aastocks`), and `/api/instrument` returns
it as the bundle's `provider`. The two static maps above are now **fallback defaults for HK**: each
dispatch point reads the resolved value instead — the modal via the bundle's `provider`
(`d.provider || PROVIDERS[market]`), and `index.html`/`overview.html` via a `hkChartProvider` var set
from `/api/settings` on load (`marketProvider`/`loadExpandedCharts` special-case `market==='hk'`).
`'yfinance'` reuses the JP/US render path (`/api/stock/hk/<symbol>/chart/<period>`).

**Settings page** (`settings.html`): master-detail. Left nav (**Admin** → General, Static Data,
Market Data, FX Rates, Tools; **Markets** → HK, Crypto). **Tools** is a tabbed workbench:
**Application Health** · **Migrations**. (Vendored-only inside magi: the Telegram panel and the
Database tool were removed — Telegram is the host's Tools → Telegram and the DB browser is the
host's Tools → Database, which reads `portfolio.db` directly. The `/api/admin/db/*` routes remain
in `app.py` (byte-identical to prod) but are unused here.)

**Sub-tab convention (MUST OBEY): when one Settings panel hosts several distinct things, group them
under a sub-tab strip — don't stack them as separate cards and don't split into more left-nav items.**
The canonical pattern: a `.tools-tab`/`.tools-view` strip (mirrors Market Data's `.md-tab` per-market
tabs) inside a single `data-panel-id` panel; `selectTool(tool)` toggles `.tools-view.active` and
**lazy-loads only the active tool's data** (so e.g. the cross-LAN prod health probe fires only when
its tab is opened). Keep one left-nav item per panel. If old panel ids are folded into a tab, add them
to `TOOL_ALIASES` so `selectPanel()` + `#hash` deep-links still resolve (Tools + the right sub-tab).
**Deliberate exception — Markets → HK** (`data-panel-id="stock-url"`): two stacked `.settings-section`
cards (**HK Chart Style** selector + **AAStocks URL Configuration**), NOT sub-tabs. This was a
user-directed layout choice (master setting gating a conditional detail) — don't refactor it into a
sub-tab strip.

**Notifications page** (`notifications.html`): same master-detail shell. **Portfolio Summary** panel
(live Telegram preview, market checkboxes, last-sent, Send Now, Recurring Schedule). Master toggle
dims `.notif-card-body` when off; `.paused-notice` stays readable. Future panels follow this.

### Database Schema

```
portfolio:        id, symbol, market, name, "group", added_date, comment,
                  monitor_price, trigger_price, bought, created_at
transactions:     id, market, symbol, txn_type('buy'/'sell'), price, quantity, txn_date, created_at
                  -- buy/sell ledger; joined to portfolio on canonical (market, symbol); idx_txn_instrument
settings:         key, value
hk_securities:    stock_code (PK), name, category, sub_category, board_lot
us_securities:    symbol, name, board, etf, PRIMARY KEY (symbol, board)  -- board ∈ NASDAQ/NYSE/AMEX/ARCA/BATS/IEX; idx_us_symbol
coingecko_coins:  coin_id (PK), symbol, name, market_cap_rank  -- mirror of /coins/list; idx_coingecko_symbol
crypto_ohlcv:     source('coingecko'), id, coin_id, period, timestamp, o/h/l/c, fetched_at, UNIQUE(coin_id,period,timestamp,source)
stock_ohlcv:      source('yfinance'), id, market, symbol, timestamp, o/h/l/c, currency, fetched_at, UNIQUE(source,market,symbol,timestamp); idx_stock_ohlcv
fx_rates:         currency (PK), usd_per, fetched_at  -- tiny refetchable FX cache (USD per 1 unit); worker-warmed, web reads cache-or-fallback
db_meta:          key (PK), value, updated_at  -- schema version + description
```

`"group"` is a SQLite reserved word — **always quote it** (`"group"`). `transaction` is reserved too
— hence the plural table name `transactions`.

Groups are stored in `settings` as comma-separated `groups_{market}`; defaults hardcoded in
`GROUP_OPTIONS`, custom merged at read time (defaults like `Default`/`TechnicalPattern` undeletable).

Other `settings` keys: `url_template_{3m|6m|1y|1y_monthly|3m_small}`;
`hk_chart_provider` (HK **chart-style** toggle ∈ `aastocks`|`yfinance`, default `aastocks`; see HK
chart provider below);
`{hk_securities|coingecko_coins|us_securities}_updated_at`;
`{hk_securities|coingecko_catalog|us_nasdaq|us_other}_url` + `_url_enabled` (override gated on
`_enabled=='true'` AND non-empty); `coingecko_id_{SYMBOL}` (user override, highest precedence);
`coingecko_api_url`/`coingecko_api_key`; `market_data_provider_{market}` (active provider);
`market_data_backload_start_date` (global, default `2024-01-01`); `market_data_rebuild_request_{market}`
(worker mailbox flag) + `market_data_rebuild_status_{market}` (JSON progress); `market_data_job_{prefetch|backload}`
(JSON recurring-job status: state/last_started/last_finished/last_result/next_run, written by the worker via
`run_tracked`/`record_job_next_run`); `base_currency` (P&L base ∈ HKD/USD/AUD, default HKD);
`telegram_bot_token`/`telegram_chat_id`; `notification_portfolio_{last_sent|enabled|days|times|markets}`;
`default_timezone`; `prod_base_url`.

**Schema versioning & migrations.** Schema changes ship as **versioned, reversible migration
files** (`migrations/00N_*.py`), applied by the engine in **`core/migrate.py`** — never as silent
`init_db()` side effects, never as hand-edited DB files. Two stores: migration *definitions* live in
`migrations/` (git); the per-DB *applied ledger* lives in the `schema_migrations` table (`db_meta`
table still holds `version`/`description`, surfaced in `/api/health`; `version` is now a derived
mirror of the ledger's MAX). `DB_SCHEMA_VERSION` in `core/db.py` is the head the code expects (a test
enforces `head==const`).
- **`init_db()` no longer migrates an existing DB.** Fresh DB → builds the schema by running every
  migration from scratch (baseline → head) + stamps the ledger. Existing DB → only `ensure_infra` +
  `bootstrap_ledger` (backfills the ledger from the version stamp; no schema mutation).
- **Boot guard (refuse-to-start):** all entrypoints check `migrate.gate_state()` after `init_db()`.
  Mismatch → worker `sys.exit(1)`; web (`app.py`/`serve.py`) enters a **maintenance mode** (`MIGRATION_GATE`
  + a `before_request` 503 serving only `/api/admin/migrate/*` + `migrate_gate.html`). Never auto-downgrades.
- **Any DB change is an auto-versioned migration — don't wait to be asked.** A **schema change** AND
  a **one-off data augmentation** (seed/backfill) are *both* migrations; **creating the migration +
  bumping the version is an inseparable part of making the change — do it automatically, never treat
  "bump the DB version" as a separate request the user has to make.** Scaffold it:
  **`migrate.py new <slug> [--data]`** creates the next `migrations/00N_<slug>.py` (numeric prefix ==
  `VERSION`) *and* rewrites `DB_SCHEMA_VERSION` in `core/db.py` to match in one step (a test enforces
  `head==const`). Then fill in `up`/`down` and apply.
- **A migration file** (whether scaffolded or hand-written) has `VERSION`, `DESCRIPTION`, `up(cursor)`,
  `down(cursor)` (or `raise migrate.Irreversible(...)`). Refetchable caches (`crypto_ohlcv`/`stock_ohlcv`)
  may be dropped+rebuilt; **never drop** `portfolio`/`transactions`. `002_baseline.py` is
  **frozen — never edit it**.
- **Data migrations are first-class** (use `migrate.py new --data`). A migration whose `up()` is pure
  `INSERT`/`UPDATE` (no DDL) is the **only** sanctioned way to get derived/seed data onto prod —
  **never copy a dev DB up** (data is one-way, prod→dev). Keep `up()` deterministic (no
  network/clock/randomness): do heavy/networked computation on dev and bake the *result* as literal
  rows, so dev and prod converge. Live operational rows (your actual holdings) belong in the prod UI,
  not a migration.
- **Running:** `migrate.py {status|up|down|history|prune} --env dev|prod` (backs up + prunes; the only
  cross-env DB ops) + `migrate.py new <slug> [--data]` to scaffold (authoring; no `--env`, touches no
  DB). Dev also has **Settings → Admin → Database Migrations**; prod is migrated by
  `deploy/deploy.sh` before restart (panel read-only on prod).
- **Feature/design plan docs live in `migrations/plan/`** (markdown). When you write a plan for a
  change, save it there — NOT a `plans/` dir at the repo root. The migration engine ignores this
  subdir (`discover()` only loads modules with a `VERSION`/`up`), so plan `.md` files are inert.

### Instrument Symbol Normalization (MUST OBEY)

**Always normalize to canonical form before persisting, displaying, OR looking up** — never
store/compare a raw user value. `normalize_symbol(symbol, market)` is the single source of truth.
- **HK**: 5-digit zero-padded numeric + uppercase `.HK`. `700.hk`/`0700.HK`/`700` → `00700.HK`.
- **JP**: `XXXX` → `XXXX.T` (no zero-pad).  **US/Crypto**: upper-only.

Use the canonical key for persistence, lookups/existence checks, and derived artifacts (chart URLs,
backtest filenames, transaction keys).

### API Routes

```
GET  /api/markets                              - List markets
GET  /api/market/<market>                      - Stocks for a market
GET  /api/portfolio                            - All items grouped by market (+ derived bought_price, bought_qty [buys only], net_qty [buys−sells])
POST /api/portfolio                            - Add (market,symbol,name,added_date,comment,group,monitor_price,trigger_price,bought)
PUT  /api/portfolio/<id>                       - Update (same fields)
DEL  /api/portfolio/<id>                       - Delete item (also purges its transactions by market+symbol)
GET/POST /api/portfolio/<market>/<symbol>/transactions  - List (oldest-first) / add a buy
DEL  /api/portfolio/transactions/<int:txn_id>  - Delete one transaction
GET  /api/stock/<stockid>/chart                - HK AA-Stocks chart URL (3-segment)
GET  /api/stock/<market>/<symbol>/chart/<period>  - yfinance candlestick PNG (4-segment; period∈30/90/180/365; ?refresh=1)
GET  /api/crypto/coins                         - Crypto items w/ resolved coingecko_id
POST /api/crypto/<symbol>/coingecko-id         - Save user override symbol → coin id
GET  /api/crypto/<coin_id>/chart/<period>      - Candlestick PNG (period 7/30/90/365; ?refresh=1)
GET  /api/crypto/<coin_id>/ohlcv               - Stored OHLCV rows (?period=)
GET  /charts/<filename>                        - Serve a generated chart PNG/GIF from config.CHART_DIR (data/charts)
GET/POST/DEL /api/groups/<market>[/<name>]     - Merged get / save / delete custom group
GET/POST /api/settings                         - Get all / upsert
POST /api/backtest/save                        - Save training chart snapshots
GET/POST /api/admin/static-data/{hk|coingecko|us}/{status|download}  - Status / guarded refresh
GET  /api/admin/static-data/{hk|coingecko|us}/{securities|coins}     - Query (?q=,?category/?board=,?page=,?per_page=)
GET  /api/crypto/translate                     - ?symbol=BTC ⇄ ?coin_id=bitcoin (404 unmapped)
GET  /api/lookup/name                          - ?market=&symbol= → {name}
GET  /api/lookup/us                            - Board listings for a US ticker → {matches:[...]}
GET  /api/instrument/<market>/<symbol>         - Instrument detail bundle
GET  /api/instrument/<m>/<s>/performance       - Snapshot (price+currency+as_of+1d/7d/30d/1y); warms cache (?cache_only=1 to skip)
GET  /api/portfolio/performance                - Bulk cache-only snapshot keyed "market:symbol"
GET  /api/portfolio/pnl                         - Unrealized P&L on current holdings, base-normalized (?base= override; default base_currency setting)
GET  /api/fx                                    - FX provider + base + per-currency rate provenance (live vs fallback) for the FX Rates panel
GET  /api/admin/db/tables | /table/<name>      - Database Tool: tables+counts / columns+rows
GET  /api/admin/migrate/{status|history}       - Schema gate/pending + applied ledger (any env)
POST /api/admin/migrate/{up|down|prune}        - Run migration / prune backups (DEV ONLY → 403 on prod)
GET  /api/admin/market-data/markets            - Provider catalog + active provider
GET  /api/admin/market-data/<market>/{status|instruments}  - Cache stats / paginated inspector (rows carry oldest_ts+latest_ts)
POST /api/admin/market-data/<market>/{provider|clear}      - Set active provider / clear cache
POST /api/admin/market-data/<market>/rebuild               - Queue a worker-run rebuild (clear→backload to start date)
GET  /api/admin/market-data/<market>/rebuild/status        - Rebuild progress ({state: idle|queued|running|done|error})
GET  /api/admin/market-data/jobs                           - Recurring background-job status (prefetch+backload: state, last run, next_run)
POST /api/telegram/{test|detect-chat-id}       - Send test / auto-detect chat ID
POST /api/notifications/portfolio/send         - Build + send portfolio summary
GET  /api/notifications/portfolio/schedule     - Schedule + next_runs
GET  /api/health                               - This instance: env, web{version,started_at}, worker liveness, db schema
GET  /api/prod/health                          - Dev probes prod's /api/health server-side
```

### Application Health & dev-knows-prod

`GET /api/health` reports the **answering** instance: `env`, `web {version, started_at}`,
`server_time`, `worker` liveness (DB-heartbeat from `core/health.py`, stale after 90s), DB schema
version. Surfaced at **Settings → Admin → Application Health**. It also returns
`versions {app, server}` — the display labels (see below).

**Version labels.** `core/version.py` keeps the raw `WEB_VERSION`/`WORKER_VERSION` constants (still in
`web.version` / consumed by tests) and adds two display helpers: `app_version_string()` →
`betelgeuse-app-<WEB_VERSION>` and `server_version_string()` → `betelgeuse-server-<WORKER_VERSION>`
(prefix from `APP_NAME`). The magi host reads these via `core.version` to compose the function's
`META["version"]` shown on the dashboard card; betelgeuse surfaces them under `/api/health`'s
`versions`. Keep the raw constants intact when bumping — only their value changes, not their names.

A **dev** instance also probes **prod** (ground truth — what prod is actually running):
`get_prod_base_url()` resolves the target (`prod_base_url` setting wins, else `MINI_HOST`/`PORT` from
`deploy/config.sh` via `_parse_deploy_config`, else None). `GET /api/prod/health` does the probe
server-side (avoids CORS): `{configured:false}` or `{reachable, base_url, probed_at, health|error}`.
The dev title bar shows `dev <ver> ▸ <prod ver>`, polling every 60s; the dev header wears a muted
rose tint always, deeper red (`header.dev.prod-down`) when prod is unreachable.

### Market Data Manager

`MarketDataManager` (instance `market_data`) — the **one unified interface over per-market
providers**. A provider serves one market and **owns its storage/schema** ("wrap, don't migrate").
Subclass `MarketDataProvider`: data path (`is_fresh`/`refresh(force)`/`load`/`snapshot`) + admin path
(`cache_stats`/`list_cached`/`clear`).

- `MARKET_DATA_PROVIDERS`: `hk/jp/us → [YFinanceProvider(market)]`, `crypto →
  [CoinGeckoProvider()]`. **All four markets have a provider now.** Empty list still = "not available
  yet". Add one: subclass + append — admin UI lights up automatically.
- **YFinanceProvider** (hk/jp/us): stores ONE daily series per symbol in `stock_ohlcv`
  (`source='yfinance'`); `STOCK_PERIODS={'30','90','180','365'}` just slice that series. Freshness 30
  min. Currency from `_MARKET_CURRENCY`. Charts: `generate_stock_chart()` →
  `static/chart_stock_{market}_{symbol}_p{period}.png`. `refresh()` pulls a trailing 1-year window
  (cheap forward-fill); `backload(start_date)` pulls the full `[start_date, today]` range via
  `_fetch_yf_history(start=, end=)` (yfinance `end` is **exclusive** → +1 day). Both go through
  `INSERT OR REPLACE` on `UNIQUE(source,market,symbol,timestamp)`, so re-fetching is idempotent and
  one path covers forward- AND back-fill with no gap math.
- **yfinance rate-limit backoff.** `_fetch_yf_history(..., retries=N)` catches **`YFRateLimitError`**
  (distinct from an empty frame — a genuine no-data/pre-IPO range, which is NOT a throttle and must
  never be retried, else IPOs get hammered forever) and sleeps an **escalating** `_RATELIMIT_BACKOFF_SEC
  = (60, 600, 1800)` (1 min → 10 min → 30 min) before each retry; exhausting the budget returns
  `([], 'rate_limit')`, which `fetch_and_store_stock_ohlcv` turns into a **`RateLimited`** exception.
  The deep `backload()` passes `retries=len(_RATELIMIT_BACKOFF_SEC)`; the freshness `refresh()` passes
  `retries=0` (it reruns every 15 min anyway) **and swallows `RateLimited`** (returns False — it's
  called inline by `generate_stock_chart` for web requests, which must serve stale rather than 500).
  The backload **and** rebuild loops catch `RateLimited` and **break their pass** (stop hammering a
  throttling Yahoo); the hourly `_needs_backfill`-gated job resumes the unfinished instruments. The
  in-place sleeps run on the scheduler threadpool — the worker heartbeat is a separate thread, so a
  sleeping fetch never stalls liveness.
- **CoinGeckoProvider** (crypto): delegates to crypto helpers over `crypto_ohlcv WHERE
  source='coingecko'`. `generate_crypto_chart()` routes freshness through `refresh(...)`.
  `backload()` is **best-effort** — CoinGecko's OHLC endpoint can't take an arbitrary start date, so
  it fetches `days=max` into a `period='max'` bucket (TEXT period; merges/dedups by timestamp in
  `_load_crypto_ohlcv_series`). Keep `'max'` OUT of `CRYPTO_PERIODS` (never a chart dropdown option).
- **`list_cached()` per-row contract — ALL providers MUST return**: `coin_id, symbol, periods,
  rows, oldest_fetched, newest_fetched, oldest_ts, latest_ts`. (`oldest_ts`/`latest_ts` = MIN/MAX
  OHLCV timestamp = the **data date-range** shown in the admin table; `oldest_fetched`/`newest_fetched`
  are *fetch* times. YFinance once returned `oldest`/`newest` and omitted `coin_id`/`periods` → admin
  table rendered NULL/blank/'—'; `test_market_data` guards the full contract.)
- **`clear()` deletes DB rows AND derived artifacts**: yfinance → `stock_ohlcv` rows for that market
  + its `chart_stock_{market}_*` PNGs; coingecko → `crypto_ohlcv` rows + all crypto PNGs.
- **Backload + Rebuild.** `get_backload_start_date()` reads the global
  `market_data_backload_start_date` setting (default `2024-01-01`, validated). The worker runs
  `backload_market_data()` **hourly** (`BACKLOAD_INTERVAL_MIN=60`), gated by
  `_needs_backfill(market,symbol,start)` so completed instruments aren't re-fetched — the hourly tick
  only re-tries genuine laggards (a symbol that hit a transient empty/throttled deep fetch), letting
  them converge within hours instead of a day+ (yfinance only; crypto is on-demand). **Manual Rebuild is worker-run, DB-triggered**: the web route
  `set_rebuild_request(market,provider)` writes a `market_data_rebuild_request_{market}` flag + a
  `queued` status; the worker's poll loop `dispatch_rebuilds()` picks it up, **clears the flag**, and
  schedules a one-shot APScheduler `'date'` job `run_rebuild()` on the threadpool (never inline — keeps
  the heartbeat alive). `run_rebuild` writes `running`→progress→`done`/`error` into
  `market_data_rebuild_status_{market}` (JSON) which the UI polls; `manager.rebuild()` clears then
  backloads every portfolio instrument for that market via `progress_cb`. The web process NEVER runs
  the long job itself.
- **Snapshot / performance**: `snapshot(instrument, ensure_fresh=False)` → `{price, currency, as_of
  (epoch ms), performance:{period_key: pct|None}}` over `performance_periods`
  (`{'1':1,'7':7,'30':30,'365':365}`). Last close = price; `_pct_change(series, days)` per period
  (None when history insufficient). Manager exposes `market_data.snapshot/performance(market,
  instrument, ensure_fresh)`; `_provider_instrument_key(market, symbol)` maps portfolio symbol →
  provider key. Surfaced by `/api/portfolio/performance` (bulk cache-only) +
  `/api/instrument/<m>/<s>/performance` (cache-warming). The worker's `prefetch_market_data()`
  proactively refreshes stock OHLCV for yfinance-backed portfolio instruments.

**UI**: Settings → Admin → Market Data — a global **Backload settings** box (start-date input →
`POST /api/settings`) + a **Background reload** indicator (polls `/jobs` every ~10s; spinner +
"Reloading now…" while a job runs, else "Idle · last … · next in …" from `read_jobs_status()`), then
per-market sub-tabs: provider dropdown, statistics, paginated cached-instruments inspector with a
**Data range** column (`oldest_ts`–`latest_ts`), confirm-gated **Clear cache** AND **Rebuild cache**
(the latter shows a progress bar polling `/rebuild/status` every ~2s, signalling on done/error;
resumes if you revisit a market mid-rebuild).

### FX & Base-Currency P&L

`core/fx.py` — the **single source of truth for currency conversion** (Flask-free). Everything
pivots through **USD-per-currency** so we only ever need the liquid `<CCY>USD=X` Yahoo tickers (no
flaky cross-pairs): `fx_rate(C,B) = usd_per(C)/usd_per(B)`. `usd_per(ccy)` resolves **cached
`fx_rates.usd_per` → fixed `FALLBACK_USD_PER` anchor** (never None for a known currency). The
anchors are derived from the two the user supplied — `USD_per(HKD)=0.1276`, `USD_per(AUD)=0.1276/0.181≈0.705`
(triangle), `JPY` an editable best-guess — so P&L still renders offline. **The web NEVER fetches FX
inline** (cache-or-fallback only); the **worker** warms the cache via `refresh_fx_rates()` folded
into `prefetch_market_data()` (every 15 min). Network is isolated to `_fetch_fx_usd_per` (the single
test seam). `get_base_currency()` reads `base_currency` (HKD/USD/AUD, default HKD); `fx_converter(base)`
returns a one-cache-load closure used by the P&L math; `fx_status()` reports per-currency provenance
(`live`/`fallback`) for the panel + `/api/fx`.

`core/pnl.py` — **pure** `compute_pnl(rows, snapshots, fx_fn, base)` (no DB/network): **unrealized**
P&L on *current* holdings = `(price − avg_buy_cost) × net_qty`, FX-converted to base; `pnl_pct` is
currency-free. A holding needs `net_qty>0` AND a known `bought_price`; a cache-miss price → flagged
`incomplete`, listed in `missing`, excluded from the sums. Returns per-market (held markets only, app
order) + grand totals. Wired by **`GET /api/portfolio/pnl`**; `_holdings_with_basis()` feeds it **one
row per instrument** — `GROUP BY market, symbol`, since a symbol filed under multiple groups has
several `portfolio` rows and must NOT be double-counted (snapshots are cache-only).

**Schema:** tiny refetchable `fx_rates` cache (migration `004_fx_rates`, DB → v4); down() may drop it.
**UI — Settings → Admin → FX Rates** (`fx-rates` panel): the **base-currency selector** (the one
editable control; POSTs `base_currency`) + read-only **Provider: yfinance · USD-pivot** + a grid
showing each `base→ccy` rate and its **inverse side by side** (derived from `usd_per`; re-labels on
base change), with **Live/Fallback** provenance chips. **Home (`overview.html`) now has tabs**: **Watch
List** (the existing register, unchanged) and **My Portfolio** (held instruments grouped by *market*
with an aggregate-PnL pill per market). The 4th KPI card is now **PnL (base)** (was "Latest Addition").
`fmtMoney(v, ccy)` formats base-currency amounts; P&L loads after `loadPerformance()` warms the cache.

### Runtime data directory (`config.DATA_DIR`)

**Everything the app *generates* at runtime lives under one configurable root, separate from the code
tree.** `core/config.py` defines `DATA_DIR = os.environ.get('BETELGEUSE_DATA_DIR') or <repo>/data`
(resolved from `__file__`, **never the CWD**) and four derived paths — `DB_PATH`
(`data/portfolio.db` + its `-wal/-shm` siblings), `CHART_DIR` (`data/charts`), `BACKTEST_DIR`
(`data/backtest`), `LOG_DIR` (`data/logs`, unless `BETELGEUSE_LOG_DIR` overrides). DB backups
(`migrate.py` pre-migration snapshots + `update-from-prod.sh` pulls) go in `data/backup/`
(`core.migrate._backup_dir()` derives it from the DB's directory, so it follows the temp DB in
tests). The
whole `data/` tree is gitignored AND rsync-excluded (`deploy.sh` `--exclude='data/'` +
`protect data/***`). **`STATIC_DIR` is NOT runtime data** — it stays `<repo>/static` (Flask's
`static_folder`) for the committed front-end JS; never relocate it. Dirs are created at the
consumption points (`db._ensure_db_dir`, the chart generators' `makedirs(CHART_DIR)`,
`logging_setup`, the backtest save). Because `DATA_DIR` is `__file__`-relative, a process can launch
from any CWD without silently opening an empty DB. **Use `config.DB_PATH`/`CHART_DIR`/`BACKTEST_DIR`,
never a bare relative path.** Relocating files is **not** a schema change — no migration.

### Logging

All processes log through Python `logging`, configured once per process by
**`core/logging_setup.py`** — `configure_logging(process_name, env)` sets up the shared `betelgeuse`
parent logger (a `RotatingFileHandler` in `config.LOG_DIR` + a console `StreamHandler`,
`'%(asctime)s %(levelname)s %(name)s: %(message)s'`, idempotent). Modules get a child logger via
`get_logger('worker')` → `betelgeuse.worker` and **never** add handlers or call `print()`. Entry
points call `configure_logging` at startup: `worker.main()`, `app.py`/`serve.py` `__main__`. `LOG_DIR`
defaults to `data/logs` (under the runtime data root above; gitignored + rsync-excluded), overridable
via `BETELGEUSE_LOG_DIR`; files are `{process}.app.log` (distinct from launchd's raw
`{process}.out/.err.log` stdout capture, also under `data/logs`).
**Use `logger.info/warning/error`, not `print()`**, in any new server-side code.

### Chart Caching

All generated chart files (AA-Stocks GIFs + OHLCV PNGs) are written to **`config.CHART_DIR`**
(`data/charts`, the runtime data root — NOT the repo `static/`) and served to the browser via the
**`GET /charts/<filename>`** route (`send_from_directory(config.CHART_DIR)`). The chart APIs return
`{'url': '/charts/<filename>'}`; the front-end consumes that URL and never hardcodes a chart path, so
the storage location is fully decoupled from the UI.

`download_aa_stocks_chart(stockid, period)` writes `chart_{symbol}_p{period}.gif` (periods
4=3m,5=6m,6=1y,7=1y_monthly) into `CHART_DIR`; downloads only if not cached.

**OHLCV charts (crypto + stock) decouple persistence from the PNG render cache.** Both
`generate_crypto_chart()` and `generate_stock_chart()`: (1) keep the DB series fresh via the
provider's `refresh()` (DB is source of truth), (2) reuse the PNG if < 60 min old (file-age gate),
(3) render from stored rows via shared `_render_candles()`. **Never re-gate the OHLCV fetch behind
the PNG cache** (past bug: table never repopulated when image was fresh). Use `.total_seconds()` for
file-age checks, never `timedelta.seconds` (>24h regression). Card thumbnails reuse the smallest
render; modal grids request all periods.

### Backtesting / Training

`BACKTEST_PATTERNS = ['Breakthrough', 'Triangle']`. `/training` is per-market (only HK has tools).
`POST /api/backtest/save` writes
`{config.BACKTEST_DIR}/training/{market}/{pattern}/{stockid}-{YYYYMMDD}-{chart_type}.gif`
(`BACKTEST_DIR` = `data/backtest`, the runtime data root), overwriting silently.

### Telegram Notifications

`send_telegram_message(text)` is the single send helper (reads `telegram_bot_token`/
`telegram_chat_id`, POSTs with `parse_mode: HTML`, returns `(ok, error)`). **Only `<b>`, `<i>`,
`<code>` tags — no Markdown.** `_build_portfolio_message(markets_filter)` builds the summary (manual
send + scheduled job). `send_scheduled_portfolio_notification()` is the APScheduler job;
`reschedule_portfolio_notifications()` re-reads schedule at startup + after every `POST
/api/settings`. The scheduler runs in `worker.py`, not `app.py`.

**When vendored inside magi (vendored-only edit), the bot is APP-WIDE:** `send_telegram_message`
reads its credentials from magi's host settings DB (`_read_telegram_credentials()` →
`_host_settings_db()`, found via `MAGI_HOST_DB`/`MAGI_DATA_DIR`/the relative layout) instead of this
app's own `settings` table — so the token/chat id are configured once at magi → Settings → Tools →
Telegram and shared across functions. Standalone betelgeuse (no host DB) falls back to its own
`settings` unchanged. The web app + the worker both resolve it (the worker never imports the host).
The in-app Telegram **config** panel (Settings → Admin → Telegram) was removed here; only the
Notifications page (portfolio send/schedule) remains. Re-apply after re-vendoring from prod.

### Static Data

**Guarded refresh (all sources — MUST reuse).** Every reload: download → parse into memory →
**sanity-check row count** → only then mutate DB. If parsed count `< STATIC_DATA_MIN_ROWS` (100):
`download_*()` raises `StaticDataDownloadError(message, row_count)`, **existing table untouched**.
Otherwise wholesale-replace in one transaction (`DELETE` + bulk insert). Endpoints return `{success,
old_count, new_count, message}` or `{success:false, aborted:true, row_count, error}` (HTTP 200).

**Custom source URLs.** `resolve_static_url(value_key, enabled_key, default_url)` is the single
resolver — override wins **only** when `_enabled=='true'` AND value non-empty; else default.

**Staleness nudge.** `header.html` runs a once-per-session check: if HK or CoinGecko `updated_at` >
30 days (or unset), `confirm()` → `/settings#static-data` (snoozed via sessionStorage; skipped on
`/settings`). `openPanelFromHash()` honors `#<panel>` deep-links.

**Symbol → name auto-fill.** Portfolio add form populates Name via `GET
/api/lookup/name?market=&symbol=` (HK → `hk_securities`, crypto → `coingecko_coins`, US →
`us_securities`; JP returns `''`). The Monitor-price box also prefills from market data on symbol
blur (only when the box is empty): first the cached value (`?cache_only=1`, instant); on a cache
miss it **warms in the background** (the un-`cache_only` endpoint) with a spinner + a 15s abort
timeout, filling on success or showing an amber warning on timeout/no-data/error. Saving never
depends on it — the box and the Add button stay usable throughout, and a stale fetch can't backfill
a symbol you've moved on from (a `prefillSeq` token + `AbortController` guard it).

- **HK** — `download_hk_securities()` fetches the HKEX Chinese `_c.xlsx` with `openpyxl` (names are
  Chinese), parses from row 4, fully replaces `hk_securities`.
- **US** — `download_us_securities()` pulls Nasdaq Trader over FTP (`nasdaqlisted.txt` → NASDAQ;
  `otherlisted.txt` → board via `US_EXCHANGE_BOARDS`). `_parse_nasdaq_symbol_file()` skips
  header/trailer + test issues. Keyed `(symbol, board)` (~12.7k rows). `_us_base_symbol()` strips a
  trailing `.BOARD` but preserves class-share dots (`BRK.A`).
- **Crypto catalog** — `download_coingecko_coins()` pulls `/coins/list` + enriches top entries with
  `market_cap_rank`. **Symbol ⇄ id translation (single source of truth, never re-implement)**:
  `coingecko_symbol_to_id(symbol, strict=True)` resolves user override → `COMMON_CRYPTO_IDS` →
  catalog (lowest `market_cap_rank` wins on collision); `coingecko_id_to_symbol(coin_id,
  strict=True)` the reverse. Both raise `CoinGeckoMappingError` when `strict`, else None.

### Database Tool (admin)

The `GET /api/admin/db/tables` + `/table/<name>` backend (generic, schema-agnostic, read-only:
reads `sqlite_master` + `PRAGMA table_info`, validates `<name>` against the live whitelist before
interpolation) still ships in `app.py`. **Its UI was removed in the magi-vendored copy** (the
Tools → Database sub-tab + its JS), because magi's own Tools → Database now browses every
magi-owned DB — including this app's `portfolio.db` — centrally. Standalone betelgeuse keeps the
routes; only the in-app browser tab is gone. (Vendored-only: re-apply after re-vendoring.)

## Testing

⚠️ **Definition of done: every change must run `python3 -m pytest` green before you report it
ready.** If a change makes a test obsolete, update it (say why); if it exposes a real bug, fix the
code not the assertion. **New backend logic/route/helper ships with unit tests** — deterministic (no
real clock/network/randomness; mock `app.requests`/`urllib` with `FakeResponse`, seed timestamps,
temp-DB fixtures) and fast (whole suite ~0.5s; set `fetched_at` in the past instead of sleeping).

```bash
pip install -r requirements-dev.txt   # one-time
python3 -m pytest                      # whole pack
python3 -m pytest tests/test_pure.py   # single file
```

**Wiring** (`tests/`, via `pytest.ini`): `conftest.py` — `db` points `core.db.DATABASE` at a fresh
tmp SQLite + `init_db()` per test; `client`, `conn`/`setval`, `FakeResponse`; network never hit.
Files: `test_pure.py`, `test_db_logic.py`, `test_routes.py`, `test_market_data.py` (redirects
`core.config.CHART_DIR` to tmp so `clear()` never deletes real PNGs), `test_backload.py` (backload/
rebuild — mocks the yfinance seam `core.stockdata._fetch_yf_history` and `app.requests.get` for crypto),
`test_fx.py` (FX rate math + `compute_pnl` + `/api/fx` + `/api/portfolio/pnl` — mocks the FX seam
`core.fx._fetch_fx_usd_per`, seeds `stock_ohlcv`/`fx_rates`), `test_paths.py` (runtime-data path
resolution — `DATA_DIR`/`DB_PATH`/`CHART_DIR`/`BACKTEST_DIR`/`LOG_DIR` derivation + `BETELGEUSE_DATA_DIR`
override in a child process, the `/charts/<f>` serve route, backtest dir, DB-dir autocreate).

Already covered (extend, don't re-derive): cache reload/reset/ordering; the `list_cached` per-row
contract (YFinance + CoinGecko, incl. `oldest_ts`); the from-scratch `init_db()` path (caught the
unquoted-`group` regression); portfolio watch fields + transaction round-trip / weighted-avg /
`net_qty` (buys−sells) / delete-purge; **backload** (#4 reaches start_date, #5 idempotent de-dup,
#6 ascending order), crypto best-effort `days=max`, `_needs_backfill` gate, `manager.rebuild`, the
`run_rebuild` status state-machine + rebuild routes; **yfinance rate-limit backoff** (escalating
1m→10m→30m retries on `YFRateLimitError` via a fake `yfinance` injected into `sys.modules`; empty
frame ≠ throttle so it's not retried; `RateLimited` propagation; `refresh` swallows it; backload +
rebuild loops abort their pass on it); the migration engine
(up/down/gate/bootstrap/prune, head-relative) + the `migrate.py new` scaffold; schema shape +
version-resynced-on-reinit; **FX rate math** (USD-pivot, anchor round-trip, cache-vs-fallback),
`refresh_fx_rates` upsert/idempotency, `fx_status` provenance, `compute_pnl` (per-market + grand
totals, currency-free `pnl_pct`, incomplete/missing exclusion) + the `/api/portfolio/pnl` + `/api/fx`
routes. Edge-case carriers: `normalize_symbol`,
`_close_on_or_before` (003 backfill), `_pct_change`, `usd_per`/`fx_rate`,
`_us_base_symbol`, `_parse_nasdaq_symbol_file`, `coingecko_symbol_to_id`/`_id_to_symbol`,
`_crypto_ohlcv_is_fresh`, `resolve_static_url`, `_build_portfolio_message`.

## Critical Conventions

**Tests must pass before "done"** — non-negotiable.

**Database Safety** — **every DB change — a schema change OR a one-off data augmentation
(seed/backfill) — ships as a migration file** (`migrations/00N_*.py`) with a matching
`DB_SCHEMA_VERSION` bump; **creating the migration and bumping the version are part of making the
change — do them automatically, don't wait to be asked.** Scaffold both in one step with
`migrate.py new <slug> [--data]`. Migrations are applied by `migrate.py`/the dev panel (which back up
first) — **not** by editing `init_db()` and **not** by copying DB files between machines (data is
one-way, prod→dev; never push a dev DB up). The runner auto-backs-up before every up/down
(`portfolio.db.premigrate-*`) and on error leaves the DB at the last good version. **Refetchable
caches** (`crypto_ohlcv`, `stock_ohlcv`) may be dropped+rebuilt in a migration; **never drop
source-of-truth tables** (`portfolio`, `transactions`). See *Schema versioning & migrations* above
for the full workflow + the refuse-to-start boot guard.

**DB Access Pattern**:
```python
conn = get_db_connection()   # row_factory = sqlite3.Row
c = conn.cursor()
c.execute(...)
conn.commit()
conn.close()                 # always close (finally/explicit) — no pooling
```

**Group Validation** — `add_portfolio_item()`/`update_portfolio_item()` validate `group` against
`GROUP_OPTIONS[market]`; update `GROUP_OPTIONS` first or the API 400s.

## Styling

Dark theme **mirrors the magi shell's GitHub-dark palette** — canvas `#0d1117`, surface `#161b22`,
accent `#2f81f7` (`#58a6ff` hover) — NOT the old slate-blue (`#0f172a`/`#1e293b`/`#3b82f6`), which has
been retired. Colors come from `--bt-*` tokens in `static/betelgeuse-theme.css` (dark values map onto
the shell tokens; light values are chosen equivalents), applied across the app by
`scripts/tokenize_betelgeuse.py` (run from the magi root, idempotent — re-run after re-copying from
prod). `header.html` is hand-authored and excluded from that script; its `.btn-*` hues were aligned to
the GitHub-dark palette by hand. **New UI must use the `--bt-*` tokens, never hardcoded slate-blue.**
Frosted-glass cards (`backdrop-filter: blur`). Compact inputs (`padding: 0.4–0.625rem`,
`font-size: 0.8–0.9rem`). Grid
font sizes via CSS vars (`--grid-font` / `--grid-sub`); long text cells use `table-layout: fixed` +
`min-width:0` flex chain + `text-overflow: ellipsis`.

**Emoji inside gradient-text elements** — `-webkit-text-fill-color: transparent` turns child emoji
into hollow silhouettes. **Never place emoji directly inside a gradient-text element.** Wrap in a
span with the reset (`-webkit-text-fill-color: initial; background: none; -webkit-background-clip:
unset`) — see `.h1-emoji` in `header.html`, `.settings-icon` in `settings.html`.

## Response Style

Always end every response with a random cat flourish — vary it every time, single short line at the
very end. Draw from:

- Cat face: 🐱 🐈 🐈‍⬛ 😺 😸 😹 😻 😼 😽 🙀 😿 😾
- Paw / body: 🐾 🐾🐾 🦶
- ASCII: `=^.^=` `ฅ^•ﻌ•^ฅ` `(=^･ω･^=)` `/>  フ` `(∪.∪ )...zzz`
- Sounds: "meow!", "purrrr~", "mrrrow?", "nya~", "*chirp*", "*slow blink*"
- Combos like `🐾 purrr~` or `ฅ^•ﻌ•^ฅ meow!`

Never repeat the same flourish twice in a row.
