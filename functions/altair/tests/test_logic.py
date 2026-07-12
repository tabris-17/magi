"""Altair store + registry behavior — instance CRUD, ordering, guarded rendering."""
import pytest

from functions.altair import logic


# ---- registry / available types --------------------------------------------------------

def test_no_resolver_means_no_types():
    assert logic.available_types() == []


def test_available_types_excludes_render(registry):
    types = logic.available_types()
    assert [t["id"] for t in types] == ["alpha.one", "beta.two"]
    assert all("render" not in t and "mask" not in t for t in types)
    assert types[0]["params"][0]["name"] == "x"
    assert [t["maskable"] for t in types] == [True, False]


# ---- add ---------------------------------------------------------------------------------

def test_add_unknown_type_raises(registry):
    with pytest.raises(ValueError):
        logic.add_instance("nope.missing")


def test_add_without_registry_raises():
    with pytest.raises(ValueError):
        logic.add_instance("alpha.one")


def test_add_appends_and_keeps_declared_params_only(registry):
    a = logic.add_instance("alpha.one", {"x": 1, "junk": "dropped"})
    b = logic.add_instance("beta.two", "not-a-dict")
    assert a["config"] == {"x": "1"}          # stringified, junk filtered
    assert b["config"] == {}                  # non-dict config degrades to {}
    assert (a["position"], b["position"]) == (0, 1)
    assert a["known"] and a["label"] == "One" and a["source"] == "Alpha"


# ---- list / order ------------------------------------------------------------------------

def test_list_in_position_order_and_reorder(registry):
    a = logic.add_instance("alpha.one")
    b = logic.add_instance("beta.two")
    c = logic.add_instance("alpha.one")
    assert [i["id"] for i in logic.list_instances()] == [a["id"], b["id"], c["id"]]
    logic.reorder([c["id"], a["id"], b["id"]])
    assert [i["id"] for i in logic.list_instances()] == [c["id"], a["id"], b["id"]]


def test_reorder_ignores_unknown_ids(registry):
    a = logic.add_instance("alpha.one")
    logic.reorder([999999, a["id"]])          # unknown id is a no-op
    assert [i["id"] for i in logic.list_instances()] == [a["id"]]


def test_vanished_provider_still_listed_as_unknown(registry):
    a = logic.add_instance("alpha.one")
    logic.set_widget_registry_resolver(lambda: [])   # provider disappears
    (row,) = logic.list_instances()
    assert row["id"] == a["id"]
    assert row["known"] is False
    assert row["label"] == "alpha.one"        # falls back to the raw type id


# ---- the eye toggle (hidden) --------------------------------------------------------------

def test_hidden_defaults_false_and_persists(registry):
    a = logic.add_instance("alpha.one")
    assert a["hidden"] is False
    assert logic.set_hidden(a["id"], True) is True
    (row,) = logic.list_instances()
    assert row["hidden"] is True
    logic.set_hidden(a["id"], False)
    (row,) = logic.list_instances()
    assert row["hidden"] is False


def test_set_hidden_missing_id(registry):
    assert logic.set_hidden(999, True) is False


def test_hidden_column_added_to_old_db(registry):
    """A pre-eye-toggle altair.db (no `hidden` column) gains it on connect."""
    import sqlite3
    os_makedirs = __import__("os").makedirs
    os_makedirs(logic.DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(logic.DB_PATH)
    conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "widget TEXT NOT NULL, config TEXT NOT NULL DEFAULT '{}', "
                 "position INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)")
    conn.execute("INSERT INTO widgets (widget, config, position, created_at) "
                 "VALUES ('alpha.one', '{}', 0, 'x')")
    conn.commit()
    conn.close()
    (row,) = logic.list_instances()   # triggers the column-add guard
    assert row["hidden"] is False
    assert logic.set_hidden(row["id"], True) is True


# ---- remove ------------------------------------------------------------------------------

def test_remove(registry):
    a = logic.add_instance("alpha.one")
    assert logic.remove_instance(a["id"]) is True
    assert logic.remove_instance(a["id"]) is False
    assert logic.list_instances() == []


# ---- render (guarded) ---------------------------------------------------------------------

def test_render_passes_config_and_returns_title_html(registry):
    _, calls = registry
    a = logic.add_instance("alpha.one", {"x": "42"})
    out = logic.render_instance(a["id"])
    assert out == {"ok": True, "masked": False, "title": "T:42", "html": "<b>hi</b>"}
    assert calls["config"] == {"x": "42"}


def test_render_hidden_maskable_uses_mask(registry):
    _, calls = registry
    a = logic.add_instance("alpha.one", {"x": "42"})
    logic.set_hidden(a["id"], True)
    out = logic.render_instance(a["id"])
    assert out == {"ok": True, "masked": True, "title": "T:masked", "html": "<b>•••••</b>"}
    assert calls["mask_config"] == {"x": "42"}
    (row,) = logic.list_instances()
    assert row["maskable"] is True


def test_render_hidden_without_mask_returns_nothing(registry):
    b = logic.add_instance("beta.two")
    logic.set_hidden(b["id"], True)
    out = logic.render_instance(b["id"])
    # no privacy view -> empty body server-side (and beta.two's raising render
    # was never called, or ok would be False)
    assert out == {"ok": True, "masked": True, "title": "Two", "html": ""}


def test_render_missing_instance_is_none(registry):
    assert logic.render_instance(12345) is None


def test_render_raising_widget_becomes_error_card(registry):
    b = logic.add_instance("beta.two")
    out = logic.render_instance(b["id"])
    assert out["ok"] is False
    assert out["title"] == "Two"
    assert "boom" in out["error"]


def test_render_vanished_provider_becomes_error_card(registry):
    a = logic.add_instance("alpha.one")
    logic.set_widget_registry_resolver(lambda: [])
    out = logic.render_instance(a["id"])
    assert out["ok"] is False
    assert "no longer available" in out["error"]


# ---- health --------------------------------------------------------------------------------

def test_status_counts(registry):
    logic.add_instance("alpha.one")
    logic.add_instance("beta.two")
    logic.set_widget_registry_resolver(
        lambda: [t for t in registry[0] if t["id"] == "alpha.one"])
    s = logic.status()
    assert s["ok"] is True
    assert s["widgets"] == 2
    assert s["unknown"] == 1                  # beta.two's provider is gone
    assert s["types_available"] == 1
    assert s["db"] is True
