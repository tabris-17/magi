"""Market Data Manager — unified interface over per-market data providers, plus the
crypto candlestick render cache (generate_crypto_chart).

Flask-free: the chart render-cache dir is resolved via core.config.CHART_DIR (not the
Flask app object) so the background worker can render/prefetch too.
"""
import json
import os
import time
from datetime import datetime

import matplotlib
matplotlib.use('Agg')
import mplfinance as mpf
import pandas as pd

from core import config
from core.config import CRYPTO_PERIODS, STOCK_PERIODS, MARKETS
from core.db import get_db_connection
from core.crypto import (
    _crypto_ohlcv_is_fresh, fetch_and_store_crypto_ohlcv, _load_crypto_ohlcv_rows,
    _load_crypto_ohlcv_series, _pct_change, coingecko_id_to_symbol, coingecko_symbol_to_id,
)
from core.stockdata import (
    _stock_ohlcv_is_fresh, fetch_and_store_stock_ohlcv,
    _load_stock_ohlcv_rows, _load_stock_ohlcv_series,
    _MARKET_CURRENCY, RateLimited, _RATELIMIT_BACKOFF_SEC,
)
from core.fx import refresh_fx_rates
from core.logging_setup import get_logger

logger = get_logger('marketdata')

# Default earliest date the backload reaches back to when the
# `market_data_backload_start_date` setting is unset or unparseable.
DEFAULT_BACKLOAD_START_DATE = '2024-01-01'


# ── Market Data Manager ──────────────────────────────────────────────────────
# One grand unified interface over per-market data providers. Each provider OWNS
# its own storage/schema (CoinGecko keeps using crypto_ohlcv) and implements the
# same interface; the manager dispatches uniform operations and resolves the
# active provider per market. To add a provider: subclass MarketDataProvider and
# append an instance to MARKET_DATA_PROVIDERS[<market>] — the admin UI lights up
# automatically (empty markets show "not available yet").

class MarketDataProvider:
    """Interface every market-data provider implements. A provider serves exactly
    one market and owns its own cache table/files."""
    key = None                  # stable id, e.g. 'coingecko'
    name = None                 # display name, e.g. 'CoinGecko'
    market = None               # 'hk' | 'jp' | 'us' | 'crypto'
    periods = []                # supported periods, e.g. ['7','30','90','365']
    default_max_age_min = 60    # cache freshness window (minutes)
    # lookback windows for performance() — key (used by the API/UI) -> days
    performance_periods = {'1': 1, '7': 7, '30': 30, '365': 365}

    # ── data path (used by the app, e.g. chart rendering) ──
    def is_fresh(self, instrument, period):
        raise NotImplementedError
    def refresh(self, instrument, period, force=False):
        """Fetch+store if stale (or forced). Returns True if a fetch happened."""
        raise NotImplementedError
    def load(self, instrument, period):
        """Return cached rows as [[ts, open, high, low, close], ...]."""
        raise NotImplementedError
    def backload(self, instrument, start_date=None, progress_cb=None):
        """Fetch+store the deepest daily history available back to ``start_date``.

        Idempotent via the table's UNIQUE constraint + INSERT OR REPLACE. Returns the
        number of rows fetched. Providers override; base raises."""
        raise NotImplementedError
    def snapshot(self, instrument, ensure_fresh=False):
        """Latest price + currency + as-of timestamp + performance, in one pass.

        Returns {price, currency, as_of (epoch ms), performance:{period_key: pct|None}}.
        Base implementation reports no data (all None); providers override. When a
        market has no provider the manager returns the same all-None shape — so the UI
        shows a consistent N/A placeholder everywhere market data isn't available yet.
        """
        return {'price': None, 'currency': None, 'as_of': None,
                'performance': {k: None for k in self.performance_periods}}

    def performance(self, instrument, ensure_fresh=False):
        """% change map only — thin accessor over snapshot()."""
        return self.snapshot(instrument, ensure_fresh=ensure_fresh)['performance']

    # ── admin path (used by the Market Data settings panel) ──
    def cache_stats(self):
        """{instruments, rows, periods, oldest_fetched, newest_fetched, extra}."""
        raise NotImplementedError
    def list_cached(self, q='', page=1, per_page=50):
        """Paginated per-instrument cache rows."""
        raise NotImplementedError
    def clear(self):
        """Wipe this provider's cache (DB + derived artifacts). Returns a dict."""
        raise NotImplementedError


