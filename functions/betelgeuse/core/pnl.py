"""Portfolio P&L computation — pure, base-currency-normalized (Flask-free, no network).

``compute_pnl`` takes already-fetched inputs (portfolio rows with derived cost basis,
cache-only price snapshots, and an FX multiplier function) and returns per-market and
grand-total **unrealized** P&L on *current* holdings, normalized to the base currency.

The route in ``app.py`` wires the DB + market_data + fx into this; tests seed inputs
directly. Keeping it pure means the money math is deterministic and trivially testable.
"""
from core.config import MARKETS
from core.fx import _norm_ccy

# Market display order (matches the rest of the app); only held markets are emitted.
_MARKET_ORDER = list(MARKETS.keys())


def _holding_pnl(row, snapshots, fx_fn):
    """Compute one holding's P&L dict, or None when it isn't a current position.

    A current position needs ``net_qty > 0`` AND a known ``bought_price`` (weighted-avg
    buy cost). ``complete`` is False (and base amounts are None) when the price isn't
    cached yet or FX can't resolve — such a holding is shown but excluded from the sums.
    """
    net_qty = row.get('net_qty')
    avg_cost = row.get('bought_price')
    if not (net_qty and net_qty > 0) or avg_cost is None:
        return None

    market, symbol = row['market'], row['symbol']
    snap = snapshots.get(f'{market}:{symbol}') or {}
    price = snap.get('price')
    currency = _norm_ccy(snap.get('currency'))
    rate = fx_fn(currency) if currency else None

    # P&L % is currency-free; only the absolute amounts need FX.
    pnl_pct = ((price - avg_cost) / avg_cost * 100) if (price is not None and avg_cost) else None

    holding = {
        'symbol': symbol, 'name': row.get('name'), 'comment': row.get('comment'),
        'qty': net_qty, 'avg_cost': avg_cost,
        'price': price, 'currency': currency, 'fx': rate,
        # native (instrument-currency) amounts …
        'cost_local': None, 'value_local': None, 'pnl_local': None,
        # … and the same amounts converted to the base currency
        'cost': None, 'value': None, 'pnl': None, 'pnl_pct': pnl_pct,
        'complete': False,
    }
    if price is not None and rate is not None:
        cost_local = avg_cost * net_qty
        value_local = price * net_qty
        pnl_local = value_local - cost_local
        holding.update({
            'cost_local': cost_local, 'value_local': value_local, 'pnl_local': pnl_local,
            'cost': cost_local * rate, 'value': value_local * rate, 'pnl': pnl_local * rate,
            'complete': True,
        })
    return holding


def _agg(holdings):
    """Aggregate (base currency) into {cost,value,pnl,pnl_pct,count,incomplete}."""
    complete = [h for h in holdings if h['complete']]
    cost = sum(h['cost'] for h in complete)
    value = sum(h['value'] for h in complete)
    pnl = value - cost
    return {
        'cost': cost, 'value': value, 'pnl': pnl,
        'pnl_pct': (pnl / cost * 100) if cost else None,
        'count': len(holdings), 'incomplete': len(holdings) - len(complete),
    }


def _market_agg(holdings):
    """`_agg` plus the market's single-currency **local** totals. Every holding in one
    market shares a native currency, so summing native amounts is well-defined here
    (unlike the grand total, which would mix currencies and stays base-only)."""
    base = _agg(holdings)
    complete = [h for h in holdings if h['complete']]
    currency = next((h['currency'] for h in complete), None)
    cost_local = sum(h['cost_local'] for h in complete)
    value_local = sum(h['value_local'] for h in complete)
    return {**base, 'currency': currency,
            'cost_local': cost_local, 'value_local': value_local,
            'pnl_local': value_local - cost_local}


def compute_pnl(rows, snapshots, fx_fn, base):
    """Unrealized P&L on current holdings, normalized to ``base`` currency.

    rows       : iterable of portfolio dicts {market, symbol, name, net_qty, bought_price}.
    snapshots  : {"<market>:<symbol>": {price, currency, ...}} (cache-only; may be missing).
    fx_fn      : callable(native_ccy) -> multiplier into ``base`` (None when unknown).
    base       : the base currency code.

    Returns {base, totals, markets:{m:{...,holdings:[...]}}, missing:[...]}, where
    ``markets`` contains only markets you actually hold (in app order) and ``missing``
    lists "<market>:<symbol>" holdings whose price/FX wasn't available (excluded from sums).
    """
    by_market = {}
    for row in rows:
        h = _holding_pnl(row, snapshots, fx_fn)
        if h is None:
            continue
        by_market.setdefault(row['market'], []).append(h)

    markets, missing = {}, []
    all_holdings = []
    for m in _MARKET_ORDER:
        hs = by_market.get(m)
        if not hs:
            continue
        markets[m] = {**_market_agg(hs), 'holdings': hs}
        all_holdings.extend(hs)
        missing.extend(f'{m}:{h["symbol"]}' for h in hs if not h['complete'])

    totals = _agg(all_holdings)
    total_value = totals['value'] or 0

    # Allocation weight: each holding's / market's value as a % of total portfolio value
    # (base currency). FX-invariant (numerator and denominator scale together), so it's
    # identical in any base. None when the price isn't cached (no value) or the portfolio
    # has no valued holdings. Drives the My Portfolio allocation pie.
    def _weight(v):
        return (v / total_value * 100) if (total_value and v is not None) else None
    for mk in markets.values():
        mk['weight'] = _weight(mk['value'])
        for h in mk['holdings']:
            h['weight'] = _weight(h['value']) if h['complete'] else None

    return {
        'base': _norm_ccy(base),
        'totals': totals,
        'markets': markets,
        'missing': missing,
    }
