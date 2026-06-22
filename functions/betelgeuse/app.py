from flask import Flask, render_template, jsonify, request, send_from_directory
import requests
import os
import sqlite3
import time
from datetime import datetime
import json
import io
import openpyxl
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
import matplotlib
matplotlib.use('Agg')
import mplfinance as mpf
import pandas as pd

app = Flask(__name__, static_folder='static')

# Domain layer (Flask-free) lives in core/. Re-export the pieces app.py uses so
# existing `app.<name>` references (and the test suite) keep resolving.
from core.config import (
    MARKETS, GROUP_OPTIONS, BACKTEST_PATTERNS, COMMON_CRYPTO_IDS, CRYPTO_PERIODS, STOCK_PERIODS,
    HKEX_SECURITIES_URL, NASDAQ_LISTED_URL, US_OTHER_LISTED_URL, US_EXCHANGE_BOARDS,
    US_BOARD_TOKENS, STATIC_DATA_MIN_ROWS, StaticDataDownloadError, CoinGeckoMappingError,
)
from core.db import DATABASE, get_db_connection, init_db, get_db_meta, set_db_meta, DB_SCHEMA_VERSION
from core.symbols import normalize_symbol, _us_base_symbol
from core.notifications import (
    scheduler, send_telegram_message, _build_portfolio_message, _record_portfolio_sent,
    send_scheduled_portfolio_notification, _compute_schedule_next_runs,
    reschedule_portfolio_notifications,
)
from core.crypto import (
    get_coingecko_config, coingecko_symbol_to_id, coingecko_id_to_symbol, get_coingecko_id,
    fetch_and_store_crypto_ohlcv, _crypto_ohlcv_is_fresh, _load_crypto_ohlcv_rows,
    _load_crypto_ohlcv_series, _pct_change, _has_user_coingecko_id,
)
from core.marketdata import (
    MarketDataProvider, CoinGeckoProvider, YFinanceProvider, MarketDataManager,
    MARKET_DATA_PROVIDERS, market_data, DEFAULT_PERFORMANCE_PERIODS,
    generate_crypto_chart, generate_stock_chart, prefetch_market_data, _provider_instrument_key,
    set_rebuild_request, read_rebuild_status, get_backload_start_date, read_jobs_status,
)
from core.staticdata import (
    download_aa_stocks_chart, resolve_static_url, download_hk_securities,
    download_us_securities, download_coingecko_coins, _fetch_url_text, _parse_nasdaq_symbol_file,
)
from core.fx import BASE_CURRENCIES, get_base_currency, fx_converter, fx_status, _norm_ccy
from core.pnl import compute_pnl
from core import health, runtime, config
from core.version import WEB_VERSION, app_version_string, server_version_string
from core.logging_setup import configure_logging, get_logger

logger = get_logger('web')

# Runtime mode (dev|prod). The default keeps imports/tests working; the entrypoints
# (app.py / serve.py __main__) override it from the mandatory --env arg before serving.
APP_ENV = 'dev'

# When this web process started (epoch seconds). Surfaced via /api/health as
# web.started_at so a dev instance probing prod can show "when did prod last restart".
APP_START_TIME = time.time()

# Where dev looks up prod's host when no `prod_base_url` setting is configured. This is
# the same canonical file the deploy script reads, so the two never drift. Git-ignored +
# not rsynced, so it only exists on the dev machine — exactly where the probe runs.
# Module-level so tests can monkeypatch it to a tmp path.
DEPLOY_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'deploy', 'config.sh')


@app.context_processor
def inject_app_meta():
    """Expose runtime mode + web version to every template (drives header branding)."""
    return {'app_env': APP_ENV, 'web_version': WEB_VERSION}


# DB schema gate. 'OK' lets the app serve normally; 'NEEDS_UP'/'DB_NEWER' put the web
# process into a maintenance mode (see _enforce_migration_gate). It is computed once at
# startup by the entrypoints (refresh_migration_gate, after init_db) and recomputed after
# a successful migration via the dev panel. Default 'OK' so the test client — which never
# runs an entrypoint — is never gated. The worker has no UI: it hard-exits on a mismatch.
MIGRATION_GATE = 'OK'

# Endpoints that must stay reachable while the gate is closed: the migration API (so the
# dev panel can fix it) and static assets (so the maintenance page can style itself).
_GATE_OPEN_PREFIXES = ('/static/', '/api/admin/migrate')


def refresh_migration_gate():
    """Recompute the schema gate from the live DB. Returns the new state."""
    global MIGRATION_GATE
    from core import migrate
    conn = get_db_connection()
    try:
        MIGRATION_GATE = migrate.gate_state(conn)
    finally:
        conn.close()
    return MIGRATION_GATE


@app.before_request
def _enforce_migration_gate():
    """When the DB version ≠ the code's, refuse to serve the normal app (the user's
    'refuse to start' choice). The process stays up serving ONLY the migration surface,
    so the dev panel can still drive the upgrade; everything else returns 503."""
    if MIGRATION_GATE == 'OK':
        return None
    path = request.path
    if any(path.startswith(p) for p in _GATE_OPEN_PREFIXES):
        return None
    from core import migrate
    conn = get_db_connection()
    try:
        st = migrate.status(conn)
    finally:
        conn.close()
    if path.startswith('/api/'):
        return jsonify({'error': 'Database migration required', 'gate': MIGRATION_GATE, **st}), 503
    return render_template('migrate_gate.html', gate=MIGRATION_GATE, status=st), 503


@app.route('/')
def index():
    """Home — the consolidated portfolio overview (all markets in one register)."""
    return render_template('overview.html')

@app.route('/tracker')
def tracker():
    """Legacy per-market card-grid tracker. Superseded by the overview at '/'; kept as a fallback."""
    return render_template('index.html', markets=list(MARKETS.keys()))

@app.route('/portfolio')
def portfolio():
    return render_template('portfolio.html')

@app.route('/groups')
def groups():
    return render_template('groups.html')

@app.route('/settings')
def settings():
    return render_template('settings.html')

@app.route('/api/health')
def api_health():
    """Application health: runtime mode, web version, and worker liveness.

    Powers Settings -> Admin -> Application Health. Worker readiness comes from the
    DB heartbeat (core.health), so it reflects the separate worker process's state.
    """
    from core import migrate
    conn = get_db_connection()
    try:
        mig = migrate.status(conn)
    finally:
        conn.close()
    return jsonify({
        'env': APP_ENV,
        'server_time': int(time.time() * 1000),
        # raw WEB_VERSION kept for back-compat; *_label are the betelgeuse-app/-server display strings.
        'versions': {'app': app_version_string(), 'server': server_version_string()},
        'web': {'version': WEB_VERSION, 'started_at': int(APP_START_TIME * 1000)},
        'worker': health.worker_status(),
        'db': {
            'version': get_db_meta('version'),
            'description': get_db_meta('description'),
            # schema gate so a card can show "up to date" vs "N pending" on dev AND prod
            'schema_version': mig['current'],
            'schema_head': mig['head'],
            'schema_gate': mig['gate'],
            'pending_count': len(mig['pending']),
        },
    })


def _parse_deploy_config(text):
    """Parse the shell `deploy/config.sh` into a dict of its KEY=value assignments.

    Pure (string in, dict out) so it is unit-testable without the filesystem. Handles
    `KEY="value"`, `KEY='value'` and bare `KEY=value`; ignores comments and blank lines;
    strips an inline `# ...` comment off bare values. Not a real shell parser — just the
    flat `KEY=...` lines this file is made of.
    """
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value[:1] in ('"', "'"):
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            value = value.split('#', 1)[0].strip()
        out[key] = value
    return out


