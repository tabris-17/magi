"""Centralized logging for every Betelgeuse process (web, worker, CLI).

One helper — ``configure_logging(process_name, env)`` — sets up the shared
``betelgeuse`` parent logger with a rotating file handler (in ``config.LOG_DIR``)
plus a console handler, so all processes log consistently with a timestamp, level,
and logger name. Modules obtain a child logger via ``get_logger('worker')`` (→
``betelgeuse.worker``) and NEVER configure handlers themselves — they just log.

Design notes:
- The ``betelgeuse`` root logger owns the handlers and has ``propagate=False`` so
  records don't also bubble to the Python root (no double lines). Child loggers
  (``betelgeuse.*``) propagate up to it by default — that's how their records reach
  the handlers.
- Idempotent: calling ``configure_logging`` more than once per process (e.g. app
  import + ``serve.py``) won't double-attach handlers.
- File logging is best-effort: if ``LOG_DIR`` can't be created the console handler
  still works (and tests, which never call ``configure_logging``, simply stay quiet).
- The console handler is captured by launchd's StandardOutPath on prod and shows up
  in the terminal on dev; the rotating file is the durable, size-bounded record.
"""
import logging
import os
from logging.handlers import RotatingFileHandler

from core.config import LOG_DIR

_ROOT_LOGGER = 'betelgeuse'
_FORMAT = '%(asctime)s %(levelname)s %(name)s: %(message)s'
_MAX_BYTES = 2_000_000
_BACKUP_COUNT = 5


def configure_logging(process_name, env='dev', level=logging.INFO):
    """Configure the shared ``betelgeuse`` logger once for this process.

    Writes to ``<LOG_DIR>/<process_name>.app.log`` (rotating) AND the console.
    ``process_name`` is e.g. ``'web'`` or ``'worker'``. Safe to call repeatedly.
    Returns the configured logger.
    """
    logger = logging.getLogger(_ROOT_LOGGER)
    logger.setLevel(level)
    logger.propagate = False
    if getattr(logger, '_betelgeuse_configured', False):
        return logger

    fmt = logging.Formatter(_FORMAT)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fh = RotatingFileHandler(
            os.path.join(LOG_DIR, f'{process_name}.app.log'),
            maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding='utf-8')
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        # File logging is best-effort — the console handler still works.
        pass

    logger._betelgeuse_configured = True
    logger.info('logging configured (process=%s env=%s dir=%s)', process_name, env, LOG_DIR)
    return logger


def get_logger(name):
    """Return a child logger under the ``betelgeuse`` namespace.

    ``get_logger('worker')`` → ``betelgeuse.worker``. A name already starting with
    ``betelgeuse`` is returned as-is.
    """
    if not name.startswith(_ROOT_LOGGER):
        name = f'{_ROOT_LOGGER}.{name}'
    return logging.getLogger(name)
