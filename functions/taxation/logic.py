"""Taxation — RBA daily FX rates, Flask-free logic.

Downloads the RBA historical daily exchange-rate .xls, parses it (xlrd — the file is
old binary .xls, which openpyxl can't read), and answers "what were the AUD rates for
this date?". Self-contained: nothing here imports the host or another function.

The RBA F11.1 "Data" sheet quotes rates as **A$1 = X** (units of foreign currency per
1 AUD). Layout: row 1 = headers (`A$1=USD`, …), data from row 11, col 0 = Excel-serial
date. We resolve currency columns by header match (not fixed indices) so a reordering
upstream can't silently mis-map. Business days only — a missing date falls back to the
nearest prior available day.
"""
import bisect
import os
import ssl
import threading
import time
import urllib.request
from datetime import datetime

import xlrd


def _ssl_context():
    """Verify TLS against the OS trust store (macOS keychain / system store) via
    truststore — so a corporate/firewall TLS-intercepting proxy whose root CA the OS
    trusts (the same reason `curl` works) doesn't break the download. Falls back to
    Python's default context when truststore isn't installed. Scoped to this download —
    we never disable verification."""
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001
        return None  # urlopen(context=None) uses the default verifying context

# Default source; overridable via the host setting `taxation_rba_url` (injected resolver)
# or the MAGI_RBA_URL env var. The host stores the configured URL in data/magi.db.
DEFAULT_RBA_URL = "https://www.rba.gov.au/statistics/tables/xls-hist/2023-current.xls"

# The currencies this function surfaces, matched against the row-1 `A$1=<CCY>` headers.
CURRENCIES = ("USD", "GBP", "HKD")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CACHE_FILE = os.path.join(DATA_DIR, "rba-cache.xls")
CACHE_TTL_SEC = 12 * 3600  # re-download at most ~twice a day (data updates on business days)

DATA_ROW_START = 11  # first data row (0-indexed) on the "Data" sheet
HEADER_ROW = 1       # row holding `A$1=USD` etc.

# Host-injected URL resolver (the configured host setting); None when running standalone.
_url_resolver = None
_lock = threading.Lock()
# Parsed cache: {"dates": [date,...sorted], "rows": {date: {ccy: rate}}, "fetched_at": ts,
# "source_url": str}. None until first load.
_cache = None


def set_rba_url_resolver(fn):
    """Let the host supply the source URL (the `taxation_rba_url` setting). `fn` takes no
    args and may return a URL or a falsy value (→ fall back)."""
    global _url_resolver
    _url_resolver = fn


def current_rba_url():
    """Active source URL. Precedence: host resolver → MAGI_RBA_URL env → DEFAULT_RBA_URL.
    Never touches the network/filesystem (safe at import)."""
    if _url_resolver is not None:
        try:
            chosen = (_url_resolver() or "").strip()
        except Exception:  # noqa: BLE001
            chosen = ""
        if chosen:
            return chosen
    return (os.environ.get("MAGI_RBA_URL") or "").strip() or DEFAULT_RBA_URL


def _download(url):
    """Fetch the .xls to the local cache file (dir created lazily here, never at import)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "magi-taxation/1.0"})
    with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as resp:
        data = resp.read()
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, CACHE_FILE)


def _parse(path):
    """Parse the cached .xls into {dates:[...], rows:{date:{ccy:rate}}}."""
    wb = xlrd.open_workbook(path)
    sh = wb.sheet_by_name("Data") if "Data" in wb.sheet_names() else wb.sheet_by_index(0)

    # Map each wanted currency to its column via the `A$1=<CCY>` header (robust to reorder).
    col_of = {}
    for c in range(sh.ncols):
        header = str(sh.cell_value(HEADER_ROW, c)).strip().upper().replace(" ", "")
        for ccy in CURRENCIES:
            if header == f"A$1={ccy}":
                col_of[ccy] = c
    missing = [c for c in CURRENCIES if c not in col_of]
    if missing:
        raise ValueError(f"RBA file missing expected currency columns: {missing}")

    rows = {}
    for r in range(DATA_ROW_START, sh.nrows):
        raw = sh.cell_value(r, 0)
        if not isinstance(raw, (int, float)) or not raw:
            continue
        d = xlrd.xldate.xldate_as_datetime(raw, wb.datemode).date()
        rec = {}
        for ccy, c in col_of.items():
            v = sh.cell_value(r, c)
            if isinstance(v, (int, float)) and v:
                rec[ccy] = float(v)
        if rec:
            rows[d] = rec
    if not rows:
        raise ValueError("RBA file contained no parseable rate rows")
    return {"dates": sorted(rows), "rows": rows}


def _is_stale(parsed_source_url, want_date=None):
    """Decide whether the in-memory cache must be (re)built."""
    if _cache is None:
        return True
    if _cache.get("source_url") != parsed_source_url:
        return True  # URL changed in settings
    if (time.time() - _cache["fetched_at"]) > CACHE_TTL_SEC:
        return True
    if want_date is not None and _cache["dates"] and want_date > _cache["dates"][-1]:
        return True  # asked beyond what we have — try a fresh pull
    return False


def _ensure_loaded(want_date=None, force=False):
    """Load (download+parse) the dataset into memory if stale. Thread-safe."""
    global _cache
    url = current_rba_url()
    with _lock:
        if not force and not _is_stale(url, want_date):
            return _cache
        # (Re)download unless we have a usable file and aren't forcing a network refresh.
        need_dl = force or not os.path.exists(CACHE_FILE) or \
            (time.time() - os.path.getmtime(CACHE_FILE)) > CACHE_TTL_SEC
        if need_dl:
            _download(url)
        parsed = _parse(CACHE_FILE)
        parsed["fetched_at"] = time.time()
        parsed["source_url"] = url
        _cache = parsed
        return _cache


def refresh():
    """Force a re-download + reparse (the page's Refresh button)."""
    return _ensure_loaded(force=True)


def status():
    """Lightweight health/info snapshot (does NOT trigger a download)."""
    info = {"source_url": current_rba_url(), "cached": _cache is not None}
    if _cache:
        ds = _cache["dates"]
        info.update(
            rows=len(ds),
            date_range=[ds[0].isoformat(), ds[-1].isoformat()] if ds else None,
            last_fetched=int(_cache["fetched_at"] * 1000),
        )
    return info


def rates_for(date_str):
    """Return the RBA rates for `date_str` (YYYY-MM-DD), falling back to the nearest prior
    business day. Result: {requested, used, exact, out_of_range, rates:{ccy:{published,
    inverse}}, basis, source_url}."""
    try:
        want = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        raise ValueError("date must be YYYY-MM-DD")

    data = _ensure_loaded(want_date=want)
    dates = data["dates"]
    out = {"requested": want.isoformat(), "basis": "A$1 = X (foreign per 1 AUD)",
           "source_url": data["source_url"], "exact": False, "out_of_range": False,
           "used": None, "rates": {}}

    if not dates or want < dates[0]:
        out["out_of_range"] = True
        out["range"] = [dates[0].isoformat(), dates[-1].isoformat()] if dates else None
        return out

    if want in data["rows"]:
        used = want
        out["exact"] = True
    else:
        # nearest prior available day: rightmost date <= want
        idx = bisect.bisect_right(dates, want) - 1
        used = dates[idx]
    out["used"] = used.isoformat()
    for ccy, pub in data["rows"][used].items():
        out["rates"][ccy] = {"published": pub, "inverse": (1.0 / pub) if pub else None}
    return out