def get_prod_base_url():
    """Resolve the base URL of the prod instance for dev's health probe, or None.

    Precedence: the `prod_base_url` setting (an explicit override — e.g. an IP/tunnel when
    off-LAN) wins when set; otherwise fall back to the mini's host from `deploy/config.sh`
    (`http://{MINI_HOST}:{PORT}`), the same canonical source the deploy script uses. Returns
    None when neither is available (e.g. on the prod box itself, where config.sh isn't present).
    """
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key = 'prod_base_url'")
        row = c.fetchone()
    finally:
        conn.close()
    if row and row['value'] and row['value'].strip():
        return row['value'].strip().rstrip('/')

    try:
        with open(DEPLOY_CONFIG_PATH, 'r') as f:
            cfg = _parse_deploy_config(f.read())
    except OSError:
        return None
    host = cfg.get('MINI_HOST')
    if not host:
        return None
    port = cfg.get('PORT') or '8000'
    return f'http://{host}:{port}'


@app.route('/api/prod/health')
def api_prod_health():
    """Probe prod's /api/health server-side (dev → prod over the LAN).

    Done on the server, not the browser, to avoid CORS and keep the prod host out of
    client code. The dev UI polls this every 60s for the title-bar version chip + the
    Application Health "Prod" cards; it tolerates prod being down (reachable:false) and
    just keeps polling. Returns configured:false when there is no prod target (e.g. on
    the prod box itself), so callers can distinguish "unconfigured" from "down".
    """
    base = get_prod_base_url()
    if not base:
        return jsonify({'configured': False})
    probed_at = int(time.time() * 1000)
    try:
        resp = requests.get(base + '/api/health', timeout=3)
        return jsonify({
            'configured': True, 'reachable': True, 'base_url': base,
            'probed_at': probed_at, 'health': resp.json(),
        })
    except Exception as e:
        return jsonify({
            'configured': True, 'reachable': False, 'base_url': base,
            'probed_at': probed_at, 'error': str(e),
        })

@app.route('/training')
def training():
    return render_template('training.html', markets=list(MARKETS.keys()))

@app.route('/api/market/<market>')
def get_market(market):
    if market not in MARKETS:
        return jsonify({'error': 'Market not found'}), 404
    
    # Get stocks from portfolio database
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM portfolio WHERE market = ? ORDER BY symbol', (market,))
    rows = c.fetchall()
    conn.close()
    
    stocks = []
    for row in rows:
        stocks.append({
            'id': row['id'],
            'symbol': row['symbol'],
            'market': row['market'],
            'name': row['name'],
            'group': row['group'],
            'price': 0,
            'change': 0,
            'stockid': row['symbol'],
            'comment': row['comment']
        })
    
    return jsonify({
        'name': MARKETS[market]['name'],
        'stocks': stocks
    })

@app.route('/api/markets')
def get_markets():
    return jsonify({key: {'name': MARKETS[key]['name']} for key in MARKETS})

@app.route('/charts/<path:filename>')
def serve_chart(filename):
    """Serve a generated chart PNG/GIF from the runtime data dir (config.CHART_DIR).

    Charts are runtime artifacts that live OUTSIDE the code tree (under DATA_DIR), so
    they can't ride Flask's repo-bound /static mount (which serves the committed JS).
    send_from_directory guards against path traversal.
    """
    return send_from_directory(config.CHART_DIR, filename)

@app.route('/api/stock/<stockid>/chart')
def get_stock_chart(stockid):
    """Get chart for a stock"""
    chart_file = download_aa_stocks_chart(stockid)
    if chart_file:
        return jsonify({'url': f'{request.script_root}/charts/{chart_file}'})
    return jsonify({'error': 'Could not download chart'}), 404

@app.route('/crypto')
def crypto_page():
    # Crypto renders inline in the tracker (index.html) with the crypto tab preselected.
    return render_template('index.html', markets=list(MARKETS.keys()), initial_market='crypto')

@app.route('/api/crypto/coins')
def get_crypto_coins():
    """List all crypto items from portfolio, each with their resolved coingecko_id."""
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('SELECT symbol, name, "group" FROM portfolio WHERE market=? ORDER BY symbol', ('crypto',))
        rows = c.fetchall()
    finally:
        conn.close()
    coins = []
    for r in rows:
        sym = r['symbol']
        coins.append({
            'symbol': sym,
            'name': r['name'],
            'group': r['group'],
            'coingecko_id': get_coingecko_id(sym),
            'coingecko_id_source': 'user' if _has_user_coingecko_id(sym) else ('common' if sym.upper() in COMMON_CRYPTO_IDS else None),
        })
    return jsonify(coins)

@app.route('/api/crypto/<symbol>/coingecko-id', methods=['POST'])
def set_coingecko_id(symbol):
    """Save user-defined CoinGecko coin ID for a symbol."""
    body = request.json or {}
    coin_id = (body.get('coingecko_id') or '').strip().lower()
    if not coin_id:
        return jsonify({'error': 'coingecko_id is required'}), 400
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)',
                  (f'coingecko_id_{symbol.upper()}', coin_id))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'success': True, 'coingecko_id': coin_id})

@app.route('/api/crypto/<coin_id>/chart/<period>')
def get_crypto_chart(coin_id, period):
    """Return URL of the rendered chart PNG, generating it if needed."""
    if period not in CRYPTO_PERIODS:
        return jsonify({'error': 'Invalid period. Use 7, 30, 90, or 365'}), 400
    force = request.args.get('refresh') == '1'
    filename = generate_crypto_chart(coin_id, period, force=force)
    if filename:
        return jsonify({'url': f'{request.script_root}/charts/{filename}', 'coin_id': coin_id, 'period': period})
    return jsonify({'error': f'Could not fetch data for {coin_id}'}), 404

@app.route('/api/stock/<market>/<symbol>/chart/<period>')
def get_stock_chart_yf(market, symbol, period):
    """Return URL of the rendered yfinance candlestick PNG for HK/JP/US.

    No collision with the existing /api/stock/<stockid>/chart HK AA-Stocks route
    because this route has four segments vs three.
    """
    market = (market or '').strip().lower()
    if market not in MARKETS:
        return jsonify({'error': 'Market not found'}), 404
    if period not in STOCK_PERIODS:
        return jsonify({'error': f'Invalid period. Use {", ".join(STOCK_PERIODS)}'}), 400
    canonical = normalize_symbol(symbol, market)
    force = request.args.get('refresh') == '1'
    filename = generate_stock_chart(market, canonical, period, force=force)
    if filename:
        return jsonify({'url': f'{request.script_root}/charts/{filename}', 'market': market,
                        'symbol': canonical, 'period': period})
    return jsonify({'error': f'Could not fetch data for {canonical}'}), 404


@app.route('/api/crypto/<coin_id>/ohlcv')
def get_crypto_ohlcv(coin_id):
    """Return stored OHLCV rows for a coin (for analysis / export)."""
    period = request.args.get('period')
    conn = get_db_connection()
    try:
        c = conn.cursor()
        if period:
            c.execute(
                'SELECT source, period, timestamp, open, high, low, close, fetched_at FROM crypto_ohlcv '
                'WHERE coin_id=? AND period=? ORDER BY timestamp',
                (coin_id, period)
            )
        else:
            c.execute(
                'SELECT source, period, timestamp, open, high, low, close, fetched_at FROM crypto_ohlcv '
                'WHERE coin_id=? ORDER BY period, timestamp',
                (coin_id,)
            )
        rows = [dict(r) for r in c.fetchall()]
    finally:
        conn.close()
    return jsonify({'coin_id': coin_id, 'count': len(rows), 'rows': rows})


