# Portfolio Performance (PnL) System

## Goal

Add a base-currency-normalized P&L view to Betelgeuse: a free FX provider (yfinance, USD-pivot)
with a fixed-anchor fallback, a chosen **base currency** (HKD default / USD / AUD) saved in
Settings → Admin → General, a **Watch List / My Portfolio** tab split on the home page, a top-line
**PnL (base)** KPI card, and a **market-grouped holdings** view that shows aggregate PnL per market.

## Decisions baked in (defaults — flag if any is wrong)

- **PnL = unrealized**, on *current* holdings: `(price − avg_buy_cost) × net_qty`, converted to base
  via FX. `net_qty` = buys − sells (what you still hold); cost basis = weighted-avg **buy** cost
  (`bought_price`). Realized P&L from sells is out of scope (future extension). A held instrument with
  no buy transactions / no cached price contributes nothing and is flagged "incomplete".
- **PnL % is currency-free** (`(price−cost)/cost`); only absolute amounts get FX-converted.
- **FX is cache-or-fallback in the web** (never an inline network call) — mirrors
  `/api/portfolio/performance` cache-only philosophy. The **worker** keeps the FX cache warm. Before
  the first worker pass the fixed anchors are used. User OK'd delayed rates.
- **PnL card lives in the shared KPI strip** (replacing "Latest Addition"); the strip stays above the
  tabs so it's visible on both. My Portfolio tab additionally shows per-market aggregate PnL.

---

## 1. FX provider — new `core/fx.py` (Flask-free)

Everything pivots through **USD-per-currency** so we only ever need liquid `<CCY>USD=X` Yahoo tickers
(no cross-pairs like `JPYHKD=X` that may not exist):

```
usd_per(ccy):  'USD'→1.0 ; else cached fx_rates.usd_per ; else FALLBACK_USD_PER[ccy] ; else None
fx_rate(C, B) = usd_per(C) / usd_per(B)      # units of B per 1 unit of C ; C==B → 1.0
```

- `BASE_CURRENCIES = ['HKD', 'USD', 'AUD']`; `get_base_currency()` reads settings `base_currency`
  (default `'HKD'`, validated against the list).
- **Fallback anchors (1.1), triangle via USD pivot** — derived from the two anchors the user gave:
  - `HKD→USD = 0.1276` ⇒ `FALLBACK_USD_PER['HKD'] = 0.1276`
  - `HKD→AUD = 0.181` ⇒ `USD_per(AUD) = 0.1276 / 0.181 ≈ 0.7050` (triangle)
  - `USD_per(USD) = 1.0`; `USD_per(JPY) ≈ 0.0064` (editable constant — JP wasn't anchored by the user;
    needed so JP holdings convert in fallback mode). All editable at the top of `fx.py`.
  - Sanity check: `fx_rate('HKD','AUD') = 0.1276/0.7050 = 0.181` ✓ (reproduces the given anchor).
- **Network seam (the only yfinance touch, monkeypatched in tests):**
  `_fetch_fx_usd_per(ccy)` → `yf.Ticker(f'{ccy}USD=X').history(period='5d')` last close, or `None`.
- **Cache read (web, no network):** `usd_per()` reads the `fx_rates` table; any age is acceptable
  (delayed OK). `refresh_fx_rates(currencies)` (worker) fetches each needed `<ccy>USD=X` via the seam
  and `INSERT OR REPLACE`s `fx_rates`. Currencies needed = `{HKD, JPY, USD, AUD}` (instrument
  currencies HKD/JPY/USD + base AUD).
- Currency normalization helper: snapshot currency comes back as `'usd'` (crypto, lowercase) or
  `'HKD'/'JPY'/'USD'` (stocks). `_norm_ccy()` upper-cases; crypto → `'USD'`.
- **Status helper for the UI:** `fx_status()` → `{ provider:'yfinance', pivot:'USD', base, generated_at,
  rates:[ { currency, usd_per, source:'live'|'fallback', fetched_at } ] }` — reports, per currency,
  whether the value came from the cache (`live`) or the hard-coded anchors (`fallback`), so the panel
  can show provenance + age. (`usd_per()` gains a sibling that returns value **and** source.)

## 2. Schema — migration `004_fx_rates` (DB v3 → v4)

Scaffold with `python3 migrate.py new fx_rates` (writes `migrations/004_fx_rates.py` **and** bumps
`DB_SCHEMA_VERSION` 3→4 in `core/db.py`; the head==const test enforces it). A tiny refetchable cache:

```sql
CREATE TABLE fx_rates (
    currency   TEXT PRIMARY KEY,   -- 'HKD','JPY','USD','AUD'
    usd_per    REAL NOT NULL,      -- USD per 1 unit of currency
    fetched_at TEXT NOT NULL
);
```

`up` creates it; `down` drops it (refetchable cache → safe to drop, never `portfolio`/`transactions`).
`init_db()` builds it from scratch on a fresh DB via the migration runner (no `init_db` edits).

