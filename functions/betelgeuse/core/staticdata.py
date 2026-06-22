"""Static reference-data downloaders (HK / US / CoinGecko catalog), the AA-Stocks
chart fetch, and the source-URL override resolver.

Flask-free. Each downloader follows the guarded-refresh procedure: download -> parse
-> row-count sanity check -> wholesale replace, aborting (StaticDataDownloadError)
without touching the table when the data looks corrupt.
"""
import io
import os
from datetime import datetime

import requests
import openpyxl

from core import config
from core.config import (
    HKEX_SECURITIES_URL, NASDAQ_LISTED_URL, US_OTHER_LISTED_URL, US_EXCHANGE_BOARDS,
    STATIC_DATA_MIN_ROWS, StaticDataDownloadError,
)
from core.db import get_db_connection
from core.crypto import get_coingecko_config
from core.logging_setup import get_logger

logger = get_logger('staticdata')


def download_aa_stocks_chart(stockid, period=9):
    """Download chart from AA Stocks and cache locally"""
    filename = f"chart_{stockid.replace('.', '_')}_p{period}.gif"
    os.makedirs(config.CHART_DIR, exist_ok=True)
    filepath = os.path.join(config.CHART_DIR, filename)

    if os.path.exists(filepath):
        return filename
    
    url = (
        f"https://charts.aastocks.com/servlet/Charts?"
        f"fontsize=12&15MinDelay=T&lang=1&titlestyle=1&vol=1&Indicator=1"
        f"&indpara1=10&indpara2=20&indpara3=50&indpara4=100&indpara5=150"
        f"&subChart1=2&ref1para1=14&ref1para2=0&ref1para3=0"
        f"&subChart2=3&ref2para1=12&ref2para2=26&ref2para3=9"
        f"&subChart3=12&ref3para1=0&ref3para2=0&ref3para3=0"
        f"&scheme=3&com=100&chartwidth=870&chartheight=855"
        f"&stockid={stockid}&period={period}&type=1&logoStyle=1"
    )
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'
        }
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            with open(filepath, 'wb') as f:
                f.write(response.content)
            return filename
    except Exception as e:
        logger.error("Error downloading chart: %s", e)

    return None

def resolve_static_url(value_key, enabled_key, default_url):
    """Resolve a static-data source URL with an optional user override.

    The override (stored under `value_key`) is only used when its checkbox
    (`enabled_key` == 'true') is on AND the value is non-empty; otherwise the
    hardcoded `default_url` is used. This makes the UI placeholder/default a safe
    rollback if the user clears or mangles the URL.

    Returns (effective_url, info) where info = {url_default, url_value, url_enabled}
    for feeding the settings UI.
    """
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT key, value FROM settings WHERE key IN (?, ?)", (value_key, enabled_key))
        s = {r['key']: r['value'] for r in c.fetchall()}
    finally:
        conn.close()
    value = (s.get(value_key) or '').strip()
    enabled = (s.get(enabled_key) or '').strip().lower() == 'true'
    effective = value if (enabled and value) else default_url
    return effective, {'url_default': default_url, 'url_value': value, 'url_enabled': enabled}