# ── Market Data admin endpoints (Settings → Admin → Market Data) ──
@app.route('/api/admin/market-data/markets', methods=['GET'])
def market_data_markets():
    """Per-market provider catalog + active provider — drives the panel + dropdowns."""
    return jsonify({'markets': market_data.markets()})


@app.route('/api/admin/market-data/<market>/status', methods=['GET'])
def market_data_status(market):
    """Cache statistics for a market's (active or ?provider=) provider, plus server_time."""
    provider = (request.args.get('provider') or '').strip() or None
    return jsonify(market_data.status(market, provider))


@app.route('/api/admin/market-data/<market>/instruments', methods=['GET'])
def market_data_instruments(market):
    """Paginated per-instrument cache inspector (search via ?q=)."""
    provider = (request.args.get('provider') or '').strip() or None
    q = (request.args.get('q') or '').strip()
    try:
        page = max(1, int(request.args.get('page', 1)))
        per_page = min(200, max(10, int(request.args.get('per_page', 50))))
    except ValueError:
        page, per_page = 1, 50
    return jsonify(market_data.list_cached(market, provider, q=q, page=page, per_page=per_page))


@app.route('/api/admin/market-data/<market>/provider', methods=['POST'])
def market_data_set_provider(market):
    """Set the active provider for a market."""
    data = request.get_json(silent=True) or {}
    key = (data.get('provider') or '').strip()
    try:
        market_data.set_active(market, key)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    return jsonify({'success': True, 'market': market, 'active_provider': key})


@app.route('/api/admin/market-data/<market>/clear', methods=['POST'])
def market_data_clear(market):
    """Clear a market's cached data (DB rows + any derived render artifacts)."""
    provider = (request.args.get('provider') or '').strip() or None
    result = market_data.clear(market, provider)
    if not result.get('available'):
        return jsonify({'success': False, 'error': 'No provider for this market'}), 400
    return jsonify({'success': True, **result})


@app.route('/api/admin/market-data/<market>/rebuild', methods=['POST'])
def market_data_rebuild(market):
    """Queue a full cache rebuild for a market+provider (clear → backload to start date).

    The web process only sets a request flag + initial status in the DB; the *worker*
    picks it up on its poll loop and runs the (long) job on its threadpool, writing
    progress back. Allowed on dev AND prod (it's an operational refresh, not a schema
    mutation) precisely because the web process never does the heavy work itself.
    """
    prov = market_data.get(market, (request.args.get('provider') or '').strip() or None)
    if not prov:
        return jsonify({'success': False, 'error': 'No provider for this market'}), 400
    set_rebuild_request(market, prov.key)
    return jsonify({'success': True, 'queued': True, 'market': market,
                    'provider': prov.key, 'start_date': get_backload_start_date()})


@app.route('/api/admin/market-data/<market>/rebuild/status', methods=['GET'])
def market_data_rebuild_status(market):
    """Current rebuild progress/status for a market ({'state':'idle'} when none)."""
    return jsonify(read_rebuild_status(market))


@app.route('/api/admin/market-data/jobs', methods=['GET'])
def market_data_jobs():
    """Status of the worker's recurring background jobs (price prefetch + history
    backfill): {name: {state, last_started, last_finished, last_result, next_run}}.
    These jobs are global (they sweep the whole portfolio), so the route isn't
    per-market. Drives the 'Background reload' indicator in the Market Data panel."""
    return jsonify(read_jobs_status())


def save_backtest_chart(market, pattern, stockid, chart_type, url):
    """Download a chart from the given URL and save it under the backtest folder, overwriting."""
    date_str = datetime.now().strftime('%Y%m%d')
    dir_path = os.path.join(config.BACKTEST_DIR, 'training', market, pattern)
    os.makedirs(dir_path, exist_ok=True)
    safe_stockid = stockid.replace('/', '').replace('\\', '')
    filename = f"{safe_stockid}-{date_str}-{chart_type}.gif"
    filepath = os.path.join(dir_path, filename)
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            with open(filepath, 'wb') as f:
                f.write(resp.content)
            return filename
    except Exception as e:
        logger.error("Error saving backtest chart: %s", e)
    return None

@app.route('/api/backtest/save', methods=['POST'])
def save_backtest():
    """Save selected charts for a stock/pattern to the backtest training folder."""
    data = request.json or {}
    market = data.get('market')
    pattern = data.get('pattern')
    stockid = (data.get('stockid') or '').strip()
    chart_types = data.get('chart_types', [])
    if market not in MARKETS:
        return jsonify({'error': 'Market not found'}), 404
    if pattern not in BACKTEST_PATTERNS:
        return jsonify({'error': 'Invalid pattern'}), 400
    if not stockid or not chart_types:
        return jsonify({'error': 'stockid and at least one chart are required'}), 400
    stockid = normalize_symbol(stockid, market)
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute('SELECT key, value FROM settings')
        templates = {row['key']: row['value'] for row in c.fetchall()}
    finally:
        conn.close()
    saved, failed = [], []
    for t in chart_types:
        template = templates.get(f'url_template_{t}')
        if not template:
            failed.append(t)
            continue
        url = template.replace('{stockid}', stockid)
        fn = save_backtest_chart(market, pattern, stockid, t, url)
        (saved if fn else failed).append(t)
    return jsonify({'success': True, 'saved': saved, 'failed': failed})

# Portfolio API endpoints
@app.route('/api/portfolio', methods=['GET'])
def get_portfolio():
    """Get all portfolio items grouped by market.

    Each item carries the new watch fields (monitor_price/trigger_price/bought) plus a
    derived `bought_price` (weighted-average buy cost), `bought_qty` (total bought, buys
    only) and `net_qty` (current net position = buys − sells), computed by joining the
    transactions ledger on the canonical (market, symbol) pair. bought_price/bought_qty are
    None when there are no buy transactions; net_qty is None when there are no transactions
    at all (and can be 0 once a position is fully sold).
    """
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM portfolio ORDER BY market, symbol')
    rows = c.fetchall()

    # Weighted-average cost basis per instrument, over buy rows only (sells come later).
    c.execute('''
        SELECT market, symbol,
               SUM(price * quantity) / NULLIF(SUM(quantity), 0) AS avg_cost,
               SUM(quantity) AS qty
        FROM transactions
        WHERE txn_type = 'buy'
        GROUP BY market, symbol
    ''')
    cost = {(r['market'], r['symbol']): r for r in c.fetchall()}

    # Current net position per instrument = buys minus sells (what you actually still hold).
    c.execute('''
        SELECT market, symbol,
               SUM(CASE WHEN txn_type = 'buy' THEN quantity ELSE -quantity END) AS net_qty
        FROM transactions
        GROUP BY market, symbol
    ''')
    net = {(r['market'], r['symbol']): r['net_qty'] for r in c.fetchall()}
    conn.close()

    portfolio = {
        'hk': [],
        'jp': [],
        'us': [],
        'crypto': []
    }

    for row in rows:
        cb = cost.get((row['market'], row['symbol']))
        item = {
            'id': row['id'],
            'symbol': row['symbol'],
            'market': row['market'],
            'name': row['name'],
            'group': row['group'],
            'added_date': row['added_date'],
            'comment': row['comment'],
            'monitor_price': row['monitor_price'],
            'trigger_price': row['trigger_price'],
            'bought': bool(row['bought']),
            'bought_price': cb['avg_cost'] if cb else None,
            'bought_qty': cb['qty'] if cb else None,
            'net_qty': net.get((row['market'], row['symbol'])),
        }
        portfolio[row['market']].append(item)

    return jsonify(portfolio)