## 3. PnL computation + route — `app.py` + helper in `core/marketdata.py` (or new `core/pnl.py`)

New endpoint **`GET /api/portfolio/pnl`** (`?base=` optional override; else `base_currency` setting):

```
{ base, as_of,
  totals:  { cost, value, pnl, pnl_pct, incomplete },
  markets: { hk: { cost, value, pnl, pnl_pct, count, incomplete,
                   holdings:[ { symbol, name, qty, price, currency, cost, value,
                                pnl, pnl_pct, fx } ] }, jp:{…}, us:{…}, crypto:{…} },
  fx: { HKD:…, JPY:…, USD:…, AUD:… }, missing:[…] }
```

Per holding (only `net_qty>0` **and** `bought_price` known):
- `r = fx_rate(native_ccy, base)`; `value = price·net_qty·r`; `cost = bought_price·net_qty·r`;
  `pnl = value − cost`; `pnl_pct = (price−cost_native)/cost_native·100` (currency-free).
- price/currency from `market_data.snapshot(market, inst, ensure_fresh=False)` (cache-only).
  No cached price → holding flagged `incomplete`, excluded from `value`/`pnl` sums (counted in `missing`).
- Per-market + grand totals sum `cost`/`value`/`pnl`; `pnl_pct = pnl/cost·100`.

Computation lives in a **pure, testable** `compute_pnl(portfolio_rows, snapshots, fx_fn, base)` helper
(no DB/network) so tests seed inputs directly; the route just wires DB + `market_data` + `fx` into it.

## 4. FX Rates panel — Settings → Admin → **FX Rates** (`templates/settings.html`) + `GET /api/fx`

One home for currency: the **base-currency selector lives here** (moved off General, per the request),
alongside the read-only provider + rate provenance. The FX **provider must be visible even though it
isn't configurable** — this dedicated panel gives both the control and the provenance in one place
(cleaner than burying it in Market Data).

- **New nav item** under Admin (`data-panel="fx-rates"`, after Market Data) + a matching
  `settings-panel` (`data-panel-id="fx-rates"`). General panel is **left untouched** (no base-currency
  control there).
- **Route `GET /api/fx`** → `fx_status()` (provider, pivot=USD, active base, `generated_at`, per-currency
  rows). Allowed on dev + prod (read-only).
- **Panel renders:**
  - **Base Currency `<select>`** (HKD / USD / AUD) — the one editable control. Loads from
    `settings.base_currency || 'HKD'`; **Save** POSTs `{base_currency}` to `/api/settings`, then
    re-fetches `/api/fx` and the home PnL re-denominates.
  - A header line: **Provider: `yfinance` · USD-pivot · delayed** (static label).
  - A **pair grid relative to the active base** — one row per other currency in `{HKD, JPY, USD, AUD}`
    (skip the base itself), each row showing the **rate and its inverse side by side**:
    **`<base>→<ccy>` (e.g. `HKD → USD  0.1276`) | `<ccy>→<base>` (inverse, e.g. `USD → HKD  7.837`)**,
    plus **Source (Live / Fallback)** chip and **Updated**. Both numbers are derived from the cached
    `usd_per` values (`<base>→<ccy> = usd_per(base)/usd_per(ccy)`; the inverse is its reciprocal), so the
    grid re-labels itself when the base changes. `Source` = **Live** (green) only when *both* legs resolve
    from cache (USD is always live/identity), else **Fallback** (amber); `Updated` = relative age of the
    non-USD leg's `fetched_at` (Fallback rows show "—").
  - A footnote: "Rates are delayed, USD-pivoted, and refreshed by the worker every 15 min. When the
    provider is unreachable, fixed anchors (HKD/USD = 0.1276, HKD/AUD = 0.181, triangle for the rest)
    are used and shown as **Fallback**."
- Loads once on panel open (no auto-refresh; rates move slowly). A manual **🔄 Refresh** button re-GETs
  `/api/fx` (still no network from the web — just re-reads the cache the worker maintains).
- General's `saveGeneralSettings()` / `loadSettings()` are **unchanged** — `base_currency` is owned by
  this panel's own load/save handlers.

## 5. Home page tabs + PnL card + market-grouped holdings (`templates/overview.html`)

**Layout:** page-head → **KPI strip (shared, with PnL card)** → **tab bar [Watch List | My Portfolio]**
→ tab panels.

- **Tab bar:** two tabs, `Watch List` active by default. Pure client-side show/hide; remember last tab
  in `sessionStorage`. No router change — still one `/` page.
- **Watch List panel** = the *current* overview body verbatim (toolbar: search + market chips +
  refresh status; the per-market `Market › Group` sections). Unchanged behaviour.
- **PnL KPI card (#4):** replace the 4th KPI ("Latest Addition") with **"PnL (HKD)"** (label shows the
  active base). Value = signed base-currency total PnL (green/red); sub = `+x.xx% · N holdings`
  (or `… · M incomplete`). Fetched from `/api/portfolio/pnl`; `—` until it resolves.
