"""CoinGecko config + symbol/id translation + crypto OHLCV cache helpers.

Flask-free domain logic. The DB (crypto_ohlcv table) is the source of truth; the
PNG render cache lives in core.marketdata.
"""
import requests
from datetime import datetime

from core.config import COMMON_CRYPTO_IDS, CoinGeckoMappingError
from core.db import get_db_connection
from core.logging_setup import get_logger

logger = get_logger('crypto')


def get_coingecko_config():
    """Return (base_url, api_key) from settings."""
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT key, value FROM settings WHERE key IN ('coingecko_api_url','coingecko_api_key')")
        cfg = {r['key']: r['value'] for r in c.fetchall()}
    finally:
        conn.close()
    base = (cfg.get('coingecko_api_url') or '').strip() or 'https://api.coingecko.com/api/v3'
    key  = (cfg.get('coingecko_api_key') or '').strip()
    return base, key


def coingecko_symbol_to_id(symbol, strict=True):
    """Translate a ticker symbol → CoinGecko coin ID.

    Resolution order (most authoritative first):
      1. user-defined override in settings (`coingecko_id_<SYM>`)
      2. hardcoded COMMON_CRYPTO_IDS map (deterministic for the majors)
      3. the cached coingecko_coins catalog — on symbol collisions the entry
         with the best (lowest) market_cap_rank wins.

    Raises CoinGeckoMappingError if nothing matches and strict=True;
    otherwise returns None.
    """
    if not symbol or not symbol.strip():
        if strict:
            raise CoinGeckoMappingError('empty symbol')
        return None
    sym = symbol.strip().upper()

    conn = get_db_connection()
    try:
        c = conn.cursor()
        # 1. user override
        c.execute("SELECT value FROM settings WHERE key=?", (f'coingecko_id_{sym}',))
        row = c.fetchone()
        if row and row['value']:
            return row['value'].strip()
        # 2. common majors
        if sym in COMMON_CRYPTO_IDS:
            return COMMON_CRYPTO_IDS[sym]
        # 3. cached catalog — highest market cap wins on collisions
        c.execute(
            'SELECT coin_id FROM coingecko_coins WHERE symbol=? '
            'ORDER BY market_cap_rank IS NULL, market_cap_rank LIMIT 1',
            (sym.lower(),)
        )
        row = c.fetchone()
    finally:
        conn.close()
    if row and row['coin_id']:
        return row['coin_id']
    if strict:
        raise CoinGeckoMappingError(f'no CoinGecko id for symbol {sym!r}')
    return None


def coingecko_id_to_symbol(coin_id, strict=True):
    """Translate a CoinGecko coin ID → ticker symbol (uppercase).

    Resolution order:
      1. the cached coingecko_coins catalog
      2. reverse of the hardcoded COMMON_CRYPTO_IDS map

    Raises CoinGeckoMappingError if nothing matches and strict=True;
    otherwise returns None.
    """
    if not coin_id or not coin_id.strip():
        if strict:
            raise CoinGeckoMappingError('empty coin_id')
        return None
    cid = coin_id.strip().lower()

    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('SELECT symbol FROM coingecko_coins WHERE coin_id=?', (cid,))
        row = c.fetchone()
    finally:
        conn.close()
    if row and row['symbol']:
        return row['symbol'].upper()
    # reverse the common map as a fallback
    for sym, mapped_id in COMMON_CRYPTO_IDS.items():
        if mapped_id == cid:
            return sym
    if strict:
        raise CoinGeckoMappingError(f'no symbol for CoinGecko id {cid!r}')
    return None


def get_coingecko_id(symbol):
    """Backward-compatible non-strict symbol → coin ID lookup (None on miss)."""
    return coingecko_symbol_to_id(symbol, strict=False)

