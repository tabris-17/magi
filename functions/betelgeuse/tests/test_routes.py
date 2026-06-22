"""Route/integration tests via the Flask test client against the temp DB."""
import time

import app
from conftest import FakeResponse
from core import health, version


# ── Application Health (/api/health) + dev branding ─────────────────────────
class TestHealth:
    def test_health_shape_and_versions(self, client):
        body = client.get("/api/health").get_json()
        assert body["env"] == "dev"  # module default in tests
        assert body["web"]["version"] == version.WEB_VERSION
        assert "server_time" in body
        # No worker heartbeat on a fresh DB -> not ready.
        assert body["worker"]["ready"] is False
        assert body["worker"]["version"] is None

    def test_health_worker_ready_when_heartbeat_fresh(self, client):
        health.record_worker_heartbeat("1.0", "prod", now=int(time.time()))
        body = client.get("/api/health").get_json()
        assert body["worker"]["ready"] is True
        assert body["worker"]["version"] == "1.0"
        assert body["worker"]["env"] == "prod"

    def test_dev_branding_in_header(self, client, monkeypatch):
        monkeypatch.setattr(app, "APP_ENV", "dev")
        html = client.get("/settings").get_data(as_text=True)
        assert "Betelgeuse-Dev" in html
        # Dev also rewrites the browser tab title (one source of truth in header.html).
        assert "document.title = document.title.replace" in html

    def test_prod_branding_in_header(self, client, monkeypatch):
        monkeypatch.setattr(app, "APP_ENV", "prod")
        html = client.get("/settings").get_data(as_text=True)
        assert "Betelgeuse-Dev" not in html
        assert "Betelgeuse" in html
        # No tab-title rewrite in prod.
        assert "document.title = document.title.replace" not in html


# ── Portfolio CRUD ──────────────────────────────────────────────────────────
class TestPortfolioRoutes:
    def test_add_persists_canonical_symbol(self, client):
        resp = client.post("/api/portfolio", json={
            "market": "hk", "symbol": "700.hk", "name": "Tencent", "group": "Default",
        })
        assert resp.status_code == 201
        # Stored form must be canonical, not the raw input.
        data = client.get("/api/portfolio").get_json()
        assert any(item["symbol"] == "00700.HK" for item in data["hk"])

    def test_add_rejects_invalid_market(self, client):
        resp = client.post("/api/portfolio", json={"market": "xx", "symbol": "AAA"})
        assert resp.status_code == 400

    def test_add_rejects_empty_symbol(self, client):
        resp = client.post("/api/portfolio", json={"market": "hk", "symbol": "  "})
        assert resp.status_code == 400

    def test_add_rejects_invalid_group(self, client):
        resp = client.post("/api/portfolio", json={
            "market": "hk", "symbol": "00700.HK", "group": "NotARealGroup",
        })
        assert resp.status_code == 400

    def test_update_and_delete(self, client):
        add = client.post("/api/portfolio", json={
            "market": "hk", "symbol": "00700.HK", "name": "Tencent", "group": "Default",
        }).get_json()
        item_id = add["id"]

        upd = client.put(f"/api/portfolio/{item_id}", json={
            "market": "hk", "name": "Tencent Holdings", "group": "Growth",
            "added_date": "2026-01-01", "comment": "x",
        })
        assert upd.status_code == 200

        bad = client.put(f"/api/portfolio/{item_id}", json={
            "market": "hk", "name": "Tencent", "group": "Nope", "added_date": "2026-01-01",
        })
        assert bad.status_code == 400

        dele = client.delete(f"/api/portfolio/{item_id}")
        assert dele.status_code == 200
        data = client.get("/api/portfolio").get_json()
        assert data["hk"] == []

    def test_add_persists_watch_fields(self, client):
        resp = client.post("/api/portfolio", json={
            "market": "hk", "symbol": "00700.HK", "name": "Tencent", "group": "Default",
            "monitor_price": 300, "trigger_price": 250.5, "bought": True,
        })
        assert resp.status_code == 201
        item = client.get("/api/portfolio").get_json()["hk"][0]
        assert item["monitor_price"] == 300
        assert item["trigger_price"] == 250.5
        assert item["bought"] is True

    def test_update_persists_watch_fields(self, client):
        item_id = client.post("/api/portfolio", json={
            "market": "hk", "symbol": "00700.HK", "name": "Tencent", "group": "Default",
        }).get_json()["id"]
        upd = client.put(f"/api/portfolio/{item_id}", json={
            "market": "hk", "name": "Tencent", "group": "Default", "added_date": "2026-01-01",
            "monitor_price": 123.45, "trigger_price": None, "bought": True,
        })
        assert upd.status_code == 200
        item = client.get("/api/portfolio").get_json()["hk"][0]
        assert item["monitor_price"] == 123.45
        assert item["trigger_price"] is None
        assert item["bought"] is True

    def test_blank_watch_fields_default_to_null(self, client):
        client.post("/api/portfolio", json={
            "market": "hk", "symbol": "00700.HK", "name": "Tencent", "group": "Default",
        })
        item = client.get("/api/portfolio").get_json()["hk"][0]
        assert item["monitor_price"] is None
        assert item["trigger_price"] is None
        assert item["bought"] is False
        assert item["bought_price"] is None


