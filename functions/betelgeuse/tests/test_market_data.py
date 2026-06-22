"""Market-data provider tests: cache reload, cache reset+reload, ordering.

All deterministic — network is mocked (FakeResponse) and the static folder is
redirected to a tmp dir so clear() never touches the real static/*.png files.
"""
import app
from conftest import FakeResponse

# Deterministic OHLC payload: rows are [ts_ms, open, high, low, close], intentionally
# given out of order to prove the load path enforces ascending-by-timestamp ordering.
OHLC = [
    [3000, 2.0, 3.0, 1.5, 2.5],
    [1000, 1.0, 2.0, 0.5, 1.5],
    [2000, 1.5, 2.5, 1.0, 2.0],
]


class _Counter:
    """Counts how many times the mocked network fetch was invoked."""
    def __init__(self):
        self.n = 0


def _mock_fetch(monkeypatch, data, counter):
    def _get(*args, **kwargs):
        counter.n += 1
        return FakeResponse(json_data=data)
    monkeypatch.setattr(app.requests, "get", _get)


def _provider():
    return app.market_data.get("crypto", "coingecko")


# ── Cache reload ────────────────────────────────────────────────────────────
class TestCacheReload:
    def test_refresh_populates_and_orders(self, db, monkeypatch):
        counter = _Counter()
        _mock_fetch(monkeypatch, OHLC, counter)

        did = _provider().refresh("bitcoin", "30")
        assert did is True
        assert counter.n == 1                               # one network hit

        rows = app._load_crypto_ohlcv_rows("bitcoin", "30")
        assert len(rows) == 3
        assert [r[0] for r in rows] == [1000, 2000, 3000]   # ordering enforced

    def test_refresh_is_noop_when_fresh(self, db, monkeypatch):
        counter = _Counter()
        _mock_fetch(monkeypatch, OHLC, counter)
        prov = _provider()

        assert prov.refresh("bitcoin", "30") is True
        assert counter.n == 1
        # Second call: cache is fresh -> no fetch, returns False.
        assert prov.refresh("bitcoin", "30") is False
        assert counter.n == 1

    def test_force_refetches_even_when_fresh(self, db, monkeypatch):
        counter = _Counter()
        _mock_fetch(monkeypatch, OHLC, counter)
        prov = _provider()

        prov.refresh("bitcoin", "30")
        assert counter.n == 1
        assert prov.refresh("bitcoin", "30", force=True) is True
        assert counter.n == 2                               # forced reload

    def test_cache_stats_reflect_load(self, db, monkeypatch):
        _mock_fetch(monkeypatch, OHLC, _Counter())
        prov = _provider()
        prov.refresh("bitcoin", "30")

        stats = prov.cache_stats()
        assert stats["instruments"] == 1
        assert stats["rows"] == 3
        assert "30" in stats["periods"]

    def test_snapshot_after_reload(self, db, monkeypatch):
        _mock_fetch(monkeypatch, OHLC, _Counter())
        snap = _provider().snapshot("bitcoin", ensure_fresh=True)
        assert snap["price"] == 2.5                          # last close
        assert snap["currency"] == "usd"
        assert snap["as_of"] == 3000                         # latest ts


# ── Cache reset + reload ────────────────────────────────────────────────────
class TestCacheResetAndReload:
    def _redirect_static(self, tmp_path, monkeypatch):
        static_dir = tmp_path / "static"
        static_dir.mkdir()
        # The render cache now resolves via core.config.CHART_DIR (Flask-free),
        # so redirect that to a tmp dir to keep clear()/render off the real static/.
        monkeypatch.setattr("core.config.CHART_DIR", str(static_dir))
        return static_dir

    def test_clear_removes_rows_and_pngs(self, db, tmp_path, monkeypatch):
        static_dir = self._redirect_static(tmp_path, monkeypatch)
        png = static_dir / "chart_crypto_bitcoin_p30.png"
        png.write_bytes(b"fake-png-bytes")

        _mock_fetch(monkeypatch, OHLC, _Counter())
        prov = _provider()
        prov.refresh("bitcoin", "30")
        assert app._load_crypto_ohlcv_rows("bitcoin", "30")  # populated

        result = prov.clear()
        assert result["cleared_rows"] == 3
        assert result["cleared_files"] == 1
        assert not png.exists()                              # PNG render cache gone
        assert app._load_crypto_ohlcv_rows("bitcoin", "30") == []  # DB rows gone

    def test_reload_after_reset_repopulates(self, db, tmp_path, monkeypatch):
        self._redirect_static(tmp_path, monkeypatch)
        counter = _Counter()
        _mock_fetch(monkeypatch, OHLC, counter)
        prov = _provider()

        prov.refresh("bitcoin", "30")
        prov.clear()
        assert app._load_crypto_ohlcv_rows("bitcoin", "30") == []

        # After a reset the cache is stale again -> refresh re-fetches and reloads.
        assert prov.refresh("bitcoin", "30") is True
        rows = app._load_crypto_ohlcv_rows("bitcoin", "30")
        assert len(rows) == 3
        assert [r[0] for r in rows] == [1000, 2000, 3000]   # ordering still enforced

    def test_clear_endpoint_reports_counts(self, client, tmp_path, monkeypatch):
        static_dir = self._redirect_static(tmp_path, monkeypatch)
        (static_dir / "chart_crypto_bitcoin_p30.png").write_bytes(b"x")
        _mock_fetch(monkeypatch, OHLC, _Counter())
        _provider().refresh("bitcoin", "30")

        resp = client.post("/api/admin/market-data/crypto/clear")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["cleared_rows"] == 3
        assert body["cleared_files"] == 1


