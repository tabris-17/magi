"""yfinance fetch + OHLCV storage + symbol translation for HK/JP/US stocks.

Flask-free domain logic. The DB (stock_ohlcv table) is the source of truth; PNG
render cache lives in core.marketdata.generate_stock_chart.

Network is isolated to _fetch_yf_history so tests can monkeypatch that one
function rather than mocking yfinance internals.
"""
import math
import time
from datetime import datetime, date, timedelta

from core.config import STOCK_PERIODS
from core.crypto import _pct_change
from core.db import get_db_connection
from core.logging_setup import get_logger

logger = get_logger('stockdata')

# Default currency per market (yfinance fast_info.currency is an optional later enrichment)
_MARKET_CURRENCY = {'hk': 'HKD', 'jp': 'JPY', 'us': 'USD'}

# Escalating waits (seconds) for yfinance rate-limit retries on the deep backload fetch:
# 1 min → 10 min → 30 min. Long, growing pauses ride out a Yahoo throttle without
# re-hammering it; `len()` of this is the retry budget the backload passes in. The fetch
# sleeps in-place on the worker threadpool — the heartbeat runs on a separate thread, so a
# sleeping fetch never stalls liveness. The freshness prefetch passes retries=0 (it reruns
# every 15 min anyway), so only the deep backfill pays these waits.
_RATELIMIT_BACKOFF_SEC = (60, 600, 1800)


class RateLimited(Exception):
    """Raised when yfinance throttling outlasts the retry budget.

    Distinct from an empty result (a genuine no-data case, e.g. a pre-IPO date — which
    yfinance returns as an empty frame, *not* an exception). Lets a backfill/rebuild loop
    stop its current pass and let a later run resume — history fill is _needs_backfill-gated,
    so nothing is lost. Web/chart refreshes swallow it (serve stale rather than 500)."""


def _to_yahoo(market, symbol):
    """Translate a canonical portfolio symbol to a Yahoo Finance ticker.

    HK canonical is 5-digit zero-padded (00700.HK); Yahoo wants 4-digit (0700.HK).
    JP is already XXXX.T — pass through.
    US class-share dots become dashes (BRK.A → BRK-A); plain tickers pass through.
    """
    if market == 'hk':
        # Strip suffix, strip leading zeros, pad to 4 digits, re-append .HK
        code = symbol.upper().replace('.HK', '')
        return (code.lstrip('0') or '0').zfill(4) + '.HK'
    if market == 'us':
        # Yahoo uses dashes for class shares (BRK.A → BRK-A) but preserves plain dots
        # only when they're not separating a class letter. Simple heuristic: replace
        # any trailing .<uppercase-letter> with -<letter>.
        import re
        return re.sub(r'\.([A-Z])$', r'-\1', symbol.upper())
    # JP (XXXX.T) and any other market: pass through
    return symbol


def _fetch_yf_history(yahoo_symbol, start=None, end=None, retries=0,
                      backoff=_RATELIMIT_BACKOFF_SEC):
    """Fetch daily OHLCV from Yahoo Finance.

    With no bounds, fetches a trailing 1-year window (the freshness-refresh default).
    When ``start`` is given (YYYY-MM-DD), fetches the explicit [start, end) date range
    instead — how the backload extends history back to a configured start date.

    Returns ``(rows, err)`` where rows = [[ts_ms, open, high, low, close], ...] ascending
    and ``err`` is None normally or ``'rate_limit'`` when Yahoo throttled us past the retry
    budget. A genuine empty/no-data result (e.g. a pre-IPO date range) is ``([], None)`` —
    NOT a throttle — so callers don't retry it forever. On a ``YFRateLimitError`` and
    ``retries`` remaining, sleeps ``backoff[attempt]`` (the last value repeats) and retries.
    This is the ONLY place yfinance is imported — monkeypatch this in tests.
    """
    import yfinance as yf
    from yfinance.exceptions import YFRateLimitError
    attempt = 0
    while True:
        try:
            ticker = yf.Ticker(yahoo_symbol)
            if start:
                hist = ticker.history(start=start, end=end, interval='1d')
            else:
                hist = ticker.history(period='1y', interval='1d')
            if hist is None or hist.empty:
                return [], None
            rows = []
            for dt, row in hist.iterrows():
                ts_ms = int(dt.timestamp() * 1000)
                o, h, l, cl = (float(row['Open']), float(row['High']),
                               float(row['Low']), float(row['Close']))
                # yfinance pads a not-yet-settled trailing day with NaN OHLC. Storing it
                # turns into a NULL-close row that snapshot() would read as the "current
                # price" (None) — drop it so the last real close stays the price.
                if any(math.isnan(v) for v in (o, h, l, cl)):
                    continue
                rows.append([ts_ms, o, h, l, cl])
            rows.sort(key=lambda r: r[0])
            return rows, None
        except YFRateLimitError:
            if attempt < retries:
                wait = backoff[min(attempt, len(backoff) - 1)]
                logger.warning('yfinance rate-limited (%s); backing off %ds (retry %d/%d)',
                               yahoo_symbol, wait, attempt + 1, retries)
                time.sleep(wait)
                attempt += 1
                continue
            logger.warning('yfinance rate-limited (%s); retry budget (%d) exhausted',
                           yahoo_symbol, retries)
            return [], 'rate_limit'
        except Exception as e:
            logger.error('yfinance fetch error (%s): %s', yahoo_symbol, e)
            return [], None