# ── Transactions ledger ─────────────────────────────────────────────────────
class TestTransactions:
    def _add_position(self, client):
        return client.post("/api/portfolio", json={
            "market": "hk", "symbol": "00700.HK", "name": "Tencent", "group": "Default",
            "bought": True,
        }).get_json()["id"]

    def test_add_list_delete_roundtrip_normalizes_symbol(self, client):
        self._add_position(client)
        # POST using a raw (non-canonical) symbol — server must normalize before storing.
        add = client.post("/api/portfolio/hk/700.hk/transactions", json={
            "txn_type": "buy", "price": 10, "quantity": 100, "txn_date": "2024-01-05",
        })
        assert add.status_code == 201
        # GET via the canonical symbol returns it.
        rows = client.get("/api/portfolio/hk/00700.HK/transactions").get_json()
        assert len(rows) == 1
        assert rows[0]["symbol"] == "00700.HK"
        assert rows[0]["price"] == 10 and rows[0]["quantity"] == 100

        dele = client.delete(f"/api/portfolio/transactions/{rows[0]['id']}")
        assert dele.status_code == 200
        assert client.get("/api/portfolio/hk/00700.HK/transactions").get_json() == []

    def test_list_is_date_ascending(self, client):
        self._add_position(client)
        for d in ("2024-03-01", "2024-01-01", "2024-02-01"):
            client.post("/api/portfolio/hk/00700.HK/transactions",
                        json={"price": 1, "quantity": 1, "txn_date": d})
        dates = [r["txn_date"] for r in client.get("/api/portfolio/hk/00700.HK/transactions").get_json()]
        assert dates == ["2024-01-01", "2024-02-01", "2024-03-01"]

    def test_weighted_avg_bought_price(self, client):
        # 100@10 + 100@12 + 50@11 → (1000+1200+550)/250 = 11.00
        self._add_position(client)
        for price, qty in ((10, 100), (12, 100), (11, 50)):
            client.post("/api/portfolio/hk/00700.HK/transactions",
                        json={"price": price, "quantity": qty, "txn_date": "2024-01-01"})
        item = client.get("/api/portfolio").get_json()["hk"][0]
        assert item["bought_price"] == 11.0
        assert item["bought_qty"] == 250

    def test_net_qty_subtracts_sells(self, client):
        # buy 100 + buy 100, then sell 60 → bought_qty=200 (buys only), net_qty=140 (buys−sells)
        self._add_position(client)
        for price, qty in ((10, 100), (12, 100)):
            client.post("/api/portfolio/hk/00700.HK/transactions",
                        json={"price": price, "quantity": qty, "txn_date": "2024-01-01"})
        client.post("/api/portfolio/hk/00700.HK/transactions",
                    json={"price": 13, "quantity": 60, "txn_date": "2024-02-01", "txn_type": "sell"})
        item = client.get("/api/portfolio").get_json()["hk"][0]
        assert item["bought_qty"] == 200      # buys only (cost basis qty)
        assert item["net_qty"] == 140         # current position = buys − sells

    def test_net_qty_none_without_transactions(self, client):
        self._add_position(client)
        assert client.get("/api/portfolio").get_json()["hk"][0]["net_qty"] is None

    def test_add_rejects_invalid_market(self, client):
        resp = client.post("/api/portfolio/xx/AAA/transactions",
                           json={"price": 1, "quantity": 1})
        assert resp.status_code == 400

    def test_add_rejects_nonpositive_qty(self, client):
        self._add_position(client)
        resp = client.post("/api/portfolio/hk/00700.HK/transactions",
                           json={"price": 10, "quantity": 0})
        assert resp.status_code == 400

    def test_add_rejects_missing_numbers(self, client):
        self._add_position(client)
        resp = client.post("/api/portfolio/hk/00700.HK/transactions",
                           json={"txn_date": "2024-01-01"})
        assert resp.status_code == 400

    def test_delete_position_purges_transactions(self, client):
        item_id = self._add_position(client)
        client.post("/api/portfolio/hk/00700.HK/transactions",
                    json={"price": 10, "quantity": 100, "txn_date": "2024-01-01"})
        client.delete(f"/api/portfolio/{item_id}")
        # Ledger for that instrument is gone, so a re-add starts clean.
        assert client.get("/api/portfolio/hk/00700.HK/transactions").get_json() == []

    def test_delete_one_group_keeps_shared_transactions(self, client):
        # The same instrument filed under two groups → two portfolio rows sharing one
        # (market, symbol); the buy ledger is shared. Deleting one grouping must NOT purge
        # the ledger (it would orphan the surviving row's holding and drop it from P&L).
        keep = client.post("/api/portfolio", json={
            "market": "us", "symbol": "CRWV", "name": "CoreWeave", "group": "Growth",
            "bought": True}).get_json()["id"]
        drop = client.post("/api/portfolio", json={
            "market": "us", "symbol": "CRWV", "name": "CoreWeave", "group": "Momentum",
            "bought": True}).get_json()["id"]
        client.post("/api/portfolio/us/CRWV/transactions",
                    json={"price": 100, "quantity": 50, "txn_date": "2024-01-01"})

        client.delete(f"/api/portfolio/{drop}")          # remove the 'Momentum' grouping
        # The shared ledger survives because the 'Growth' row still references (us, CRWV).
        txns = client.get("/api/portfolio/us/CRWV/transactions").get_json()
        assert len(txns) == 1 and txns[0]["quantity"] == 50
        rows = [r for r in client.get("/api/portfolio").get_json()["us"] if r["id"] == keep]
        assert rows and rows[0]["net_qty"] == 50         # surviving holding intact

        client.delete(f"/api/portfolio/{keep}")          # now remove the LAST grouping
        # No portfolio row references the instrument anymore → ledger is purged.
        assert client.get("/api/portfolio/us/CRWV/transactions").get_json() == []


