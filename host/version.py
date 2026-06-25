"""magi application version — the host/shell version, distinct from any function's.

Bump VERSION on host-level changes (shell, settings, deploy). Functions keep their
own versions (e.g. betelgeuse's core.version.WEB_VERSION).
"""
APP_NAME = "magi"
VERSION = "1.8.0"


def full_version():
    """e.g. 'magi-1.0.0' — what the UI and /api/settings surface."""
    return f"{APP_NAME}-{VERSION}"
