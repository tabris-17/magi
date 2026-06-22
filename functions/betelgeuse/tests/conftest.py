"""Shared pytest fixtures for the Betelgeuse test pack.

Every test runs against a throwaway SQLite DB in a tmp dir — `app.DATABASE` is
monkeypatched so nothing ever touches the real `portfolio.db`. Network is never
hit: tests that exercise a downloader/notifier monkeypatch `app.requests`.
"""
import os
import sys

import pytest

# Make the repo root importable regardless of pytest's rootdir insertion order.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402
from core import db as core_db  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Point the DB path at a fresh temp file and initialize the schema.

    `get_db_connection`/`init_db` live in core.db and read `core.db.DATABASE`, so
    that is the binding we must patch for isolation (app.DATABASE is just a
    re-exported copy; we keep it in sync for any code that reads it directly).
    """
    db_path = tmp_path / "test_portfolio.db"
    monkeypatch.setattr(core_db, "DATABASE", str(db_path))
    monkeypatch.setattr(app, "DATABASE", str(db_path))
    app.init_db()
    return db_path


@pytest.fixture
def conn(db):
    """A live connection to the temp DB (row_factory set). Closed at teardown."""
    c = app.get_db_connection()
    yield c
    c.close()


@pytest.fixture
def client(db):
    """Flask test client bound to the temp DB."""
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c


@pytest.fixture
def setval(db):
    """Helper to upsert a settings key/value into the temp DB."""
    def _set(key, value):
        c = app.get_db_connection()
        try:
            cur = c.cursor()
            cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
            c.commit()
        finally:
            c.close()
    return _set


class FakeResponse:
    """Minimal stand-in for a requests.Response used to mock network calls."""
    def __init__(self, json_data=None, status_code=200, content=b"", text=""):
        self._json = json_data
        self.status_code = status_code
        self.content = content
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")
