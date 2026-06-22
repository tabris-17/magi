"""FX rates + portfolio P&L — pure math, cache/fallback resolution, and the routes.

Deterministic and offline: the yfinance seam ``core.fx._fetch_fx_usd_per`` is
monkeypatched, never imported. Price snapshots come from seeded ``stock_ohlcv`` rows.
"""
import pytest

from core import fx
from core.fx import usd_per, fx_rate, fx_converter, refresh_fx_rates, fx_status, get_base_currency
from core.pnl import compute_pnl


# ── pure rate math (explicit caches → no DB needed) ───────────────────────────
class TestFxPure:
    def test_usd_is_identity(self):
        assert usd_per('USD', cache={}) == 1.0
        assert usd_per('usd', cache={}) == 1.0          # crypto's lowercase normalizes

    def test_cache_hit_used(self):
        assert usd_per('HKD', cache={'HKD': (0.13, 'ts')}) == 0.13

    def test_fallback_when_no_cache(self):
        # Empty cache → fixed anchor (the value the user supplied).
        assert usd_per('HKD', cache={}) == pytest.approx(0.1276)

    def test_rate_identity_is_one(self):
        assert fx_rate('AUD', 'AUD', cache={}) == 1.0

    def test_anchor_roundtrip_hkd_to_aud(self):
        # Reproduces the user's second anchor HKD→AUD = 0.181 via the USD pivot.
        assert fx_rate('HKD', 'AUD', cache={}) == pytest.approx(0.181, rel=1e-6)

    def test_usd_pivot_cross(self):
        # JPY→AUD with explicit USD-per legs: usd_per(JPY)/usd_per(AUD).
        cache = {'JPY': (0.0064, 't'), 'AUD': (0.70, 't')}
        assert fx_rate('JPY', 'AUD', cache=cache) == pytest.approx(0.0064 / 0.70)

    def test_converter_closure(self):
        rate = fx_converter('HKD', cache={'USD': (1.0, 't'), 'HKD': (0.1276, 't')})
        assert rate('HKD') == 1.0
        assert rate('USD') == pytest.approx(1 / 0.1276)

    def test_triangle_via_usd_pivot(self):
        # HKD→JPY is routed through the USD pivot (no direct HKDJPY pair is ever needed):
        # fx_rate(HKD,JPY) == usd_per(HKD) / usd_per(JPY).
        cache = {'HKD': (0.1276, 't'), 'JPY': (0.0064, 't')}
        assert fx_rate('HKD', 'JPY', cache=cache) == pytest.approx(0.1276 / 0.0064)

    def test_inversion_is_reciprocal(self):
        # The panel renders each pair's inverse as a reciprocal — assert the math holds:
        # fx_rate(A,B) * fx_rate(B,A) == 1 and fx_rate(B,A) == 1 / fx_rate(A,B), for crosses
        # that DON'T touch USD directly (HKD↔AUD, HKD↔JPY) and ones that do.
        cache = {'HKD': (0.1276, 't'), 'AUD': (0.705, 't'), 'JPY': (0.0064, 't'), 'USD': (1.0, 't')}
        for a, b in [('HKD', 'AUD'), ('HKD', 'JPY'), ('AUD', 'JPY'), ('JPY', 'USD')]:
            fwd, rev = fx_rate(a, b, cache=cache), fx_rate(b, a, cache=cache)
            assert fwd * rev == pytest.approx(1.0)
            assert rev == pytest.approx(1.0 / fwd)

    def test_inversion_with_fallback_anchors(self):
        # Same reciprocal invariant when both legs come from the fixed fallback anchors
        # (empty cache) — covers the user's HKD/USD = 0.1276, inverse USD/HKD ≈ 7.837.
        assert fx_rate('HKD', 'USD', cache={}) == pytest.approx(0.1276)
        assert fx_rate('USD', 'HKD', cache={}) == pytest.approx(1 / 0.1276)   # ≈ 7.837
        assert fx_rate('HKD', 'USD', cache={}) * fx_rate('USD', 'HKD', cache={}) == pytest.approx(1.0)