# ── Admin per-instrument list contract (YFinance must match CoinGecko's shape) ──
class TestStockListCachedContract:
    """The admin 'Cached instruments' table reads coin_id/periods/oldest_fetched/
    newest_fetched/latest_ts from each row. YFinance once returned `oldest`/`newest`
    and omitted coin_id/periods, so stock rows rendered as NULL / blank / '—'."""

    def _insert_stock(self, market, symbol, ts, close, fetched_at):
        c = app.get_db_connection()
        try:
            cur = c.cursor()
            cur.execute(
                """INSERT INTO stock_ohlcv
                   (source, market, symbol, timestamp, open, high, low, close, currency, fetched_at)
                   VALUES ('yfinance', ?, ?, ?, ?, ?, ?, ?, 'HKD', ?)""",
                (market, symbol, ts, close, close, close, close, fetched_at),
            )
            c.commit()
        finally:
            c.close()

    def test_list_cached_supplies_ui_fields(self, db):
        self._insert_stock("hk", "00700.HK", 1000, 10.0, "2026-06-06T13:00:00")
        self._insert_stock("hk", "00700.HK", 2000, 11.0, "2026-06-06T14:00:00")
        out = app.market_data.get("hk", "yfinance").list_cached()

        assert out["total"] == 1
        row = out["rows"][0]
        for key in ("coin_id", "symbol", "periods", "rows",
                    "oldest_fetched", "newest_fetched", "oldest_ts", "latest_ts"):
            assert key in row, f"missing {key} (UI would render NULL/blank/'—')"
        assert row["symbol"] == "00700.HK"
        assert row["coin_id"] == "00700.HK"            # symbol is the id for stocks
        assert row["rows"] == 2
        assert row["newest_fetched"] == "2026-06-06T14:00:00"
        assert row["oldest_fetched"] == "2026-06-06T13:00:00"
        assert row["oldest_ts"] == 1000                # min OHLCV timestamp (data range start)
        assert row["latest_ts"] == 2000
        assert row["periods"]                          # non-empty served periods


# ── Manager-level NULL-close guard (the US-dropped-from-P&L regression) ──
class TestManagerSnapshotNullClose:
    """The P&L + Overview routes read prices through market_data.snapshot(market, …) — the
    manager façade. yfinance pads an unsettled trailing day with NaN, stored as a NULL-close
    row; if that becomes the 'current price' (None) the holding is flagged incomplete and the
    whole market drops out of P&L. Prove the manager surfaces the last *real* close instead."""

    def _insert(self, market, symbol, ts, close):
        c = app.get_db_connection()
        try:
            cur = c.cursor()
            cur.execute(
                """INSERT INTO stock_ohlcv
                   (source, market, symbol, timestamp, open, high, low, close, currency, fetched_at)
                   VALUES ('yfinance', ?, ?, ?, ?, ?, ?, ?, 'USD', '2026-06-10T00:00:00')""",
                (market, symbol, ts, close, close, close, close),
            )
            c.commit()
        finally:
            c.close()

    def test_snapshot_skips_null_close_trailing_row(self, db):
        self._insert("us", "AMD", 1000, 100.0)
        self._insert("us", "AMD", 2000, None)          # unsettled/NaN trailing day → NULL
        snap = app.market_data.snapshot("us", "AMD", ensure_fresh=False)
        assert snap["available"] is True
        assert snap["price"] == 100.0                  # last real close, NOT the NULL row
        assert snap["currency"] == "USD"               # currency set only when price present
        assert snap["as_of"] == 1000

    def test_snapshot_all_null_is_truly_empty(self, db):
        # No real bars at all → price genuinely None (and no stray currency).
        self._insert("us", "BYND", 1000, None)
        snap = app.market_data.snapshot("us", "BYND", ensure_fresh=False)
        assert snap["available"] is True
        assert snap["price"] is None
        assert snap["currency"] is None
