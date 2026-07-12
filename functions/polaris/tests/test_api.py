"""Polaris blueprint battery — the JSON API + media routes exercised through a Flask
test client (the two HTML page routes render the host's base.html, so they're covered by
the logic + JS suites, not here). Confirms status codes, error shapes, the tags-on-save
path, the upload guards, and the inline-vs-download security gate on /media."""
import io

import pytest
from flask import Flask

from functions.polaris import bp, logic


@pytest.fixture
def client():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(bp)
    return app.test_client()


def _new(client, **body):
    return client.post("/polaris/api/entries", json=body).get_json()["entry"]


# ---- entries ---------------------------------------------------------------------------

def test_entry_create_get_update_delete(client):
    e = _new(client, title="hello", body="world")
    eid = e["id"]

    got = client.get(f"/polaris/api/entries/{eid}").get_json()["entry"]
    assert got["title"] == "hello"

    upd = client.post("/polaris/api/entries", json={"id": eid, "title": "hi again"})
    assert upd.get_json()["entry"]["title"] == "hi again"

    assert client.delete(f"/polaris/api/entries/{eid}").get_json() == {"ok": True}
    assert client.get(f"/polaris/api/entries/{eid}").status_code == 404


def test_update_missing_entry_404(client):
    r = client.post("/polaris/api/entries", json={"id": 99999, "title": "x"})
    assert r.status_code == 404
    assert r.get_json()["error"] == "no entry 99999"


def test_delete_missing_entry_404(client):
    assert client.delete("/polaris/api/entries/4242").status_code == 404


def test_list_and_search(client):
    _new(client, date="2026-07-05", title="Groceries", body="milk")
    _new(client, date="2026-08-05", title="Other", body="stuff")
    all_ = client.get("/polaris/api/entries").get_json()["entries"]
    assert {e["title"] for e in all_} == {"Groceries", "Other"}
    hits = client.get("/polaris/api/entries?q=milk").get_json()["entries"]
    assert [e["title"] for e in hits] == ["Groceries"]


def test_save_replaces_tag_set(client):
    tag = client.post("/polaris/api/tags", json={"name": "work"}).get_json()["tag"]
    e = _new(client, title="t", tags=[tag["id"], 99999])   # unknown id dropped
    assert [t["name"] for t in e["tags"]] == ["work"]
    # re-saving without a tags key must leave the set untouched (not wipe it)
    again = client.post("/polaris/api/entries", json={"id": e["id"], "title": "t2"}).get_json()["entry"]
    assert [t["name"] for t in again["tags"]] == ["work"]
    # explicitly clearing with an empty tags list does replace the set
    cleared = client.post("/polaris/api/entries", json={"id": e["id"], "tags": []}).get_json()["entry"]
    assert cleared["tags"] == []


# ---- tags ------------------------------------------------------------------------------

def test_tag_crud_and_conflicts(client):
    created = client.post("/polaris/api/tags", json={"name": "Work", "emoji": "book"})
    assert created.status_code == 200
    tid = created.get_json()["tag"]["id"]

    assert client.post("/polaris/api/tags", json={"name": "work"}).status_code == 400   # dup NOCASE
    assert client.post("/polaris/api/tags", json={"name": "  "}).status_code == 400     # empty

    assert client.get("/polaris/api/tags").get_json()["tags"][0]["name"] == "Work"

    assert client.post(f"/polaris/api/tags/{tid}", json={"name": "Deep Work"}).get_json() == {"ok": True}
    assert client.post(f"/polaris/api/tags/{tid}", json={"emoji": "star"}).status_code == 200

    client.post("/polaris/api/tags", json={"name": "taken"})
    assert client.post(f"/polaris/api/tags/{tid}", json={"name": "taken"}).status_code == 400  # rename conflict
    assert client.post("/polaris/api/tags/99999", json={"name": "x"}).status_code == 404       # missing

    assert client.delete(f"/polaris/api/tags/{tid}").get_json() == {"ok": True}
    assert client.delete(f"/polaris/api/tags/{tid}").status_code == 404


# ---- attachments -----------------------------------------------------------------------

def _upload(client, eid, name, mime, data):
    return client.post(
        f"/polaris/api/entries/{eid}/attachments",
        data={"file": (io.BytesIO(data), name, mime)},
        content_type="multipart/form-data",
    )


def test_attachment_upload_and_guards(client):
    e = _new(client, title="t")
    eid = e["id"]

    ok = _upload(client, eid, "pic.png", "image/png", b"\x89PNG-data")
    assert ok.status_code == 200
    att = ok.get_json()["attachment"]
    assert att["size"] == len(b"\x89PNG-data") and att["inline"] is True

    # no file part / empty file / oversize
    assert client.post(f"/polaris/api/entries/{eid}/attachments",
                       data={}, content_type="multipart/form-data").status_code == 400
    assert _upload(client, eid, "e.png", "image/png", b"").status_code == 400
    assert _upload(client, 99999, "x.png", "image/png", b"x").status_code == 404


def test_attachment_oversize_413(client, monkeypatch):
    monkeypatch.setattr(logic, "ATTACH_MAX_BYTES", 4)      # tiny cap so we needn't send 25MB
    e = _new(client, title="t")
    r = _upload(client, e["id"], "big.png", "image/png", b"12345")
    assert r.status_code == 413


def test_attachment_delete(client):
    e = _new(client, title="t")
    att = _upload(client, e["id"], "f.png", "image/png", b"bytes").get_json()["attachment"]
    assert client.delete(f"/polaris/api/attachments/{att['id']}").get_json() == {"ok": True}
    assert client.delete(f"/polaris/api/attachments/{att['id']}").status_code == 404


# ---- media (the inline-vs-download security gate) --------------------------------------

def test_media_serves_png_inline_svg_as_download(client):
    e = _new(client, title="t")
    png = _upload(client, e["id"], "pic.png", "image/png", b"\x89PNG").get_json()["attachment"]
    svg = _upload(client, e["id"], "x.svg", "image/svg+xml",
                  b"<svg xmlns='http://www.w3.org/2000/svg'/>").get_json()["attachment"]

    r_png = client.get(f"/polaris/media/{png['id']}")
    assert r_png.status_code == 200
    assert r_png.headers["X-Content-Type-Options"] == "nosniff"
    assert "attachment" not in r_png.headers.get("Content-Disposition", "")   # inline

    r_svg = client.get(f"/polaris/media/{svg['id']}")
    assert "attachment" in r_svg.headers["Content-Disposition"]                # forced download
    assert r_svg.headers["X-Content-Type-Options"] == "nosniff"

    assert client.get("/polaris/media/99999").status_code == 404


# ---- health ----------------------------------------------------------------------------

def test_health_endpoint(client):
    _new(client, title="t")
    st = client.get("/polaris/api/health").get_json()
    assert st["ok"] is True and st["entries"] == 1