# ── refresh + status (DB cache) ───────────────────────────────────────────────
class TestRefreshFx:
    def test_refresh_upserts_and_is_idempotent(self, db, monkeypatch, conn):
        monkeypatch.setattr(fx, '_fetch_fx_usd_per',
                            lambda ccy: {'HKD': 0.128, 'JPY': 0.0065, 'USD': 1.0, 'AUD': 0.70}.get(ccy))
        res = refresh_fx_rates()
        assert res['updated'] == 4 and res['failed'] == []
        rows = {r['currency']: r['usd_per'] for r in conn.execute('SELECT currency, usd_per FROM fx_rates')}
        assert rows['HKD'] == pytest.approx(0.128)
        # Re-run replaces, never duplicates (currency is the PK).
        refresh_fx_rates()
        assert conn.execute('SELECT COUNT(*) FROM fx_rates').fetchone()[0] == 4

    def test_refresh_records_failures(self, db, monkeypatch, conn):
        monkeypatch.setattr(fx, '_fetch_fx_usd_per',
                            lambda ccy: 0.128 if ccy == 'HKD' else None)
        res = refresh_fx_rates(['HKD', 'JPY'])
        assert res['updated'] == 1 and res['failed'] == ['JPY']
        assert conn.execute('SELECT COUNT(*) FROM fx_rates').fetchone()[0] == 1


class TestFxStatus:
    def test_status_marks_live_vs_fallback(self, db, conn):
        conn.execute("INSERT INTO fx_rates (currency, usd_per, fetched_at) VALUES ('HKD', 0.129, '2026-06-08T10:00:00')")
        conn.commit()
        st = fx_status()
        assert st['provider'] == 'yfinance' and st['pivot'] == 'USD' and st['base'] == 'HKD'
        by = {r['currency']: r for r in st['rates']}
        assert by['HKD']['source'] == 'live' and by['HKD']['usd_per'] == pytest.approx(0.129)
        assert by['HKD']['fetched_at'] == '2026-06-08T10:00:00'
        assert by['JPY']['source'] == 'fallback'           # not cached → anchor
        assert by['USD']['source'] == 'live' and by['USD']['usd_per'] == 1.0

    def test_base_currency_setting(self, db, setval):
        assert get_base_currency() == 'HKD'                 # default
        setval('base_currency', 'AUD')
        assert get_base_currency() == 'AUD'
        setval('base_currency', 'bogus')
        assert get_base_currency() == 'HKD'                 # invalid → default

    def test_fx_route(self, client):
        d = client.get('/api/fx').get_json()
        assert d['provider'] == 'yfinance'
        assert [r['currency'] for r in d['rates']] == ['HKD', 'JPY', 'USD', 'AUD']


