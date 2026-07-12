"""Shared fixtures for the polaris Python suite.

The logic module keeps its DB/backup paths and the once-per-process schema guard in
module globals. Each test gets a throwaway data dir and a reset guard, so the suite
never touches the real journal and every test re-runs the schema path from scratch.

Run from the repo root (no dev server needed — unlike tests/run.sh):

    python3 -m pytest functions/polaris/tests/ -q
"""
import os
import sys

import pytest

# Make `functions.polaris` importable no matter where pytest is invoked from.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from functions.polaris import logic  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_db(tmp_path, monkeypatch):
    """Point polaris.logic at a throwaway DB + backup dir and reset the schema guard."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(logic, "DATA_DIR", str(data))
    monkeypatch.setattr(logic, "DB_PATH", str(data / "polaris.db"))
    monkeypatch.setattr(logic, "BACKUP_DIR", str(data / "backup"))
    logic._schema_checked = False
    yield
    logic._schema_checked = False
