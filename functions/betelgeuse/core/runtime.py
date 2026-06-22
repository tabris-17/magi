"""Startup runtime config shared by every entrypoint (app.py / serve.py / worker.py).

The dev/prod mode is a **mandatory** `--env` argument: a process refuses to start
without it. This is deliberate — dev and prod otherwise look identical, and running
the wrong one against the wrong data/machine is the mistake we want to make impossible.

Only the `__main__` blocks call `parse_env_arg()`, never import time, so importing
`app`/`worker` (as the test suite does) does not require the flag.
"""
import argparse


def parse_env_arg(argv=None):
    """Return the mandatory runtime mode ('dev' | 'prod') from `--env`.

    Uses `parse_known_args` so entrypoints that read other config elsewhere
    (e.g. serve.py reads BETELGEUSE_HOST/PORT from the environment) are unaffected.
    A missing or invalid `--env` makes argparse print usage and exit with code 2 —
    that hard stop *is* the "mandatory" enforcement.
    """
    parser = argparse.ArgumentParser(description='Betelgeuse runtime')
    parser.add_argument(
        '--env', required=True, choices=['dev', 'prod'],
        help='runtime mode (mandatory): dev or prod',
    )
    args, _ = parser.parse_known_args(argv)
    return args.env