# ── compute_pnl (pure) ────────────────────────────────────────────────────────
class TestComputePnl:
    def _rows(self):
        return [
            {'market': 'hk', 'symbol': '00700.HK', 'name': 'Tencent', 'net_qty': 10, 'bought_price': 100.0},
            {'market': 'us', 'symbol': 'AAPL', 'name': 'Apple', 'net_qty': 2, 'bought_price': 50.0},
            {'market': 'hk', 'symbol': '00005.HK', 'name': 'HSBC', 'net_qty': 5, 'bought_price': 40.0},  # no price
            {'market': 'us', 'symbol': 'NOPE', 'name': 'x', 'net_qty': 0, 'bought_price': None},          # not held
        ]

    def _snaps(self):
        return {
            'hk:00700.HK': {'price': 120.0, 'currency': 'HKD'},
            'us:AAPL': {'price': 60.0, 'currency': 'usd'},   # lowercase normalizes
            'hk:00005.HK': {'price': None, 'currency': None},  # cache miss
        }

    def test_aggregates_in_base(self):
        # base HKD; USD→HKD = 1/0.13 ; HKD→HKD = 1
        rate = fx_converter('HKD', cache={'USD': (1.0, 't'), 'HKD': (0.13, 't')})
        out = compute_pnl(self._rows(), self._snaps(), rate, 'HKD')
        # HK 00700: (120-100)*10*1 = 200 ; US AAPL: (60-50)*2*(1/0.13) = 153.846
        assert out['markets']['hk']['pnl'] == pytest.approx(200.0)
        assert out['markets']['us']['pnl'] == pytest.approx(20 / 0.13)
        assert out['totals']['pnl'] == pytest.approx(200 + 20 / 0.13)
        # pnl_pct is currency-free
        assert out['markets']['us']['holdings'][0]['pnl_pct'] == pytest.approx(20.0)
        # 00005 has no cached price → incomplete, in missing, excluded from sums
        assert 'hk:00005.HK' in out['missing']
        assert out['markets']['hk']['incomplete'] == 1 and out['markets']['hk']['count'] == 2
        # the fully-sold / unheld row never appears
        assert all(h['symbol'] != 'NOPE' for h in out['markets'].get('us', {}).get('holdings', []))

    def test_only_held_markets_emitted(self):
        rate = fx_converter('HKD', cache={'HKD': (0.13, 't')})
        out = compute_pnl([self._rows()[0]], {'hk:00700.HK': {'price': 120.0, 'currency': 'HKD'}}, rate, 'HKD')
        assert list(out['markets'].keys()) == ['hk']     # jp/us/crypto absent

    # One holding per market/currency: HK→HKD, JP→JPY, US→USD, crypto→USD(lowercase).
    _USD_PER = {'HKD': (0.13, 't'), 'JPY': (0.0065, 't'), 'USD': (1.0, 't'), 'AUD': (0.70, 't')}
    _MULTI_ROWS = [
        {'market': 'hk', 'symbol': '00700.HK', 'name': 'Tencent', 'net_qty': 10, 'bought_price': 100.0},
        {'market': 'jp', 'symbol': '7203.T', 'name': 'Toyota', 'net_qty': 3, 'bought_price': 2000.0},
        {'market': 'us', 'symbol': 'AAPL', 'name': 'Apple', 'net_qty': 2, 'bought_price': 50.0},
        {'market': 'crypto', 'symbol': 'BTC', 'name': 'Bitcoin', 'net_qty': 0.5, 'bought_price': 30000.0},
    ]
    _MULTI_SNAPS = {
        'hk:00700.HK': {'price': 120.0, 'currency': 'HKD'},
        'jp:7203.T': {'price': 2500.0, 'currency': 'JPY'},
        'us:AAPL': {'price': 60.0, 'currency': 'usd'},      # lowercase normalizes
        'crypto:BTC': {'price': 40000.0, 'currency': 'usd'},
    }

    def test_conversion_spans_markets_and_currencies(self):
        # Every market converts from ITS OWN currency, not a shared one.
        out = compute_pnl(self._MULTI_ROWS, self._MULTI_SNAPS, fx_converter('HKD', cache=self._USD_PER), 'HKD')
        assert set(out['markets'].keys()) == {'hk', 'jp', 'us', 'crypto'}
        # HK already in base → 200 ; US 20 USD → 20/0.13 HKD ; JP 1500 JPY → 1500*(0.0065/0.13) HKD
        assert out['markets']['hk']['pnl'] == pytest.approx((120 - 100) * 10)
        assert out['markets']['us']['pnl'] == pytest.approx((60 - 50) * 2 * (1 / 0.13))
        assert out['markets']['jp']['pnl'] == pytest.approx((2500 - 2000) * 3 * (0.0065 / 0.13))
        assert out['markets']['crypto']['pnl'] == pytest.approx((40000 - 30000) * 0.5 * (1 / 0.13))

    def test_allocation_weights(self):
        # weight = value / total_value (base). Per-holding and per-market weights each sum
        # to 100, and a holding's weight matches value/total exactly. FX-invariant.
        out = compute_pnl(self._MULTI_ROWS, self._MULTI_SNAPS,
                          fx_converter('HKD', cache=self._USD_PER), 'HKD')
        total = out['totals']['value']
        market_w = sum(out['markets'][m]['weight'] for m in out['markets'])
        holding_w = sum(h['weight'] for m in out['markets'] for h in out['markets'][m]['holdings'])
        assert market_w == pytest.approx(100.0)
        assert holding_w == pytest.approx(100.0)
        # concrete: HK market value share
        assert out['markets']['hk']['weight'] == pytest.approx(out['markets']['hk']['value'] / total * 100)
        # FX-invariant: weights are identical in a different base
        out_usd = compute_pnl(self._MULTI_ROWS, self._MULTI_SNAPS,
                              fx_converter('USD', cache=self._USD_PER), 'USD')
        assert out_usd['markets']['us']['weight'] == pytest.approx(out['markets']['us']['weight'])

    def test_incomplete_holding_has_no_weight(self):
        # A cache-miss holding contributes no value → weight None, excluded from the 100%.
        rows = self._rows()           # includes 00005 with no cached price
        out = compute_pnl(rows, self._snaps(), fx_converter('HKD', cache={'HKD': (0.13, 't'), 'USD': (1.0, 't')}), 'HKD')
        incomplete = [h for m in out['markets'] for h in out['markets'][m]['holdings'] if not h['complete']]
        assert incomplete and all(h['weight'] is None for h in incomplete)
        live_w = sum(h['weight'] for m in out['markets'] for h in out['markets'][m]['holdings'] if h['complete'])
        assert live_w == pytest.approx(100.0)

    def test_local_and_base_amounts(self):
        # US holding (USD) with base HKD → native P&L in USD, base P&L = native × fx.
        rows = [{'market': 'us', 'symbol': 'AAPL', 'name': 'Apple', 'net_qty': 2, 'bought_price': 50.0}]
        snaps = {'us:AAPL': {'price': 60.0, 'currency': 'usd'}}
        out = compute_pnl(rows, snaps, fx_converter('HKD', cache={'USD': (1.0, 't'), 'HKD': (0.13, 't')}), 'HKD')
        h = out['markets']['us']['holdings'][0]
        assert h['pnl_local'] == pytest.approx(20.0)         # (60-50)*2 USD
        assert h['value_local'] == pytest.approx(120.0)      # 60*2 USD
        assert h['pnl'] == pytest.approx(20 / 0.13)          # base HKD
        mk = out['markets']['us']
        assert mk['currency'] == 'USD'
        assert mk['value_local'] == pytest.approx(120.0) and mk['pnl_local'] == pytest.approx(20.0)
        assert mk['pnl'] == pytest.approx(20 / 0.13)

    def test_local_equals_base_when_same_currency(self):
        # When the market currency IS the base, local and base amounts coincide (fx = 1).
        rows = [{'market': 'hk', 'symbol': '00700.HK', 'name': 'T', 'net_qty': 10, 'bought_price': 100.0}]
        snaps = {'hk:00700.HK': {'price': 120.0, 'currency': 'HKD'}}
        mk = compute_pnl(rows, snaps, fx_converter('HKD', cache={'HKD': (0.13, 't')}), 'HKD')['markets']['hk']
        assert mk['pnl_local'] == pytest.approx(mk['pnl'])
        assert mk['value_local'] == pytest.approx(mk['value'])

    def test_rebasing_is_consistent(self):
        # The same portfolio valued in HKD / USD / AUD must agree: the value-in-USD is
        # base-independent  →  pnl(base) * usd_per(base) is the SAME for every base.
        outs = {b: compute_pnl(self._MULTI_ROWS, self._MULTI_SNAPS,
                               fx_converter(b, cache=self._USD_PER), b)
                for b in ('HKD', 'USD', 'AUD')}
        usd_total = outs['USD']['totals']['pnl']            # USD base → already in USD
        for b in ('HKD', 'AUD'):
            assert outs[b]['totals']['pnl'] * self._USD_PER[b][0] == pytest.approx(usd_total)
        # per-market too, and pnl_pct is currency-free → identical across every base
        for m in ('hk', 'jp', 'us', 'crypto'):
            assert outs['HKD']['markets'][m]['pnl'] * 0.13 == pytest.approx(outs['USD']['markets'][m]['pnl'])
            pcts = {outs[b]['markets'][m]['holdings'][0]['pnl_pct'] for b in outs}
            assert len(pcts) == 1                            # one distinct value across all bases