def download_hk_securities():
    """Download HKEX List of Securities, sanity-check it, then fully replace hk_securities.

    Procedure: download → parse into memory → if < STATIC_DATA_MIN_ROWS rows, abort
    (raise StaticDataDownloadError) leaving the existing table untouched; otherwise
    replace the table wholesale inside one transaction. Returns (old_count, new_count, updated_at).
    """
    headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}
    url, _ = resolve_static_url('hk_securities_url', 'hk_securities_url_enabled', HKEX_SECURITIES_URL)
    resp = requests.get(url, headers=headers, timeout=60)
    resp.raise_for_status()

    wb = openpyxl.load_workbook(io.BytesIO(resp.content), data_only=True)
    ws = wb.active
    rows = []
    for row in ws.iter_rows(min_row=4, values_only=True):  # row 1-2 title/date, row 3 header
        if not row[0]:
            continue
        rows.append((
            str(row[0]).strip(),
            str(row[1]).strip() if row[1] else '',
            str(row[2]).strip() if row[2] else '',
            str(row[3]).strip() if row[3] else '',
            str(row[4]).strip() if row[4] else '',
        ))
    wb.close()

    # Sanity check BEFORE touching the DB — abort if the download looks corrupt.
    if len(rows) < STATIC_DATA_MIN_ROWS:
        raise StaticDataDownloadError(
            f'Downloaded HK securities data looks corrupted: only {len(rows)} row(s) '
            f'(expected at least {STATIC_DATA_MIN_ROWS}). Existing data left unchanged.',
            len(rows))

    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute('SELECT COUNT(*) AS cnt FROM hk_securities')
        old_count = c.fetchone()['cnt']
        # Full replace (drop stale/delisted rows) — atomic via the single transaction.
        c.execute('DELETE FROM hk_securities')
        c.executemany(
            'INSERT OR REPLACE INTO hk_securities (stock_code, name, category, sub_category, board_lot) VALUES (?, ?, ?, ?, ?)',
            rows
        )
        c.execute('SELECT COUNT(*) AS cnt FROM hk_securities')
        new_count = c.fetchone()['cnt']
        updated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                  ('hk_securities_updated_at', updated_at))
        conn.commit()
    finally:
        conn.close()

    return old_count, new_count, updated_at


def _fetch_url_text(url, timeout=60):
    """Fetch a text resource, supporting both http(s) (requests) and ftp:// (urllib).
    The Nasdaq Trader symbol files are served over anonymous FTP."""
    if url.lower().startswith('ftp://'):
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read().decode('latin-1')
    headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text


def _parse_nasdaq_symbol_file(text, symbol_col, name_col, test_col, etf_col, board_from):
    """Parse a pipe-delimited Nasdaq Trader symbol file into (symbol, name, board, etf) rows.

    Skips the header (first line) and the trailing 'File Creation Time' line, and drops
    test issues. `board_from` is either a constant board string (nasdaqlisted) or a
    callable(fields) -> board (otherlisted, which maps the Exchange code column).
    """
    rows = []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for ln in lines[1:]:                       # skip header row
        if ln.startswith('File Creation Time'):  # trailer
            continue
        f = ln.split('|')
        if len(f) <= max(symbol_col, name_col, test_col, etf_col):
            continue
        if (f[test_col] or '').strip().upper() == 'Y':   # drop test issues
            continue
        symbol = (f[symbol_col] or '').strip().upper()
        if not symbol:
            continue
        name = (f[name_col] or '').strip()
        board = board_from(f) if callable(board_from) else board_from
        if not board:
            continue
        etf = 1 if (f[etf_col] or '').strip().upper() == 'Y' else 0
        rows.append((symbol, name, board, etf))
    return rows


def download_us_securities():
    """Download the Nasdaq Trader symbol directory (both files), sanity-check, then
    fully replace us_securities. Returns (old_count, new_count, updated_at).

    nasdaqlisted.txt → board 'NASDAQ'; otherlisted.txt → board from its Exchange code
    (N=NYSE, A=AMEX, P=ARCA, Z=BATS, V=IEX). Test issues are dropped.
    """
    nasdaq_url, _ = resolve_static_url('us_nasdaq_url', 'us_nasdaq_url_enabled', NASDAQ_LISTED_URL)
    other_url, _ = resolve_static_url('us_other_url', 'us_other_url_enabled', US_OTHER_LISTED_URL)

    # nasdaqlisted.txt: Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
    rows = _parse_nasdaq_symbol_file(_fetch_url_text(nasdaq_url), symbol_col=0, name_col=1,
                                     test_col=3, etf_col=6, board_from='NASDAQ')

    # otherlisted.txt: ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
    rows += _parse_nasdaq_symbol_file(
        _fetch_url_text(other_url), symbol_col=0, name_col=1, test_col=6, etf_col=4,
        board_from=lambda f: US_EXCHANGE_BOARDS.get((f[2] or '').strip().upper(), 'OTHER'))

    # Sanity check BEFORE touching the DB — abort if the download looks corrupt.
    if len(rows) < STATIC_DATA_MIN_ROWS:
        raise StaticDataDownloadError(
            f'Downloaded US securities data looks corrupted: only {len(rows)} row(s) '
            f'(expected at least {STATIC_DATA_MIN_ROWS}). Existing data left unchanged.',
            len(rows))

    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute('SELECT COUNT(*) AS cnt FROM us_securities')
        old_count = c.fetchone()['cnt']
        c.execute('DELETE FROM us_securities')
        c.executemany(
            'INSERT OR REPLACE INTO us_securities (symbol, name, board, etf) VALUES (?, ?, ?, ?)',
            rows
        )
        c.execute('SELECT COUNT(*) AS cnt FROM us_securities')
        new_count = c.fetchone()['cnt']
        updated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                  ('us_securities_updated_at', updated_at))
        conn.commit()
    finally:
        conn.close()

    return old_count, new_count, updated_at


