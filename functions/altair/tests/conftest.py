"""Shared fixtures for the altair pytest suite.

Every test runs against a throwaway DB under tmp_path (never functions/altair/data/)
and a controllable fake widget registry — the suite needs no server and no host.
Run from the repo root:  python3 -m pytest functions/altair/tests/ -q
"""
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from functions.altair import logic  # noqa: E402


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Point the store at a per-test DB and reset the injected registry both sides."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(logic, "DATA_DIR", str(data))
    monkeypatch.setattr(logic, "DB_PATH", str(data / "altair.db"))
    logic.set_widget_registry_resolver(None)
    yield
    logic.set_widget_registry_resolver(None)


@pytest.fixture
def registry():
    """A two-type fake registry: alpha.one renders fine (recording its config) and is
    MASKABLE (has a privacy view); beta.two always raises and has no mask. Returns
    (types, calls) for assertions."""
    calls = {}

    def render_ok(config):
        calls["config"] = config
        return {"html": "<b>hi</b>", "title": "T:" + config.get("x", "")}

    def mask_ok(config):
        calls["mask_config"] = config
        return {"html": "<b>•••••</b>", "title": "T:masked"}

    def render_boom(config):
        raise RuntimeError("boom")

    types = [
        {"id": "alpha.one", "source": "Alpha", "key": "one", "label": "One",
         "description": "d1", "params": [{"name": "x", "label": "X", "type": "text"}],
         "default_size": "1x4", "render": render_ok, "mask": mask_ok},
        {"id": "beta.two", "source": "Beta", "key": "two", "label": "Two",
         "description": "d2", "params": [], "render": render_boom},   # no default_size
    ]
    logic.set_widget_registry_resolver(lambda: types)
    return types, calls
