"""DB-backed logic tests — run against the temp DB fixture, no network."""
from datetime import datetime, timedelta

import pytest

import app


def _insert_ohlcv(coin_id, period, ts, close, fetched_at, source="coingecko"):
    c = app.get_db_connection()
    try:
        cur = c.cursor()
        cur.execute(
            """INSERT OR REPLACE INTO crypto_ohlcv
               (source, coin_id, period, timestamp, open, high, low, close, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (source, coin_id, str(period), ts, close, close, close, close, fetched_at),
        )
        c.commit()
    finally:
        c.close()


def _insert_coin(coin_id, symbol, name, rank=None):
    c = app.get_db_connection()
    try:
        cur = c.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO coingecko_coins (coin_id, symbol, name, market_cap_rank) VALUES (?,?,?,?)",
            (coin_id, symbol, name, rank),
        )
        c.commit()
    finally:
        c.close()


# ── coingecko_symbol_to_id / id_to_symbol ───────────────────────────────────
class TestCoingeckoMapping:
    def test_user_override_wins(self, db, setval):
        _insert_coin("catalogcoin", "foo", "Catalog Foo", 5)
        setval("coingecko_id_FOO", "user-foo")
        assert app.coingecko_symbol_to_id("foo") == "user-foo"

    def test_common_majors(self, db):
        assert app.coingecko_symbol_to_id("BTC") == "bitcoin"
        assert app.coingecko_symbol_to_id("eth") == "ethereum"

    def test_catalog_collision_lowest_rank_wins(self, db):
        _insert_coin("dup-high", "dup", "High Rank", 900)
        _insert_coin("dup-low", "dup", "Low Rank", 3)
        assert app.coingecko_symbol_to_id("DUP") == "dup-low"

    def test_strict_raises_on_miss(self, db):
        with pytest.raises(app.CoinGeckoMappingError):
            app.coingecko_symbol_to_id("nothinghere")

    def test_non_strict_returns_none(self, db):
        assert app.coingecko_symbol_to_id("nothinghere", strict=False) is None

    def test_id_to_symbol_from_catalog(self, db):
        _insert_coin("solana", "sol", "Solana", 5)
        assert app.coingecko_id_to_symbol("solana") == "SOL"

    def test_id_to_symbol_reverse_common(self, db):
        assert app.coingecko_id_to_symbol("bitcoin") == "BTC"


# ── _load_crypto_ohlcv_series ───────────────────────────────────────────────
class TestLoadSeries:
    def test_merges_and_dedupes_across_periods(self, db):
        now = datetime.now().isoformat()
        _insert_ohlcv("bitcoin", "30", 1000, 10.0, now)
        _insert_ohlcv("bitcoin", "30", 2000, 20.0, now)
        _insert_ohlcv("bitcoin", "365", 2000, 99.0, now)  # same ts, different period
        _insert_ohlcv("bitcoin", "365", 3000, 30.0, now)
        series = app._load_crypto_ohlcv_series("bitcoin")
        timestamps = [ts for ts, _ in series]
        assert timestamps == [1000, 2000, 3000]           # ascending, de-duped
        assert len(series) == 3


# ── _crypto_ohlcv_is_fresh ──────────────────────────────────────────────────
class TestFreshness:
    def test_recent_is_fresh(self, db):
        _insert_ohlcv("bitcoin", "30", 1000, 10.0, datetime.now().isoformat())
        assert app._crypto_ohlcv_is_fresh("bitcoin", "30") is True

    def test_two_hours_old_is_stale(self, db):
        old = (datetime.now() - timedelta(hours=2)).isoformat()
        _insert_ohlcv("bitcoin", "30", 1000, 10.0, old)
        assert app._crypto_ohlcv_is_fresh("bitcoin", "30", max_age_min=60) is False

    def test_over_24h_regression_not_fresh(self, db):
        # Guards the .total_seconds() bug where >24h-old was wrongly seen as fresh.
        old = (datetime.now() - timedelta(hours=25)).isoformat()
        _insert_ohlcv("bitcoin", "30", 1000, 10.0, old)
        assert app._crypto_ohlcv_is_fresh("bitcoin", "30", max_age_min=60) is False

    def test_missing_is_not_fresh(self, db):
        assert app._crypto_ohlcv_is_fresh("ghostcoin", "30") is False


# ── resolve_static_url ──────────────────────────────────────────────────────
class TestResolveStaticUrl:
    DEFAULT = "https://default.example/data"

    def test_default_when_unset(self, db):
        url, info = app.resolve_static_url("k_url", "k_url_enabled", self.DEFAULT)
        assert url == self.DEFAULT
        assert info["url_default"] == self.DEFAULT
        assert info["url_enabled"] is False

    def test_override_used_when_enabled_and_nonempty(self, db, setval):
        setval("k_url", "https://override.example/x")
        setval("k_url_enabled", "true")
        url, _ = app.resolve_static_url("k_url", "k_url_enabled", self.DEFAULT)
        assert url == "https://override.example/x"

    def test_disabled_override_falls_back(self, db, setval):
        setval("k_url", "https://override.example/x")
        setval("k_url_enabled", "false")
        url, _ = app.resolve_static_url("k_url", "k_url_enabled", self.DEFAULT)
        assert url == self.DEFAULT

    def test_enabled_but_empty_falls_back(self, db, setval):
        setval("k_url", "")
        setval("k_url_enabled", "true")
        url, _ = app.resolve_static_url("k_url", "k_url_enabled", self.DEFAULT)
        assert url == self.DEFAULT


# ── get_market_groups ───────────────────────────────────────────────────────
class TestGetMarketGroups:
    def test_defaults_when_unset(self, db):
        c = app.get_db_connection()
        try:
            groups = app.get_market_groups("hk", c.cursor())
        finally:
            c.close()
        assert groups == app.GROUP_OPTIONS["hk"]

    def test_stored_overrides_defaults(self, db, setval):
        setval("groups_hk", "Alpha,Beta")
        c = app.get_db_connection()
        try:
            groups = app.get_market_groups("hk", c.cursor())
        finally:
            c.close()
        assert groups == ["Alpha", "Beta"]


# ── _build_portfolio_message ────────────────────────────────────────────────
def _add_item(symbol, market, name, group="Default"):
    c = app.get_db_connection()
    try:
        cur = c.cursor()
        cur.execute(
            'INSERT INTO portfolio (symbol, market, name, "group", added_date, comment) VALUES (?,?,?,?,?,?)',
            (symbol, market, name, group, "2026-01-01", ""),
        )
        c.commit()
    finally:
        c.close()


class TestBuildPortfolioMessage:
    def test_empty_returns_none(self, db):
        msg, total = app._build_portfolio_message()
        assert msg is None
        assert total == 0

    def test_singular_and_content(self, db):
        _add_item("00700.HK", "hk", "Tencent")
        msg, total = app._build_portfolio_message()
        assert total == 1
        assert "🇭🇰 HK: 00700.HK (Default)" in msg
        assert "Total: 1 position</i>" in msg          # singular

    def test_market_ordering_and_filter(self, db):
        _add_item("BTC", "crypto", "Bitcoin")
        _add_item("00700.HK", "hk", "Tencent")
        msg, total = app._build_portfolio_message()
        assert total == 2
        assert msg.index("🇭🇰 HK") < msg.index("🟠 Crypto")  # hk before crypto
        # filtering to crypto only
        msg2, total2 = app._build_portfolio_message(["crypto"])
        assert total2 == 1
        assert "🇭🇰 HK" not in msg2


# ── db_meta table ─────────────────────────────────────────────────────────────
class TestDbMeta:
    def test_version_seeded_on_fresh_db(self, db):
        from core.db import get_db_meta, DB_SCHEMA_VERSION
        assert get_db_meta('version') == str(DB_SCHEMA_VERSION)

    def test_description_seeded_on_fresh_db(self, db):
        from core.db import get_db_meta
        desc = get_db_meta('description')
        assert desc is not None and len(desc) > 0

    def test_get_missing_key_returns_default(self, db):
        from core.db import get_db_meta
        assert get_db_meta('no_such_key') is None
        assert get_db_meta('no_such_key', default='fallback') == 'fallback'

    def test_set_and_get_roundtrip(self, db):
        from core.db import get_db_meta, set_db_meta
        set_db_meta('description', 'my custom note')
        assert get_db_meta('description') == 'my custom note'

    def test_set_updates_existing_key(self, db):
        from core.db import get_db_meta, set_db_meta
        set_db_meta('version', '99')
        assert get_db_meta('version') == '99'

    def test_insert_or_ignore_preserves_existing_on_reinit(self, db):
        # If init_db() is called again on an existing DB (e.g. worker restart),
        # INSERT OR IGNORE must NOT overwrite a user-edited description.
        from core.db import get_db_meta, set_db_meta
        import app
        set_db_meta('description', 'custom description')
        app.init_db()   # second call — simulates a restart
        assert get_db_meta('description') == 'custom description'

    def test_version_resynced_from_ledger_on_reinit(self, db):
        # db_meta.version is a derived mirror of the schema_migrations ledger (MAX version).
        # If something scribbles a stale value, init_db() re-syncs it from the ledger on the
        # next startup so /api/health never reports a wrong version.
        from core.db import get_db_meta, set_db_meta, DB_SCHEMA_VERSION
        import app
        set_db_meta('version', '1')          # stale/scribbled value
        app.init_db()                        # restart re-syncs from the ledger
        assert get_db_meta('version') == str(DB_SCHEMA_VERSION)


# ── Schema shape: portfolio watch columns + transactions ledger ──────────────
class TestSchemaShape:
    def test_portfolio_has_watch_columns(self, conn):
        cols = [r[1] for r in conn.execute("PRAGMA table_info(portfolio)").fetchall()]
        for col in ("monitor_price", "trigger_price", "bought"):
            assert col in cols

    def test_transactions_table_exists_and_queryable(self, conn):
        # 'transactions' (plural) dodges the SQL reserved word 'transaction'; it must be
        # queryable unquoted and carry the expected columns.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()]
        assert cols == ["id", "market", "symbol", "txn_type", "price",
                        "quantity", "txn_date", "created_at"]
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0

    def test_schema_version_is_current(self, db):
        from core.db import get_db_meta, DB_SCHEMA_VERSION
        from core import migrate
        assert get_db_meta("version") == str(DB_SCHEMA_VERSION)
        assert DB_SCHEMA_VERSION == migrate.head_version()   # constant tracks the migration head

    def test_fresh_init_stamps_migration_ledger(self, conn):
        # A from-scratch init_db() builds the schema by running every migration, so the
        # ledger must record them all (not just the db_meta version stamp).
        from core.db import DB_SCHEMA_VERSION
        from core import migrate
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        versions = sorted(r[0] for r in rows)
        assert versions == sorted(m.version for m in migrate.discover())   # every migration stamped
        assert max(versions) == DB_SCHEMA_VERSION                          # head == code constant


# ── worker started_at (heartbeat companion: "when did it last restart?") ─────
class TestWorkerStartedAt:
    def test_record_and_report(self, db):
        from core import health
        health.record_worker_start(now=1_700_000_000)
        st = health.worker_status(now=1_700_000_050)
        assert st["started_at"] == 1_700_000_000

    def test_started_at_none_when_unset(self, db):
        from core import health
        st = health.worker_status(now=1_700_000_000)
        assert st["started_at"] is None
