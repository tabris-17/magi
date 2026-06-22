"""magi — unified web control panel (Flask host + mounted function apps).

The host owns only the shell (templates/ + static/) and the shared /settings page.
Each capability is an isolated "function" under functions/<name>/. Lightweight
functions (youtube) are Flask blueprints registered on the host; heavier, formerly
standalone apps (betelgeuse) are mounted unchanged as WSGI sub-apps under a URL
prefix via DispatcherMiddleware — so the copy stays byte-for-byte in sync with prod.

Run:  ./magi run --env dev    # canonical CLI (dev → 127.0.0.1, prod → LAN)
      python3 magi.py         # dev shortcut — delegates to the serve.py launcher
"""
import os
import sys

from flask import Flask, jsonify, render_template, request
from werkzeug.middleware.dispatcher import DispatcherMiddleware

from functions.youtube import bp as youtube_bp, META as YOUTUBE_META
from host import db as hostdb
from host.version import full_version

ROOT = os.path.dirname(os.path.abspath(__file__))
BETEL_DIR = os.path.join(ROOT, "functions", "betelgeuse")

# dev|prod — set by serve.py (prod) before import; defaults to dev for `python3 magi.py`.
# Surfaced to the shell (sidebar mode chip + dev red brand) and to /api/settings.
APP_ENV = os.environ.get("MAGI_ENV", "dev")


def _betelgeuse_version():
    """Compose betelgeuse's app/server version label from its own constants.

    Betelgeuse's dir goes on sys.path (as load_betelgeuse_wsgi does) so `core.version`
    resolves to the vendored package. Done lazily so a missing/renamed constant degrades
    to a blank label instead of breaking host import.
    """
    if BETEL_DIR not in sys.path:
        sys.path.insert(0, BETEL_DIR)
    try:
        from core.version import app_version_string, server_version_string
        return f"{app_version_string()} · {server_version_string()}"
    except Exception:  # noqa: BLE001
        return ""

CHART_ICON = (
    '<svg width="16" height="16" viewBox="0 0 16 16"><path d="M1.5 1.75V13.5h13.75a.75.75 0 0 '
    '1 0 1.5H.75a.75.75 0 0 1-.75-.75V1.75a.75.75 0 0 1 1.5 0Zm14.28 2.53-5.25 5.25a.75.75 0 '
    '0 1-1.06 0L7 7.06 4.28 9.78a.751.751 0 0 1-1.042-.018.751.751 0 0 1-.018-1.042l3.25-3.25a'
    '.75.75 0 0 1 1.06 0L10 7.94l4.72-4.72a.751.751 0 0 1 1.042.018.751.751 0 0 1 .018 '
    '1.042Z"/></svg>'
)
BETELGEUSE_META = {
    "key": "betelgeuse",
    "label": "Betelgeuse",
    "description": "Stock & crypto portfolio with custom technical indicators.",
    "icon": CHART_ICON,
    "url": "/betelgeuse/",
    "version": _betelgeuse_version(),
}

FUNCTIONS = [YOUTUBE_META, BETELGEUSE_META]


def load_betelgeuse_wsgi():
    """Import betelgeuse's Flask app unchanged and return its WSGI object.

    Its package dir goes on sys.path first so its `from core import …` / `import app`
    resolve exactly as standalone (keeps the vendored copy in sync with prod). All
    runtime paths in core.config are derived from __file__, so it finds
    functions/betelgeuse/data/ regardless of the host's working directory.

    Runs the same startup betelgeuse's own serve.py does (env, logging, init_db,
    schema gate), keyed off MAGI_ENV (dev|prod) so a prod deploy serves it as prod
    and honors its refuse-to-start migration gate.
    """
    env = os.environ.get("MAGI_ENV", "dev")
    if BETEL_DIR not in sys.path:
        sys.path.insert(0, BETEL_DIR)
    import app as betelgeuse  # functions/betelgeuse/app.py  (module name 'app')
    betelgeuse.APP_ENV = env
    betelgeuse.configure_logging("web", env)
    betelgeuse.init_db()
    # On a schema mismatch this puts betelgeuse into its maintenance page (it does
    # NOT auto-migrate) — deploys migrate first via migrate_all.py.
    betelgeuse.refresh_migration_gate()
    return betelgeuse.app


def create_host_app():
    app = Flask(__name__, template_folder="templates", static_folder="static")
    hostdb.ensure_schema()  # create data/magi.db (common settings store) if missing
    app.register_blueprint(youtube_bp)

    @app.context_processor
    def inject_nav():
        # nav_functions + app_version + app_env drive the sidebar on every host page
        # (app_env → the dev/prod mode chip + dev red brand via data-env).
        return {
            "nav_functions": FUNCTIONS,
            "app_version": full_version(),
            "app_env": APP_ENV,
        }

    @app.route("/")
    def home():
        return render_template("home.html", active="home")

    @app.route("/settings")
    def settings():
        # Collect each function's optional settings section (kept inside its package).
        sections = []
        for meta in FUNCTIONS:
            fn = meta.get("settings_section")
            if callable(fn):
                sections.append(fn())
        return render_template("settings.html", active="settings", sections=sections)

    @app.route("/api/settings", methods=["GET", "POST"])
    def api_settings():
        """Common (cross-function) settings, persisted in data/magi.db.
        GET -> {version, settings};  POST {key,value} -> persist one setting."""
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            key, value = data.get("key"), data.get("value")
            if not hostdb.is_valid(key, value):
                return jsonify(error="unknown setting or invalid value"), 400
            hostdb.set_setting(key, value)
            return jsonify(ok=True, key=key, value=value)
        # env + per-function versions let function pages (which fill the shell from
        # this endpoint) reflect the mode + show each function's version.
        functions = [
            {"key": m["key"], "label": m["label"], "version": m.get("version", "")}
            for m in FUNCTIONS
        ]
        return jsonify(
            version=full_version(),
            env=APP_ENV,
            functions=functions,
            settings=hostdb.all_settings(),
        )

    return app


host_app = create_host_app()
application = DispatcherMiddleware(host_app, {"/betelgeuse": load_betelgeuse_wsgi()})


if __name__ == "__main__":
    # The launcher lives in serve.py — one place picks werkzeug-dev vs waitress-prod.
    # `python3 magi.py` stays a dev shortcut by delegating there (canonical: ./magi run).
    os.execv(sys.executable, [sys.executable, os.path.join(ROOT, "serve.py"),
                              "--env", os.environ.get("MAGI_ENV", "dev"), *sys.argv[1:]])
