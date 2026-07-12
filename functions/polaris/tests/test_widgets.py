"""Polaris's altair widget — entries_for_widget filters + the Journal-feed render.

Runs on the same isolated-DB conftest as the rest of the polaris suite.
"""
from datetime import date, timedelta

import pytest

from functions.polaris import logic, widgets


def _seed():
    """Three entries across time, two tagged 'work'."""
    today = date.today()
    old = (today - timedelta(days=400)).isoformat()      # > 1 year
    recent = (today - timedelta(days=10)).isoformat()    # inside 30d
    e1 = logic.save_entry(date=old, title="ancient", body="dusty words")
    e2 = logic.save_entry(date=recent, title="fresh", body="new words")
    e3 = logic.save_entry(date=today.isoformat(), title="today", body="now words")
    tag = logic.create_tag("work")
    logic.set_entry_tags(e1["id"], [tag["id"]])
    logic.set_entry_tags(e3["id"], [tag["id"]])
    return e1, e2, e3, tag


# ---- entries_for_widget -----------------------------------------------------------------

def test_entries_newest_first_with_limit():
    e1, e2, e3, _ = _seed()
    got = logic.entries_for_widget(limit=2)
    assert [g["id"] for g in got] == [e3["id"], e2["id"]]
    assert got[0]["preview"].startswith("now")


def test_tag_filter():
    e1, e2, e3, tag = _seed()
    got = logic.entries_for_widget(tag_id=tag["id"])
    assert [g["id"] for g in got] == [e3["id"], e1["id"]]


def test_date_window():
    e1, e2, e3, _ = _seed()
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    assert [g["id"] for g in logic.entries_for_widget(since=cutoff)] == [e3["id"], e2["id"]]
    assert [g["id"] for g in logic.entries_for_widget(before=cutoff)] == [e1["id"]]


def test_tag_and_window_combine():
    e1, e2, e3, tag = _seed()
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    got = logic.entries_for_widget(tag_id=tag["id"], since=cutoff)
    assert [g["id"] for g in got] == [e3["id"]]


# ---- the widget type / render -----------------------------------------------------------

def test_widget_types_tag_options_are_live():
    _seed()
    (t,) = widgets.widget_types()
    assert t["key"] == "tag-feed"
    tag_param = next(p for p in t["params"] if p["name"] == "tag")
    labels = [o["label"] for o in tag_param["options"]]
    assert labels[0] == "All entries" and "work" in labels
    age_param = next(p for p in t["params"] if p["name"] == "age")
    assert {"value": "older-365", "label": "Older than 1 year"} in age_param["options"]


def test_age_window():
    today = date.today()
    assert widgets._age_window("") == (None, None)
    since, before = widgets._age_window("30")
    assert since == (today - timedelta(days=30)).isoformat() and before is None
    since, before = widgets._age_window("older-365")
    assert since is None and before == (today - timedelta(days=365)).isoformat()


def test_render_all_entries():
    _seed()
    out = widgets.render_tag_feed({"tag": "", "age": "", "limit": "10"})
    assert out["title"] == "Journal"
    assert "today" in out["html"] and "/polaris/?entry=" in out["html"]


def test_render_tag_title_and_narrowing():
    e1, e2, e3, tag = _seed()
    out = widgets.render_tag_feed({"tag": str(tag["id"]), "age": "", "limit": "10"})
    assert out["title"] == "Journal · work"
    assert "fresh" not in out["html"]         # untagged entry filtered out


def test_render_older_than_a_year():
    _seed()
    out = widgets.render_tag_feed({"tag": "", "age": "older-365", "limit": "10"})
    assert "ancient" in out["html"] and "today" not in out["html"]


def test_render_escapes_title():
    logic.save_entry(title="<img src=x>", body="b")
    out = widgets.render_tag_feed({})
    assert "<img" not in out["html"] and "&lt;img" in out["html"]


def test_render_empty_and_missing_tag():
    out = widgets.render_tag_feed({"tag": "", "age": "", "limit": "5"})
    assert "No matching entries" in out["html"]
    with pytest.raises(RuntimeError):
        widgets.render_tag_feed({"tag": "9999"})
