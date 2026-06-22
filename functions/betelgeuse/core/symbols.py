"""Symbol formatting — the single source of truth for canonical instrument forms."""
from core.config import US_BOARD_TOKENS


def normalize_symbol(symbol, market):
    """Normalize an instrument symbol to its canonical market-specific form.

    This is the single source of truth for symbol formatting. Always call it before
    persisting, displaying, or looking up a symbol.
    - HK: 5-digit zero-padded numeric code, uppercase, '.HK' suffix (e.g. '700.hk' -> '00700.HK')
    - JP: '.T' suffix
    - US / Crypto: uppercased, otherwise unchanged
    """
    symbol = (symbol or '').strip().upper()
    if not symbol:
        return symbol
    if market == 'hk':
        code = symbol[:-3] if symbol.endswith('.HK') else symbol
        code = code.rstrip('.')
        if code.isdigit():
            code = code.zfill(5)
        return f'{code}.HK'
    if market == 'jp':
        # Like HK's suffix rule but the code is alphanumeric (no zero-pad). Always XXXX.T.
        code = symbol[:-2] if symbol.endswith('.T') else symbol
        code = code.rstrip('.')
        return f'{code}.T'
    return symbol


def _us_base_symbol(symbol):
    """Strip a trailing `.BOARD` extension (e.g. AAPL.NASDAQ → AAPL) but preserve
    real symbol dots like class shares (BRK.A stays BRK.A)."""
    s = (symbol or '').strip().upper()
    i = s.rfind('.')
    if i > 0 and s[i + 1:] in US_BOARD_TOKENS:
        return s[:i]
    return s
