"""Static configuration: constants and domain exceptions shared across the app.

Pure data only — no DB, no network, no Flask. The web layer re-exports these so
existing `app.<NAME>` references keep working.
"""
import os

# Repo root (this file is core/config.py → two dirs up). All code-relative paths are
# resolved from here, never from the current working directory.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── Runtime data root ──────────────────────────────────────────────────────────────
# A SINGLE configurable directory that holds everything the app *generates* at runtime
# — the SQLite DB, the chart render-cache, backtest snapshots, and logs — kept cleanly
# separate from the (committed) code tree. Override to any absolute path (an external
# disk, a shared volume) via BETELGEUSE_DATA_DIR; defaults to <repo>/data. Resolving it
# from THIS FILE (not the CWD) is what lets the web app and worker launch from anywhere
# without silently opening an empty portfolio.db. Reference these as module attributes
# (config.DB_PATH, config.CHART_DIR, …) so tests can monkeypatch them to a tmp dir.
DATA_DIR = os.environ.get('BETELGEUSE_DATA_DIR') or os.path.join(_REPO_ROOT, 'data')

# The SQLite database file (+ its -wal/-shm sidecars). Pre-migration and pulled-from-prod
# backups go in DATA_DIR/backup/ (see core.migrate._backup_dir + deploy/update-from-prod.sh).
DB_PATH = os.path.join(DATA_DIR, 'portfolio.db')

# Directory the chart PNG/GIF render-cache is written to and served from (via the Flask
# /charts/<filename> route). A pure regenerable cache — distinct from STATIC_DIR below.
CHART_DIR = os.path.join(DATA_DIR, 'charts')

# Directory the Training-page backtest chart snapshots are written under.
BACKTEST_DIR = os.path.join(DATA_DIR, 'backtest')

# Repo's committed static assets (symbol-field.js / instrument-detail.js) — Flask's
# `static_folder`. These are CODE, not runtime data, so they stay in the code tree.
STATIC_DIR = os.path.join(_REPO_ROOT, 'static')

# Directory application log files (rotating) are written to. Overridable via the
# BETELGEUSE_LOG_DIR env var (handy for tests/ops); otherwise lives under the data
# root (data/logs). core.logging_setup writes <process>.app.log here.
LOG_DIR = os.environ.get('BETELGEUSE_LOG_DIR') or os.path.join(DATA_DIR, 'logs')

# Market names reference
MARKETS = {
    'hk': {'name': 'Hong Kong'},
    'jp': {'name': 'Japan'},
    'us': {'name': 'United States'},
    'crypto': {'name': 'Cryptocurrency'}
}

# Predefined group options for each market
GROUP_OPTIONS = {
    'hk': ['Default', 'TechnicalPattern', 'Value', 'Growth', 'Dividend', 'Momentum', 'Governance', 'ESG', 'Sector'],
    'jp': ['Default', 'TechnicalPattern', 'Value', 'Growth', 'Dividend', 'Momentum', 'Governance', 'ESG', 'Sector'],
    'us': ['Default', 'TechnicalPattern', 'Value', 'Growth', 'Dividend', 'Momentum', 'Governance', 'ESG', 'Sector'],
    'crypto': ['Default', 'TechnicalPattern', 'Value', 'Growth', 'Momentum', 'Governance', 'Commodity', 'DeFi']
}

# Fixed pattern options for the backtesting / training page
BACKTEST_PATTERNS = ['Breakthrough', 'Triangle']

# Well-known CoinGecko coin IDs — used as fallback when user hasn't set one explicitly
COMMON_CRYPTO_IDS = {
    'BTC': 'bitcoin', 'ETH': 'ethereum', 'SOL': 'solana', 'BNB': 'binancecoin',
    'XRP': 'ripple', 'ADA': 'cardano', 'DOGE': 'dogecoin', 'AVAX': 'avalanche-2',
    'DOT': 'polkadot', 'MATIC': 'matic-network', 'LINK': 'chainlink', 'UNI': 'uniswap',
    'LTC': 'litecoin', 'ATOM': 'cosmos', 'USDT': 'tether', 'USDC': 'usd-coin',
    'BCH': 'bitcoin-cash', 'TON': 'the-open-network', 'SHIB': 'shiba-inu',
    'TRX': 'tron', 'NEAR': 'near', 'APT': 'aptos', 'ARB': 'arbitrum',
    'OP': 'optimism', 'SUI': 'sui',
}

# Crypto chart periods: label → CoinGecko days param
CRYPTO_PERIODS = {'7': '7', '30': '30', '90': '90', '365': '365'}

# Stock chart periods: label → lookback days (used by YFinanceProvider + chart route)
STOCK_PERIODS = {'30': 30, '90': 90, '180': 180, '365': 365}

# Static data sources
HKEX_SECURITIES_URL = 'https://www.hkex.com.hk/chi/services/trading/securities/securitieslists/ListOfSecurities_c.xlsx'

# US tickers — Nasdaq Trader Symbol Directory (covers all US exchange-listed boards).
# nasdaqlisted = Nasdaq; otherlisted = NYSE / NYSE American / Arca / Cboe / IEX.
# Served over FTP (the HTTPS mirror returns a "Page Not Available" stub).
NASDAQ_LISTED_URL = 'ftp://ftp.nasdaqtrader.com/SymbolDirectory/nasdaqlisted.txt'
US_OTHER_LISTED_URL = 'ftp://ftp.nasdaqtrader.com/SymbolDirectory/otherlisted.txt'
# otherlisted.txt 'Exchange' code → compact board token used as the symbol extension.
US_EXCHANGE_BOARDS = {'A': 'AMEX', 'N': 'NYSE', 'P': 'ARCA', 'Z': 'BATS', 'V': 'IEX'}
# Board tokens that may appear as a `.BOARD` extension on a US symbol (for stripping).
US_BOARD_TOKENS = {'NASDAQ', 'NYSE', 'AMEX', 'ARCA', 'BATS', 'IEX', 'OTHER'}

# A static-data download with fewer than this many parsed rows is treated as
# corrupt/incomplete and is aborted WITHOUT replacing the existing table.
STATIC_DATA_MIN_ROWS = 100


class StaticDataDownloadError(Exception):
    """Raised when a static-data download looks corrupt (too few rows) so the
    existing table must be kept. Carries the (bad) parsed row count."""
    def __init__(self, message, row_count):
        super().__init__(message)
        self.row_count = row_count


class CoinGeckoMappingError(Exception):
    """Raised when a CoinGecko symbol⇄id mapping cannot be resolved (strict mode)."""
    pass


__all__ = [
    'DATA_DIR', 'DB_PATH', 'CHART_DIR', 'BACKTEST_DIR', 'STATIC_DIR', 'LOG_DIR',
    'MARKETS', 'GROUP_OPTIONS', 'BACKTEST_PATTERNS', 'COMMON_CRYPTO_IDS',
    'CRYPTO_PERIODS', 'STOCK_PERIODS',
    'HKEX_SECURITIES_URL', 'NASDAQ_LISTED_URL', 'US_OTHER_LISTED_URL',
    'US_EXCHANGE_BOARDS', 'US_BOARD_TOKENS', 'STATIC_DATA_MIN_ROWS',
    'StaticDataDownloadError', 'CoinGeckoMappingError',
]
