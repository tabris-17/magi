"""Application versions — the single source of truth for both processes.

The web app and the background worker are versioned **independently** (one day the
worker may be deployed on its own cadence). They start in lockstep at 1.0.

Convention: when asked to "bump the version" with no qualifier, bump BOTH constants
together. Bump only one when explicitly told (e.g. "bump the web version").
"""

APP_NAME = "betelgeuse"

WEB_VERSION = "1.5.4"
WORKER_VERSION = "1.5.4"


def app_version_string():
    """e.g. 'betelgeuse-app-1.5.4' — the web (Flask app) display label."""
    return f"{APP_NAME}-app-{WEB_VERSION}"


def server_version_string():
    """e.g. 'betelgeuse-server-1.5.4' — the worker/scheduler ("server") label."""
    return f"{APP_NAME}-server-{WORKER_VERSION}"
