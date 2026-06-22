"""YFinanceProvider + stock OHLCV helpers tests.

Mirrors the structure of test_market_data.py. Network is fully mocked via
monkeypatching core.stockdata._fetch_yf_history — yfinance is never imported.
Static folder redirected to tmp so generate_stock_chart / clear() never touch
the real static/*.png files.
"""
import app
import core.stockdata as stockdata
from core.stockdata import _to_yahoo

# One year of fake daily OHLCV bars (ts_ms, open, high, low, close).
# Intentionally out of order to prove the load path enforces ascending ordering.
# Timestamps are spaced 1 day apart; most-recent = ts 4000.
_DAY_MS = 86_400_000
BARS = [
    [4 * _DAY_MS, 4.0, 5.0, 3.5, 4.5],
    [1 * _DAY_MS, 1.0, 2.0, 0.5, 1.5],
    [2 * _DAY_MS, 2.0, 3.0, 1.5, 2.5],
    [3 * _DAY_MS, 3.0, 4.0, 2.5, 3.5],
]


def _mock_fetch(monkeypatch, rows=None):
    if rows is None:
        rows = BARS
    # accept the start/end/retries kwargs the fetch seam now takes (rate-limit backoff)
    monkeypatch.setattr(stockdata, '_fetch_yf_history', lambda *a, **k: (rows, None))


def _hk_provider():
    return app.market_data.get('hk', 'yfinance')


def _us_provider():
    return app.market_data.get('us', 'yfinance')


def _jp_provider():
    return app.market_data.get('jp', 'yfinance')


# ── _to_yahoo symbol translation ──────────────────────────────────────────────
class TestToYahoo:
    def test_hk_strips_leading_zeros_to_4_digits(self):
        assert _to_yahoo('hk', '00700.HK') == '0700.HK'

    def test_hk_preserves_4_digit_numbers(self):
        assert _to_yahoo('hk', '09988.HK') == '9988.HK'

    def test_hk_single_digit_padded(self):
        assert _to_yahoo('hk', '00001.HK') == '0001.HK'

    def test_hk_3_digit_padded(self):
        assert _to_yahoo('hk', '00388.HK') == '0388.HK'

    def test_jp_passthrough(self):
        assert _to_yahoo('jp', '7203.T') == '7203.T'

    def test_us_plain_passthrough(self):
        assert _to_yahoo('us', 'AAPL') == 'AAPL'

    def test_us_class_share_dot_to_dash(self):
        assert _to_yahoo('us', 'BRK.A') == 'BRK-A'

    def test_us_class_share_lowercase(self):
        assert _to_yahoo('us', 'brk.b') == 'BRK-B'


