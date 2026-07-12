"""Altair blueprint routes — JSON shapes and status codes (no server, no host)."""
import flask
import pytest

from functions.altair import bp, logic


@pytest.fixture
def client():
    app = flask.Flask(__name__)
    app.register_blueprint(bp)
    return app.test_client()


def test_feed_shape(client, registry):
    a = logic.add_instance("alpha.one", {"x": "1"})
    data = client.get("/altair/api/feed").get_json()
    assert [t["id"] for t in data["types"]] == ["alpha.one", "beta.two"]
    assert all("render" not in t for t in data["types"])
    assert data["widgets"][0]["id"] == a["id"]
    assert data["widgets"][0]["config"] == {"x": "1"}


def test_add_widget_route(client, registry):
    r = client.post("/altair/api/widgets", json={"widget": "alpha.one", "config": {"x": "9"}})
    assert r.status_code == 200
    assert r.get_json()["widget"]["config"] == {"x": "9"}

    r = client.post("/altair/api/widgets", json={"widget": "nope"})
    assert r.status_code == 400
    assert "unknown widget" in r.get_json()["error"]


def test_order_route(client, registry):
    a = logic.add_instance("alpha.one")
    b = logic.add_instance("beta.two")
    r = client.post("/altair/api/widgets/order", json={"ids": [b["id"], a["id"]]})
    assert r.status_code == 200
    assert [i["id"] for i in logic.list_instances()] == [b["id"], a["id"]]

    assert client.post("/altair/api/widgets/order", json={"ids": "x"}).status_code == 400
    assert client.post("/altair/api/widgets/order", json={"ids": ["NaN"]}).status_code == 400


def test_hidden_route(client, registry):
    a = logic.add_instance("alpha.one")
    r = client.post(f"/altair/api/widgets/{a['id']}", json={"hidden": True})
    assert r.status_code == 200 and r.get_json() == {"ok": True, "hidden": True}
    assert logic.list_instances()[0]["hidden"] is True

    r = client.post(f"/altair/api/widgets/{a['id']}", json={"hidden": False})
    assert r.status_code == 200
    assert logic.list_instances()[0]["hidden"] is False

    assert client.post(f"/altair/api/widgets/{a['id']}", json={}).status_code == 400
    assert client.post("/altair/api/widgets/999", json={"hidden": True}).status_code == 404


def test_delete_route(client, registry):
    a = logic.add_instance("alpha.one")
    assert client.delete(f"/altair/api/widgets/{a['id']}").status_code == 200
    assert client.delete(f"/altair/api/widgets/{a['id']}").status_code == 404


def test_render_route(client, registry):
    a = logic.add_instance("alpha.one", {"x": "7"})
    b = logic.add_instance("beta.two")
    ok = client.get(f"/altair/api/widgets/{a['id']}/render")
    assert ok.status_code == 200
    assert ok.get_json() == {"ok": True, "title": "T:7", "html": "<b>hi</b>"}

    # a raising widget is STILL 200 — the feed shows it as an error card
    bad = client.get(f"/altair/api/widgets/{b['id']}/render")
    assert bad.status_code == 200
    assert bad.get_json()["ok"] is False

    assert client.get("/altair/api/widgets/999/render").status_code == 404


def test_health_route(client, registry):
    logic.add_instance("alpha.one")
    data = client.get("/altair/api/health").get_json()
    assert data["ok"] is True and data["widgets"] == 1
