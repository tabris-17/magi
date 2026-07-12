"""The betelgeuse Portfolio-P&L widget (host/betelgeuse_widget.py) over a fake client.

The adapter is host-side code, but its output renders inside altair's feed, so it's
tested here with the rest of the widget stack. The fake client stands in for
betel_app.test_client() and returns a canned /api/portfolio/pnl payload.
"""
import pytest

from host import betelgeuse_widget as bw


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def get_json(self, silent=False):
        return self._payload


class FakeClient:
    def __init__(self, payload, status=200):
        self._resp = FakeResp(payload, status)

    def get(self, path):
        assert path == "/api/portfolio/pnl"
        return self._resp


def _payload(holdings_us, totals=None, base="HKD"):
    return {
        "base": base,
        "totals": totals or {"cost": 600000, "value": 400000, "pnl": -200000,
                             "pnl_pct": -33.3, "count": len(holdings_us), "incomplete": 0},
        "markets": {"US": {"holdings": holdings_us}},
        "missing": [],
    }


def _h(symbol, pnl, pct, complete=True):
    return {"symbol": symbol, "pnl": pnl, "pnl_pct": pct, "complete": complete,
            "value": 1000, "cost": (1000 - pnl) if pnl is not None else None}


def test_widget_types_shape():
    types = bw.widget_types(lambda: FakeClient(_payload([])))
    (t,) = types
    assert t["key"] == "pnl" and t["params"] == [] and callable(t["render"])


def test_render_rows_sorted_and_colored():
    client = FakeClient(_payload([_h("AAA", -50, -10), _h("BBB", 100, 25)]))
    out = bw.render_pnl(client)
    assert out["title"] == "Portfolio P&L · HKD"
    html = out["html"]
    # winner first (sorted by pnl desc), green right of axis / red left of axis
    assert html.index("BBB") < html.index("AAA")
    assert "left:50%" in html and "var(--success-fg)" in html
    assert "right:50%" in html and "var(--danger-fg)" in html
    assert "+100" in html and "(+25%)" in html
    assert "-50" in html and "(-10%)" in html
    # total row with value/cost meta
    assert "Total" in html and "value 400,000" in html and "cost 600,000" in html
    assert "-200,000" in html


def test_render_escapes_symbol():
    client = FakeClient(_payload([_h("<script>x</script>", 1, 1)]))
    html = bw.render_pnl(client)["html"]
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_render_incomplete_note_and_exclusion():
    totals = {"cost": 100, "value": 110, "pnl": 10, "pnl_pct": 10, "count": 2, "incomplete": 1}
    client = FakeClient(_payload([_h("OK", 10, 10), _h("NOPX", None, None, complete=False)],
                                 totals=totals))
    html = bw.render_pnl(client)["html"]
    assert "missing a cached price" in html
    assert "NOPX" not in html                 # incomplete holdings aren't drawn as rows


def test_render_no_holdings():
    out = bw.render_pnl(FakeClient(_payload([])))
    assert "No current holdings" in out["html"]


def test_render_http_error_raises():
    with pytest.raises(RuntimeError):
        bw.render_pnl(FakeClient(None, status=503))
