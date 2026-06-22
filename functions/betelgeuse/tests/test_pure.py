"""Pure-logic tests — no DB, no network. Highest-ROI coverage."""
import pytest

import app
from core import runtime, version


# ── runtime mode (mandatory --env) ──────────────────────────────────────────
class TestParseEnvArg:
    def test_returns_dev_and_prod(self):
        assert runtime.parse_env_arg(["--env", "dev"]) == "dev"
        assert runtime.parse_env_arg(["--env", "prod"]) == "prod"

    def test_tolerates_extra_args(self):
        # parse_known_args so serve.py's other config isn't disturbed.
        assert runtime.parse_env_arg(["--env", "dev", "--something", "else"]) == "dev"

    def test_missing_env_exits(self):
        with pytest.raises(SystemExit):
            runtime.parse_env_arg([])

    def test_invalid_choice_exits(self):
        with pytest.raises(SystemExit):
            runtime.parse_env_arg(["--env", "staging"])


# ── versions ────────────────────────────────────────────────────────────────
class TestVersions:
    def test_versions_are_nonempty_strings(self):
        assert isinstance(version.WEB_VERSION, str) and version.WEB_VERSION
        assert isinstance(version.WORKER_VERSION, str) and version.WORKER_VERSION

    def test_display_labels(self):
        # magi surfaces these as betelgeuse-app-<x> / betelgeuse-server-<x>.
        assert version.app_version_string() == f"betelgeuse-app-{version.WEB_VERSION}"
        assert version.server_version_string() == f"betelgeuse-server-{version.WORKER_VERSION}"


# ── normalize_symbol ────────────────────────────────────────────────────────
class TestNormalizeSymbol:
    def test_hk_zero_pads_and_suffixes(self):
        assert app.normalize_symbol("700.hk", "hk") == "00700.HK"
        assert app.normalize_symbol("0700.HK", "hk") == "00700.HK"
        assert app.normalize_symbol("700", "hk") == "00700.HK"

    def test_hk_already_canonical_is_stable(self):
        once = app.normalize_symbol("700.hk", "hk")
        assert app.normalize_symbol(once, "hk") == once  # idempotent

    def test_hk_non_numeric_code_not_padded(self):
        # Non-digit HK codes keep their text, still uppercased + suffixed.
        assert app.normalize_symbol("hsi", "hk") == "HSI.HK"

    def test_jp_suffix_no_zero_pad(self):
        assert app.normalize_symbol("7203", "jp") == "7203.T"
        assert app.normalize_symbol("7203.t", "jp") == "7203.T"
        assert app.normalize_symbol("7203.T", "jp") == "7203.T"

    def test_us_and_crypto_uppercase_only(self):
        assert app.normalize_symbol("aapl", "us") == "AAPL"
        assert app.normalize_symbol("btc", "crypto") == "BTC"
        assert app.normalize_symbol("BRK.A", "us") == "BRK.A"

    def test_empty_and_whitespace(self):
        assert app.normalize_symbol("", "hk") == ""
        assert app.normalize_symbol("   ", "hk") == ""
        assert app.normalize_symbol(None, "us") == ""


# ── _pct_change ─────────────────────────────────────────────────────────────
DAY = 86400000


class TestPctChange:
    def test_empty_or_single_point_is_none(self):
        assert app._pct_change([], 1) is None
        assert app._pct_change([(0, 100.0)], 1) is None

    def test_basic_change(self):
        series = [(0, 100.0), (DAY, 110.0)]
        assert app._pct_change(series, 1) == 100.0 * (110 - 100) / 100  # +10%

    def test_picks_candle_at_or_before_target(self):
        series = [(0, 100.0), (DAY, 105.0), (2 * DAY, 120.0)]
        # latest ts = 2*DAY, lookback 1 day -> target = DAY -> past_close = 105
        assert app._pct_change(series, 1) == pytest.approx(100.0 * (120 - 105) / 105)

    def test_insufficient_history_returns_none(self):
        # latest at 2*DAY, lookback 10d => target far before earliest, beyond slop
        series = [(DAY, 110.0), (2 * DAY, 120.0)]
        assert app._pct_change(series, 10) is None

    def test_far_edge_slop_resolves_to_earliest(self):
        # lookback equals the span; earliest candle is within the slop window.
        series = [(0, 100.0), (365 * DAY, 200.0)]
        assert app._pct_change(series, 365) == 100.0  # (200-100)/100

    def test_zero_past_close_is_none(self):
        series = [(0, 0.0), (DAY, 110.0)]
        assert app._pct_change(series, 1) is None