def download_coingecko_coins():
    """Download the CoinGecko coin catalog, sanity-check it, then fully replace coingecko_coins.

    Pulls the full /coins/list (id, symbol, name) and enriches the top entries
    with market_cap_rank from /coins/markets so symbol→id collisions can be
    resolved by market cap. Aborts (StaticDataDownloadError) without touching the
    table if < STATIC_DATA_MIN_ROWS rows came back. Returns (old_count, new_count, updated_at).
    """
    base_url, api_key = get_coingecko_config()
    headers = {'x-cg-demo-api-key': api_key} if api_key else {}

    catalog_url, _ = resolve_static_url(
        'coingecko_catalog_url', 'coingecko_catalog_url_enabled', f'{base_url}/coins/list')
    resp = requests.get(catalog_url, headers=headers, timeout=60)
    resp.raise_for_status()
    coins = resp.json()

    # Enrich with market cap ranks for the top coins (a few pages of 250).
    ranks = {}
    for page in range(1, 5):
        try:
            mresp = requests.get(
                f'{base_url}/coins/markets',
                params={'vs_currency': 'usd', 'order': 'market_cap_desc',
                        'per_page': 250, 'page': page},
                headers=headers, timeout=30
            )
            mresp.raise_for_status()
            batch = mresp.json()
        except Exception as e:
            logger.error('CoinGecko markets page %s error: %s', page, e)
            break
        if not batch:
            break
        for m in batch:
            if m.get('id') and m.get('market_cap_rank') is not None:
                ranks[m['id']] = m['market_cap_rank']

    rows = [
        (
            str(c['id']).strip().lower(),
            str(c.get('symbol') or '').strip().lower(),
            str(c.get('name') or '').strip(),
            ranks.get(str(c['id']).strip().lower()),
        )
        for c in coins if c.get('id')
    ]

    # Sanity check BEFORE touching the DB — abort if the download looks corrupt.
    if len(rows) < STATIC_DATA_MIN_ROWS:
        raise StaticDataDownloadError(
            f'Downloaded CoinGecko catalog looks corrupted: only {len(rows)} row(s) '
            f'(expected at least {STATIC_DATA_MIN_ROWS}). Existing data left unchanged.',
            len(rows))

    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute('SELECT COUNT(*) AS cnt FROM coingecko_coins')
        old_count = c.fetchone()['cnt']
        # Full replace (drop delisted coins) — atomic via the single transaction.
        c.execute('DELETE FROM coingecko_coins')
        c.executemany(
            'INSERT OR REPLACE INTO coingecko_coins (coin_id, symbol, name, market_cap_rank) VALUES (?, ?, ?, ?)',
            rows
        )
        c.execute('SELECT COUNT(*) AS cnt FROM coingecko_coins')
        new_count = c.fetchone()['cnt']
        updated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                  ('coingecko_coins_updated_at', updated_at))
        conn.commit()
    finally:
        conn.close()

    return old_count, new_count, updated_at
