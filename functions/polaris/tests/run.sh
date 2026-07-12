#!/usr/bin/env bash
# Polaris converter test battery. Run BEFORE touching static/polaris-md.js
# (and after — it must print ALL PASS both times).
#
#   ./magi launch dev -d          # the battery loads polaris-md.js from the dev server
#   functions/polaris/tests/run.sh
#
# Exit 0 on ALL PASS, 1 otherwise (prints the failures).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
OUT="$(mktemp -d)/battery.html"

if ! curl -sf -o /dev/null http://127.0.0.1:8080/static/polaris-md.js; then
  echo "run.sh: dev server not answering on :8080 — start it with ./magi launch dev -d" >&2
  exit 2
fi

"$CHROME" --headless=old --disable-gpu --no-sandbox \
  --user-data-dir="$(mktemp -d)" --virtual-time-budget=3000 \
  --dump-dom "file://$HERE/md-battery.html" > "$OUT" 2>/dev/null || true

python3 - "$OUT" <<'PY'
import html, pathlib, re, sys
d = pathlib.Path(sys.argv[1]).read_text(errors="replace")
m = re.search(r'<pre id="out">(.*?)</pre>', d, re.S)
if not m:
    print("run.sh: no test output captured (chrome failed?)"); sys.exit(1)
out = html.unescape(m.group(1))
fails = [l for l in out.splitlines() if l.startswith("FAIL")]
total = sum(1 for l in out.splitlines() if l.startswith(("ok", "FAIL")))
if fails or "ALL PASS" not in out:
    print("\n".join(fails) or out)
    print(f"{len(fails)} FAILURES / {total} cases"); sys.exit(1)
print(f"ALL PASS ({total} cases)")
PY