# ── /api/portfolio/pnl route ──────────────────────────────────────────────────
# Module-level seed helpers so each test can build its own buy/sell scenario.
def _add(conn, market, symbol, name):
    conn.execute('INSERT INTO portfolio (symbol, market, name, "group", added_date) '
                 "VALUES (?,?,?,'Default','2026-01-01')", (symbol, market, name))

def _txn(conn, market, symbol, ttype, price, qty):
    conn.execute('INSERT INTO transactions (market, symbol, txn_type, price, quantity, txn_date, created_at) '
                 "VALUES (?,?,?,?,?, '2026-01-02', '2026-01-02T00:00:00')", (market, symbol, ttype, price, qty))

def _buy(conn, market, symbol, price, qty):  _txn(conn, market, symbol, 'buy', price, qty)
def _sell(conn, market, symbol, price, qty): _txn(conn, market, symbol, 'sell', price, qty)

def _ohlcv(conn, market, symbol, close, currency):
    conn.execute('INSERT INTO stock_ohlcv (source, market, symbol, timestamp, open, high, low, close, currency, fetched_at) '
                 "VALUES ('yfinance',?,?,?,?,?,?,?,?, '2026-06-08T00:00:00')",
                 (market, symbol, 1_700_000_000_000, close, close, close, close, currency))

def _fx(conn, **rates):
    for ccy, usd_per in rates.items():
        conn.execute('INSERT OR REPLACE INTO fx_rates (currency, usd_per, fetched_at) VALUES (?,?,?)',
                     (ccy, usd_per, 't'))


