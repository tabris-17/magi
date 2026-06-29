"""magi — unified web control panel (Flask host + mounted function apps).

The host owns only the shell (templates/ + static/) and the shared /settings page.
Each capability is an isolated "function" under functions/<name>/. Lightweight
functions (youtube) are Flask blueprints registered on the host; heavier, formerly
standalone apps (betelgeuse) are mounted unchanged as WSGI sub-apps under a URL
prefix via DispatcherMiddleware — so the copy stays byte-for-byte in sync with prod.

Run:  ./magi run --env dev    # canonical CLI (dev → 127.0.0.1, prod → LAN)
      python3 magi.py         # dev shortcut — delegates to the serve.py launcher
"""
import json
import os
import sys
import time
import urllib.request

from flask import Flask, jsonify, render_template, request
from werkzeug.middleware.dispatcher import DispatcherMiddleware

from functions.youtube import bp as youtube_bp, META as YOUTUBE_META, logic as youtube_logic
from functions.taxation import bp as taxation_bp, META as TAXATION_META, logic as taxation_logic
from functions.notifier import bp as notifier_bp, META as NOTIFIER_META
from host import db as hostdb
from host import dbtool as host_dbtool
from host import telegram as host_telegram
from host.version import full_version

ROOT = os.path.dirname(os.path.abspath(__file__))
BETEL_DIR = os.path.join(ROOT, "functions", "betelgeuse")

# dev|prod — set by serve.py (prod) before import; defaults to dev for `python3 magi.py`.
# Surfaced to the shell (sidebar mode chip + dev red brand) and to /api/settings.
APP_ENV = os.environ.get("MAGI_ENV", "dev")

# In dev, the shell shows a one-click "open prod" link to the always-on mini.
# Machine-local default (the mini's browser-resolvable Bonjour URL — NOT the SSH
# alias), overridable via MAGI_PROD_URL; set it empty to hide the link.
PROD_URL = os.environ.get("MAGI_PROD_URL", "http://wklin3s-mac-mini.local:8080/")

# When this host process started (epoch seconds) — surfaced on the Health page as the
# host's "started_at"/uptime (so a dev probe of prod can show when prod last restarted).
APP_START_TIME = time.time()


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

