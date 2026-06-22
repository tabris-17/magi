"""backfill-monitor-from-added-date — VERSION 3 (data migration).

One-off backfill: set EVERY portfolio row's monitor_price to the instrument's market close
on (or just before) its added_date — the "price when I added it". OVERWRITES any existing
monitor_price (chosen policy: all rows).

NOTE — this migration does NETWORK I/O, a deliberate exception to the usual
"migrations are deterministic/offline" rule (the user opted into it). It warms each
instrument's OHLCV via the market-data manager, then reads the close at added_date from the
stored series, sleeping between instruments to be gentle on the upstream sources. It is
RESILIENT: an instrument whose data can't be fetched (source down, bad symbol, or an
added_date older than the ~1y of available history) is skipped — never raising, so a
transient yfinance/CoinGecko hiccup can't abort a prod deploy's migrate step. The added-date
close is historical/stable, so dev and prod converge to the same values.

Irreversible: overwriting drops the previous monitor levels, which are not recoverable from
the DB. down() restores nothing — use the automatic pre-migration backup to roll back.
"""
from datetime import datetime, timezone

from core.migrate import Irreversible

VERSION = 3
DESCRIPTION = "data: backfill monitor_price from each instrument's price on its added_date (overwrite all)"

# Politeness delay between instruments — market data is rate-limited / fetched synchronously.
SLEEP_SECONDS = 1.0


def _close_on_or_before(series, added_date):
    """Last close in `series` (ascending ``[(ts_ms, close), ...]``) dated on/before
    `added_date` ('YYYY-MM-DD'). None when added_date is unparseable or predates the
    available history. Comparison is by end-of-day UTC, which tolerates the small tz/intraday
    skew in how daily bars are stamped (a bar dated added_date in any reasonable tz lands
    on/before that day's 23:59:59 UTC cutoff)."""
    try:
        d = datetime.strptime(added_date, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None
    cutoff_ms = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000
    best = None
    for ts, close in series:
        if ts <= cutoff_ms:
            best = close            # ascending → the last one on/before the cutoff wins
        else:
            break
    return best


def up(c):
    # Read the targets up front via the cursor's own connection-independent fetch. On a fresh
    # / empty DB (e.g. the whole test suite) there's nothing to backfill — return BEFORE the
    # heavy, network-touching imports so from-scratch init_db() stays fast and offline.
    from core.db import get_db_connection
    rconn = get_db_connection()
    try:
        rows = rconn.execute(
            'SELECT id, market, symbol, added_date FROM portfolio').fetchall()
    finally:
        rconn.close()
    if not rows:
        return

    # Lazy (heavy) imports — only when there's real work. Reading via a separate connection
    # above means our migration connection `c` hasn't taken a read snapshot yet, so the
    # OHLCV writes the warming below commits on other connections won't make c's later
    # UPDATEs hit SQLITE_BUSY_SNAPSHOT under WAL.
    import time
    from core.marketdata import market_data, _provider_instrument_key
    from core.crypto import _load_crypto_ohlcv_series
    from core.stockdata import _load_stock_ohlcv_series

    print(f'[003] backfilling monitor_price for {len(rows)} instrument(s) from added_date price...')
    updates, skipped = [], []
    for row in rows:
        pid, market, symbol, added_date = row[0], row[1], row[2], row[3]
        try:
            inst = _provider_instrument_key(market, symbol)
            if not inst or not added_date:
                skipped.append(f'{market}:{symbol} (no provider / no added_date)')
                continue
            try:
                market_data.snapshot(market, inst, ensure_fresh=True)   # warm OHLCV (network)
            except Exception:
                pass            # warming failed — fall through, maybe cached data covers it
            series = (_load_crypto_ohlcv_series(inst) if market == 'crypto'
                      else _load_stock_ohlcv_series(market, inst))
            price = _close_on_or_before(series, added_date)
            if price is None:
                skipped.append(f'{market}:{symbol} (no price at {added_date})')
            else:
                updates.append((float(price), pid))
                print(f'  {market}:{symbol}  added {added_date} -> monitor {float(price):.6g}')
        except Exception as e:                  # never let one instrument abort the migration
            skipped.append(f'{market}:{symbol} ({e})')
        time.sleep(SLEEP_SECONDS)

    # Apply every write at the very end so the migration holds its write lock only briefly —
    # the long network/sleep phase ran without one.
    for price, pid in updates:
        c.execute('UPDATE portfolio SET monitor_price=? WHERE id=?', (price, pid))
    print(f'[003] done: set {len(updates)} monitor price(s); skipped {len(skipped)}.')
    for s in skipped:
        print(f'  skipped {s}')


def down(c):
    raise Irreversible(
        "003 overwrote monitor_price for every portfolio row from each instrument's "
        "added_date price; the previous levels are not recoverable from the DB. Restore the "
        "automatic pre-migration backup (portfolio.db.premigrate-v2-to-v3-*) to roll back.")