def fetch_and_store_crypto_ohlcv(coin_id, period='30'):
    """Fetch OHLC data from CoinGecko, persist to crypto_ohlcv, return raw list.

    ``period`` is the CoinGecko ``days`` param and may be a number ('7'/'30'/'90'/'365')
    or the literal ``'max'`` used by the best-effort crypto backload (CoinGecko's OHLC
    endpoint can't take an arbitrary start date, so 'max' fetches its deepest history).
    The stored ``period`` column is TEXT, so 'max' lives in its own bucket and merges
    cleanly with the numbered periods in _load_crypto_ohlcv_series (dedup by timestamp).
    """
    base_url, api_key = get_coingecko_config()
    headers = {'x-cg-demo-api-key': api_key} if api_key else {}
    try:
        resp = requests.get(
            f'{base_url}/coins/{coin_id}/ohlc',
            params={'vs_currency': 'usd', 'days': period},
            headers=headers,
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error('CoinGecko fetch error (%s %sd): %s', coin_id, period, e)
        return None

    fetched_at = datetime.now().isoformat()
    conn = get_db_connection()
    try:
        c = conn.cursor()
        for row in data:
            ts, o, h, l, cl = row[0], row[1], row[2], row[3], row[4]
            c.execute(
                '''INSERT OR REPLACE INTO crypto_ohlcv
                   (source, coin_id, period, timestamp, open, high, low, close, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?)''',
                ('coingecko', coin_id, str(period), ts, o, h, l, cl, fetched_at)
            )
        conn.commit()
    finally:
        conn.close()
    return data


def _crypto_ohlcv_is_fresh(coin_id, period, source='coingecko', max_age_min=60):
    """True if stored OHLCV for this coin/period/source was fetched recently."""
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute(
            'SELECT MAX(fetched_at) AS last FROM crypto_ohlcv '
            'WHERE coin_id=? AND period=? AND source=?',
            (coin_id, str(period), source)
        )
        row = c.fetchone()
    finally:
        conn.close()
    if not row or not row['last']:
        return False
    try:
        last = datetime.fromisoformat(row['last'])
    except ValueError:
        return False
    return (datetime.now() - last).total_seconds() < max_age_min * 60


def _load_crypto_ohlcv_rows(coin_id, period, source='coingecko'):
    """Return stored OHLC as [[timestamp, open, high, low, close], ...] ordered by time."""
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute(
            'SELECT timestamp, open, high, low, close FROM crypto_ohlcv '
            'WHERE coin_id=? AND period=? AND source=? ORDER BY timestamp',
            (coin_id, str(period), source)
        )
        return [[r['timestamp'], r['open'], r['high'], r['low'], r['close']] for r in c.fetchall()]
    finally:
        conn.close()


def _load_crypto_ohlcv_series(coin_id, source='coingecko'):
    """Return a merged (timestamp, close) series across ALL cached periods for a coin.

    Different periods are cached at different granularities (7/30d → 4-hourly,
    90/365d → 4-daily); merging + de-duplicating by timestamp gives one dense-recent,
    sparse-older series — ideal for finding the close nearest any lookback target.
    Returns [(ts_ms, close), ...] ascending.
    """
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute(
            'SELECT timestamp, close FROM crypto_ohlcv WHERE coin_id=? AND source=? '
            'ORDER BY timestamp',
            (coin_id, source)
        )
        merged = {}
        for r in c.fetchall():
            merged[r['timestamp']] = r['close']
    finally:
        conn.close()
    return sorted(merged.items())


def _pct_change(series, lookback_days):
    """Percent change of the latest close vs the close ~lookback_days ago.

    `series` is the merged (ts_ms, close) list. Picks the candle at-or-before the
    target time; allows a small slop at the far edge so e.g. a 365-day lookback still
    resolves against a ~365-day dataset whose earliest candle lands just after target.
    Returns a float percentage, or None when there isn't enough history to compute it.
    """
    if not series or len(series) < 2:
        return None
    latest_ts, latest_close = series[-1]
    if not latest_close:
        return None
    target = latest_ts - lookback_days * 86400000
    past_close = None
    for ts, close in series:
        if ts <= target:
            past_close = close
        else:
            break
    if past_close is None:
        # target predates our earliest candle — tolerate a little so a full-window
        # lookback still resolves at the boundary (2 days + 5% of the window).
        slop = 2 * 86400000 + lookback_days * 86400000 * 0.05
        if series[0][0] - target <= slop:
            past_close = series[0][1]
        else:
            return None
    if not past_close:
        return None
    return (latest_close - past_close) / past_close * 100.0


def _has_user_coingecko_id(symbol):
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key=?", (f'coingecko_id_{symbol.upper()}',))
        row = c.fetchone()
        return bool(row and row['value'])
    finally:
        conn.close()
