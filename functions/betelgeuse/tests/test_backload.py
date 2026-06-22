"""Backload / rebuild tests — proves the deep-history fetch, idempotent de-dup, and
ascending ordering (the user's #4–#6), plus the worker-run rebuild state machine and
the new routes.

Deterministic: yfinance is mocked by patching core.stockdata._fetch_yf_history (the
single network seam — the real yfinance is never imported); CoinGecko is mocked via
app.requests.get (the same module object core.crypto imports). Temp DB per test.
"""
import sys
import types

import pytest

import app
from conftest import FakeResponse
from core import stockdata, crypto, marketdata

# A fixed epoch-ms base so timestamps are explicit and tz-independent.
DAY = 86400000
BASE = 1704067200000  # 2024-01-01T00:00:00Z


# ── helpers ───────────────────────────────────────────────────────────────────
def _insert_stock(market, symbol, ts, close, fetched_at='2026-01-01T00:00:00'):
    c = app.get_db_connection()
    try:
        cur = c.cursor()
        cur.execute("""INSERT OR REPLACE INTO stock_ohlcv
                       (source, market, symbol, timestamp, open, high, low, close, currency, fetched_at)
                       VALUES ('yfinance', ?, ?, ?, ?, ?, ?, ?, 'HKD', ?)""",
                    (market, symbol, ts, close, close, close, close, fetched_at))
        c.commit()
    finally:
        c.close()