- **My Portfolio panel (#5):** dynamic, **grouped by market only** (HK/JP/US/crypto — markets you
  actually hold), reusing the existing `.market-section` look. Each market header carries an **aggregate
  PnL pill** (signed, colored, base currency) + holdings count. Rows = held instruments with columns
  **`# · Instrument · Qty · Avg Cost · Price · Mkt Value · PnL`** (PnL = base amount + % like the perf
  cells; Avg Cost/Price in native currency; Mkt Value + PnL in base). Empty state when nothing held.
- **Money formatting:** add `fmtMoney(v, ccy)` with a symbol map (`HKD`/`USD`→`$`, `AUD`→`A$`,
  `JPY`→`¥`) + sign; keep `fmtPrice` for native per-share prices.

## 6. Worker — warm the FX cache (`worker.py` / `core/marketdata.py`)

Fold FX warming into the existing **prefetch** path (runs every 15 min, already network-touching, fires
at startup): at the top of `prefetch_market_data()` call `refresh_fx_rates(['HKD','JPY','USD','AUD'])`
(3 live tickers — USD is identity). No new scheduler job, no new UI surface. Failures fall back to
anchors and are logged, never fatal.

## 7. Tests (`tests/test_fx.py` new; extend `tests/test_routes.py`)

- **FX pure:** `usd_per('USD')==1`; cached row used; fallback when no cache; `fx_rate` identity == 1;
  `fx_rate('HKD','AUD')≈0.181` (reproduces the anchor); USD-pivot cross (`JPY→AUD`).
- **`refresh_fx_rates`** with `_fetch_fx_usd_per` monkeypatched → rows upserted; idempotent re-run.
- **`fx_status` / `GET /api/fx`:** with a cached row → that currency reports `source:'live'` + its
  `fetched_at`; an un-cached currency reports `source:'fallback'`; shape carries provider + base.
- **`compute_pnl`** pure: seed mixed-currency holdings + snapshots + a stub `fx_fn` → assert per-market
  and grand totals, `pnl_pct`, and that a cache-miss holding lands in `missing`/`incomplete` and is
  excluded from sums.
- **`/api/portfolio/pnl`** route: seed `portfolio` + `transactions` + `stock_ohlcv` + `fx_rates` →
  assert base-currency aggregates; `?base=` override; default from `base_currency` setting.
- **Migration 004:** covered by the existing head==const test + from-scratch `init_db()` schema-shape
  test (add `fx_rates` to expectations).
- Full suite green before "done" (deterministic; no real clock/network; `_fetch_fx_usd_per` is the seam).

## 8. Docs — `CLAUDE.md`

New **FX / Base Currency** subsection (USD-pivot, `core/fx.py`, fallback anchors + triangle, worker
warms cache, web is cache-or-fallback, **FX Rates** admin panel = base-currency selector + provider +
per-currency provenance); `fx_rates` table in the schema block + settings key `base_currency`; routes
`/api/portfolio/pnl` + `/api/fx`; overview tab split + PnL card; testing carriers (`tests/test_fx.py`,
`compute_pnl`, `fx_status`, `_fetch_fx_usd_per` seam). Version bump (web+worker) at the end.

---

## Implementation order
1. `core/fx.py` (pure: fallback anchors, `usd_per`, `fx_rate`, `_norm_ccy`, `fx_status`) + unit tests.
2. Migration `004_fx_rates` (scaffold → fill up/down) + `refresh_fx_rates` + `_fetch_fx_usd_per` seam + tests.
3. `compute_pnl` pure helper + routes `/api/portfolio/pnl` + `/api/fx` + route tests.
4. Worker: FX warm in `prefetch_market_data`.
5. Settings → **FX Rates** panel (nav item + panel): base-currency select (load/save) + read-only provider/rates table.
6. `overview.html`: tab bar, PnL KPI card, My Portfolio market-grouped holdings, `fmtMoney`.
7. CLAUDE.md + version bump.

## Verification
- `python3 -m pytest` green.
- Settings → FX Rates: provider shows `yfinance · USD-pivot`; rows show Live (green) with a fresh
  timestamp after the worker runs, Fallback (amber) before it / when offline.
- Settings → FX Rates: pick AUD, Save → PnL card + My Portfolio re-denominate in A$.
- My Portfolio: each held market shows a colored aggregate PnL pill; rows reconcile to the pill; grand
  total reconciles to the KPI card.
- Kill the worker / empty `fx_rates` → PnL still renders via fallback anchors (HKD/USD=0.1276, HKD/AUD=0.181).

## Risks / notes
- yfinance FX ticker shape `<CCY>USD=X` — USD-pivot avoids missing cross-pairs; fallback covers outages.
- JP fallback rate is an assumed constant (user only anchored HKD/USD + HKD/AUD) — editable in `fx.py`.
- Web never fetches FX inline (fast, no rate-limit risk); worker owns refresh.
- DB change ⇒ migration 004 + version bump is part of this change (not a separate ask).
