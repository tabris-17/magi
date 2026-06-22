"""FX rates + base-currency conversion (Flask-free, no inline network in the web path).

Everything pivots through **USD-per-currency** so we only ever need the liquid
``<CCY>USD=X`` Yahoo tickers (no flaky cross-pairs like ``JPYHKD=X``):

    usd_per(ccy)  : 'USD' -> 1.0 ; else cached fx_rates.usd_per ; else FALLBACK_USD_PER[ccy]
    fx_rate(C, B) : usd_per(C) / usd_per(B)   # units of B per 1 unit of C ; C == B -> 1.0

The **worker** keeps the cache warm (``refresh_fx_rates``); the **web** only ever reads
the cache or the fixed anchors — it never fetches inline (fast, no rate-limit risk).
Network is isolated to ``_fetch_fx_usd_per`` (the single test seam).
"""
from datetime import datetime

from core.db import get_db_connection
from core.logging_setup import get_logger

logger = get_logger('fx')

# Currencies that ever appear: instrument-native (HK->HKD, JP->JPY, US/crypto->USD)
# plus the selectable base currencies (HKD/USD/AUD). USD is the pivot/identity.
BASE_CURRENCIES = ['HKD', 'USD', 'AUD']
TRACKED_CURRENCIES = ['HKD', 'JPY', 'USD', 'AUD']
DEFAULT_BASE_CURRENCY = 'HKD'

FX_PROVIDER = 'yfinance'
FX_PIVOT = 'USD'

# Fixed fallback anchors as USD per 1 unit (same shape as the cache), used ONLY when the
# live cache is empty / the provider is unreachable. Derived via the USD pivot from the
# two anchors the user supplied:
#   HKD -> USD = 0.1276            => USD_per(HKD) = 0.1276
#   HKD -> AUD = 0.181 (triangle)  => USD_per(AUD) = 0.1276 / 0.181 ~= 0.7050
# JPY was not anchored by the user — this is an editable best-guess so JP holdings still
# convert when offline; the live yfinance path overrides it in normal operation.
FALLBACK_USD_PER = {
    'USD': 1.0,
    'HKD': 0.1276,
    'AUD': 0.1276 / 0.181,   # ~= 0.7050
    'JPY': 0.0064,
}


def _norm_ccy(ccy):
    """Upper-case a currency code (maps crypto's lowercase 'usd' -> 'USD'). None-safe."""
    return (ccy or '').strip().upper() or None


def get_base_currency():
    """Active base currency from the `base_currency` setting (default HKD), validated."""
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key='base_currency'")
        row = c.fetchone()
    finally:
        conn.close()
    val = _norm_ccy(row['value'] if row else None)
    return val if val in BASE_CURRENCIES else DEFAULT_BASE_CURRENCY


def _load_fx_cache():
    """All cached rates as {CCY: (usd_per, fetched_at)}. Empty dict when the table is bare."""
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('SELECT currency, usd_per, fetched_at FROM fx_rates')
        return {r['currency']: (r['usd_per'], r['fetched_at']) for r in c.fetchall()}
    finally:
        conn.close()


def usd_per(ccy, cache=None):
    """USD per 1 unit of ``ccy``: cache -> fixed fallback anchor. NEVER fetches (web-safe).

    Pass a pre-loaded ``cache`` (from ``_load_fx_cache``) to avoid re-querying per call.
    Returns None only for an unknown currency with no anchor.
    """
    ccy = _norm_ccy(ccy)
    if ccy is None:
        return None
    if ccy == 'USD':
        return 1.0
    if cache is None:
        cache = _load_fx_cache()
    cached = cache.get(ccy)
    if cached and cached[0]:
        return cached[0]
    return FALLBACK_USD_PER.get(ccy)