def _insert_crypto(coin_id, period, ts, close, fetched_at='2026-01-01T00:00:00'):
    c = app.get_db_connection()
    try:
        cur = c.cursor()
        cur.execute("""INSERT OR REPLACE INTO crypto_ohlcv
                       (source, coin_id, period, timestamp, open, high, low, close, fetched_at)
                       VALUES ('coingecko', ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (coin_id, str(period), ts, close, close, close, close, fetched_at))
        c.commit()
    finally:
        c.close()


def _add_portfolio(market, symbol):
    c = app.get_db_connection()
    try:
        cur = c.cursor()
        cur.execute('INSERT INTO portfolio (symbol, market, name, "group", added_date) '
                    'VALUES (?,?,?,?,?)', (symbol, market, symbol, 'Default', '2024-01-01'))
        c.commit()
    finally:
        c.close()


def _stock_agg(market, symbol):
    c = app.get_db_connection()
    try:
        cur = c.cursor()
        cur.execute('SELECT COUNT(*) AS n, MIN(timestamp) AS lo FROM stock_ohlcv '
                    'WHERE market=? AND symbol=?', (market, symbol))
        r = cur.fetchone()
        return r['n'], r['lo']
    finally:
        c.close()


# ── #4: backfill reaches the configured start date ──────────────────────────────
class TestStockBackfill:
    def test_backfill_requests_start_and_stores_earliest(self, db, monkeypatch):
        captured = {}
        rows = [[BASE + i * DAY, 1.0, 2.0, 0.5, 1.5 + i] for i in range(5)]

        def fake(yahoo_symbol, start=None, end=None, **kwargs):
            captured['symbol'] = yahoo_symbol
            captured['start'] = start
            captured['end'] = end
            return rows, None

        monkeypatch.setattr(stockdata, '_fetch_yf_history', fake)
        stockdata.fetch_and_store_stock_ohlcv('hk', '00700.HK', start_date='2024-01-01')

        # the fetch was asked for the explicit [start, end) range, not a 1y window
        assert captured['start'] == '2024-01-01'
        assert captured['end'] is not None
        # and the stored history reaches back to the seeded earliest candle
        n, lo = _stock_agg('hk', '00700.HK')
        assert n == 5 and lo == BASE

    def test_no_start_date_uses_period_window(self, db, monkeypatch):
        captured = {}

        def fake(yahoo_symbol, start=None, end=None, **kwargs):
            captured['start'] = start
            return [[BASE, 1, 2, 0.5, 1.5]], None

        monkeypatch.setattr(stockdata, '_fetch_yf_history', fake)
        stockdata.fetch_and_store_stock_ohlcv('hk', '00700.HK')  # no start_date
        assert captured['start'] is None   # falls back to the trailing 1y window


# ── #5: re-fetching never duplicates rows (de-dup at save time) ─────────────────
class TestIdempotentStore:
    def test_stock_double_store_no_duplicates(self, db, monkeypatch):
        rows = [[BASE + i * DAY, 1, 2, 0.5, 1.5] for i in range(4)]
        monkeypatch.setattr(stockdata, '_fetch_yf_history', lambda *a, **k: (rows, None))
        stockdata.fetch_and_store_stock_ohlcv('us', 'AAPL', start_date='2024-01-01')
        n1, _ = _stock_agg('us', 'AAPL')
        stockdata.fetch_and_store_stock_ohlcv('us', 'AAPL', start_date='2024-01-01')
        n2, _ = _stock_agg('us', 'AAPL')
        assert n1 == 4 and n2 == 4   # UNIQUE + INSERT OR REPLACE → stable count

    def test_crypto_double_store_no_duplicates(self, db, monkeypatch):
        data = [[BASE, 1, 2, 0.5, 1.5], [BASE + DAY, 1.5, 2.5, 1, 2]]
        monkeypatch.setattr(app.requests, 'get', lambda *a, **k: FakeResponse(json_data=data))
        crypto.fetch_and_store_crypto_ohlcv('bitcoin', 'max')
        crypto.fetch_and_store_crypto_ohlcv('bitcoin', 'max')
        rows = crypto._load_crypto_ohlcv_rows('bitcoin', 'max')
        assert len(rows) == 2


# ── #6: load returns ascending order regardless of insert order ─────────────────
class TestOrdering:
    def test_stock_load_orders_ascending(self, db):
        for ts, close in [(3 * DAY, 3.0), (1 * DAY, 1.0), (2 * DAY, 2.0)]:
            _insert_stock('hk', '00001.HK', ts, close)
        assert [r[0] for r in stockdata._load_stock_ohlcv_rows('hk', '00001.HK')] \
            == [DAY, 2 * DAY, 3 * DAY]
        assert [t for t, _ in stockdata._load_stock_ohlcv_series('hk', '00001.HK')] \
            == [DAY, 2 * DAY, 3 * DAY]

    def test_crypto_series_merges_dedups_and_orders(self, db):
        # overlapping timestamps across the numbered + 'max' buckets, inserted out of order
        _insert_crypto('bitcoin', '30', 2 * DAY, 20.0)
        _insert_crypto('bitcoin', '30', 1 * DAY, 10.0)
        _insert_crypto('bitcoin', 'max', 1 * DAY, 99.0)   # same ts as a '30' row → deduped
        _insert_crypto('bitcoin', 'max', 500, 5.0)        # older, deep-history point
        series = crypto._load_crypto_ohlcv_series('bitcoin')
        assert [t for t, _ in series] == [500, DAY, 2 * DAY]   # ascending + deduped


# ── crypto best-effort backload (days=max bucket) ───────────────────────────────
class TestCryptoBackload:
    def test_backload_stores_max_bucket(self, db, monkeypatch):
        data = [[BASE, 1, 2, 0.5, 1.5], [BASE + DAY, 1.5, 2.5, 1, 2]]
        monkeypatch.setattr(app.requests, 'get', lambda *a, **k: FakeResponse(json_data=data))
        prov = app.market_data.get('crypto', 'coingecko')
        n = prov.backload('bitcoin', '2024-01-01')
        assert n == 2
        rows = crypto._load_crypto_ohlcv_rows('bitcoin', 'max')
        assert [r[0] for r in rows] == [BASE, BASE + DAY]
        # the deep-history bucket merges into the unified series (still ascending)
        series = crypto._load_crypto_ohlcv_series('bitcoin')
        assert [t for t, _ in series] == [BASE, BASE + DAY]


# ── start-date setting accessor ─────────────────────────────────────────────────
class TestBackloadStartDate:
    def test_default_when_unset(self, db):
        assert marketdata.get_backload_start_date() == marketdata.DEFAULT_BACKLOAD_START_DATE

    def test_reads_setting(self, db, setval):
        setval('market_data_backload_start_date', '2023-05-01')
        assert marketdata.get_backload_start_date() == '2023-05-01'

    def test_invalid_falls_back_to_default(self, db, setval):
        setval('market_data_backload_start_date', 'not-a-date')
        assert marketdata.get_backload_start_date() == marketdata.DEFAULT_BACKLOAD_START_DATE


# ── _needs_backfill gate ─────────────────────────────────────────────────────────
class TestNeedsBackfill:
    def test_true_when_no_history(self, db):
        assert marketdata._needs_backfill('hk', '00700.HK', '2024-01-01') is True

    def test_false_when_history_reaches_start(self, db):
        _insert_stock('hk', '00700.HK', BASE + DAY, 10.0)   # 2024-01-02, within slop of start
        assert marketdata._needs_backfill('hk', '00700.HK', '2024-01-01') is False

    def test_true_when_history_too_recent(self, db):
        _insert_stock('hk', '00700.HK', BASE + 400 * DAY, 10.0)   # ~2025 — far after start
        assert marketdata._needs_backfill('hk', '00700.HK', '2024-01-01') is True


# ── periodic backload job (yfinance only, gated) ────────────────────────────────
class TestBackloadMarketData:
    def test_backfills_yfinance_skips_crypto(self, db, monkeypatch):
        monkeypatch.setattr(marketdata.time, 'sleep', lambda *a, **k: None)
        _add_portfolio('hk', '00700.HK')
        _add_portfolio('crypto', 'BTC')
        rows = [[BASE + i * DAY, 1, 2, 0.5, 1.5] for i in range(3)]
        monkeypatch.setattr(stockdata, '_fetch_yf_history', lambda *a, **k: (rows, None))
        res = marketdata.backload_market_data('2024-01-01')
        assert res['backfilled'] == 1            # only the yfinance stock
        assert res['skipped'] >= 1               # crypto skipped (best-effort/on-demand)
        n, lo = _stock_agg('hk', '00700.HK')
        assert n == 3 and lo == BASE

    def test_skips_when_already_filled(self, db, monkeypatch):
        monkeypatch.setattr(marketdata.time, 'sleep', lambda *a, **k: None)
        _add_portfolio('hk', '00700.HK')
        _insert_stock('hk', '00700.HK', BASE + DAY, 10.0)   # already reaches start
        called = {'n': 0}

        def fake(*a, **k):
            called['n'] += 1
            return ([[BASE, 1, 2, 0.5, 1.5]], None)

        monkeypatch.setattr(stockdata, '_fetch_yf_history', fake)
        res = marketdata.backload_market_data('2024-01-01')
        assert res['backfilled'] == 0 and called['n'] == 0   # _needs_backfill gated it out


# ── manager.rebuild: clear → backload every portfolio instrument ─────────────────
class TestManagerRebuild:
    def test_rebuild_clears_then_backfills_with_progress(self, db, monkeypatch):
        monkeypatch.setattr(marketdata.time, 'sleep', lambda *a, **k: None)
        _add_portfolio('hk', '00700.HK')
        _insert_stock('hk', '00700.HK', 999, 1.0, '2020-01-01T00:00:00')   # stale row to be cleared
        rows = [[BASE + i * DAY, 1, 2, 0.5, 1.5] for i in range(3)]
        monkeypatch.setattr(stockdata, '_fetch_yf_history', lambda *a, **k: (rows, None))
        seen = []
        res = app.market_data.rebuild('hk', 'yfinance', '2024-01-01',
                                      progress_cb=lambda p, t, c: seen.append((p, t, c)))
        assert res['available'] and res['provider'] == 'yfinance'
        assert res['processed'] == 1 and res['total'] == 1
        n, lo = _stock_agg('hk', '00700.HK')
        assert n == 3 and lo == BASE                    # stale row gone, fresh range stored
        assert seen[0] == (0, 1, '00700.HK') and seen[-1] == (1, 1, None)


# ── run_rebuild: DB-backed status state machine ─────────────────────────────────
class TestRunRebuildStateMachine:
    def test_queued_running_done_and_clears_request(self, db, monkeypatch):
        seen = []

        def fake_rebuild(market, key, start_date, progress_cb=None):
            seen.append(marketdata.read_rebuild_status(market)['state'])  # must be 'running'
            progress_cb(0, 2, 'aaa')
            seen.append(marketdata.read_rebuild_status(market))
            progress_cb(2, 2, None)
            return {'available': True, 'provider': key, 'processed': 2, 'total': 2, 'cleared_rows': 9}

        monkeypatch.setattr(marketdata.market_data, 'rebuild', fake_rebuild)
        marketdata.set_rebuild_request('hk', 'yfinance')
        assert marketdata.read_rebuild_status('hk')['state'] == 'queued'
        assert ('hk', 'yfinance') in marketdata.pending_rebuild_requests()

        final = marketdata.run_rebuild('hk', 'yfinance', '2024-01-01', now='2026-06-07T00:00:00')

        assert seen[0] == 'running'
        assert seen[1]['state'] == 'running' and seen[1]['processed'] == 0 and seen[1]['current'] == 'aaa'
        assert final['state'] == 'done' and final['processed'] == 2 and final['total'] == 2
        assert marketdata.read_rebuild_status('hk')['state'] == 'done'
        assert marketdata.pending_rebuild_requests() == []   # request flag cleared

    def test_error_path_records_error_and_clears_request(self, db, monkeypatch):
        def boom(market, key, start_date, progress_cb=None):
            raise RuntimeError('kaboom')

        monkeypatch.setattr(marketdata.market_data, 'rebuild', boom)
        marketdata.set_rebuild_request('us', 'yfinance')
        final = marketdata.run_rebuild('us', 'yfinance', '2024-01-01')
        assert final['state'] == 'error' and 'kaboom' in final['error']
        assert marketdata.read_rebuild_status('us')['state'] == 'error'
        assert marketdata.pending_rebuild_requests() == []


# ── routes ───────────────────────────────────────────────────────────────────────
class TestRebuildRoutes:
    def test_rebuild_queues_request(self, client):
        resp = client.post('/api/admin/market-data/hk/rebuild')
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['success'] and body['queued'] and body['provider'] == 'yfinance'
        assert ('hk', 'yfinance') in marketdata.pending_rebuild_requests()
        st = client.get('/api/admin/market-data/hk/rebuild/status').get_json()
        assert st['state'] == 'queued'

    def test_rebuild_status_idle_default(self, client):
        st = client.get('/api/admin/market-data/jp/rebuild/status').get_json()
        assert st['state'] == 'idle'

    def test_instruments_row_carries_oldest_ts(self, client):
        _insert_stock('hk', '00700.HK', 1000, 10.0, '2026-06-06T13:00:00')
        _insert_stock('hk', '00700.HK', 2000, 11.0, '2026-06-06T14:00:00')
        d = client.get('/api/admin/market-data/hk/instruments').get_json()
        assert d['available']
        assert d['rows'][0]['oldest_ts'] == 1000
        assert d['rows'][0]['latest_ts'] == 2000


# ── background-job status (the "is a reload happening?" indicator) ──────────────
class TestBackgroundJobStatus:
    def test_status_idle_by_default(self, db):
        assert marketdata.read_job_status('prefetch') == {'state': 'idle'}
        assert marketdata.read_jobs_status() == {'prefetch': {'state': 'idle'},
                                                 'backload': {'state': 'idle'}}

    def test_run_tracked_records_running_then_idle_with_result(self, db):
        seen = {}

        def fake_job():
            seen['state_during'] = marketdata.read_job_status('prefetch')['state']
            return {'fetched': 3, 'skipped': 2}

        out = marketdata.run_tracked('prefetch', fake_job)
        assert out == {'fetched': 3, 'skipped': 2}
        assert seen['state_during'] == 'running'        # 'running' visible while the job runs
        st = marketdata.read_job_status('prefetch')
        assert st['state'] == 'idle'                    # back to idle when finished
        assert st['last_result'] == {'fetched': 3, 'skipped': 2}
        assert st['last_started'] and st['last_finished']
        assert st.get('last_error') is None

    def test_run_tracked_records_error_and_reraises(self, db):
        import pytest
        def boom():
            raise RuntimeError('nope')
        with pytest.raises(RuntimeError):
            marketdata.run_tracked('backload', boom)
        st = marketdata.read_job_status('backload')
        assert st['state'] == 'idle' and 'nope' in st['last_error']

    def test_next_run_is_recorded(self, db):
        marketdata.record_job_next_run('prefetch', '2026-06-07T12:00:00')
        assert marketdata.read_job_status('prefetch')['next_run'] == '2026-06-07T12:00:00'

    def test_jobs_route_shape(self, client):
        marketdata.record_job_next_run('backload', '2026-06-07T18:00:00')
        d = client.get('/api/admin/market-data/jobs').get_json()
        assert 'prefetch' in d and 'backload' in d
        assert d['backload']['next_run'] == '2026-06-07T18:00:00'


# ── yfinance rate-limit backoff (retry inside the fetch seam) ───────────────────
def _install_fake_yf(monkeypatch):
    """Inject a fake `yfinance` (+ `.exceptions`) into sys.modules so the lazy imports
    inside _fetch_yf_history resolve without the real package. `state['actions']` is a
    per-`.history()`-call script: ``'raise'`` → raise the rate-limit error; ``[]`` → an
    empty frame (genuine no-data); a non-empty ``[(ts_sec,o,h,l,c),...]`` → that data."""
    class FakeRateLimitError(Exception):
        pass

    state = {'actions': [], 'calls': 0}

    class _DT:
        def __init__(self, ts): self._ts = ts
        def timestamp(self): return self._ts

    class _Hist:
        def __init__(self, rows): self._rows = rows
        @property
        def empty(self): return not self._rows
        def iterrows(self):
            for ts, o, h, l, c in self._rows:
                yield _DT(ts), {'Open': o, 'High': h, 'Low': l, 'Close': c}

    class _Ticker:
        def __init__(self, symbol): self.symbol = symbol
        def history(self, **kwargs):
            i = state['calls']
            state['calls'] += 1
            action = state['actions'][i] if i < len(state['actions']) else []
            if action == 'raise':
                raise FakeRateLimitError('429 Too Many Requests')
            return _Hist(action if isinstance(action, list) else [])

    fake_yf = types.ModuleType('yfinance')
    fake_yf.Ticker = _Ticker
    fake_exc = types.ModuleType('yfinance.exceptions')
    fake_exc.YFRateLimitError = FakeRateLimitError
    fake_yf.exceptions = fake_exc
    monkeypatch.setitem(sys.modules, 'yfinance', fake_yf)
    monkeypatch.setitem(sys.modules, 'yfinance.exceptions', fake_exc)
    return state, FakeRateLimitError


class TestRateLimitBackoff:
    def test_retries_then_succeeds_with_escalating_backoff(self, db, monkeypatch):
        state, _ = _install_fake_yf(monkeypatch)
        # throttle twice, then return one candle (ts in SECONDS — the seam multiplies by 1000)
        state['actions'] = ['raise', 'raise', [(BASE / 1000, 1.0, 2.0, 0.5, 1.5)]]
        waits = []
        monkeypatch.setattr(stockdata.time, 'sleep', lambda s: waits.append(s))
        rows, err = stockdata._fetch_yf_history('0700.HK', start='2024-01-01',
                                                end='2024-02-01', retries=3)
        assert err is None
        assert rows == [[BASE, 1.0, 2.0, 0.5, 1.5]]
        assert waits == [60, 600]   # two retries used the first two backoff steps

    def test_exhausts_budget_returns_rate_limit_signal(self, db, monkeypatch):
        state, _ = _install_fake_yf(monkeypatch)
        state['actions'] = ['raise', 'raise', 'raise', 'raise']   # never recovers
        waits = []
        monkeypatch.setattr(stockdata.time, 'sleep', lambda s: waits.append(s))
        rows, err = stockdata._fetch_yf_history('0700.HK', retries=3)
        assert rows == [] and err == 'rate_limit'
        assert waits == [60, 600, 1800]   # full 1m→10m→30m schedule, then give up

    def test_no_retry_budget_signals_immediately(self, db, monkeypatch):
        state, _ = _install_fake_yf(monkeypatch)
        state['actions'] = ['raise']
        waits = []
        monkeypatch.setattr(stockdata.time, 'sleep', lambda s: waits.append(s))
        rows, err = stockdata._fetch_yf_history('0700.HK')   # retries=0 (the prefetch default)
        assert rows == [] and err == 'rate_limit' and waits == []

    def test_empty_frame_is_not_a_throttle_and_not_retried(self, db, monkeypatch):
        # a pre-IPO date range comes back empty — that's no-data, NOT a rate-limit, so we
        # must NOT burn the retry budget on it (else IPOs would be hammered forever)
        state, _ = _install_fake_yf(monkeypatch)
        state['actions'] = [[]]
        waits = []
        monkeypatch.setattr(stockdata.time, 'sleep', lambda s: waits.append(s))
        rows, err = stockdata._fetch_yf_history('0700.HK', retries=3)
        assert rows == [] and err is None and waits == []
        assert state['calls'] == 1   # tried once, accepted the empty result


class TestNaNCandleHandling:
    """yfinance pads a not-yet-settled trailing day with NaN OHLC. If stored, it becomes a
    NULL-close row that snapshot() reads as the current price (None) — which once dropped a
    whole market (US) out of the P&L. Guard both ends: never persist a NaN candle, and never
    let a NULL-close row win as the last close."""

    def test_fetch_drops_nan_trailing_candle(self, db, monkeypatch):
        state, _ = _install_fake_yf(monkeypatch)
        nan = float('nan')
        # one real candle followed by a padded NaN day (ts in SECONDS — the seam ×1000)
        state['actions'] = [[(BASE / 1000, 1.0, 2.0, 0.5, 1.5),
                             ((BASE + DAY) / 1000, nan, nan, nan, nan)]]
        rows, err = stockdata._fetch_yf_history('AMD', retries=0)
        assert err is None
        assert rows == [[BASE, 1.0, 2.0, 0.5, 1.5]]   # NaN row dropped, real close kept

    def test_series_excludes_null_close_row(self, db):
        # a good close, then a NULL-close trailing row (an older fetch's NaN day)
        _insert_stock('us', 'AMD', BASE, 100.0)
        _insert_stock('us', 'AMD', BASE + DAY, None)
        series = stockdata._load_stock_ohlcv_series('us', 'AMD')
        assert series == [(BASE, 100.0)]              # NULL-close row filtered out

    def test_snapshot_uses_last_real_close_not_null(self, db):
        _insert_stock('us', 'AMD', BASE, 100.0)
        _insert_stock('us', 'AMD', BASE + DAY, None)   # unsettled trailing day
        snap = marketdata.YFinanceProvider('us').snapshot('AMD', ensure_fresh=False)
        assert snap['price'] == 100.0                  # last real close, not the NULL
        assert snap['currency'] == 'USD'
        assert snap['as_of'] == BASE


class TestRateLimitedPropagation:
    def test_store_raises_ratelimited_when_exhausted(self, db, monkeypatch):
        monkeypatch.setattr(stockdata, '_fetch_yf_history', lambda *a, **k: ([], 'rate_limit'))
        with pytest.raises(stockdata.RateLimited):
            stockdata.fetch_and_store_stock_ohlcv('hk', '00700.HK',
                                                  start_date='2024-01-01', retries=3)

    def test_empty_result_still_returns_none(self, db, monkeypatch):
        # genuine no-data must stay a quiet None, never a RateLimited
        monkeypatch.setattr(stockdata, '_fetch_yf_history', lambda *a, **k: ([], None))
        assert stockdata.fetch_and_store_stock_ohlcv('hk', '00700.HK',
                                                     start_date='2024-01-01') is None

    def test_refresh_swallows_ratelimited(self, db, monkeypatch):
        # refresh is called inline by generate_stock_chart for a web request — a throttle
        # must never propagate (serve stale), so refresh returns False instead of raising
        monkeypatch.setattr(stockdata, '_fetch_yf_history', lambda *a, **k: ([], 'rate_limit'))
        prov = app.market_data.get('hk', 'yfinance')
        assert prov.refresh('00700.HK', force=True) is False


class TestBackloadRateLimitAbort:
    def test_backload_pass_aborts_on_persistent_throttle(self, db, monkeypatch):
        monkeypatch.setattr(marketdata.time, 'sleep', lambda *a, **k: None)
        for sym in ('00001.HK', '00002.HK', '00003.HK'):
            _add_portfolio('hk', sym)
        calls = {'n': 0}

        def fake(*a, **k):
            calls['n'] += 1
            return ([], 'rate_limit')

        monkeypatch.setattr(stockdata, '_fetch_yf_history', fake)
        res = marketdata.backload_market_data('2024-01-01')
        assert calls['n'] == 1            # stopped after the first instrument throttled
        assert res['backfilled'] == 0     # nothing completed before the abort

    def test_rebuild_stops_early_on_throttle(self, db, monkeypatch):
        monkeypatch.setattr(marketdata.time, 'sleep', lambda *a, **k: None)
        for sym in ('00001.HK', '00002.HK'):
            _add_portfolio('hk', sym)
        calls = {'n': 0}

        def fake(*a, **k):
            calls['n'] += 1
            return ([], 'rate_limit')

        monkeypatch.setattr(stockdata, '_fetch_yf_history', fake)
        seen = []
        res = app.market_data.rebuild('hk', 'yfinance', '2024-01-01',
                                      progress_cb=lambda p, t, c: seen.append((p, t, c)))
        assert res['available'] and res['total'] == 2
        assert calls['n'] == 1            # stopped after the first throttle
        assert res['processed'] == 0      # broke before counting it done
        assert seen[-1] == (0, 2, None)   # final progress callback still fires
