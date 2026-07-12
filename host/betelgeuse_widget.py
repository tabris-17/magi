"""Betelgeuse's altair widgets — a HOST-SIDE adapter (no betelgeuse code change).

Betelgeuse is vendored byte-identical to prod, so its widget contribution lives here
instead of inside the package (the same pattern as magi.py's _betelgeuse_health):
the render calls betelgeuse's own /api/portfolio/pnl in-process via its Flask test
client and builds the card HTML host-side, with all data escaped and every color a
theme token (the .alt-pnl-* classes live in theme.css's altair block).

magi.py wires this onto BETELGEUSE_META["widgets"] with the betel_app client factory,
so this module never imports betelgeuse either.
"""
from markupsafe import escape


def widget_types(get_client):
    """The widget TYPE list for the registry. `get_client` → a fresh Flask test client
    bound to the mounted betelgeuse app (called at render time, never at import)."""
    return [{
        "key": "pnl",
        "label": "Portfolio P&L",
        "description": "Unrealized P&L per holding plus the total, in your base currency.",
        "params": [],
        "render": lambda config: render_pnl(get_client()),
    }]


def _fmt(amount, signed=False):
    """1234567.8 → '1,234,568' (whole units, like betelgeuse's own P&L panel)."""
    if amount is None:
        return "—"
    s = f"{abs(amount):,.0f}"
    if signed:
        return ("+" if amount >= 0 else "-") + s
    return s


def _fmt_pct(pct):
    if pct is None:
        return ""
    return f"({pct:+.0f}%)"


def render_pnl(client):
    resp = client.get("/api/portfolio/pnl")
    data = resp.get_json(silent=True)
    if resp.status_code != 200 or not data:
        raise RuntimeError(f"betelgeuse P&L unavailable (HTTP {resp.status_code})")

    base = data.get("base", "")
    totals = data.get("totals") or {}
    holdings = []
    for market in (data.get("markets") or {}).values():
        holdings.extend(market.get("holdings") or [])
    complete = [h for h in holdings if h.get("complete")]
    complete.sort(key=lambda h: h["pnl"], reverse=True)

    if not holdings:
        return {"title": "Portfolio P&L", "html": '<div class="alt-note">No current holdings.</div>'}

    max_abs = max((abs(h["pnl"]) for h in complete), default=0) or 1
    rows = []
    for h in complete:
        pnl, pct = h["pnl"], h.get("pnl_pct")
        up = pnl >= 0
        color = "var(--success-fg)" if up else "var(--danger-fg)"
        width = abs(pnl) / max_abs * 50  # % of the track; the zero axis sits at 50%
        side = "left:50%;" if up else "right:50%;"
        rows.append(
            f'<div class="alt-pnl-row">'
            f'<span class="alt-pnl-sym">{escape(h["symbol"])}</span>'
            f'<span class="alt-pnl-track"><span class="alt-pnl-bar" '
            f'style="{side}width:{width:.1f}%;background:{color}"></span></span>'
            f'<span class="alt-pnl-val" style="color:{color}">{_fmt(pnl, signed=True)} '
            f'<small>{_fmt_pct(pct)}</small></span>'
            f'</div>')

    t_pnl, t_pct = totals.get("pnl"), totals.get("pnl_pct")
    t_color = "var(--success-fg)" if (t_pnl or 0) >= 0 else "var(--danger-fg)"
    rows.append(
        f'<div class="alt-pnl-total"><span>Total</span>'
        f'<span class="alt-pnl-meta">value {_fmt(totals.get("value"))} · '
        f'cost {_fmt(totals.get("cost"))}</span>'
        f'<span style="color:{t_color}">{_fmt(t_pnl, signed=True)} '
        f'<small>{_fmt_pct(t_pct)}</small></span></div>')

    incomplete = totals.get("incomplete") or 0
    if incomplete:
        rows.append(f'<div class="alt-note">{incomplete} holding(s) missing a cached '
                    f'price — excluded from the sums.</div>')

    return {"title": f"Portfolio P&L · {escape(base)}", "html": "".join(rows)}