def fx_rate(from_ccy, to_ccy, cache=None):
    """Multiplier converting an amount in ``from_ccy`` into ``to_ccy`` (to-units per 1 from).

    ``fx_rate(C, B) = usd_per(C) / usd_per(B)``. Identity -> 1.0; None if either leg unknown.
    """
    from_ccy, to_ccy = _norm_ccy(from_ccy), _norm_ccy(to_ccy)
    if from_ccy is None or to_ccy is None:
        return None
    if from_ccy == to_ccy:
        return 1.0
    if cache is None:
        cache = _load_fx_cache()
    uf, ut = usd_per(from_ccy, cache), usd_per(to_ccy, cache)
    if not uf or not ut:
        return None
    return uf / ut


def fx_converter(base, cache=None):
    """Return a closure ``rate(from_ccy) -> multiplier into base`` sharing one cache load.

    Used by the P&L computation so every holding converts off a single FX snapshot.
    """
    base = _norm_ccy(base) or DEFAULT_BASE_CURRENCY
    if cache is None:
        cache = _load_fx_cache()
    return lambda from_ccy: fx_rate(from_ccy, base, cache)


def _fetch_fx_usd_per(ccy):
    """USD per 1 unit of ``ccy`` from Yahoo (``<ccy>USD=X`` last close), or None.

    The ONLY place yfinance is touched for FX — monkeypatch this in tests. USD is the
    identity pivot and is never fetched. Rates are delayed (the user opted into that).
    """
    ccy = _norm_ccy(ccy)
    if ccy == 'USD':
        return 1.0
    import yfinance as yf
    try:
        hist = yf.Ticker(f'{ccy}USD=X').history(period='5d', interval='1d')
        if hist is None or hist.empty:
            return None
        close = float(hist['Close'].iloc[-1])
        return close if close > 0 else None
    except Exception as e:
        logger.error('FX fetch error (%sUSD=X): %s', ccy, e)
        return None


def refresh_fx_rates(currencies=None):
    """Fetch live ``<ccy>USD=X`` for each currency and upsert ``fx_rates``. WORKER-ONLY
    (touches the network via the seam). Idempotent. Returns {'updated': N, 'failed': [..]}.
    """
    currencies = currencies or TRACKED_CURRENCIES
    fetched_at = datetime.now().isoformat()
    updated, failed = 0, []
    conn = get_db_connection()
    try:
        c = conn.cursor()
        for ccy in currencies:
            ccy = _norm_ccy(ccy)
            val = _fetch_fx_usd_per(ccy)
            if val is None:
                failed.append(ccy)
                continue
            c.execute(
                'INSERT OR REPLACE INTO fx_rates (currency, usd_per, fetched_at) VALUES (?,?,?)',
                (ccy, float(val), fetched_at)
            )
            updated += 1
        conn.commit()
    finally:
        conn.close()
    if failed:
        logger.warning('FX refresh: %d updated, failed=%s', updated, failed)
    else:
        logger.info('FX refresh: %d rate(s) updated', updated)
    return {'updated': updated, 'failed': failed}


def fx_status():
    """Provider + base + per-currency provenance for the FX Rates admin panel / ``/api/fx``.

    Returns {provider, pivot, base, generated_at, rates:[{currency, usd_per,
    source:'live'|'fallback', fetched_at}]} over every TRACKED_CURRENCY. USD is the
    identity pivot (always 'live'); other currencies report 'live' when a cached rate
    exists, else 'fallback' (the fixed anchor). The panel derives each base<->ccy pair
    and its inverse from these usd_per values.
    """
    cache = _load_fx_cache()
    rates = []
    for ccy in TRACKED_CURRENCIES:
        if ccy == 'USD':
            rates.append({'currency': 'USD', 'usd_per': 1.0, 'source': 'live', 'fetched_at': None})
            continue
        cached = cache.get(ccy)
        if cached and cached[0]:
            rates.append({'currency': ccy, 'usd_per': cached[0],
                          'source': 'live', 'fetched_at': cached[1]})
        else:
            rates.append({'currency': ccy, 'usd_per': FALLBACK_USD_PER.get(ccy),
                          'source': 'fallback', 'fetched_at': None})
    return {'provider': FX_PROVIDER, 'pivot': FX_PIVOT, 'base': get_base_currency(),
            'generated_at': datetime.now().isoformat(), 'rates': rates}
