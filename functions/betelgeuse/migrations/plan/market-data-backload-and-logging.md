# Market-Data Backload / Rebuild + Proper Logging

## Context

The worker (`worker.py`) currently only does a 15-min freshness refresh (`prefetch_market_data`)
that fetches a **fixed trailing 1-year** window per stock (`_fetch_yf_history(..., period='1y')`).
There is no way to (a) extend history *backward* to a chosen start date, (b) keep filling *forward*
across gaps, or (c) rebuild a market's cache on demand. The user wants the worker to backload deep
daily history for portfolio instruments, surface the loaded date-range in the UI, and add a
per-(market, provider) **Rebuild cache** button (with progress) beside the existing **Clear cache**.
Separately, all process output is scattered `print()` with `[prefix]` tags and no rotation — the user
asked for proper logging and removal of obsolete logs.

**Confirmed decisions:** stocks (yfinance) get true daily backfill via `start=/end=`; crypto
(CoinGecko OHLC, coarse `days` buckets only) is **best-effort** (`days=max`). The long jobs are
**worker-run, DB-triggered** (web sets a flag in `settings`; worker executes; UI polls progress) —
never in the Flask process. Logging gets a **full migration** to the `logging` module with a rotating
file handler + console.

**Dedup (#5) and ordering (#6) are already structurally guaranteed** — both OHLCV tables have UNIQUE
constraints with `INSERT OR REPLACE`, and every load path is `ORDER BY timestamp`. This work mostly
*widens the fetch window* and *adds tests* proving those invariants hold for backfilled data.

**No DB migration needed** — new behavior reuses `settings(key,value)` for config/flags/progress, the
crypto `'max'` bucket reuses `crypto_ohlcv` (period is TEXT), wider stock history reuses
`stock_ohlcv`, and the date-range column is a query-only `MIN(timestamp)`. `DB_SCHEMA_VERSION` stays 3.

**Verified grounding:** `deploy/logs/` is already gitignored + rsync-excluded (reuse for log files,
no `.gitignore` change); stale `deploy/logs/dev-web.log` / `dev-worker.log` already exist (the
"obsolete logs" to remove); the worker poll loop `while True: sleep(30); heartbeat; tick(state)` is
the dispatch point; `list_cached` SQL already computes `MAX(timestamp)` → add `MIN(timestamp)`.

---

## A. Backload engine — `core/stockdata.py`, `core/crypto.py`
- `_fetch_yf_history(yahoo_symbol, start=None, end=None)`: when `start` set, call
  `.history(start=start, end=end, interval='1d')`; else keep `period='1y'`. Same shape/sort, same lazy
  `import yfinance` (the single test seam).
- `fetch_and_store_stock_ohlcv(market, symbol, start_date=None)`: if `start_date`, compute
  `end = (date.today()+timedelta(days=1)).isoformat()` (yfinance `end` is **exclusive** → +1 includes
  today) and pass through. INSERT OR REPLACE block unchanged. **#3 forward-fill + #4 backfill both fall
  out** of fetching `[start_date, today]` once: UNIQUE + INSERT OR REPLACE makes overlap a no-op and
  fills missing earlier/recent rows in one pass. No chunking (~600 rows = one call). STOCK_PERIODS
  client-side slicing unaffected.
- `fetch_and_store_crypto_ohlcv(coin_id, period='max')`: allow literal `'max'` → `days=max`, stored in
  a `period='max'` bucket. Merges via `_load_crypto_ohlcv_series` (dedupe-by-ts) — keep `'max'` OUT of
  `CRYPTO_PERIODS` so it never appears as a chart dropdown option.
- `get_backload_start_date()` in `core/marketdata.py` (Flask-free): reads
  `market_data_backload_start_date`, default `'2024-01-01'`, validates `YYYY-MM-DD` (else default).

## B. Provider + manager — `core/marketdata.py`
- **oldest_ts (#7):** add `MIN(timestamp) AS oldest_ts` to both providers' `list_cached` GROUP BY SQL
  and `'oldest_ts'` to each row. New per-row contract = previous fields **+ `oldest_ts`** (update the
  contract test + CLAUDE.md line).
- `MarketDataProvider.backload(instrument, start_date, progress_cb=None)`: base NotImplementedError;
  YFinance → `fetch_and_store_stock_ohlcv(market, inst, start_date=start_date)`; CoinGecko →
  best-effort `fetch_and_store_crypto_ohlcv(inst, period='max')` (start_date ignored).
- `MarketDataManager.rebuild(market, key, start_date, progress_cb=None)`: resolve provider →
  `p.clear()` (already per-market/per-source ⇒ satisfies #10) → instrument set =
  `SELECT symbol FROM portfolio WHERE market=?` mapped via existing `_provider_instrument_key` →
  loop `progress_cb(i,total,inst); p.backload(...)` with a small inter-symbol `sleep`.
- `_needs_backfill(market, symbol, start_date)`: True iff stored `MIN(timestamp)` missing/later than
  start_date (gate so finished backfill isn't re-fetched). `backload_market_data(start_date=None)`:
  iterate portfolio yfinance instruments, skip unless `_needs_backfill`, else `backload`. Keep
  `prefetch_market_data` as-is (cheap recent forward-fill; never prunes older rows).

## C. Worker — `worker.py` + helpers in `core/marketdata.py`
- **Settings keys:** request `market_data_rebuild_request_{market}` = provider key (presence ⇒
  pending); status `market_data_rebuild_status_{market}` = JSON `{state,provider,processed,total,
  current,started,finished,error}`, `state ∈ queued|running|done|error` (missing ⇒ idle).
- **Pure helpers (testable, mirror `core/health.py` injectable `now`):** `write_rebuild_status`,
  `read_rebuild_status`, `pending_rebuild_requests()` (scan `settings` LIKE
  `market_data_rebuild_request_%`), `run_rebuild(market, provider, start_date, now=None)` (writes
  running → progress_cb upserts processed/total/current → done/error, then deletes the request key).
- **Loop wiring:** add interval job `backload_market_data` (`BACKLOAD_INTERVAL_MIN=360`, idempotent,
  `_needs_backfill`-gated, `next_run_time=now`); in the `while True` body after `tick`, call
  `dispatch_rebuilds(scheduler)` — for each pending request write `queued`, delete the request key
  immediately, and schedule a **one-shot `'date'` job** `run_rebuild` on the threadpool (never inline —
  keeps heartbeats alive). Pickup latency ≤ 30s; UI shows `queued`.

## D. Routes — `app.py` (after `/clear`, ~line 447)
- `POST /api/admin/market-data/<market>/rebuild` (`?provider=`): validate provider, upsert request key
  + initial `{state:'queued',...}`, return `{success:True, queued:True}`. Allowed dev+prod (web only
  sets a flag; the worker does the work → CLAUDE.md-compliant).
- `GET /api/admin/market-data/<market>/rebuild/status` (`?provider=`) → `read_rebuild_status` (idle default).
- Global start date: reuse existing `POST /api/settings` with `market_data_backload_start_date`
  (no new route; `GET /api/settings` prefills). `/instruments` rows carry `oldest_ts` via B.

## E. UI — `templates/settings.html` (inline JS, ~1878–2060)
- **Global "Market Data Settings" group box** above `#mdTabs`: `<input type=date id=mdBackloadStart>` +
  Save (`loadMarketData` prefills from `/api/settings`, default 2024-01-01; `saveMdBackloadStart` POSTs).
- **Rebuild button** beside Clear in the `selectMdMarket` body, `confirm()`-gated → POST `.../rebuild`
  → start `pollMdRebuild()`.
- **Progress indicator** `#mdRebuild` (bar + text): poll `.../rebuild/status` every ~2s; render
  `processed/total` width + `current`; on done/error stop, signal in `#mdMsg`, refresh stats + table.
- **Date-range column (#7):** add `<th>Date range</th>`; cell
  `${mdFmtEpoch(r.oldest_ts)} – ${mdFmtEpoch(r.latest_ts)}`; bump loading/empty/error colspan 6→7.
- Clear (#9) already `confirm()`-gated — unchanged.

## F. Logging — new `core/logging_setup.py` + `core/config.py`
- `config.LOG_DIR = os.environ.get('BETELGEUSE_LOG_DIR') or <repo>/deploy/logs` (already
  gitignored/rsync-excluded; no new ignore entry). Distinct filenames `{process}.app.log` so the
  RotatingFileHandler never clashes with launchd's raw `{process}.out/.err.log`.
- `configure_logging(process_name, env)`: `makedirs(LOG_DIR, exist_ok=True)`; configure the
  `betelgeuse` parent logger with `RotatingFileHandler(maxBytes=2_000_000, backupCount=5)` +
  `StreamHandler()`, format `'%(asctime)s %(levelname)s %(name)s: %(message)s'`; idempotent.
  `get_logger(name)` → `logging.getLogger(name)`. Call once per process: `worker.main()`, app.py init,
  serve.py before serving.
- **print → logger** (`betelgeuse.<module>`): worker.py (`betelgeuse.worker`), app.py:466/1579/1585 +
  serve.py:29/31 (`betelgeuse.web`), notifications.py (`betelgeuse.notifications`), marketdata.py +
  new backload/rebuild logs (`betelgeuse.marketdata`), stockdata.py/crypto.py error prints.
- **Obsolete cleanup:** delete the replaced `print()`s; remove the stale
  `deploy/logs/dev-web.log` / `dev-worker.log`. No old log-writer module exists to remove.

## G. Tests (#12) — new `tests/test_backload.py` + extend `tests/test_market_data.py`
Reuse `db`/`client`/`conn`/`setval`/`FakeResponse`/`_redirect_static`; patch
`core.stockdata._fetch_yf_history` (lazy import ⇒ real yfinance never loaded).
- **#4 backfill reaches start_date:** fake `_fetch_yf_history` records the `start` kwarg + returns
  seeded rows whose earliest ts == start_date; assert stored `MIN(timestamp)` == earliest AND fake
  received `start='2024-01-01'`.
- **#5 no duplicates:** store seeded rows twice (stock double-call / crypto double FakeResponse);
  assert `COUNT(*)` unchanged.
- **#6 ascending order:** insert out-of-order rows; assert `_load_stock_ohlcv_rows`,
  `_load_stock_ohlcv_series`, `_load_crypto_ohlcv_series` all return strictly ascending timestamps.
- **oldest_ts contract** (both providers); **crypto best-effort** (`period='max'` bucket created,
  merged series ascending/deduped); **rebuild state machine** (`run_rebuild` queued→running→done,
  progress advances, request key deleted, exception → error); **route shapes** (rebuild sets flag +
  `{queued:True}`; status idle→written; instruments row has `oldest_ts`).
All deterministic, temp DB, seeded timestamps; full suite green before "done".

## H. Docs — `CLAUDE.md`
Update Market Data Manager (backload + DB-triggered worker rebuild, request/status keys, one-shot
'date' job), Chart Caching/OHLCV (stock backfill to `market_data_backload_start_date`; crypto
best-effort `days=max`), the `list_cached` contract line (+`oldest_ts`), Worker section (new keys,
`BACKLOAD_INTERVAL_MIN`, `market_data_backload` job), a new **Logging** section
(`core/logging_setup.py`, `LOG_DIR`/`BETELGEUSE_LOG_DIR`, `betelgeuse.<module>` scheme), Testing
carriers (`tests/test_backload.py`, `_fetch_yf_history` as the yfinance seam).

---

## Implementation order
1. Logging: `core/logging_setup.py` + `config.LOG_DIR` + print→logger sweep + remove stale dev logs.
2. Backload params (`stockdata`/`crypto`) + `get_backload_start_date` + `oldest_ts` (+ contract test).
3. `provider.backload` + `manager.rebuild` + status helpers + `backload_market_data`/`_needs_backfill`.
4. Worker wiring (backload interval job + `dispatch_rebuilds` one-shot).
5. Routes (rebuild + status).
6. `settings.html` UI (global box, rebuild button, progress poller, date-range column).
7. `tests/test_backload.py` + route tests.
8. CLAUDE.md.

## Verification
- `python3 -m pytest` green (esp. new `tests/test_backload.py` for #4–#6).
- Set start date 2024-01-01 in the global box → trigger Rebuild for HK/yfinance → watch the progress
  bar fill, then confirm the instruments table date-range column starts ~2024-01-01.
- Restart worker → confirm `deploy/logs/worker.app.log` gets timestamped, rotating, leveled lines;
  web logs to `deploy/logs/web.app.log`.
- Confirm Clear and Rebuild both prompt; both act only on the selected market+provider.

## Risks
- yfinance rate limits on rebuilding many symbols → sequential + inter-symbol sleep; periodic job is
  `_needs_backfill`-gated.
- 30s worker pickup latency → UI shows `queued` (acceptable).
- Never run rebuild inline in the poll loop → one-shot 'date' job on the threadpool keeps heartbeats alive.
- Keep `'max'` out of `CRYPTO_PERIODS`.

## Critical files
- `core/marketdata.py`, `core/stockdata.py`, `core/crypto.py`, `worker.py`, `app.py`,
  `templates/settings.html`, `core/config.py`
- New: `core/logging_setup.py`, `tests/test_backload.py`