class CoinGeckoProvider(MarketDataProvider):
    """Crypto provider — delegates to the existing CoinGecko OHLCV helpers and the
    crypto_ohlcv table (rows tagged source='coingecko')."""
    key = 'coingecko'
    name = 'CoinGecko'
    market = 'crypto'
    periods = list(CRYPTO_PERIODS.keys())
    default_max_age_min = 60

    def is_fresh(self, instrument, period):
        return _crypto_ohlcv_is_fresh(instrument, period, source=self.key,
                                      max_age_min=self.default_max_age_min)

    def refresh(self, instrument, period, force=False):
        if force or not self.is_fresh(instrument, period):
            return fetch_and_store_crypto_ohlcv(instrument, period) is not None
        return False

    def load(self, instrument, period):
        return _load_crypto_ohlcv_rows(instrument, period, source=self.key)

    def backload(self, instrument, start_date=None, progress_cb=None):
        # Best-effort: CoinGecko's OHLC endpoint can't take an arbitrary start date,
        # so fetch its deepest window (days=max) into a 'max' bucket. start_date is
        # advisory only (logged for parity); the merge in _load_crypto_ohlcv_series
        # dedupes 'max' against the numbered period buckets by timestamp.
        data = fetch_and_store_crypto_ohlcv(instrument, period='max')
        return len(data or [])

    def snapshot(self, instrument, ensure_fresh=False):
        # `instrument` is a CoinGecko coin-id here. When ensure_fresh, warm every
        # period so each lookback (1d→7d data, 7d→30d, 30d→90d, 1y→365d) has the
        # history it needs; refresh() is a no-op when the cache is already fresh.
        if ensure_fresh:
            for p in self.periods:
                try:
                    self.refresh(instrument, p)
                except Exception:
                    pass
        series = _load_crypto_ohlcv_series(instrument, source=self.key)
        perf = {k: _pct_change(series, days) for k, days in self.performance_periods.items()}
        as_of, price = series[-1] if series else (None, None)
        return {'price': price, 'currency': 'usd' if price is not None else None,
                'as_of': as_of, 'performance': perf}

    def _png_files(self):
        import glob
        return glob.glob(os.path.join(config.CHART_DIR, 'chart_crypto_*_p*.png'))

    def cache_stats(self):
        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute('SELECT COUNT(DISTINCT coin_id) AS instruments, COUNT(*) AS rows, '
                      'MIN(fetched_at) AS oldest, MAX(fetched_at) AS newest '
                      'FROM crypto_ohlcv WHERE source=?', (self.key,))
            row = c.fetchone()
            c.execute('SELECT DISTINCT period FROM crypto_ohlcv WHERE source=?', (self.key,))
            periods = sorted((r['period'] for r in c.fetchall()),
                             key=lambda p: int(p) if str(p).isdigit() else 0)
        finally:
            conn.close()
        files = [f for f in self._png_files() if os.path.exists(f)]
        png_bytes = sum(os.path.getsize(f) for f in files)
        return {
            'instruments': row['instruments'] or 0,
            'rows': row['rows'] or 0,
            'periods': periods,
            'oldest_fetched': row['oldest'],
            'newest_fetched': row['newest'],
            'extra': {'rendered_pngs': len(files), 'rendered_png_bytes': png_bytes},
        }

    def list_cached(self, q='', page=1, per_page=50):
        q = (q or '').strip().lower()
        page = max(1, int(page))
        per_page = min(200, max(10, int(per_page)))
        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute('SELECT coin_id, COUNT(*) AS rows, GROUP_CONCAT(DISTINCT period) AS periods, '
                      'MIN(fetched_at) AS oldest, MAX(fetched_at) AS newest, '
                      'MIN(timestamp) AS oldest_ts, MAX(timestamp) AS latest_ts '
                      'FROM crypto_ohlcv WHERE source=? GROUP BY coin_id ORDER BY coin_id', (self.key,))
            agg = [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
        out = []
        for r in agg:
            sym = coingecko_id_to_symbol(r['coin_id'], strict=False) or ''
            if q and q not in r['coin_id'].lower() and q not in sym.lower():
                continue
            periods = sorted((r['periods'] or '').split(','),
                             key=lambda p: int(p) if p.isdigit() else 0)
            out.append({'coin_id': r['coin_id'], 'symbol': sym, 'periods': periods,
                        'rows': r['rows'], 'oldest_fetched': r['oldest'],
                        'newest_fetched': r['newest'], 'oldest_ts': r['oldest_ts'],
                        'latest_ts': r['latest_ts']})
        total = len(out)
        start = (page - 1) * per_page
        return {'rows': out[start:start + per_page], 'total': total, 'page': page,
                'per_page': per_page, 'pages': max(1, (total + per_page - 1) // per_page)}

    def clear(self):
        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute('SELECT COUNT(*) AS cnt FROM crypto_ohlcv WHERE source=?', (self.key,))
            cleared_rows = c.fetchone()['cnt']
            c.execute('DELETE FROM crypto_ohlcv WHERE source=?', (self.key,))
            conn.commit()
        finally:
            conn.close()
        # PNGs are a pure render cache — drop them too so a clear takes effect
        # immediately (they are gated by file-age, not by the OHLCV table).
        cleared_files = 0
        for f in self._png_files():
            try:
                os.remove(f)
                cleared_files += 1
            except OSError:
                pass
        return {'cleared_rows': cleared_rows, 'cleared_files': cleared_files}


class YFinanceProvider(MarketDataProvider):
    """HK/JP/US provider — stores a single 1-year daily series per symbol in stock_ohlcv."""
    key = 'yfinance'
    name = 'Yahoo Finance'
    periods = list(STOCK_PERIODS.keys())
    default_max_age_min = 30

    def __init__(self, market):
        self.market = market

    def is_fresh(self, instrument, period=None):
        return _stock_ohlcv_is_fresh(self.market, instrument,
                                     max_age_min=self.default_max_age_min)

    def refresh(self, instrument, period=None, force=False):
        if force or not self.is_fresh(instrument):
            # retries=0: the freshness refresh reruns every 15 min, so it doesn't pay the
            # long rate-limit backoff. Swallow a throttle (serve stale) — refresh is also
            # called inline by generate_stock_chart for a web request, which must never 500.
            try:
                return fetch_and_store_stock_ohlcv(self.market, instrument) is not None
            except RateLimited:
                return False
        return False

    def load(self, instrument, period=None):
        days = int(period) if period else None
        return _load_stock_ohlcv_rows(self.market, instrument, days=days)

    def backload(self, instrument, start_date=None, progress_cb=None):
        # Deep fetch — pay the escalating rate-limit backoff (1/10/30 min). A throttle past
        # that budget raises RateLimited up to the backfill/rebuild loop, which stops its pass.
        rows = fetch_and_store_stock_ohlcv(self.market, instrument, start_date=start_date,
                                           retries=len(_RATELIMIT_BACKOFF_SEC))
        return len(rows or [])

    def snapshot(self, instrument, ensure_fresh=False):
        if ensure_fresh:
            try:
                self.refresh(instrument)
            except Exception:
                pass
        series = _load_stock_ohlcv_series(self.market, instrument)
        perf = {k: _pct_change(series, days) for k, days in self.performance_periods.items()}
        as_of, price = series[-1] if series else (None, None)
        currency = _MARKET_CURRENCY.get(self.market) if price is not None else None
        return {'price': price, 'currency': currency, 'as_of': as_of, 'performance': perf}

    def _png_files(self):
        import glob
        return glob.glob(os.path.join(config.CHART_DIR,
                                      f'chart_stock_{self.market}_*_p*.png'))

    def cache_stats(self):
        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute('SELECT COUNT(DISTINCT symbol) AS instruments, COUNT(*) AS rows, '
                      'MIN(fetched_at) AS oldest, MAX(fetched_at) AS newest '
                      'FROM stock_ohlcv WHERE source=? AND market=?', (self.key, self.market))
            row = c.fetchone()
        finally:
            conn.close()
        files = [f for f in self._png_files() if os.path.exists(f)]
        png_bytes = sum(os.path.getsize(f) for f in files)
        return {
            'instruments': row['instruments'] or 0,
            'rows': row['rows'] or 0,
            'periods': list(STOCK_PERIODS.keys()),
            'oldest_fetched': row['oldest'],
            'newest_fetched': row['newest'],
            'extra': {'rendered_pngs': len(files), 'rendered_png_bytes': png_bytes},
        }

    def list_cached(self, q='', page=1, per_page=50):
        q = (q or '').strip().lower()
        page = max(1, int(page))
        per_page = min(200, max(10, int(per_page)))
        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute('SELECT symbol, COUNT(*) AS rows, MIN(fetched_at) AS oldest, '
                      'MAX(fetched_at) AS newest, MIN(timestamp) AS oldest_ts, '
                      'MAX(timestamp) AS latest_ts '
                      'FROM stock_ohlcv WHERE source=? AND market=? GROUP BY symbol ORDER BY symbol',
                      (self.key, self.market))
            agg = [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
        filtered = [r for r in agg if not q or q in r['symbol'].lower()]
        total = len(filtered)
        start = (page - 1) * per_page
        # Conform to the per-row contract the admin UI expects (the same shape
        # CoinGeckoProvider returns): coin_id/periods/oldest_fetched/newest_fetched.
        # Stocks store one continuous daily series, so the symbol IS the instrument id
        # and every cached symbol covers all served periods.
        rows = [{'coin_id': r['symbol'], 'symbol': r['symbol'], 'periods': self.periods,
                 'rows': r['rows'], 'oldest_fetched': r['oldest'],
                 'newest_fetched': r['newest'], 'oldest_ts': r['oldest_ts'],
                 'latest_ts': r['latest_ts']}
                for r in filtered[start:start + per_page]]
        return {'rows': rows, 'total': total, 'page': page,
                'per_page': per_page, 'pages': max(1, (total + per_page - 1) // per_page)}

    def clear(self):
        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute('SELECT COUNT(*) AS cnt FROM stock_ohlcv WHERE source=? AND market=?',
                      (self.key, self.market))
            cleared_rows = c.fetchone()['cnt']
            c.execute('DELETE FROM stock_ohlcv WHERE source=? AND market=?',
                      (self.key, self.market))
            conn.commit()
        finally:
            conn.close()
        cleared_files = 0
        for f in self._png_files():
            try:
                os.remove(f)
                cleared_files += 1
            except OSError:
                pass
        return {'cleared_rows': cleared_rows, 'cleared_files': cleared_files}


# market -> [provider instances]; an empty list = "not available yet"
MARKET_DATA_PROVIDERS = {
    'hk': [YFinanceProvider('hk')],
    'jp': [YFinanceProvider('jp')],
    'us': [YFinanceProvider('us')],
    'crypto': [CoinGeckoProvider()],
}


class MarketDataManager:
    """Dispatches uniform operations to per-market providers and resolves the
    active provider per market (settings key `market_data_provider_<market>`,
    defaulting to the first registered provider)."""

    def providers(self, market):
        return [{'key': p.key, 'name': p.name, 'periods': list(p.periods)}
                for p in MARKET_DATA_PROVIDERS.get(market, [])]

    def get(self, market, key=None):
        provs = MARKET_DATA_PROVIDERS.get(market, [])
        if not provs:
            return None
        if key:
            return next((p for p in provs if p.key == key), None)
        return self._active(market, provs)

    def _active(self, market, provs):
        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute('SELECT value FROM settings WHERE key=?', (f'market_data_provider_{market}',))
            row = c.fetchone()
        finally:
            conn.close()
        if row and row['value']:
            match = next((p for p in provs if p.key == row['value']), None)
            if match:
                return match
        return provs[0]

    def active_provider(self, market):
        p = self.get(market)
        return p.key if p else None

    def set_active(self, market, key):
        provs = MARKET_DATA_PROVIDERS.get(market, [])
        if not any(p.key == key for p in provs):
            raise ValueError(f'unknown provider {key!r} for market {market!r}')
        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)',
                      (f'market_data_provider_{market}', key))
            conn.commit()
        finally:
            conn.close()

    def markets(self):
        out = []
        for m in MARKETS:
            provs = self.providers(m)
            active = self.get(m)
            out.append({
                'market': m,
                'name': MARKETS[m]['name'],
                'available': bool(provs),
                'providers': provs,
                'active_provider': active.key if active else None,
                'freshness_min': active.default_max_age_min if active else None,
            })
        return out

    def status(self, market, key=None):
        base = {'market': market, 'server_time': datetime.now().isoformat()}
        p = self.get(market, key)
        if not p:
            return {**base, 'available': False}
        return {**base, 'available': True, 'provider': p.key,
                'freshness_min': p.default_max_age_min, 'stats': p.cache_stats()}

    def list_cached(self, market, key=None, q='', page=1, per_page=50):
        p = self.get(market, key)
        if not p:
            return {'available': False, 'rows': [], 'total': 0,
                    'page': 1, 'pages': 1, 'per_page': per_page}
        return {'available': True, **p.list_cached(q=q, page=page, per_page=per_page)}

    def clear(self, market, key=None):
        p = self.get(market, key)
        if not p:
            return {'available': False}
        return {'available': True, 'provider': p.key, **p.clear()}

    def rebuild(self, market, key=None, start_date=None, progress_cb=None):
        """Clear a provider's cache then backload every portfolio instrument for the
        market from ``start_date`` to today.

        ``progress_cb(processed, total, current)`` is called before each instrument
        (and once more with ``current=None`` at the end) so a caller can report live
        progress. Returns {available, provider, processed, total, cleared_rows}.
        """
        p = self.get(market, key)
        if not p:
            return {'available': False}
        start_date = start_date or get_backload_start_date()
        cleared = p.clear()
        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute('SELECT symbol FROM portfolio WHERE market=?', (market,))
            symbols = [r['symbol'] for r in c.fetchall()]
        finally:
            conn.close()
        # Map portfolio symbols → provider-native instrument keys (crypto symbol→coin-id),
        # de-duplicating while preserving order.
        seen, instruments = set(), []
        for s in symbols:
            inst = _provider_instrument_key(market, s)
            if inst and inst not in seen:
                seen.add(inst)
                instruments.append(inst)
        total = len(instruments)
        processed = 0
        for inst in instruments:
            if progress_cb:
                progress_cb(processed, total, inst)
            try:
                p.backload(inst, start_date)
            except RateLimited:
                # Throttled past the backoff budget — stop hammering. The remaining
                # instruments get filled by the hourly _needs_backfill-gated backload job.
                logger.warning('[rebuild] rate-limited at %s:%s — stopping rebuild early, '
                               'backload will finish the rest', market, inst)
                break
            except Exception as e:
                logger.error('[rebuild] %s:%s error: %s', market, inst, e)
            processed += 1
            time.sleep(0.5)   # be gentle on the upstream API
        if progress_cb:
            progress_cb(processed, total, None)
        logger.info('[rebuild] %s/%s done — processed=%d cleared_rows=%d (start=%s)',
                    market, p.key, processed, cleared.get('cleared_rows', 0), start_date)
        return {'available': True, 'provider': p.key, 'processed': processed,
                'total': total, 'cleared_rows': cleared.get('cleared_rows', 0)}

    def snapshot(self, market, instrument, ensure_fresh=False):
        """Last price + performance for one instrument via the market's active provider.

        `instrument` must be the provider-native key (a coin-id for crypto). Returns
        {'available': bool, 'price', 'currency', 'as_of', 'performance':{period_key: pct|None}}
        — `available` is False (and every value None) when the market has no provider
        yet, so callers render a uniform N/A placeholder regardless of why data is missing.
        """
        p = self.get(market)
        if not p:
            return {'available': False, 'price': None, 'currency': None, 'as_of': None,
                    'performance': {k: None for k in DEFAULT_PERFORMANCE_PERIODS}}
        return {'available': True, **p.snapshot(instrument, ensure_fresh=ensure_fresh)}

    def performance(self, market, instrument, ensure_fresh=False):
        """% change map only — thin accessor over snapshot()."""
        snap = self.snapshot(market, instrument, ensure_fresh=ensure_fresh)
        return {'available': snap['available'], 'performance': snap['performance']}


# Period-key set used when a market has no provider (keeps the API shape uniform).
DEFAULT_PERFORMANCE_PERIODS = ['1', '7', '30', '365']

market_data = MarketDataManager()


def _render_candles(df, filepath):
    """Render a dark-theme candlestick PNG to filepath via mplfinance. Returns True on success."""
    mc = mpf.make_marketcolors(
        up='#22c55e', down='#ef4444',
        edge={'up': '#22c55e', 'down': '#ef4444'},
        wick={'up': '#22c55e', 'down': '#ef4444'}
    )
    style = mpf.make_mpf_style(
        base_mpf_style='nightclouds', marketcolors=mc,
        facecolor='#0d1929', edgecolor='#1e293b', figcolor='#0d1929',
        gridcolor='#1e293b', gridstyle='--', y_on_right=True,
        rc={
            'axes.labelcolor': '#94a3b8', 'xtick.color': '#64748b',
            'ytick.color': '#64748b', 'axes.edgecolor': '#1e293b',
        }
    )
    try:
        mpf.plot(df, type='candle', style=style,
                 savefig=dict(fname=filepath, dpi=100, bbox_inches='tight'),
                 figsize=(10, 5), tight_layout=True)
        return True
    except Exception as e:
        logger.error('Chart render error (%s): %s', os.path.basename(filepath), e)
        return False


def _ohlcv_df(data):
    """Convert [[ts_ms, o, h, l, c], ...] to a clean mplfinance-ready DataFrame."""
    df = pd.DataFrame(data, columns=['timestamp', 'Open', 'High', 'Low', 'Close'])
    df['Date'] = pd.to_datetime(df['timestamp'], unit='ms')
    df = df.set_index('Date')[['Open', 'High', 'Low', 'Close']].astype(float)
    return df[~df.index.duplicated(keep='last')].sort_index()


def generate_crypto_chart(coin_id, period='30', force=False):
    """Render a candlestick PNG via mplfinance. Returns filename or None on failure.

    OHLCV persistence is decoupled from the PNG render cache: the DB is always
    kept fresh for this coin/period (refetched when stale), and the chart is
    rendered from the stored rows. The PNG file is a pure render cache.
    """
    safe_id = coin_id.replace('/', '_').replace('\\', '_')
    filename = f'chart_crypto_{safe_id}_p{period}.png'
    os.makedirs(config.CHART_DIR, exist_ok=True)
    filepath = os.path.join(config.CHART_DIR, filename)

    # 1. Keep the OHLCV table fresh, independent of whether the PNG is cached.
    prov = market_data.get('crypto', 'coingecko')
    if prov:
        prov.refresh(coin_id, period, force=force)
    elif force or not _crypto_ohlcv_is_fresh(coin_id, period):
        fetch_and_store_crypto_ohlcv(coin_id, period)

    # 2. PNG render cache: skip re-rendering if the image is < 60 min old.
    if not force and os.path.exists(filepath):
        age = (datetime.now() - datetime.fromtimestamp(os.path.getmtime(filepath))).total_seconds()
        if age < 3600:
            return filename

    # 3. Render from the stored OHLCV rows (DB is the source of truth).
    data = _load_crypto_ohlcv_rows(coin_id, period)
    if not data:
        return None
    df = _ohlcv_df(data)
    if df.empty:
        return None
    return filename if _render_candles(df, filepath) else None


def generate_stock_chart(market, symbol, period='30', force=False):
    """Render a candlestick PNG for a HK/JP/US stock. Returns filename or None.

    Same decoupling as generate_crypto_chart: DB series is the source of truth,
    PNG is a pure render cache (60-min file-age gate).
    """
    safe_sym = symbol.replace('/', '_').replace('\\', '_').replace('.', '_')
    filename = f'chart_stock_{market}_{safe_sym}_p{period}.png'
    os.makedirs(config.CHART_DIR, exist_ok=True)
    filepath = os.path.join(config.CHART_DIR, filename)

    prov = market_data.get(market, 'yfinance')
    if prov:
        prov.refresh(symbol, force=force)

    if not force and os.path.exists(filepath):
        age = (datetime.now() - datetime.fromtimestamp(os.path.getmtime(filepath))).total_seconds()
        if age < 3600:
            return filename

    days = int(period)
    data = _load_stock_ohlcv_rows(market, symbol, days=days)
    if not data:
        return None
    df = _ohlcv_df(data)
    if df.empty:
        return None
    return filename if _render_candles(df, filepath) else None


def prefetch_market_data():
    """Proactively refresh stock OHLCV for every yfinance-backed portfolio instrument.

    Skips instruments already within the freshness window; skips crypto (it
    refreshes on demand). Also warms the FX-rate cache (cheap — a few `<ccy>USD=X`
    tickers) so P&L always has fresh base-currency rates. Called by the worker scheduler
    on a fixed interval. Returns {'fetched': N, 'skipped': N}.
    """
    # FX first — a handful of currency tickers, independent of the per-symbol refresh.
    try:
        refresh_fx_rates()
    except Exception as e:
        logger.error('[prefetch] FX refresh error: %s', e)

    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('SELECT market, symbol FROM portfolio')
        items = c.fetchall()
    finally:
        conn.close()

    fetched = skipped = 0
    for it in items:
        mkt, sym = it['market'], it['symbol']
        prov = market_data.get(mkt)
        if not prov or prov.key != 'yfinance':
            skipped += 1
            continue
        try:
            did = prov.refresh(sym)
            if did:
                fetched += 1
            else:
                skipped += 1
        except Exception as e:
            logger.error('[prefetch] %s:%s error: %s', mkt, sym, e)
            skipped += 1
    logger.info('[prefetch] done — fetched=%d skipped=%d', fetched, skipped)
    return {'fetched': fetched, 'skipped': skipped}

def _provider_instrument_key(market, canonical_symbol):
    """Translate a canonical portfolio symbol into the provider-native instrument key.

    Crypto providers key on a CoinGecko coin-id, not a ticker, so resolve symbol→id
    (falling back to the lower-cased input as a possible id). Other markets pass the
    symbol through unchanged. Returns None only when crypto resolution fails entirely.
    """
    if market == 'crypto':
        coin_id = coingecko_symbol_to_id(canonical_symbol, strict=False)
        return coin_id or (canonical_symbol or '').lower() or None
    return canonical_symbol


# ── Backload (deep history) ───────────────────────────────────────────────────

def get_backload_start_date():
    """The global backload start date (YYYY-MM-DD) from settings, validated.

    Falls back to DEFAULT_BACKLOAD_START_DATE when unset or unparseable. This is the
    single source of truth for how far back history is filled.
    """
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key='market_data_backload_start_date'")
        row = c.fetchone()
    finally:
        conn.close()
    val = ((row['value'] if row else None) or '').strip()
    try:
        datetime.strptime(val, '%Y-%m-%d')
        return val
    except (ValueError, TypeError):
        return DEFAULT_BACKLOAD_START_DATE


def _needs_backfill(market, symbol, start_date):
    """True if a yfinance instrument's stored history doesn't yet reach start_date.

    True when there are no rows, or the earliest stored candle is more than a few days
    after start_date (markets close on weekends/holidays, so the first real candle can
    legitimately land a little after the requested date — tolerate ~5 days of slop so a
    completed backfill isn't re-fetched forever).
    """
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('SELECT MIN(timestamp) AS oldest FROM stock_ohlcv '
                  'WHERE source=? AND market=? AND symbol=?', ('yfinance', market, symbol))
        row = c.fetchone()
    finally:
        conn.close()
    oldest = row['oldest'] if row else None
    if oldest is None:
        return True
    try:
        start_ms = datetime.strptime(start_date, '%Y-%m-%d').timestamp() * 1000
    except (ValueError, TypeError):
        return False
    return oldest > start_ms + 5 * 86400000


def backload_market_data(start_date=None):
    """Ensure every yfinance-backed portfolio instrument has daily history back to the
    configured start date.

    Idempotent and gated by _needs_backfill, so instruments already filled are skipped
    (keeps the periodic job cheap and avoids re-hammering the API). Crypto is best-effort
    /on-demand and excluded here (handled by manual Rebuild). The worker runs this on a
    low-frequency interval. Returns {'backfilled': N, 'skipped': N}.
    """
    start_date = start_date or get_backload_start_date()
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('SELECT market, symbol FROM portfolio')
        items = c.fetchall()
    finally:
        conn.close()
    backfilled = skipped = 0
    for it in items:
        mkt, sym = it['market'], it['symbol']
        prov = market_data.get(mkt)
        if not prov or prov.key != 'yfinance' or not _needs_backfill(mkt, sym, start_date):
            skipped += 1
            continue
        try:
            prov.backload(sym, start_date)
            backfilled += 1
            time.sleep(0.5)
        except RateLimited:
            # Yahoo is throttling past our backoff budget — stop this pass rather than
            # hammer it. The next (hourly) tick resumes the unfinished ones (_needs_backfill).
            logger.warning('[backload] rate-limited at %s:%s — aborting this pass, '
                           'will resume next tick', mkt, sym)
            break
        except Exception as e:
            logger.error('[backload] %s:%s error: %s', mkt, sym, e)
            skipped += 1
    logger.info('[backload] done — backfilled=%d skipped=%d (start=%s)',
                backfilled, skipped, start_date)
    return {'backfilled': backfilled, 'skipped': skipped}


# ── DB-backed rebuild request/status ──────────────────────────────────────────
# The web UI sets a *request* flag in `settings`; the worker's poll loop picks it up,
# runs the rebuild on the scheduler threadpool, and writes *progress* back to `settings`.
# This keeps the long job in the worker (never the Flask process) and survives a web
# restart. Mirrors core.health's settings-as-mailbox pattern.

_REBUILD_REQUEST_PREFIX = 'market_data_rebuild_request_'
_REBUILD_STATUS_PREFIX = 'market_data_rebuild_status_'


def _set_setting(key, value):
    conn = get_db_connection()
    try:
        conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)', (key, value))
        conn.commit()
    finally:
        conn.close()


def _get_setting(key):
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('SELECT value FROM settings WHERE key=?', (key,))
        row = c.fetchone()
    finally:
        conn.close()
    return row['value'] if row else None


def _del_setting(key):
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM settings WHERE key=?', (key,))
        conn.commit()
    finally:
        conn.close()


def write_rebuild_status(market, status):
    """Persist the rebuild progress/status dict (JSON) for a market."""
    _set_setting(f'{_REBUILD_STATUS_PREFIX}{market}', json.dumps(status))


def read_rebuild_status(market):
    """Return the rebuild status dict for a market ({'state':'idle'} when none/garbled)."""
    raw = _get_setting(f'{_REBUILD_STATUS_PREFIX}{market}')
    if not raw:
        return {'state': 'idle'}
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {'state': 'idle'}


def set_rebuild_request(market, provider):
    """Queue a rebuild (called by the web route): record the request flag AND an initial
    'queued' status so the UI shows progress immediately, before the worker picks it up."""
    _set_setting(f'{_REBUILD_REQUEST_PREFIX}{market}', provider)
    write_rebuild_status(market, {'state': 'queued', 'provider': provider, 'processed': 0,
                                  'total': None, 'current': None, 'started': None,
                                  'finished': None, 'error': None})


def pending_rebuild_requests():
    """Return [(market, provider)] for every queued rebuild request flag in settings."""
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('SELECT key, value FROM settings WHERE key LIKE ?',
                  (f'{_REBUILD_REQUEST_PREFIX}%',))
        rows = c.fetchall()
    finally:
        conn.close()
    return [(r['key'][len(_REBUILD_REQUEST_PREFIX):], r['value']) for r in rows]


def clear_rebuild_request(market):
    _del_setting(f'{_REBUILD_REQUEST_PREFIX}{market}')


def run_rebuild(market, provider, start_date=None, now=None):
    """Execute a queued rebuild end-to-end, writing progress to settings.

    Writes 'running' → per-instrument progress → 'done' (or 'error'), then clears the
    request flag. ``now`` (an ISO timestamp string) is injectable for deterministic
    tests. Returns the final status dict.
    """
    start_date = start_date or get_backload_start_date()
    ts = now or datetime.now().isoformat()
    write_rebuild_status(market, {'state': 'running', 'provider': provider, 'processed': 0,
                                  'total': None, 'current': None, 'started': ts,
                                  'finished': None, 'error': None})

    def progress(processed, total, current):
        write_rebuild_status(market, {'state': 'running', 'provider': provider,
                                      'processed': processed, 'total': total,
                                      'current': current, 'started': ts,
                                      'finished': None, 'error': None})

    try:
        result = market_data.rebuild(market, provider, start_date, progress_cb=progress)
        if not result.get('available'):
            raise RuntimeError(f'no provider {provider!r} for market {market!r}')
        status = {'state': 'done', 'provider': provider,
                  'processed': result.get('processed', 0), 'total': result.get('total', 0),
                  'current': None, 'started': ts, 'finished': now or datetime.now().isoformat(),
                  'error': None}
    except Exception as e:
        logger.error('[rebuild] %s/%s failed: %s', market, provider, e)
        status = {'state': 'error', 'provider': provider, 'processed': 0, 'total': None,
                  'current': None, 'started': ts, 'finished': now or datetime.now().isoformat(),
                  'error': str(e)}
    write_rebuild_status(market, status)
    clear_rebuild_request(market)
    return status


# ── Background-job status (periodic prefetch / backload) ────────────────────────
# The worker records each recurring job's lifecycle (running/idle, last run, last
# result, next scheduled run) into per-job settings keys so the UI can show whether a
# background reload is happening now and when the next one fires. Per-job keys (not one
# shared blob) so the two jobs never clobber each other's status from separate threads.

_JOB_STATUS_PREFIX = 'market_data_job_'
JOB_NAMES = ('prefetch', 'backload')


def read_job_status(name):
    """Status dict for a recurring job ({'state':'idle'} when never run)."""
    raw = _get_setting(f'{_JOB_STATUS_PREFIX}{name}')
    if not raw:
        return {'state': 'idle'}
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {'state': 'idle'}


def read_jobs_status(names=JOB_NAMES):
    """{name: status} for the recurring background jobs — drives the UI indicator."""
    return {n: read_job_status(n) for n in names}


def _write_job_status(name, status):
    _set_setting(f'{_JOB_STATUS_PREFIX}{name}', json.dumps(status))


def record_job_start(name, now=None):
    st = read_job_status(name)
    st.update(state='running', last_started=(now or datetime.now().isoformat()))
    _write_job_status(name, st)


def record_job_finish(name, result=None, error=None, now=None):
    st = read_job_status(name)
    st.update(state='idle', last_finished=(now or datetime.now().isoformat()),
              last_result=result, last_error=error)
    _write_job_status(name, st)


def record_job_next_run(name, next_run_iso):
    """Persist a job's next scheduled fire time (read off APScheduler by the worker)."""
    st = read_job_status(name)
    st['next_run'] = next_run_iso
    _write_job_status(name, st)


def run_tracked(name, fn):
    """Run a recurring job ``fn`` while recording its lifecycle to the DB for the UI.

    Marks 'running' before, 'idle' + last_result/last_error after (always, even on
    failure). The status is what the Market Data panel polls to show "reloading now".
    """
    record_job_start(name)
    try:
        result = fn()
    except Exception as e:
        logger.error('[job] %s failed: %s', name, e)
        record_job_finish(name, None, error=str(e))
        raise
    record_job_finish(name, result)
    return result


def tracked_prefetch():
    """Scheduled wrapper: freshness prefetch with UI lifecycle recording."""
    return run_tracked('prefetch', prefetch_market_data)


def tracked_backload():
    """Scheduled wrapper: deep-history backfill with UI lifecycle recording."""
    return run_tracked('backload', backload_market_data)