def _opt_price(value):
    """Coerce an optional user-entered price to float, or None when blank/invalid.
    Used for monitor_price / trigger_price (both nullable watch levels)."""
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@app.route('/api/portfolio', methods=['POST'])
def add_portfolio_item():
    """Add new portfolio item"""
    data = request.json

    # Validate market
    if data.get('market') not in ['hk', 'jp', 'us', 'crypto']:
        return jsonify({'error': 'Invalid market'}), 400

    # Validate symbol format
    market = data.get('market')

    if not data.get('symbol', '').strip():
        return jsonify({'error': 'Symbol is required'}), 400

    symbol = normalize_symbol(data.get('symbol'), market)

    conn = get_db_connection()
    c = conn.cursor()

    try:
        group = data.get('group', 'Default')
        valid_groups = get_market_groups(market, c)
        if group not in valid_groups:
            return jsonify({'error': 'Invalid group value'}), 400

        c.execute('''
            INSERT INTO portfolio (symbol, market, name, "group", added_date, comment,
                                   monitor_price, trigger_price, bought)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            symbol,
            market,
            data.get('name', symbol),
            group,
            data.get('added_date', datetime.now().strftime('%Y-%m-%d')),
            data.get('comment', ''),
            _opt_price(data.get('monitor_price')),
            _opt_price(data.get('trigger_price')),
            1 if data.get('bought') else 0,
        ))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'id': c.lastrowid}), 201
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 400

@app.route('/api/portfolio/<int:item_id>', methods=['PUT'])
def update_portfolio_item(item_id):
    """Update portfolio item"""
    data = request.json
    
    conn = get_db_connection()
    c = conn.cursor()
    
    try:
        if 'group' in data:
            group = data.get('group')
            market = data.get('market')
            valid_groups = get_market_groups(market, c)
            if group not in valid_groups:
                return jsonify({'error': 'Invalid group value'}), 400

        c.execute('''
            UPDATE portfolio
            SET name = ?, comment = ?, added_date = ?, "group" = ?,
                monitor_price = ?, trigger_price = ?, bought = ?
            WHERE id = ?
        ''', (
            data.get('name'),
            data.get('comment', ''),
            data.get('added_date'),
            data.get('group', 'Default'),
            _opt_price(data.get('monitor_price')),
            _opt_price(data.get('trigger_price')),
            1 if data.get('bought') else 0,
            item_id
        ))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 400

@app.route('/api/portfolio/<int:item_id>', methods=['DELETE'])
def delete_portfolio_item(item_id):
    """Delete portfolio item, along with its transaction ledger.

    SQLite foreign keys are off by default, so the (market, symbol)-keyed transactions
    are cleaned up explicitly here — otherwise re-adding the same instrument later would
    silently resurrect stale lots.

    BUT the same instrument can be filed under several groups (multiple portfolio rows
    share one (market, symbol)), while transactions are keyed only on (market, symbol).
    So purge the ledger ONLY when this was the LAST row for that instrument — otherwise
    deleting one grouping would orphan the surviving row's holding (its lots would vanish
    and it would drop out of the P&L).
    """
    conn = get_db_connection()
    c = conn.cursor()

    try:
        c.execute('SELECT market, symbol FROM portfolio WHERE id = ?', (item_id,))
        row = c.fetchone()
        c.execute('DELETE FROM portfolio WHERE id = ?', (item_id,))
        if row:
            c.execute('SELECT COUNT(*) AS n FROM portfolio WHERE market = ? AND symbol = ?',
                      (row['market'], row['symbol']))
            if c.fetchone()['n'] == 0:
                c.execute('DELETE FROM transactions WHERE market = ? AND symbol = ?',
                          (row['market'], row['symbol']))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 400


# ── Transactions (buy/sell ledger, keyed by canonical market+symbol) ──

@app.route('/api/portfolio/<market>/<symbol>/transactions', methods=['GET'])
def get_transactions(market, symbol):
    """List transactions for an instrument, oldest first."""
    market = (market or '').strip().lower()
    if market not in MARKETS:
        return jsonify({'error': 'Invalid market'}), 400
    canonical = normalize_symbol(symbol, market)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        SELECT id, market, symbol, txn_type, price, quantity, txn_date, created_at
        FROM transactions WHERE market = ? AND symbol = ?
        ORDER BY txn_date, id
    ''', (market, canonical))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route('/api/portfolio/<market>/<symbol>/transactions', methods=['POST'])