# ── Cache reload ───────────────────────────────────────────────────────────────
class TestCacheReload:
    def test_refresh_populates_and_orders(self, db, monkeypatch):
        _mock_fetch(monkeypatch)
        did = _hk_provider().refresh('00700.HK')
        assert did is True

        rows = stockdata._load_stock_ohlcv_rows('hk', '00700.HK')
        assert len(rows) == 4
        assert [r[0] for r in rows] == sorted(r[0] for r in BARS)

    def test_refresh_is_noop_when_fresh(self, db, monkeypatch):
        _mock_fetch(monkeypatch)
        prov = _hk_provider()
        assert prov.refresh('00700.HK') is True
        # second call: cache within freshness window → no fetch
        fetch_count = [0]
        monkeypatch.setattr(stockdata, '_fetch_yf_history',
                            lambda *a, **k: (fetch_count.__setitem__(0, fetch_count[0]+1) or (BARS, None)))
        assert prov.refresh('00700.HK') is False
        assert fetch_count[0] == 0

    def test_force_refetches_even_when_fresh(self, db, monkeypatch):
        count = [0]
        def fake_fetch(*a, **k):
            count[0] += 1
            return BARS, None
        monkeypatch.setattr(stockdata, '_fetch_yf_history', fake_fetch)
        prov = _hk_provider()
        prov.refresh('00700.HK')
        assert count[0] == 1
        prov.refresh('00700.HK', force=True)
        assert count[0] == 2

    def test_empty_fetch_returns_false(self, db, monkeypatch):
        _mock_fetch(monkeypatch, rows=[])
        assert _hk_provider().refresh('00700.HK') is False

    def test_cache_stats_reflect_load(self, db, monkeypatch):
        _mock_fetch(monkeypatch)
        _hk_provider().refresh('00700.HK')
        stats = _hk_provider().cache_stats()
        assert stats['instruments'] == 1
        assert stats['rows'] == 4

    def test_snapshot_price_currency_as_of(self, db, monkeypatch):
        _mock_fetch(monkeypatch)
        snap = _hk_provider().snapshot('00700.HK', ensure_fresh=True)
        assert snap['price'] == 4.5        # last close in BARS
        assert snap['currency'] == 'HKD'
        assert snap['as_of'] == 4 * _DAY_MS

    def test_snapshot_us_currency(self, db, monkeypatch):
        _mock_fetch(monkeypatch)
        snap = _us_provider().snapshot('AAPL', ensure_fresh=True)
        assert snap['currency'] == 'USD'

    def test_snapshot_jp_currency(self, db, monkeypatch):
        _mock_fetch(monkeypatch)
        snap = _jp_provider().snapshot('7203.T', ensure_fresh=True)
        assert snap['currency'] == 'JPY'

    def test_snapshot_empty_cache_all_none(self, db, monkeypatch):
        _mock_fetch(monkeypatch, rows=[])
        snap = _hk_provider().snapshot('00700.HK', ensure_fresh=True)
        assert snap['price'] is None
        assert snap['currency'] is None
        assert all(v is None for v in snap['performance'].values())

    def test_snapshot_performance_1d(self, db, monkeypatch):
        # BARS spans 4 days; 1d lookback should find a non-None pct_change.
        _mock_fetch(monkeypatch)
        snap = _hk_provider().snapshot('00700.HK', ensure_fresh=True)
        # last close = 4.5 (day 4), 1d prior close = 3.5 (day 3)
        assert snap['performance']['1'] is not None

    def test_load_rows_days_slice(self, db, monkeypatch):
        _mock_fetch(monkeypatch)
        _hk_provider().refresh('00700.HK')
        # Request last 2 days of data (cutoff = 4*DAY_MS - 2*DAY_MS = 2*DAY_MS)
        rows = stockdata._load_stock_ohlcv_rows('hk', '00700.HK', days=2)
        # Should include rows at ts 2*DAY_MS, 3*DAY_MS, 4*DAY_MS (within 2-day window)
        assert all(r[0] >= 2 * _DAY_MS for r in rows)
        assert len(rows) >= 2


# ── Cache reset + reload ───────────────────────────────────────────────────────
class TestCacheResetAndReload:
    def _redirect_static(self, tmp_path, monkeypatch):
        static_dir = tmp_path / 'static'
        static_dir.mkdir()
        monkeypatch.setattr('core.config.CHART_DIR', str(static_dir))
        return static_dir

    def test_clear_removes_rows_and_pngs(self, db, tmp_path, monkeypatch):
        static_dir = self._redirect_static(tmp_path, monkeypatch)
        png = static_dir / 'chart_stock_hk_00700_HK_p30.png'
        png.write_bytes(b'fake')

        _mock_fetch(monkeypatch)
        prov = _hk_provider()
        prov.refresh('00700.HK')
        assert stockdata._load_stock_ohlcv_rows('hk', '00700.HK')

        result = prov.clear()
        assert result['cleared_rows'] == 4
        assert result['cleared_files'] == 1
        assert not png.exists()
        assert stockdata._load_stock_ohlcv_rows('hk', '00700.HK') == []

    def test_clear_endpoint_reports_counts(self, client, tmp_path, monkeypatch):
        static_dir = tmp_path / 'static'
        static_dir.mkdir()
        monkeypatch.setattr('core.config.CHART_DIR', str(static_dir))
        (static_dir / 'chart_stock_hk_00700_HK_p30.png').write_bytes(b'x')

        _mock_fetch(monkeypatch)
        _hk_provider().refresh('00700.HK')

        resp = client.post('/api/admin/market-data/hk/clear')
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['success'] is True
        assert body['cleared_rows'] == 4
        assert body['cleared_files'] == 1

    def test_reload_after_reset_repopulates(self, db, tmp_path, monkeypatch):
        self._redirect_static(tmp_path, monkeypatch)
        _mock_fetch(monkeypatch)
        prov = _hk_provider()
        prov.refresh('00700.HK')
        prov.clear()
        assert stockdata._load_stock_ohlcv_rows('hk', '00700.HK') == []
        assert prov.refresh('00700.HK') is True
        assert len(stockdata._load_stock_ohlcv_rows('hk', '00700.HK')) == 4