# ── Database Tool whitelist ─────────────────────────────────────────────────
class TestDatabaseTool:
    def test_known_table_ok(self, client):
        resp = client.get("/api/admin/db/table/portfolio")
        assert resp.status_code == 200
        assert "columns" in resp.get_json()

    def test_unknown_table_404(self, client):
        resp = client.get("/api/admin/db/table/sqlite_master")  # not in whitelist
        assert resp.status_code == 404
        resp2 = client.get("/api/admin/db/table/does_not_exist")
        assert resp2.status_code == 404


# ── DB migration routes + maintenance gate ──────────────────────────────────
class TestMigrateRoutes:
    def test_status_reports_current_head_gate(self, client):
        d = client.get("/api/admin/migrate/status").get_json()
        assert d["current"] == app.DB_SCHEMA_VERSION
        assert d["head"] == app.DB_SCHEMA_VERSION
        assert d["gate"] == "OK"
        assert d["pending"] == []

    def test_history_lists_baseline(self, client):
        d = client.get("/api/admin/migrate/history").get_json()
        versions = [r["version"] for r in d["history"]]
        assert app.DB_SCHEMA_VERSION in versions

    def test_mutators_blocked_on_prod(self, client, monkeypatch):
        monkeypatch.setattr(app, "APP_ENV", "prod")
        for path in ("up", "down", "prune"):
            resp = client.post(f"/api/admin/migrate/{path}", json={"to": 1, "keep": 1})
            assert resp.status_code == 403

    def test_mutators_allowed_on_dev(self, client, monkeypatch):
        monkeypatch.setattr(app, "APP_ENV", "dev")
        # prune is a safe no-op mutator (no backups exist) — proves dev isn't 403'd
        resp = client.post("/api/admin/migrate/prune", json={"keep": 5})
        assert resp.status_code == 200

    def test_gate_blocks_normal_routes_but_not_migrate(self, client, monkeypatch):
        # Closed gate -> normal pages 503; migrate endpoints + static stay open.
        monkeypatch.setattr(app, "MIGRATION_GATE", "NEEDS_UP")
        assert client.get("/").status_code == 503
        assert client.get("/api/portfolio").status_code == 503
        assert client.get("/api/admin/migrate/status").status_code == 200