FUNCTIONS = [YOUTUBE_META, TAXATION_META, NOTIFIER_META, BETELGEUSE_META]


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
    # Point the mounted betelgeuse (and any in-process consumer) at the host settings DB
    # so its Telegram sends read the APP-WIDE bot credentials (Tools -> Telegram), not its
    # own. The worker process reads the same DB via its own relative-path fallback.
    os.environ.setdefault("MAGI_HOST_DB", hostdb.DB_PATH)
    app.register_blueprint(youtube_bp)
    # Host injects the env-scoped download dir into the (otherwise self-contained) youtube
    # function, so it resolves the per-env `youtube_download_dir` setting without importing
    # the host. None/unset → youtube falls back to MAGI_YOUTUBE_DIR / its hardcoded default.
    youtube_logic.set_download_dir_resolver(lambda: hostdb.get_setting("youtube_download_dir"))
    app.register_blueprint(taxation_bp)
    # Same pattern: the host owns the RBA source URL (taxation_rba_url setting); the
    # taxation function reads it via this resolver without importing the host.
    taxation_logic.set_rba_url_resolver(lambda: hostdb.get_setting("taxation_rba_url"))
    # The Notifier is fully self-contained (reads the app-wide bot creds + its per-env enable
    # gate straight from the host DB files, no resolver injection needed). The shared magi
    # worker (worker.py) drives its schedule; the web app serves its page + Send Now.
    app.register_blueprint(notifier_bp)

    @app.context_processor
    def inject_nav():
        # nav_functions + app_version + app_env drive the sidebar on every host page
        # (app_env → the dev/prod mode chip + dev red brand via data-env; prod_url →
        # the dev-only "open prod" link).
        return {
            "nav_functions": FUNCTIONS,
            "app_version": full_version(),
            "app_env": APP_ENV,
            "prod_url": PROD_URL,
        }

    @app.route("/")
    def home():
        return render_template("home.html", active="home")

    @app.route("/favicon.ico")
    def favicon():
        # Browsers request /favicon.ico at the root regardless of <link> tags.
        return app.send_static_file("favicon.ico")

    @app.route("/health")
    def health_page():
        return render_template("health.html", active="health")

    @app.route("/api/health")
    def api_health():
        """Aggregated health for the host + every function (powers the Health page).

        Each function opts in with a `health` callable on its META; the host calls it
        guarded so one function's failure can't break the page. The values are opaque to
        the host — the UI color-codes them (ffmpeg, worker liveness, schema gate, …)."""
        functions = []
        for m in FUNCTIONS:
            entry = {"key": m["key"], "label": m["label"], "version": m.get("version", "")}
            fn = m.get("health")
            if callable(fn):
                try:
                    entry["health"] = fn()
                    entry["ok"] = True
                except Exception as e:  # noqa: BLE001
                    entry["ok"], entry["error"] = False, str(e)
            else:
                entry["ok"] = None  # function reports no health
            functions.append(entry)
        return jsonify(
            host={
                "name": "magi",
                "version": full_version(),
                "env": APP_ENV,
                "server_time": int(time.time() * 1000),
                "started_at": int(APP_START_TIME * 1000),
                "ok": True,
            },
            functions=functions,
        )

    @app.route("/api/prod/health")
    def api_prod_health():
        """Probe prod's aggregated /api/health server-side (dev → mini over the LAN).

        Done on the server (not the browser) to avoid CORS. Returns configured:false when
        not on dev or PROD_URL is empty; tolerates prod being down (reachable:false)."""
        if APP_ENV != "dev" or not PROD_URL:
            return jsonify(configured=False)
        base = PROD_URL.rstrip("/")
        probed_at = int(time.time() * 1000)
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=3) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            return jsonify(configured=True, reachable=True, base_url=base,
                           probed_at=probed_at, health=payload)
        except Exception as e:  # noqa: BLE001
            return jsonify(configured=True, reachable=False, base_url=base,
                           probed_at=probed_at, error=str(e))

    @app.route("/settings")
    def settings():
        # Settings -> General -> Appearance: the host's own look (theme) only.
        return render_template("settings.html", active="appearance")

    @app.route("/general/config")
    def config_page():
        # Settings -> General -> Config: GLOBAL, DB-backed app settings, aggregated from each
        # function's settings_section (a callable on its META returning {id,label,html}; the
        # function owns its storage + save route, the host only composes). Env-scoped "user
        # profile" settings are deliberately NOT here — they live on the function's own page.
        sections = []
        for meta in FUNCTIONS:
            fn = meta.get("settings_section")
            if callable(fn):
                sections.append(fn())
        return render_template("general_config.html", active="config", sections=sections)

    @app.route("/tools/database")
    def tools_database():
        # Settings -> Tools -> Database: a read-only browser over every magi-owned DB (host
        # data/magi.db + betelgeuse portfolio.db), categorized per database. The table list is
        # rendered server-side; row data loads on click from /api/db/<dbkey>/table/<name>.
        return render_template("tools_database.html", active="database",
                               databases=host_dbtool.list_all())

    @app.route("/api/db/tables")
    def api_db_tables():
        """All databases + their tables/row-counts (powers the Database page)."""
        return jsonify(databases=host_dbtool.list_all())

    @app.route("/api/db/<dbkey>/table/<name>")
    def api_db_table(dbkey, name):
        """Columns + a paginated page of rows for one table (read-only)."""
        try:
            page = int(request.args.get("page", 1))
            per_page = int(request.args.get("per_page", 50))
        except ValueError:
            page, per_page = 1, 50
        payload, err = host_dbtool.table_data(dbkey, name, page=page, per_page=per_page)
        if err:
            return jsonify(error=err), 404
        return jsonify(payload)

    @app.route("/tools/telegram")
    def tools_telegram():
        # Settings -> Tools -> Telegram: a short OVERVIEW. There is no shared bot — each
        # consumer (magi control, betelgeuse) owns its own bot, configured on its sub-page.
        consumers = [
            {"id": "magi", "label": "magi control", "url": "/tools/telegram/magi",
             "desc": "magi's own notifications (the Notifier function).",
             "configured": host_telegram.is_configured("magi")},
            {"id": "betelgeuse", "label": "betelgeuse", "url": "/tools/telegram/betelgeuse",
             "desc": "Betelgeuse's portfolio summaries.",
             "configured": host_telegram.is_configured("betelgeuse")},
        ]
        return render_template("tools_telegram.html", active="telegram", consumers=consumers)

    def _render_telegram_consumer(*, active, consumer, label, lead_html, note_html):
        # Settings -> Tools -> Telegram -> {magi control | betelgeuse}: this consumer's OWN bot
        # (token + chat id, GLOBAL — rendered server-side since the token is secret) PLUS a
        # per-env (dev/prod) enable toggle (telegram_<consumer>_enabled, SCOPED). Bot creds save
        # via POST /api/settings {key,value}; the enables via {key,value,env}. Test/Auto-detect
        # POST {consumer} to the /api/telegram/* routes.
        enable_key = f"telegram_{consumer}_enabled"
        cfg = host_telegram.get_config(consumer)
        return render_template(
            "tools_telegram_consumer.html", active=active, consumer=consumer,
            consumer_label=label, envs=list(hostdb.ENVS), current_env=APP_ENV,
            enable_key=enable_key, enabled=hostdb.env_config(enable_key).get(enable_key, {}),
            token_key=f"telegram_{consumer}_bot_token", chat_key=f"telegram_{consumer}_chat_id",
            bot_token=cfg["bot_token"], chat_id=cfg["chat_id"],
            configured=host_telegram.is_configured(consumer),
            lead_html=lead_html, note_html=note_html)

    @app.route("/tools/telegram/magi")
    def tools_telegram_magi():
        return _render_telegram_consumer(
            active="telegram-magi", consumer="magi", label="magi control",
            lead_html=("<strong>magi's own</strong> notification bot (the <strong>Notifier</strong> "
                       "function). Configure its bot below, then enable it per environment."),
            note_html=("Compose &amp; schedule reminders under "
                       "<a href=\"/notifier/\">Home → Notifier</a>."))

    @app.route("/tools/telegram/betelgeuse")
    def tools_telegram_betelgeuse():
        return _render_telegram_consumer(
            active="telegram-betelgeuse", consumer="betelgeuse", label="betelgeuse",
            lead_html=("<strong>Betelgeuse's</strong> notification bot (portfolio summaries). "
                       "Configure its bot below, then enable it per environment."),
            note_html=("Betelgeuse composes &amp; schedules its summaries on its own "
                       "<a href=\"/betelgeuse/notifications\">Notifications</a> page."))

    @app.route("/api/telegram/test", methods=["POST"])
    def api_telegram_test():
        """Send a connectivity test message via one consumer's bot (JSON {consumer})."""
        consumer = (request.get_json(silent=True) or {}).get("consumer")
        if consumer not in host_telegram.CONSUMERS:
            return jsonify(error="unknown consumer"), 400
        ok, err = host_telegram.test(consumer)
        if ok:
            return jsonify(success=True)
        return jsonify(error=err), 400

    @app.route("/api/telegram/detect-chat-id", methods=["POST"])
    def api_telegram_detect_chat_id():
        """Auto-detect the chat id from one consumer's bot recent updates (JSON {consumer})."""
        consumer = (request.get_json(silent=True) or {}).get("consumer")
        if consumer not in host_telegram.CONSUMERS:
            return jsonify(error="unknown consumer"), 400
        chat_id, err = host_telegram.detect_chat_id(consumer)
        if chat_id:
            return jsonify(chat_id=chat_id)
        return jsonify(error=err), 400

    @app.route("/api/settings", methods=["GET", "POST"])
    def api_settings():
        """Common (cross-function) settings, persisted in data/magi.db.
        GET -> {version, settings};  POST {key,value} -> persist one setting."""
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            key, value = data.get("key"), data.get("value")
            if not hostdb.is_valid(key, value):
                return jsonify(error="unknown setting or invalid value"), 400
            # Optional `env` targets a specific environment for env-scoped keys (the
            # settings page edits dev/prod side by side); ignored for global keys.
            env = data.get("env")
            if env is not None and env not in hostdb.ENVS:
                return jsonify(error="invalid env"), 400
            hostdb.set_setting(key, value, env=env)
            return jsonify(ok=True, key=key, value=value, env=env)
        # env + per-function versions let function pages (which fill the shell from
        # this endpoint) reflect the mode + show each function's version.
        functions = [
            {"key": m["key"], "label": m["label"], "version": m.get("version", "")}
            for m in FUNCTIONS
        ]
        return jsonify(
            version=full_version(),
            env=APP_ENV,
            prod_url=PROD_URL,
            envs=list(hostdb.ENVS),
            env_config=hostdb.env_config(),
            functions=functions,
            settings=hostdb.all_settings(),
        )

    return app