# ── list_cached ───────────────────────────────────────────────────────────────
class TestListCached:
    def test_returns_populated_symbol(self, db, monkeypatch):
        _mock_fetch(monkeypatch)
        _hk_provider().refresh('00700.HK')
        result = _hk_provider().list_cached()
        assert result['total'] == 1
        assert result['rows'][0]['symbol'] == '00700.HK'
        assert result['rows'][0]['rows'] == 4

    def test_filters_by_query(self, db, monkeypatch):
        _mock_fetch(monkeypatch)
        prov = _hk_provider()
        prov.refresh('00700.HK')
        prov.refresh('09988.HK')
        assert _hk_provider().list_cached(q='9988')['total'] == 1
        assert _hk_provider().list_cached(q='xxxx')['total'] == 0

    def test_does_not_cross_markets(self, db, monkeypatch):
        # Data stored under 'hk' must not appear in 'us' list_cached.
        _mock_fetch(monkeypatch)
        _hk_provider().refresh('00700.HK')
        assert _us_provider().list_cached()['total'] == 0

    def test_pagination_shape(self, db, monkeypatch):
        _mock_fetch(monkeypatch)
        prov = _hk_provider()
        for sym in ('00001.HK', '00002.HK', '00003.HK'):
            prov.refresh(sym)
        result = prov.list_cached()
        # per_page minimum is 10, so all 3 fit on page 1
        assert result['total'] == 3
        assert result['page'] == 1
        assert result['pages'] == 1
        assert len(result['rows']) == 3
        # rows are ordered by symbol
        symbols = [r['symbol'] for r in result['rows']]
        assert symbols == sorted(symbols)


# ── prefetch_market_data ───────────────────────────────────────────────────────
class TestPrefetchMarketData:
    def test_prefetch_refreshes_hk_jp_us_skips_crypto(self, db, monkeypatch):
        # Add one instrument per market to the portfolio.
        c = app.get_db_connection()
        try:
            cur = c.cursor()
            cur.executemany(
                'INSERT INTO portfolio (symbol, market, name, "group", added_date) VALUES (?,?,?,?,?)',
                [
                    ('00700.HK', 'hk', 'Tencent', 'Default', '2026-01-01'),
                    ('7203.T',   'jp', 'Toyota',  'Default', '2026-01-01'),
                    ('AAPL',     'us', 'Apple',   'Default', '2026-01-01'),
                    ('BTC',      'crypto', 'Bitcoin', 'Default', '2026-01-01'),
                ],
            )
            c.commit()
        finally:
            c.close()

        fetch_calls = []
        def fake_fetch(sym, *a, **k):
            fetch_calls.append(sym)
            return BARS, None
        monkeypatch.setattr(stockdata, '_fetch_yf_history', fake_fetch)
        # Crypto still uses requests.get for CoinGecko — mock that too so it doesn't fail.
        monkeypatch.setattr(app.requests, 'get',
                            lambda *a, **k: type('R', (), {'status_code': 200,
                                                            'json': lambda s: [], 'raise_for_status': lambda s: None})())

        result = app.prefetch_market_data()
        assert result['fetched'] == 3   # hk, jp, us each fetched once
        # yfinance fetch was called for hk/jp/us (translated yahoo symbols)
        assert len(fetch_calls) == 3
        # crypto was skipped (not a yfinance provider)
        assert result['skipped'] >= 1