def add_transaction(market, symbol):
    """Record a transaction (buy now; sell supported by the schema for later)."""
    market = (market or '').strip().lower()
    if market not in MARKETS:
        return jsonify({'error': 'Invalid market'}), 400
    canonical = normalize_symbol(symbol, market)

    data = request.json or {}
    txn_type = (data.get('txn_type') or 'buy').strip().lower()
    if txn_type not in ('buy', 'sell'):
        return jsonify({'error': 'Invalid transaction type'}), 400

    try:
        price = float(data.get('price'))
        quantity = float(data.get('quantity'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Price and quantity are required numbers'}), 400
    if price <= 0 or quantity <= 0:
        return jsonify({'error': 'Price and quantity must be positive'}), 400

    txn_date = (data.get('txn_date') or datetime.now().strftime('%Y-%m-%d')).strip()

    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute('''
            INSERT INTO transactions (market, symbol, txn_type, price, quantity, txn_date)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (market, canonical, txn_type, price, quantity, txn_date))
        conn.commit()
        new_id = c.lastrowid
        conn.close()
        return jsonify({'success': True, 'id': new_id}), 201
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 400


@app.route('/api/portfolio/transactions/<int:txn_id>', methods=['DELETE'])
def delete_transaction(txn_id):
    """Remove a single transaction."""
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute('DELETE FROM transactions WHERE id = ?', (txn_id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 400


# Settings API endpoints
@app.route('/api/settings', methods=['GET'])
def get_settings():
    """Get all settings"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT key, value FROM settings')
    rows = c.fetchall()
    conn.close()
    
    settings = {}
    for row in rows:
        settings[row['key']] = row['value']
    
    return jsonify(settings)

@app.route('/api/settings', methods=['POST'])
def save_settings():
    """Save settings"""
    data = request.json
    conn = get_db_connection()
    c = conn.cursor()

    try:
        for key, value in data.items():
            c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
        conn.commit()
        conn.close()
        # Re-evaluate schedule whenever settings change (timezone or schedule keys may have changed)
        reschedule_portfolio_notifications()
        return jsonify({'success': True})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 400

def get_market_groups(market, cursor):
    """Get current active groups for a market, initializing from defaults if not yet stored."""
    cursor.execute('SELECT value FROM settings WHERE key = ?', (f'groups_{market}',))
    row = cursor.fetchone()
    if row and row['value']:
        return [g for g in row['value'].split(',') if g]
    return list(GROUP_OPTIONS.get(market, []))

# Group management API endpoints
@app.route('/api/groups/<market>', methods=['GET'])
def get_groups(market):
    """Get current group options for a market"""
    if market not in MARKETS:
        return jsonify({'error': 'Market not found'}), 404

    conn = get_db_connection()
    c = conn.cursor()
    try:
        return jsonify(get_market_groups(market, c))
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    finally:
        conn.close()

@app.route('/api/groups/<market>', methods=['POST'])
def save_groups(market):
    """Save group options for a market"""
    if market not in MARKETS:
        return jsonify({'error': 'Market not found'}), 404

    data = request.json
    groups = data.get('groups', [])

    if not isinstance(groups, list):
        return jsonify({'error': 'Groups must be a list'}), 400

    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                  (f'groups_{market}', ','.join(groups)))
        conn.commit()
        return jsonify({'success': True, 'groups': groups})
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    finally:
        conn.close()

@app.route('/api/groups/<market>/<group_name>', methods=['DELETE'])
def delete_group(market, group_name):
    """Delete a group option for a market"""
    if market not in MARKETS:
        return jsonify({'error': 'Market not found'}), 404

    if group_name == 'Default':
        return jsonify({'error': 'Cannot delete the Default group'}), 403

    conn = get_db_connection()
    c = conn.cursor()
    try:
        current = get_market_groups(market, c)
        if group_name not in current:
            return jsonify({'error': 'Group not found'}), 404

        updated = [g for g in current if g != group_name]
        c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                  (f'groups_{market}', ','.join(updated)))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Static data — HK securities
# ---------------------------------------------------------------------------

@app.route('/api/admin/static-data/hk/status', methods=['GET'])
def hk_securities_status():
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute('SELECT COUNT(*) as cnt FROM hk_securities')
        row_count = c.fetchone()['cnt']
        c.execute("SELECT value FROM settings WHERE key = 'hk_securities_updated_at'")
        row = c.fetchone()
        updated_at = row['value'] if row else None
    finally:
        conn.close()
    _, url_info = resolve_static_url('hk_securities_url', 'hk_securities_url_enabled', HKEX_SECURITIES_URL)
    return jsonify({'row_count': row_count, 'updated_at': updated_at, **url_info})


@app.route('/api/admin/static-data/hk/download', methods=['POST'])
def hk_securities_download():
    try:
        old_count, new_count, updated_at = download_hk_securities()
        return jsonify({'success': True, 'old_count': old_count, 'new_count': new_count,
                        'row_count': new_count, 'updated_at': updated_at,
                        'message': f'Updated from {old_count:,} to {new_count:,} rows.'})
    except StaticDataDownloadError as e:
        return jsonify({'success': False, 'aborted': True, 'row_count': e.row_count, 'error': str(e)}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/static-data/hk/securities', methods=['GET'])
def hk_securities_list():
    q = (request.args.get('q') or '').strip()
    category = (request.args.get('category') or '').strip()
    try:
        page = max(1, int(request.args.get('page', 1)))
        per_page = min(200, max(10, int(request.args.get('per_page', 50))))
    except ValueError:
        page, per_page = 1, 50

    conditions, params = [], []
    if q:
        conditions.append('(stock_code LIKE ? OR name LIKE ?)')
        params += [f'%{q}%', f'%{q}%']
    if category:
        conditions.append('category = ?')
        params.append(category)

    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    offset = (page - 1) * per_page

    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute(f'SELECT COUNT(*) as cnt FROM hk_securities {where}', params)
        total = c.fetchone()['cnt']
        c.execute(
            f'SELECT stock_code, name, category, sub_category, board_lot FROM hk_securities {where} ORDER BY stock_code LIMIT ? OFFSET ?',
            params + [per_page, offset]
        )
        rows = [dict(r) for r in c.fetchall()]
        c.execute('SELECT DISTINCT category FROM hk_securities ORDER BY category')
        categories = [r['category'] for r in c.fetchall() if r['category']]
    finally:
        conn.close()

    return jsonify({
        'rows': rows,
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': max(1, (total + per_page - 1) // per_page),
        'categories': categories
    })


@app.route('/api/admin/static-data/us/status', methods=['GET'])
def us_securities_status():
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute('SELECT COUNT(*) as cnt FROM us_securities')
        row_count = c.fetchone()['cnt']
        c.execute("SELECT value FROM settings WHERE key = 'us_securities_updated_at'")
        row = c.fetchone()
        updated_at = row['value'] if row else None
    finally:
        conn.close()
    _, nasdaq_info = resolve_static_url('us_nasdaq_url', 'us_nasdaq_url_enabled', NASDAQ_LISTED_URL)
    _, other_info = resolve_static_url('us_other_url', 'us_other_url_enabled', US_OTHER_LISTED_URL)
    return jsonify({'row_count': row_count, 'updated_at': updated_at,
                    'nasdaq': nasdaq_info, 'other': other_info})


@app.route('/api/admin/static-data/us/download', methods=['POST'])
def us_securities_download():
    try:
        old_count, new_count, updated_at = download_us_securities()
        return jsonify({'success': True, 'old_count': old_count, 'new_count': new_count,
                        'row_count': new_count, 'updated_at': updated_at,
                        'message': f'Updated from {old_count:,} to {new_count:,} rows.'})
    except StaticDataDownloadError as e:
        return jsonify({'success': False, 'aborted': True, 'row_count': e.row_count, 'error': str(e)}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/static-data/us/securities', methods=['GET'])
def us_securities_list():
    q = (request.args.get('q') or '').strip()
    board = (request.args.get('board') or '').strip()
    try:
        page = max(1, int(request.args.get('page', 1)))
        per_page = min(200, max(10, int(request.args.get('per_page', 50))))
    except ValueError:
        page, per_page = 1, 50

    conditions, params = [], []
    if q:
        conditions.append('(symbol LIKE ? OR name LIKE ?)')
        params += [f'%{q}%', f'%{q}%']
    if board:
        conditions.append('board = ?')
        params.append(board)

    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    offset = (page - 1) * per_page

    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute(f'SELECT COUNT(*) as cnt FROM us_securities {where}', params)
        total = c.fetchone()['cnt']
        c.execute(
            f'SELECT symbol, name, board, etf FROM us_securities {where} ORDER BY symbol, board LIMIT ? OFFSET ?',
            params + [per_page, offset]
        )
        rows = [dict(r) for r in c.fetchall()]
        c.execute('SELECT DISTINCT board FROM us_securities ORDER BY board')
        boards = [r['board'] for r in c.fetchall() if r['board']]
    finally:
        conn.close()

    return jsonify({
        'rows': rows,
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': max(1, (total + per_page - 1) // per_page),
        'boards': boards
    })


@app.route('/api/admin/static-data/coingecko/status', methods=['GET'])
def coingecko_coins_status():
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute('SELECT COUNT(*) as cnt FROM coingecko_coins')
        row_count = c.fetchone()['cnt']
        c.execute("SELECT value FROM settings WHERE key = 'coingecko_coins_updated_at'")
        row = c.fetchone()
        updated_at = row['value'] if row else None
    finally:
        conn.close()
    base_url, _ = get_coingecko_config()
    _, url_info = resolve_static_url(
        'coingecko_catalog_url', 'coingecko_catalog_url_enabled', f'{base_url}/coins/list')
    return jsonify({'row_count': row_count, 'updated_at': updated_at, **url_info})


@app.route('/api/admin/static-data/coingecko/download', methods=['POST'])
def coingecko_coins_download():
    try:
        old_count, new_count, updated_at = download_coingecko_coins()
        return jsonify({'success': True, 'old_count': old_count, 'new_count': new_count,
                        'row_count': new_count, 'updated_at': updated_at,
                        'message': f'Updated from {old_count:,} to {new_count:,} rows.'})
    except StaticDataDownloadError as e:
        return jsonify({'success': False, 'aborted': True, 'row_count': e.row_count, 'error': str(e)}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/static-data/coingecko/coins', methods=['GET'])
def coingecko_coins_list():
    q = (request.args.get('q') or '').strip()
    try:
        page = max(1, int(request.args.get('page', 1)))
        per_page = min(200, max(10, int(request.args.get('per_page', 50))))
    except ValueError:
        page, per_page = 1, 50

    conditions, params = [], []
    if q:
        conditions.append('(coin_id LIKE ? OR symbol LIKE ? OR name LIKE ?)')
        params += [f'%{q}%', f'%{q}%', f'%{q}%']

    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    offset = (page - 1) * per_page

    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute(f'SELECT COUNT(*) as cnt FROM coingecko_coins {where}', params)
        total = c.fetchone()['cnt']
        c.execute(
            f'SELECT coin_id, symbol, name, market_cap_rank FROM coingecko_coins {where} '
            f'ORDER BY market_cap_rank IS NULL, market_cap_rank, coin_id LIMIT ? OFFSET ?',
            params + [per_page, offset]
        )
        rows = [dict(r) for r in c.fetchall()]
    finally:
        conn.close()

    return jsonify({
        'rows': rows,
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': max(1, (total + per_page - 1) // per_page),
    })


@app.route('/api/crypto/translate', methods=['GET'])
def crypto_translate():
    """Translate between a ticker symbol and a CoinGecko coin id, either direction.

    Query params (exactly one of): ?symbol=BTC  or  ?coin_id=bitcoin
    """
    symbol = request.args.get('symbol')
    coin_id = request.args.get('coin_id')
    try:
        if symbol and not coin_id:
            return jsonify({'symbol': symbol.strip().upper(),
                            'coin_id': coingecko_symbol_to_id(symbol)})
        if coin_id and not symbol:
            return jsonify({'coin_id': coin_id.strip().lower(),
                            'symbol': coingecko_id_to_symbol(coin_id)})
        return jsonify({'error': 'provide exactly one of ?symbol= or ?coin_id='}), 400
    except CoinGeckoMappingError as e:
        return jsonify({'error': str(e)}), 404


@app.route('/api/lookup/name', methods=['GET'])
def lookup_name():
    """Resolve an instrument's display name from the static-data tables.

    Used by the Portfolio add form to auto-fill the Name field from the symbol.
    Query params: ?market=hk|crypto|jp|us  &symbol=...
    HK (hk_securities), crypto (coingecko_coins) and US (us_securities) have static
    sources; JP returns an empty name. Always returns 200 with {name: ''} on a miss
    so the caller can simply leave the field unchanged.
    """
    market = (request.args.get('market') or '').strip().lower()
    symbol = (request.args.get('symbol') or '').strip()
    if not symbol or not market:
        return jsonify({'name': ''})

    name = ''
    conn = get_db_connection()
    try:
        c = conn.cursor()
        if market == 'hk':
            norm = normalize_symbol(symbol, 'hk')               # e.g. 00700.HK
            code = norm[:-3] if norm.endswith('.HK') else norm  # e.g. 00700
            c.execute('SELECT name FROM hk_securities WHERE stock_code=?', (code,))
            row = c.fetchone()
            if row:
                name = row['name']
        elif market == 'crypto':
            coin_id = coingecko_symbol_to_id(symbol, strict=False)
            if coin_id:
                c.execute('SELECT name FROM coingecko_coins WHERE coin_id=?', (coin_id,))
                row = c.fetchone()
                if row:
                    name = row['name']
        elif market == 'us':
            base = _us_base_symbol(symbol)
            c.execute('SELECT name FROM us_securities WHERE symbol=? ORDER BY board LIMIT 1', (base,))
            row = c.fetchone()
            if row:
                name = row['name']
        # jp: no static name source yet
    finally:
        conn.close()
    return jsonify({'name': name or ''})


@app.route('/api/lookup/us', methods=['GET'])
def lookup_us():
    """Return all board listings for a US ticker (for the symbol-box board suggestion).

    Query param: ?symbol=AAPL (a trailing `.BOARD` is stripped). Returns
    {matches: [{symbol, board, name, etf}]} ordered by board; empty on a miss.
    """
    base = _us_base_symbol(request.args.get('symbol') or '')
    if not base:
        return jsonify({'matches': []})
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('SELECT symbol, name, board, etf FROM us_securities WHERE symbol=? ORDER BY board', (base,))
        matches = [dict(r) for r in c.fetchall()]
    finally:
        conn.close()
    return jsonify({'matches': matches})


HK_CHART_PROVIDERS = ('aastocks', 'yfinance')


def get_hk_chart_provider():
    """Resolve the configured HK chart provider — the single source of truth.

    `'aastocks'` (default) serves the live AA Stocks GIF templates; `'yfinance'`
    serves the app's own generated candlestick PNGs (same render path as JP/US).
    Falls back to `'aastocks'` for an unset/invalid value. HK price/perf are
    yfinance-backed either way; this only switches the *chart* rendering.
    """
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key='hk_chart_provider'")
        row = c.fetchone()
    finally:
        conn.close()
    val = (row['value'] if row else '') or ''
    return val if val in HK_CHART_PROVIDERS else 'aastocks'


@app.route('/api/instrument/<market>/<path:symbol>', methods=['GET'])
def instrument_detail(market, symbol):
    """Unified instrument detail bundle for the Tracker / Overview detail view.

    Returns the canonical symbol, the portfolio row when the instrument is tracked
    (group / added_date / comment), and market-specific static reference facts
    (HK lot size + category, US board(s) + ETF flag, crypto coin-id + market-cap
    rank). Works for symbols that are NOT in the portfolio too — `portfolio` is then
    null and only reference + charts are available. The single source of truth for
    the front-end InstrumentDetail component; do not re-fetch these bits ad-hoc.
    """
    market = (market or '').strip().lower()
    if market not in MARKETS:
        return jsonify({'error': 'Market not found'}), 404

    canonical = normalize_symbol(symbol, market)

    conn = get_db_connection()
    try:
        c = conn.cursor()

        # Portfolio row (only if this instrument is tracked)
        c.execute('SELECT name, "group", added_date, comment FROM portfolio WHERE market=? AND symbol=?',
                  (market, canonical))
        prow = c.fetchone()
        portfolio = None
        name = ''
        if prow:
            portfolio = {'group': prow['group'], 'added_date': prow['added_date'], 'comment': prow['comment']}
            name = (prow['name'] or '').strip()

        # Market-specific static reference facts
        reference = {}
        if market == 'hk':
            code = canonical[:-3] if canonical.endswith('.HK') else canonical
            c.execute('SELECT name, category, sub_category, board_lot FROM hk_securities WHERE stock_code=?', (code,))
            r = c.fetchone()
            if r:
                reference = {'lot_size': r['board_lot'], 'category': r['category'], 'sub_category': r['sub_category']}
                name = name or (r['name'] or '')
        elif market == 'crypto':
            coin_id = coingecko_symbol_to_id(canonical, strict=False)
            if not coin_id:                                  # input may already be a coin-id
                c.execute('SELECT coin_id FROM coingecko_coins WHERE coin_id=?', (canonical.lower(),))
                rr = c.fetchone()
                coin_id = rr['coin_id'] if rr else None
            if coin_id:
                c.execute('SELECT name, market_cap_rank FROM coingecko_coins WHERE coin_id=?', (coin_id,))
                r = c.fetchone()
                reference = {'coin_id': coin_id, 'market_cap_rank': r['market_cap_rank'] if r else None}
                if r:
                    name = name or (r['name'] or '')
        elif market == 'us':
            base = _us_base_symbol(canonical)
            c.execute('SELECT name, board, etf FROM us_securities WHERE symbol=? ORDER BY board', (base,))
            rows = c.fetchall()
            if rows:
                reference = {
                    'base_symbol': base,
                    'boards': [x['board'] for x in rows],
                    'etf': any((x['etf'] or 0) for x in rows),
                }
                name = name or (rows[0]['name'] or '')
        # jp: no static reference source yet
    finally:
        conn.close()

    return jsonify({
        'market': market,
        'symbol': canonical,
        'name': name or canonical,
        'provider': get_hk_chart_provider() if market == 'hk' else {'crypto': 'coingecko'}.get(market),
        'in_portfolio': portfolio is not None,
        'portfolio': portfolio,
        'reference': reference,
    })


@app.route('/api/portfolio/performance', methods=['GET'])
def portfolio_performance():
    """Bulk snapshot (last price + performance) for every portfolio instrument —
    drives the Overview Price / 1D / 7D columns.

    Cache-only by default (no fetch) so it stays fast across many rows; values populate
    as the market-data cache warms (e.g. when a crypto chart is viewed). Pass
    `?refresh=1` to warm (ensure_fresh) every instrument first — the Overview uses this
    to refresh market data on load. Refresh is a no-op for instruments already fresh
    within the provider's freshness window, so only stale rows hit the network.

    Returns a map keyed by "<market>:<symbol>" →
    {available, price, currency, as_of, performance:{'1','7','30','365'}}.
    `available` is False for markets with no provider yet (HK/JP/US today) → uniform N/A.
    """
    refresh = (request.args.get('refresh', '') or '').strip().lower() in ('1', 'true', 'yes')
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('SELECT market, symbol FROM portfolio')
        items = c.fetchall()
    finally:
        conn.close()

    out = {}
    for it in items:
        market, symbol = it['market'], it['symbol']
        key = f'{market}:{symbol}'
        prov = market_data.get(market)
        if not prov:
            out[key] = {'available': False, 'price': None, 'currency': None, 'as_of': None,
                        'performance': {k: None for k in DEFAULT_PERFORMANCE_PERIODS}}
            continue
        inst = _provider_instrument_key(market, symbol)
        snap = prov.snapshot(inst, ensure_fresh=refresh) if inst else \
            {'price': None, 'currency': None, 'as_of': None,
             'performance': {k: None for k in prov.performance_periods}}
        out[key] = {'available': True, **snap}
    return jsonify(out)


@app.route('/api/instrument/<market>/<symbol>/performance', methods=['GET'])
def instrument_performance(market, symbol):
    """Snapshot (last price + performance) for a single instrument — drives the
    InstrumentDetail quote header + Performance panel.

    Unlike the bulk Overview endpoint this warms the cache (ensure_fresh) so the detail
    view shows real numbers even on a cold instrument. Returns {market, symbol,
    available, price, currency, as_of, performance:{'1','7','30','365'}}.

    Pass `?cache_only=1` to skip the network warm and read whatever is already cached
    (used by the add-form Monitor-price auto-fill — a cold instrument just returns null).
    """
    market = (market or '').strip().lower()
    if market not in MARKETS:
        return jsonify({'error': 'Market not found'}), 404
    cache_only = (request.args.get('cache_only', '') or '').strip().lower() in ('1', 'true', 'yes')
    canonical = normalize_symbol(symbol, market)
    inst = _provider_instrument_key(market, canonical)
    res = market_data.snapshot(market, inst, ensure_fresh=not cache_only) if inst else \
        {'available': False, 'price': None, 'currency': None, 'as_of': None,
         'performance': {k: None for k in DEFAULT_PERFORMANCE_PERIODS}}
    return jsonify({'market': market, 'symbol': canonical, **res})


@app.route('/api/fx', methods=['GET'])
def fx_rates_status():
    """FX provider + base currency + per-currency rate provenance (live vs fallback).

    Drives the Settings → Admin → FX Rates panel. Read-only and cache-only — the worker
    keeps the cache warm; this never fetches. See core.fx.fx_status.
    """
    return jsonify(fx_status())


def _holdings_with_basis():
    """Flat list of portfolio rows with derived net_qty (buys − sells) + bought_price
    (weighted-average BUY cost) — the inputs the P&L computation needs. Mirrors the
    derivation in get_portfolio() but returns one flat list across all markets."""
    conn = get_db_connection()
    try:
        c = conn.cursor()
        # ONE row per instrument (market, symbol). A symbol can be filed under several
        # groups → multiple portfolio rows; P&L is per instrument, so collapse them here or
        # the holding (and its value/P&L) gets counted once per group — a double-count.
        # Notes from all of its groups are merged (DISTINCT, blanks dropped) for the
        # expandable row's Notes panel.
        c.execute("SELECT market, symbol, MIN(name) AS name, "
                  "GROUP_CONCAT(DISTINCT NULLIF(TRIM(comment), '')) AS comment "
                  "FROM portfolio GROUP BY market, symbol")
        rows = c.fetchall()
        c.execute('''SELECT market, symbol,
                            SUM(price * quantity) / NULLIF(SUM(quantity), 0) AS avg_cost
                     FROM transactions WHERE txn_type = 'buy' GROUP BY market, symbol''')
        cost = {(r['market'], r['symbol']): r['avg_cost'] for r in c.fetchall()}
        c.execute('''SELECT market, symbol,
                            SUM(CASE WHEN txn_type = 'buy' THEN quantity ELSE -quantity END) AS net_qty
                     FROM transactions GROUP BY market, symbol''')
        net = {(r['market'], r['symbol']): r['net_qty'] for r in c.fetchall()}
    finally:
        conn.close()
    out = []
    for r in rows:
        k = (r['market'], r['symbol'])
        out.append({'market': r['market'], 'symbol': r['symbol'], 'name': r['name'],
                    'comment': r['comment'],
                    'net_qty': net.get(k), 'bought_price': cost.get(k)})
    return out


@app.route('/api/portfolio/pnl', methods=['GET'])
def portfolio_pnl():
    """Unrealized P&L on current holdings, normalized to the base currency.

    Base currency defaults to the `base_currency` setting; `?base=` overrides (validated).
    Price snapshots are cache-only (no network) — a holding whose price isn't cached yet
    is returned but flagged incomplete and excluded from the aggregate sums. Drives the
    home page's PnL KPI card + My Portfolio market-grouped view. See core.pnl.compute_pnl.
    """
    base = _norm_ccy(request.args.get('base'))
    if base not in BASE_CURRENCIES:
        base = get_base_currency()

    holdings = _holdings_with_basis()
    snapshots = {}
    for h in holdings:
        if not (h['net_qty'] and h['net_qty'] > 0 and h['bought_price'] is not None):
            continue
        inst = _provider_instrument_key(h['market'], h['symbol'])
        snapshots[f"{h['market']}:{h['symbol']}"] = (
            market_data.snapshot(h['market'], inst, ensure_fresh=False) if inst else None)

    result = compute_pnl(holdings, snapshots, fx_converter(base), base)
    # Echo the rates used (currency → USD-per) so the UI can show provenance if it wants.
    result['fx'] = {r['currency']: r['usd_per'] for r in fx_status()['rates']}
    return jsonify(result)


# ---------------------------------------------------------------------------
# Database Tool (admin) — generic, introspective table browser.
# Tables are discovered from sqlite_master, so any new table created via
# init_db() shows up here automatically with no extra wiring. Read-only.
# ---------------------------------------------------------------------------

def _list_db_table_names(cursor):
    """Return user table names (excludes SQLite internal tables)."""
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return [r['name'] for r in cursor.fetchall()]


@app.route('/api/admin/db/tables', methods=['GET'])
def admin_db_tables():
    """List every user table in the DB with its row count."""
    conn = get_db_connection()
    c = conn.cursor()
    try:
        tables = []
        for name in _list_db_table_names(c):
            c.execute(f'SELECT COUNT(*) AS cnt FROM "{name}"')
            tables.append({'name': name, 'row_count': c.fetchone()['cnt']})
    finally:
        conn.close()
    return jsonify({'tables': tables})


@app.route('/api/admin/db/table/<name>', methods=['GET'])
def admin_db_table(name):
    """Return columns + a paginated page of rows for one table.

    The table name is validated against the live table list before being
    interpolated into SQL (table/column identifiers cannot be parameterized).
    """
    try:
        page = max(1, int(request.args.get('page', 1)))
        per_page = min(200, max(10, int(request.args.get('per_page', 50))))
    except ValueError:
        page, per_page = 1, 50
    offset = (page - 1) * per_page

    conn = get_db_connection()
    c = conn.cursor()
    try:
        if name not in _list_db_table_names(c):
            return jsonify({'error': f'Unknown table {name!r}'}), 404
        c.execute(f'PRAGMA table_info("{name}")')
        columns = [r['name'] for r in c.fetchall()]
        c.execute(f'SELECT COUNT(*) AS cnt FROM "{name}"')
        total = c.fetchone()['cnt']
        c.execute(f'SELECT * FROM "{name}" LIMIT ? OFFSET ?', (per_page, offset))
        rows = [dict(r) for r in c.fetchall()]
    finally:
        conn.close()

    return jsonify({
        'name': name,
        'columns': columns,
        'rows': rows,
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': max(1, (total + per_page - 1) // per_page),
    })


# ---------------------------------------------------------------------------
# DB schema migrations (admin). Read endpoints work in any env; the mutators are
# dev-only — prod is migrated headlessly by deploy/deploy.sh running migrate.py.
# The engine (core.migrate) backs up before every up/down and prunes old backups.
# ---------------------------------------------------------------------------

def _require_dev_for_migrate():
    """Block migration mutators outside dev. Returns a response to short-circuit, or None."""
    if APP_ENV != 'dev':
        return jsonify({'error': 'Migrations run from the dev machine only. On prod, deploy '
                                 'runs `migrate.py` before restarting services.'}), 403
    return None


@app.route('/api/admin/migrate/status', methods=['GET'])
def admin_migrate_status():
    """Current version, head, gate state, and the pending list."""
    from core import migrate
    conn = get_db_connection()
    try:
        return jsonify(migrate.status(conn))
    finally:
        conn.close()


@app.route('/api/admin/migrate/history', methods=['GET'])
def admin_migrate_history():
    """The applied-migration ledger (schema_migrations), oldest-first."""
    from core import migrate
    conn = get_db_connection()
    try:
        return jsonify({'history': migrate.history(conn)})
    finally:
        conn.close()


@app.route('/api/admin/migrate/up', methods=['POST'])
def admin_migrate_up():
    """Apply pending migrations (default: to head). Backs up first. Dev-only."""
    blocked = _require_dev_for_migrate()
    if blocked:
        return blocked
    target = (request.json or {}).get('to')
    from core import migrate
    conn = get_db_connection()
    try:
        res = migrate.apply(conn, target=None if target is None else int(target))
    finally:
        conn.close()
    refresh_migration_gate()
    return jsonify(res), (500 if 'error' in res else 200)


@app.route('/api/admin/migrate/down', methods=['POST'])
def admin_migrate_down():
    """Revert migrations down to a target version. Backs up first. Dev-only."""
    blocked = _require_dev_for_migrate()
    if blocked:
        return blocked
    target = (request.json or {}).get('to')
    if target is None:
        return jsonify({'error': 'down requires a target version "to"'}), 400
    from core import migrate
    conn = get_db_connection()
    try:
        res = migrate.apply(conn, target=int(target))
    finally:
        conn.close()
    refresh_migration_gate()
    return jsonify(res), (500 if 'error' in res else 200)


@app.route('/api/admin/migrate/prune', methods=['POST'])
def admin_migrate_prune():
    """Delete old pre-migration backups, keeping the newest N. Dev-only."""
    blocked = _require_dev_for_migrate()
    if blocked:
        return blocked
    keep = int((request.json or {}).get('keep', 5))
    from core import migrate
    removed = migrate.prune_backups(keep)
    return jsonify({'pruned': removed, 'kept': keep})


# ---------------------------------------------------------------------------
# Telegram notifications
# ---------------------------------------------------------------------------

@app.route('/notifications')
def notifications_page():
    return render_template('notifications.html')


@app.route('/api/telegram/test', methods=['POST'])
def telegram_test():
    """Send a test message to verify Telegram credentials."""
    ok, err = send_telegram_message('🐱 Betelgeuse is connected! Notifications are working.')
    if ok:
        return jsonify({'success': True})
    return jsonify({'error': err}), 400


@app.route('/api/telegram/detect-chat-id', methods=['POST'])
def telegram_detect_chat_id():
    """Poll getUpdates to auto-detect the most recent chat ID.
    The user must have sent /start to the bot first."""
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('SELECT value FROM settings WHERE key=?', ('telegram_bot_token',))
        row = c.fetchone()
    finally:
        conn.close()
    token = (row['value'] if row else '').strip()
    if not token:
        return jsonify({'error': 'Bot token not saved yet — save it first then retry'}), 400
    try:
        resp = requests.get(f'https://api.telegram.org/bot{token}/getUpdates', timeout=10)
        data = resp.json()
    except Exception as e:
        return jsonify({'error': f'Could not reach Telegram: {e}'}), 500
    results = data.get('result', [])
    if not results:
        return jsonify({'error': 'No messages found — open Telegram, send /start to your bot, then click Auto-detect again'}), 400
    # Use the most recent message's chat ID
    for update in reversed(results):
        msg = update.get('message') or update.get('channel_post')
        if msg and msg.get('chat', {}).get('id'):
            return jsonify({'chat_id': str(msg['chat']['id'])})
    return jsonify({'error': 'Could not extract chat ID from recent messages'}), 400


@app.route('/api/notifications/portfolio/send', methods=['POST'])
def send_portfolio_notification():
    """Build a portfolio summary message and send it via Telegram."""
    body = request.json or {}
    markets_filter = body.get('markets') or list(MARKETS.keys())

    message, total = _build_portfolio_message(markets_filter)
    if message is None:
        return jsonify({'error': 'No portfolio items in the selected markets'}), 400

    ok, err = send_telegram_message(message)
    if ok:
        _record_portfolio_sent()
        return jsonify({'success': True})
    return jsonify({'error': err}), 400


@app.route('/api/notifications/portfolio/schedule', methods=['GET'])
def get_portfolio_schedule():
    """Return current schedule config and next scheduled run times."""
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('SELECT key, value FROM settings WHERE key IN (?,?,?,?,?)',
                  ('notification_portfolio_enabled', 'notification_portfolio_days',
                   'notification_portfolio_times', 'default_timezone',
                   'notification_portfolio_markets'))
        cfg = {row['key']: row['value'] for row in c.fetchall()}
    finally:
        conn.close()

    next_runs = _compute_schedule_next_runs(cfg)

    return jsonify({
        'enabled': cfg.get('notification_portfolio_enabled') == 'true',
        'days': cfg.get('notification_portfolio_days', 'mon,tue,wed,thu,fri'),
        'times': cfg.get('notification_portfolio_times', ''),
        'timezone': cfg.get('default_timezone', 'Australia/Sydney'),
        'markets': cfg.get('notification_portfolio_markets', ','.join(MARKETS.keys())),
        'next_runs': next_runs
    })


if __name__ == '__main__':
    APP_ENV = runtime.parse_env_arg()  # mandatory --env dev|prod
    configure_logging('web', APP_ENV)
    init_db()
    if refresh_migration_gate() != 'OK':
        logger.warning("[web] ⚠ DB schema gate is %s — serving the migration maintenance "
                       "page only until the DB is migrated (Settings → open the page).",
                       MIGRATION_GATE)
    # The web process serves the UI/API only. The recurring-notification scheduler
    # lives in the separate background worker (`python3 worker.py`) so it keeps
    # firing even when no browser/UI is open — run the worker on an always-awake
    # machine. Starting the scheduler here too would double-send every notification.
    logger.info("[web] %s (v%s) — UI/API only; run `python3 worker.py --env %s` "
                "for scheduled notifications & background jobs", APP_ENV, WEB_VERSION, APP_ENV)
    app.run(debug=True, host='0.0.0.0', port=8000)