# ── Crypto chart period validation ──────────────────────────────────────────
class TestCryptoChartRoute:
    def test_invalid_period_400(self, client):
        resp = client.get("/api/crypto/bitcoin/chart/999")
        assert resp.status_code == 400


# ── Stock chart route (yfinance) ─────────────────────────────────────────────
class TestStockChartRoute:
    def test_invalid_period_400(self, client):
        resp = client.get("/api/stock/us/AAPL/chart/999")
        assert resp.status_code == 400

    def test_unknown_market_404(self, client):
        resp = client.get("/api/stock/zz/AAPL/chart/30")
        assert resp.status_code == 404

    def test_empty_fetch_returns_404(self, client, monkeypatch):
        import core.stockdata as sd
        monkeypatch.setattr(sd, '_fetch_yf_history', lambda *a, **k: ([], None))
        resp = client.get("/api/stock/us/AAPL/chart/30")
        assert resp.status_code == 404

    def test_valid_returns_url(self, client, tmp_path, monkeypatch):
        static_dir = tmp_path / 'static'
        static_dir.mkdir()
        monkeypatch.setattr('core.config.CHART_DIR', str(static_dir))

        import core.stockdata as sd
        _DAY_MS = 86_400_000
        bars = [[i * _DAY_MS, float(i), float(i)+1, float(i)-0.5, float(i)+0.5]
                for i in range(1, 31)]
        monkeypatch.setattr(sd, '_fetch_yf_history', lambda *a, **k: (bars, None))

        resp = client.get("/api/stock/us/AAPL/chart/30")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['url'].startswith('/charts/')   # served off the data dir, not /static
        assert body['symbol'] == 'AAPL'
        assert body['period'] == '30'


# ── Instrument detail / performance ─────────────────────────────────────────
class TestInstrumentRoutes:
    def test_detail_unknown_market_404(self, client):
        assert client.get("/api/instrument/zz/foo").status_code == 404

    def test_performance_has_provider_no_data(self, client, monkeypatch):
        # HK now has YFinanceProvider; mock an empty fetch so no data is cached.
        # available=True (provider exists) but price/perf are all None (empty series).
        import core.stockdata as sd
        monkeypatch.setattr(sd, '_fetch_yf_history', lambda *a, **k: ([], None))
        resp = client.get("/api/instrument/hk/00700.HK/performance")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["available"] is True   # provider registered
        assert body["price"] is None
        assert all(v is None for v in body["performance"].values())

    def test_detail_returns_canonical_symbol(self, client):
        body = client.get("/api/instrument/hk/700.hk").get_json()
        assert body["symbol"] == "00700.HK"
        assert body["in_portfolio"] is False

    def test_hk_chart_provider_defaults_to_aastocks(self, client):
        # Unset → bundle reports the AA Stocks chart provider for HK.
        body = client.get("/api/instrument/hk/700.hk").get_json()
        assert body["provider"] == "aastocks"
        assert app.get_hk_chart_provider() == "aastocks"

    def test_hk_chart_provider_toggle_to_yfinance(self, client, setval):
        setval("hk_chart_provider", "yfinance")
        assert app.get_hk_chart_provider() == "yfinance"
        body = client.get("/api/instrument/hk/700.hk").get_json()
        assert body["provider"] == "yfinance"
        # Non-HK markets are unaffected by the HK setting.
        assert client.get("/api/instrument/crypto/btc").get_json()["provider"] == "coingecko"

    def test_hk_chart_provider_invalid_falls_back(self, client, setval):
        setval("hk_chart_provider", "bogus")
        assert app.get_hk_chart_provider() == "aastocks"
        assert client.get("/api/instrument/hk/700.hk").get_json()["provider"] == "aastocks"


