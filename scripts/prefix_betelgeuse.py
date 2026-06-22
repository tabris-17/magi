#!/usr/bin/env python3
"""Rewrite betelgeuse's hardcoded absolute URLs to live under the /betelgeuse mount.

betelgeuse is mounted at /betelgeuse via DispatcherMiddleware (see magi.py). Flask's
url_for + static get the prefix automatically (SCRIPT_NAME), but the app also uses
hardcoded absolute paths in templates and JS (fetch('/api/…'), href="/portfolio", …).
This rewrites those literal strings to include the prefix.

Idempotent: an already-prefixed '/betelgeuse/…' isn't matched again (the segment
right after the quote is 'betelgeuse', which isn't in OWNED). Re-run after copying a
fresh betelgeuse from prod.

    python3 scripts/prefix_betelgeuse.py
"""
import os
import re

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "functions", "betelgeuse")
PREFIX = "/betelgeuse"

# Path segments betelgeuse owns and serves itself.
OWNED = "api|portfolio|groups|notifications|settings|training|tracker|crypto|charts|static"

# A string literal opener (quote / single / backtick / paren) + /segment, where the
# segment is followed by a path boundary. Captures the opener so it's preserved.
OPEN = "\"'`("
BOUND = "/\"'`?)#"  # chars that can validly follow the segment
PAT = re.compile(rf"([{re.escape(OPEN)}])/({OWNED})(?=[{re.escape(BOUND)}]|\s)")

# Bare root link: href="/"  ->  href="/betelgeuse/"
ROOT_HREF = re.compile(r"(href=[\"'])/([\"'])")


def fix(text):
    text, n1 = PAT.subn(rf"\1{PREFIX}/\2", text)
    text, n2 = ROOT_HREF.subn(rf"\1{PREFIX}/\2", text)
    return text, n1 + n2


def main():
    total = 0
    for sub, exts in (("templates", (".html",)), ("static", (".js",))):
        base = os.path.join(ROOT, sub)
        for dirpath, _, files in os.walk(base):
            for fn in files:
                if not fn.endswith(exts):
                    continue
                p = os.path.join(dirpath, fn)
                with open(p, encoding="utf-8") as fh:
                    src = fh.read()
                out, n = fix(src)
                if n:
                    with open(p, "w", encoding="utf-8") as fh:
                        fh.write(out)
                    total += n
                    print(f"  {n:4d}  {os.path.relpath(p, ROOT)}")
    print(f"total rewrites: {total}")


if __name__ == "__main__":
    main()