class TestPnlRoute:
    def _seed(self, conn):
        _add(conn, 'hk', '00700.HK', 'Tencent'); _buy(conn, 'hk', '00700.HK', 100.0, 10); _ohlcv(conn, 'hk', '00700.HK', 120.0, 'HKD')
        _add(conn, 'us', 'AAPL', 'Apple');       _buy(conn, 'us', 'AAPL', 50.0, 2);        _ohlcv(conn, 'us', 'AAPL', 60.0, 'USD')
        _fx(conn, HKD=0.13, USD=1.0)
        conn.commit()

    def test_pnl_base_hkd(self, client, conn):
        self._seed(conn)
        d = client.get('/api/portfolio/pnl').get_json()
        assert d['base'] == 'HKD'
        assert d['markets']['hk']['pnl'] == pytest.approx(200.0)             # (120-100)*10
        assert d['markets']['us']['pnl'] == pytest.approx(20 / 0.13)          # (60-50)*2 USD→HKD
        assert d['totals']['pnl'] == pytest.approx(200 + 20 / 0.13)
        assert d['missing'] == []

    def test_base_override(self, client, conn):
        self._seed(conn)
        d = client.get('/api/portfolio/pnl?base=USD').get_json()
        assert d['base'] == 'USD'
        # HK in USD: 200 HKD * 0.13 = 26 ; US stays 20 USD
        assert d['markets']['hk']['pnl'] == pytest.approx(200 * 0.13)
        assert d['markets']['us']['pnl'] == pytest.approx(20.0)

    def test_default_from_setting(self, client, conn, setval):
        self._seed(conn)
        setval('base_currency', 'USD')
        d = client.get('/api/portfolio/pnl').get_json()
        assert d['base'] == 'USD'

    # ── sell-ledger guards ────────────────────────────────────────────────────
    # The user holds buy-only positions TODAY, but these seed real SELL rows so the
    # net-qty / cost-basis math is exercised now — if a future change stops sells from
    # reducing the position, these FAIL instead of silently passing.
    def test_sell_reduces_net_qty(self, client, conn):
        _add(conn, 'hk', '00700.HK', 'Tencent')
        _buy(conn, 'hk', '00700.HK', 100.0, 10)
        _sell(conn, 'hk', '00700.HK', 150.0, 4)              # net = 10 − 4 = 6 still held
        _ohlcv(conn, 'hk', '00700.HK', 120.0, 'HKD')
        conn.commit()
        d = client.get('/api/portfolio/pnl?base=HKD').get_json()  # HKD↔HKD is identity → no fx needed
        h = d['markets']['hk']['holdings'][0]
        assert h['qty'] == 6                                  # buys − sells, NOT 10
        assert h['avg_cost'] == pytest.approx(100.0)          # avg BUY cost; the sell doesn't move it
        # unrealized P&L is on the 6 STILL HELD: (120−100)*6 = 120 — would be 200 if sells were ignored
        assert h['pnl'] == pytest.approx(120.0)
        assert d['totals']['pnl'] == pytest.approx(120.0)
        assert d['totals']['count'] == 1

    def test_fully_sold_position_excluded(self, client, conn):
        _add(conn, 'hk', '00005.HK', 'HSBC')
        _buy(conn, 'hk', '00005.HK', 40.0, 5)
        _sell(conn, 'hk', '00005.HK', 60.0, 5)               # net = 0 → no current position
        _ohlcv(conn, 'hk', '00005.HK', 70.0, 'HKD')
        conn.commit()
        d = client.get('/api/portfolio/pnl').get_json()
        assert 'hk' not in d['markets']                       # the only position is fully sold
        assert d['totals']['count'] == 0 and d['totals']['pnl'] == 0
        assert d['missing'] == []

    def test_partial_then_more_buys_weighted_avg(self, client, conn):
        # Two buys at different prices + a sell: avg cost is the weighted BUY average over
        # ALL buys; net qty nets the sell. Locks the interaction of weighted-avg + sells.
        _add(conn, 'us', 'AAPL', 'Apple')
        _buy(conn, 'us', 'AAPL', 100.0, 10)
        _buy(conn, 'us', 'AAPL', 200.0, 10)                  # avg buy = (100*10+200*10)/20 = 150
        _sell(conn, 'us', 'AAPL', 250.0, 5)                  # net = 20 − 5 = 15
        _ohlcv(conn, 'us', 'AAPL', 180.0, 'USD')
        _fx(conn, USD=1.0)
        conn.commit()
        h = client.get('/api/portfolio/pnl?base=USD').get_json()['markets']['us']['holdings'][0]
        assert h['qty'] == 15
        assert h['avg_cost'] == pytest.approx(150.0)
        assert h['pnl'] == pytest.approx((180 - 150) * 15)    # 450 on the 15 held

    def test_instrument_in_multiple_groups_counted_once(self, client, conn):
        # Same instrument filed under two groups → two portfolio rows. P&L must count it
        # ONCE (not once per group) — value/qty/P&L are per instrument, not per row.
        _add(conn, 'us', 'CRWV', 'CoreWeave')              # group 'Default'
        conn.execute('INSERT INTO portfolio (symbol, market, name, "group", added_date) '
                     "VALUES ('CRWV','us','CoreWeave','Growth','2026-01-01')")  # second group
        _buy(conn, 'us', 'CRWV', 100.0, 100)
        _ohlcv(conn, 'us', 'CRWV', 120.0, 'USD')
        _fx(conn, USD=1.0)
        conn.commit()
        d = client.get('/api/portfolio/pnl?base=USD').get_json()
        assert len(d['markets']['us']['holdings']) == 1     # NOT 2
        assert d['totals']['count'] == 1
        h = d['markets']['us']['holdings'][0]
        assert h['qty'] == 100                              # qty not doubled
        assert d['markets']['us']['pnl'] == pytest.approx((120 - 100) * 100)  # 2000, not 4000

    def test_multi_group_notes_combined(self, client, conn):
        # A deduped holding spanning two groups merges their (distinct, non-blank) notes.
        conn.execute('INSERT INTO portfolio (symbol, market, name, "group", added_date, comment) '
                     "VALUES ('CRWV','us','CoreWeave','TechnicalPattern','2026-01-01','AI play')")
        conn.execute('INSERT INTO portfolio (symbol, market, name, "group", added_date, comment) '
                     "VALUES ('CRWV','us','CoreWeave','Growth','2026-01-01','high beta')")
        _buy(conn, 'us', 'CRWV', 100.0, 100); _ohlcv(conn, 'us', 'CRWV', 120.0, 'USD'); _fx(conn, USD=1.0)
        conn.commit()
        h = client.get('/api/portfolio/pnl?base=USD').get_json()['markets']['us']['holdings'][0]
        assert 'AI play' in h['comment'] and 'high beta' in h['comment']

    # ── end-to-end multi-currency rebasing (#3 / #3.1) ────────────────────────
    def test_rebasing_consistent_multimarket(self, client, conn):
        # HK(HKD) + JP(JPY) + US(USD) through the REAL snapshot + FX path: the value-in-USD
        # must be invariant to the chosen base, and pnl_pct must match across bases.
        _add(conn, 'hk', '00700.HK', 'Tencent'); _buy(conn, 'hk', '00700.HK', 100.0, 10); _ohlcv(conn, 'hk', '00700.HK', 120.0, 'HKD')
        _add(conn, 'jp', '7203.T', 'Toyota');    _buy(conn, 'jp', '7203.T', 2000.0, 3);   _ohlcv(conn, 'jp', '7203.T', 2500.0, 'JPY')
        _add(conn, 'us', 'AAPL', 'Apple');       _buy(conn, 'us', 'AAPL', 50.0, 2);       _ohlcv(conn, 'us', 'AAPL', 60.0, 'USD')
        usd_per = {'HKD': 0.13, 'JPY': 0.0065, 'USD': 1.0, 'AUD': 0.70}
        _fx(conn, **usd_per)
        conn.commit()
        res = {b: client.get(f'/api/portfolio/pnl?base={b}').get_json() for b in ('HKD', 'USD', 'AUD')}
        assert all(set(res[b]['markets']) == {'hk', 'jp', 'us'} for b in res)
        # total value-in-USD is base-independent: pnl(base) * usd_per(base) == pnl(USD)
        usd_total = res['USD']['totals']['pnl']
        for b in ('HKD', 'AUD'):
            assert res[b]['totals']['pnl'] * usd_per[b] == pytest.approx(usd_total)
        # currency-free pnl_pct matches across every base, per market
        for m in ('hk', 'jp', 'us'):
            pcts = {round(res[b]['markets'][m]['holdings'][0]['pnl_pct'], 9) for b in res}
            assert len(pcts) == 1