host_app = create_host_app()
betel_app = load_betelgeuse_wsgi()


def _betelgeuse_health():
    """Betelgeuse's own /api/health, called in-process via its Flask test client (the
    endpoint is reused unchanged — no betelgeuse code change). A 503 in maintenance mode
    still returns JSON (error/gate), which the Health page surfaces."""
    resp = betel_app.test_client().get("/api/health")
    return resp.get_json(silent=True) or {"error": f"HTTP {resp.status_code}"}


BETELGEUSE_META["health"] = _betelgeuse_health

# Give betelgeuse's templates the SAME shell context the host injects (base.html), so its
# hand-authored magi sidebar (header.html) renders the identical, dynamic function list —
# incl. functions added later (e.g. taxation) — plus the host version and the dev-only
# "open prod ↗" link. This keeps betelgeuse at the same UX level as the other functions
# instead of drifting from a hardcoded nav. Registered here (not in betelgeuse's app.py) so
# that file stays byte-identical to prod. betelgeuse's own context processor already supplies
# app_env/web_version; these keys don't collide.
@betel_app.context_processor
def _inject_shell_context():
    return {
        "nav_functions": FUNCTIONS,
        "app_version": full_version(),
        "prod_url": PROD_URL,
    }


application = DispatcherMiddleware(host_app, {"/betelgeuse": betel_app})


if __name__ == "__main__":
    # The launcher lives in serve.py — one place picks werkzeug-dev vs waitress-prod.
    # `python3 magi.py` stays a dev shortcut by delegating there (canonical: ./magi run).
    os.execv(sys.executable, [sys.executable, os.path.join(ROOT, "serve.py"),
                              "--env", os.environ.get("MAGI_ENV", "dev"), *sys.argv[1:]])