# ── _us_base_symbol ─────────────────────────────────────────────────────────
class TestUsBaseSymbol:
    def test_strips_board_extension(self):
        assert app._us_base_symbol("AAPL.NASDAQ") == "AAPL"
        assert app._us_base_symbol("aapl.nyse") == "AAPL"
        assert app._us_base_symbol("F.ARCA") == "F"

    def test_preserves_class_share_dot(self):
        assert app._us_base_symbol("BRK.A") == "BRK.A"
        assert app._us_base_symbol("BRK.B") == "BRK.B"

    def test_plain_symbol_unchanged(self):
        assert app._us_base_symbol("MSFT") == "MSFT"
        assert app._us_base_symbol("  msft  ") == "MSFT"

    def test_every_known_board_token_stripped(self):
        for token in app.US_BOARD_TOKENS:
            assert app._us_base_symbol(f"XYZ.{token}") == "XYZ"


# ── _parse_nasdaq_symbol_file ───────────────────────────────────────────────
NASDAQ_SAMPLE = (
    "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
    "AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N\n"
    "ZTEST|NASDAQ TEST STOCK|G|Y|N|100|N|N\n"   # test issue -> dropped
    "SPY|SPDR S&P 500 ETF|Q|N|N|100|Y|N\n"      # ETF flag set
    "BADLINE\n"                                  # too few fields -> skipped
    "File Creation Time: 0601202618:00|||||||\n"  # trailer -> skipped
)

OTHER_SAMPLE = (
    "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
    "F|Ford Motor Company|N|F|N|100|N|F\n"          # N -> NYSE
    "IEMG|iShares Core EM|P|IEMG|Y|100|N|IEMG\n"    # P -> ARCA, ETF
    "ZZZ|Some Test|N|ZZZ|N|100|Y|ZZZ\n"            # test issue -> dropped
    "File Creation Time: ...\n"
)


class TestParseNasdaqSymbolFile:
    def test_nasdaqlisted_parse(self):
        rows = app._parse_nasdaq_symbol_file(
            NASDAQ_SAMPLE, symbol_col=0, name_col=1, test_col=3, etf_col=6, board_from="NASDAQ"
        )
        by_sym = {r[0]: r for r in rows}
        assert set(by_sym) == {"AAPL", "SPY"}            # test issue + junk dropped
        assert by_sym["AAPL"] == ("AAPL", "Apple Inc. - Common Stock", "NASDAQ", 0)
        assert by_sym["SPY"][3] == 1                      # ETF flag

    def test_otherlisted_board_from_callable(self):
        rows = app._parse_nasdaq_symbol_file(
            OTHER_SAMPLE, symbol_col=0, name_col=1, test_col=6, etf_col=4,
            board_from=lambda f: app.US_EXCHANGE_BOARDS.get((f[2] or "").strip().upper(), "OTHER"),
        )
        by_sym = {r[0]: r for r in rows}
        assert set(by_sym) == {"F", "IEMG"}
        assert by_sym["F"][2] == "NYSE"
        assert by_sym["IEMG"][2] == "ARCA"
        assert by_sym["IEMG"][3] == 1


# ── deploy/config.sh parsing (dev → prod URL resolution) ─────────────────────
class TestParseDeployConfig:
    def test_extracts_host_and_port(self):
        text = (
            '# Mac mini deployment target.\n'
            'MINI_USER="kai"\n'
            'MINI_HOST="mac-mini.local"   # bonjour name\n'
            'PORT=8000\n'
        )
        cfg = app._parse_deploy_config(text)
        assert cfg["MINI_HOST"] == "mac-mini.local"
        assert cfg["PORT"] == "8000"
        assert cfg["MINI_USER"] == "kai"

    def test_ignores_comments_and_blank_lines(self):
        assert app._parse_deploy_config("# just a comment\n\n   \n") == {}

    def test_handles_single_quotes_and_bare_values(self):
        cfg = app._parse_deploy_config("A='x'\nB=bare\n")
        assert cfg["A"] == "x"
        assert cfg["B"] == "bare"

    def test_strips_inline_comment_off_bare_value(self):
        cfg = app._parse_deploy_config("PORT=8000  # the port\n")
        assert cfg["PORT"] == "8000"
