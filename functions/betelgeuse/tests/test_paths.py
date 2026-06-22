"""Runtime-data path resolution + serving.

Proves the single config seam (core.config.DATA_DIR and its derived paths) behaves:
- derived paths hang off DATA_DIR consistently;
- BETELGEUSE_DATA_DIR overrides the root (and the default is <repo>/data) — checked in
  an isolated child process so the parent's already-imported constants aren't disturbed;
- the /charts/<f> route serves files out of CHART_DIR (and 404s for misses);
- save_backtest_chart writes under BACKTEST_DIR;
- get_db_connection creates a missing data dir so sqlite never trips on it.
"""
import json
import os
import subprocess
import sys

import app
from core import config as core_config
import core.db as core_db
from tests.conftest import FakeResponse

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── derived-path consistency (in-process) ───────────────────────────────────────
class TestDerivedPaths:
    def test_db_chart_backtest_hang_off_data_dir(self):
        assert core_config.DB_PATH == os.path.join(core_config.DATA_DIR, 'portfolio.db')
        assert core_config.CHART_DIR == os.path.join(core_config.DATA_DIR, 'charts')
        assert core_config.BACKTEST_DIR == os.path.join(core_config.DATA_DIR, 'backtest')

    def test_log_dir_defaults_under_data_dir(self):
        # Only when the explicit log-dir override isn't set in the environment.
        if not os.environ.get('BETELGEUSE_LOG_DIR'):
            assert core_config.LOG_DIR == os.path.join(core_config.DATA_DIR, 'logs')

    def test_static_dir_stays_in_repo(self):
        # Committed front-end assets are code, not runtime data — they stay put.
        assert core_config.STATIC_DIR == os.path.join(_REPO_ROOT, 'static')

    def test_db_module_uses_config_path(self):
        # core.db binds the connect target to the configured path.
        assert core_db.DATABASE == core_config.DB_PATH


# ── env override / default root (isolated child process) ─────────────────────────
def _resolve_paths_in_child(env_overrides):
    """Import core.config in a fresh interpreter with env_overrides applied and return
    the resolved paths. Isolated so the parent's import-time constants are untouched."""
    env = dict(os.environ)
    # Start from a clean slate for the two knobs so the parent's env can't leak in.
    env.pop('BETELGEUSE_DATA_DIR', None)
    env.pop('BETELGEUSE_LOG_DIR', None)
    env.update(env_overrides)
    code = (
        'import json; from core import config as c; '
        'print(json.dumps({k: getattr(c, k) for k in '
        '["DATA_DIR","DB_PATH","CHART_DIR","BACKTEST_DIR","LOG_DIR","STATIC_DIR"]}))'
    )
    # check=True surfaces an import error as a failing test rather than a silent skip.
    out = subprocess.run([sys.executable, '-c', code], cwd=_REPO_ROOT, env=env,
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip())


class TestDataDirOverride:
    def test_env_var_relocates_every_runtime_path(self, tmp_path):
        ext = str(tmp_path / 'bg-data')
        p = _resolve_paths_in_child({'BETELGEUSE_DATA_DIR': ext})
        assert p['DATA_DIR'] == ext
        assert p['DB_PATH'] == os.path.join(ext, 'portfolio.db')
        assert p['CHART_DIR'] == os.path.join(ext, 'charts')
        assert p['BACKTEST_DIR'] == os.path.join(ext, 'backtest')
        assert p['LOG_DIR'] == os.path.join(ext, 'logs')
        # Code assets are NOT relocated by the data-dir override.
        assert p['STATIC_DIR'] == os.path.join(_REPO_ROOT, 'static')

    def test_default_root_is_repo_data_dir(self):
        p = _resolve_paths_in_child({})
        assert p['DATA_DIR'] == os.path.join(_REPO_ROOT, 'data')
        assert p['DB_PATH'] == os.path.join(_REPO_ROOT, 'data', 'portfolio.db')

    def test_log_dir_override_still_wins(self, tmp_path):
        logs = str(tmp_path / 'mylogs')
        p = _resolve_paths_in_child({'BETELGEUSE_DATA_DIR': str(tmp_path / 'd'),
                                     'BETELGEUSE_LOG_DIR': logs})
        assert p['LOG_DIR'] == logs            # explicit log override beats DATA_DIR/logs


# ── /charts/<filename> serving route ─────────────────────────────────────────────
class TestServeChart:
    def test_serves_file_from_chart_dir(self, client, tmp_path, monkeypatch):
        chart_dir = tmp_path / 'charts'
        chart_dir.mkdir()
        (chart_dir / 'chart_stock_us_AAPL_p30.png').write_bytes(b'PNGDATA')
        monkeypatch.setattr('core.config.CHART_DIR', str(chart_dir))

        resp = client.get('/charts/chart_stock_us_AAPL_p30.png')
        assert resp.status_code == 200
        assert resp.data == b'PNGDATA'

    def test_missing_chart_404(self, client, tmp_path, monkeypatch):
        chart_dir = tmp_path / 'charts'
        chart_dir.mkdir()
        monkeypatch.setattr('core.config.CHART_DIR', str(chart_dir))
        assert client.get('/charts/nope.png').status_code == 404


# ── backtest snapshots land under BACKTEST_DIR ───────────────────────────────────
class TestBacktestDir:
    def test_save_writes_under_backtest_dir(self, tmp_path, monkeypatch):
        bt = tmp_path / 'backtest'
        monkeypatch.setattr('core.config.BACKTEST_DIR', str(bt))
        monkeypatch.setattr(app.requests, 'get',
                            lambda *a, **k: FakeResponse(content=b'GIF89a', status_code=200))

        fn = app.save_backtest_chart('hk', 'Breakthrough', '00700.HK', '3m', 'http://x/chart.gif')
        assert fn is not None
        written = bt / 'training' / 'hk' / 'Breakthrough' / fn
        assert written.exists()
        assert written.read_bytes() == b'GIF89a'


# ── get_db_connection creates a missing data dir ─────────────────────────────────
class TestDbDirAutocreate:
    def test_connection_creates_missing_parent(self, tmp_path, monkeypatch):
        nested = tmp_path / 'deep' / 'data' / 'portfolio.db'
        assert not nested.parent.exists()
        monkeypatch.setattr(core_db, 'DATABASE', str(nested))
        conn = core_db.get_db_connection()
        try:
            assert nested.parent.is_dir()       # makedirs ran
            conn.execute('SELECT 1')            # usable connection
        finally:
            conn.close()
