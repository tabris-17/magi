"""fx-rates — VERSION 4.

Adds the `fx_rates` cache table: a tiny, refetchable store of USD-per-currency rates
(one row per currency) that the worker warms from yfinance and the web reads — cache or
fixed fallback anchors — to normalize portfolio P&L into the chosen base currency.

Refetchable cache (the worker rebuilds it from the provider), so down() safely drops it.
"""

VERSION = 4
DESCRIPTION = "schema: add fx_rates cache table (USD-per-currency FX rates for P&L)"


def up(c):
    c.execute(
        '''CREATE TABLE IF NOT EXISTS fx_rates (
               currency   TEXT PRIMARY KEY,   -- 'HKD','JPY','USD','AUD' (uppercase)
               usd_per    REAL NOT NULL,      -- USD per 1 unit of this currency
               fetched_at TEXT NOT NULL       -- ISO timestamp of the fetch
           )'''
    )


def down(c):
    # Refetchable cache — never a source-of-truth table — so dropping it is safe; the
    # worker repopulates from yfinance on its next prefetch.
    c.execute('DROP TABLE IF EXISTS fx_rates')
