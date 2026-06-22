"""The magi launcher — one place that starts the web app in dev or prod.

Picks the server by env: **dev** → werkzeug's dev server bound to 127.0.0.1 (this
machine only); **prod** → waitress, a real WSGI server bound to the LAN (the Mac mini,
via the com.magi.web LaunchAgent). One stable process serves the host + every mounted
function either way.

    python3 serve.py --env dev         # http://127.0.0.1:8080  (werkzeug)
    python3 serve.py --env prod        # http://0.0.0.0:8080    (waitress)

Normally invoked through the `./magi run` CLI; the LaunchAgent calls it directly.
`--env dev|prod` is applied as MAGI_ENV BEFORE importing magi, so the mounted
betelgeuse function starts in the right mode + honors its schema gate (see
magi.load_betelgeuse_wsgi). Functions' background workers (e.g. betelgeuse's
scheduler) run as separate processes; this serves the web only.
"""
import os
import sys


def parse_env(default=None):
    """--env dev|prod, else the MAGI_ENV env var (plist-set), else prod.

    The LaunchAgent passes `--env __ENV__` (templated at setup), so argv normally
    wins; MAGI_ENV is the fallback when launched without an explicit flag.
    """
    env = default or os.environ.get("MAGI_ENV", "prod")
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--env" and i + 1 < len(args):
            env = args[i + 1]
        elif a.startswith("--env="):
            env = a.split("=", 1)[1]
    if env not in ("dev", "prod"):
        print(f"serve.py: --env must be dev|prod (got {env!r})", file=sys.stderr)
        sys.exit(2)
    return env


PORT = int(os.environ.get("MAGI_PORT", "8080"))


if __name__ == "__main__":
    env = parse_env()
    os.environ["MAGI_ENV"] = env                     # before importing magi
    import magi                                       # noqa: E402

    if env == "dev":
        host = os.environ.get("MAGI_HOST", "127.0.0.1")  # this machine only
        from werkzeug.serving import run_simple        # noqa: E402
        print(f"\n  magi (dev) — werkzeug serving on {host}:{PORT}\n", flush=True)
        run_simple(host, PORT, magi.application, threaded=True, use_reloader=False)
    else:
        host = os.environ.get("MAGI_HOST", "0.0.0.0")    # LAN (trusted networks only)
        from waitress import serve                     # noqa: E402
        print(f"\n  magi (prod) — waitress serving on {host}:{PORT}\n", flush=True)
        serve(magi.application, host=host, port=PORT)