def fetch_and_store_stock_ohlcv(market, symbol, start_date=None, retries=0):
    """Fetch and persist daily OHLCV for one symbol. Returns raw rows list or None.

    With ``start_date`` (YYYY-MM-DD) it fetches the full [start_date, today] daily range
    (a backload); otherwise the trailing 1-year window. INSERT OR REPLACE on the
    UNIQUE(source,market,symbol,timestamp) key makes overlapping re-fetches idempotent,
    so this one path covers both forward-fill (new recent candles) and back-fill (older
    candles) with no gap computation.

    ``retries`` is the yfinance rate-limit retry budget (backload passes
    ``len(_RATELIMIT_BACKOFF_SEC)``; the freshness refresh leaves it 0). If Yahoo throttles
    past that budget this raises ``RateLimited`` so the caller can stop its pass; a genuine
    empty result still returns None.
    """
    yahoo_sym = _to_yahoo(market, symbol)
    if start_date:
        # yfinance `end` is EXCLUSIVE — +1 day so today's candle is included.
        end = (date.today() + timedelta(days=1)).isoformat()
        rows, err = _fetch_yf_history(yahoo_sym, start=start_date, end=end, retries=retries)
    else:
        rows, err = _fetch_yf_history(yahoo_sym, retries=retries)
    if err == 'rate_limit':
        raise RateLimited(symbol)
    if not rows:
        return None
    currency = _MARKET_CURRENCY.get(market, 'USD')
    fetched_at = datetime.now().isoformat()
    conn = get_db_connection()
    try:
        c = conn.cursor()
        for row in rows:
            ts, o, h, l, cl = row
            c.execute(
                '''INSERT OR REPLACE INTO stock_ohlcv
                   (source, market, symbol, timestamp, open, high, low, close, currency, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)''',
                ('yfinance', market, symbol, ts, o, h, l, cl, currency, fetched_at)
            )
        conn.commit()
    finally:
        conn.close()
    return rows


def _stock_ohlcv_is_fresh(market, symbol, max_age_min=30):
    """True if stored OHLCV for this market/symbol was fetched within max_age_min."""
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute(
            'SELECT MAX(fetched_at) AS last FROM stock_ohlcv '
            'WHERE market=? AND symbol=? AND source=?',
            (market, symbol, 'yfinance')
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


def _load_stock_ohlcv_series(market, symbol):
    """Return (ts_ms, close) series ascending — used by _pct_change for performance."""
    conn = get_db_connection()
    try:
        c = conn.cursor()
        # close IS NOT NULL: skip NaN/placeholder candles (e.g. an unsettled trailing day
        # an older fetch stored as NULL) so series[-1] is the last *real* close.
        c.execute(
            'SELECT timestamp, close FROM stock_ohlcv '
            'WHERE market=? AND symbol=? AND source=? AND close IS NOT NULL ORDER BY timestamp',
            (market, symbol, 'yfinance')
        )
        return [(r['timestamp'], r['close']) for r in c.fetchall()]
    finally:
        conn.close()


def _load_stock_ohlcv_rows(market, symbol, days=None):
    """Return [[ts, open, high, low, close], ...] ascending, optionally last N days."""
    conn = get_db_connection()
    try:
        c = conn.cursor()
        # close IS NOT NULL: a NULL-close candle (unsettled/padded day) is not a real bar —
        # excluding it keeps charts and the last-close price clean.
        c.execute(
            'SELECT timestamp, open, high, low, close FROM stock_ohlcv '
            'WHERE market=? AND symbol=? AND source=? AND close IS NOT NULL ORDER BY timestamp',
            (market, symbol, 'yfinance')
        )
        rows = [[r['timestamp'], r['open'], r['high'], r['low'], r['close']]
                for r in c.fetchall()]
    finally:
        conn.close()
    if days and rows:
        cutoff = rows[-1][0] - days * 86400000
        rows = [r for r in rows if r[0] >= cutoff]
    return rows
