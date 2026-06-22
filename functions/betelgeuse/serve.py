"""Production web entrypoint (Mac mini server).

Runs the Flask app under waitress — a real WSGI server — instead of the Werkzeug
dev server, so there's no interactive debugger exposed on the LAN and it's one
stable process. Dev on the MacBook still uses `python3 app.py` (debug + reloader).

    python3 serve.py        # serves http://0.0.0.0:8000

The recurring-notification scheduler still lives in worker.py — run that separately
(both are started as LaunchAgents on the mini; see deploy/).
"""
import os

from waitress import serve

import app
from core import runtime
from core.logging_setup import configure_logging, get_logger

HOST = os.environ.get('BETELGEUSE_HOST', '0.0.0.0')
PORT = int(os.environ.get('BETELGEUSE_PORT', '8000'))

if __name__ == '__main__':
    app.APP_ENV = runtime.parse_env_arg()  # mandatory --env dev|prod
    configure_logging('web', app.APP_ENV)
    log = get_logger('web')
    app.init_db()
    # Refuse to serve the normal app against a mismatched DB: on a version gap the web
    # process stays up but serves only the (read-only, on prod) migration maintenance
    # page until `migrate.py` runs. Prod is normally migrated by deploy BEFORE this starts.
    if app.refresh_migration_gate() != 'OK':
        log.warning("[web] ⚠ DB schema gate is %s — serving the migration maintenance "
                    "page only. Run: python3 migrate.py up --env %s",
                    app.MIGRATION_GATE, app.APP_ENV)
    log.info("[web] %s (v%s) — waitress serving Betelgeuse on %s:%s",
             app.APP_ENV, app.WEB_VERSION, HOST, PORT)
    serve(app.app, host=HOST, port=PORT)