# ── Static-data corrupt-download abort (mocked network) ─────────────────────
class TestStaticDataAbort:
    def test_coingecko_short_download_aborts_and_keeps_table(self, client, monkeypatch):
        # Seed an existing catalog that must survive a corrupt download.
        c = app.get_db_connection()
        try:
            cur = c.cursor()
            cur.executemany(
                "INSERT INTO coingecko_coins (coin_id, symbol, name, market_cap_rank) VALUES (?,?,?,?)",
                [("bitcoin", "btc", "Bitcoin", 1), ("ethereum", "eth", "Ethereum", 2)],
            )
            c.commit()
        finally:
            c.close()

        # Any network call returns a too-short payload (< STATIC_DATA_MIN_ROWS).
        monkeypatch.setattr(
            app.requests, "get",
            lambda *a, **k: FakeResponse(json_data=[{"id": "x", "symbol": "x", "name": "X"}]),
        )

        resp = client.post("/api/admin/static-data/coingecko/download")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is False
        assert body["aborted"] is True

        # Existing table left untouched.
        count = client.get("/api/admin/db/table/coingecko_coins").get_json()["total"]
        assert count == 2


# ── Prod health probe (dev → prod over the LAN) ─────────────────────────────
class TestProdHealthProbe:
    def test_health_includes_web_started_at(self, client):
        body = client.get("/api/health").get_json()
        assert isinstance(body["web"]["started_at"], int)
        # Worker section now carries started_at too (None on a fresh DB).
        assert "started_at" in body["worker"]

    def test_health_includes_db_meta(self, client):
        from core.db import DB_SCHEMA_VERSION
        body = client.get("/api/health").get_json()
        assert "db" in body
        assert body["db"]["version"] == str(DB_SCHEMA_VERSION)
        assert body["db"]["description"] is not None

    def test_unconfigured_when_no_setting_or_config_file(self, client, monkeypatch, tmp_path):
        # No prod_base_url setting + a non-existent config.sh => no target known.
        monkeypatch.setattr(app, "DEPLOY_CONFIG_PATH", str(tmp_path / "nope.sh"))
        body = client.get("/api/prod/health").get_json()
        assert body == {"configured": False}

    def test_reachable_passes_through_prod_health(self, client, setval, monkeypatch):
        setval("prod_base_url", "http://prod.test:8000")
        prod_payload = {"env": "prod", "web": {"version": "9.9"}, "worker": {"ready": True}}
        monkeypatch.setattr(app.requests, "get", lambda *a, **k: FakeResponse(json_data=prod_payload))
        body = client.get("/api/prod/health").get_json()
        assert body["configured"] is True
        assert body["reachable"] is True
        assert body["base_url"] == "http://prod.test:8000"
        assert body["health"]["web"]["version"] == "9.9"

    def test_unreachable_returns_error(self, client, setval, monkeypatch):
        setval("prod_base_url", "http://prod.test:8000")
        def boom(*a, **k):
            raise Exception("Connection refused")
        monkeypatch.setattr(app.requests, "get", boom)
        body = client.get("/api/prod/health").get_json()
        assert body["configured"] is True
        assert body["reachable"] is False
        assert "Connection refused" in body["error"]

    def test_setting_overrides_config_and_strips_trailing_slash(self, client, setval, monkeypatch):
        setval("prod_base_url", "http://override.test:9000/")
        monkeypatch.setattr(app.requests, "get", lambda *a, **k: FakeResponse(json_data={"web": {"version": "1.0"}}))
        body = client.get("/api/prod/health").get_json()
        assert body["base_url"] == "http://override.test:9000"
