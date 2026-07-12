#!/usr/bin/env bash
# Polaris converter test battery. Run BEFORE touching static/polaris-md.js
# (and after — it must print ALL PASS both times).
#
#   ./magi launch dev -d          # the battery tests the polaris-md.js the dev server serves
#   functions/polaris/tests/run.sh
#
# Exit 0 on ALL PASS, 1 otherwise (prints the failures).
#
# How it runs (two Chrome-149 workarounds, do not "simplify" back):
#   * The page is staged FILE-LOCAL: a file:// page whose <script src> points at
#     http://127.0.0.1:8080 stalls forever in headless (the Local Network Access
#     prompt can never be shown). We curl the served converter next to a rewritten
#     copy of the page, so we still test exactly the bytes the dev server serves.
#   * Chrome is driven over CDP, not --dump-dom: batch dump-dom hangs on this page
#     (the contenteditable/execCommand flow never signals batch-mode completion),
#     while a CDP navigate + read of #out finishes in seconds.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

STAGE="$(mktemp -d)"
if ! curl -sf -o "$STAGE/polaris-md.js" http://127.0.0.1:8080/static/polaris-md.js; then
  echo "run.sh: dev server not answering on :8080 — start it with ./magi launch dev -d" >&2
  exit 2
fi
sed 's|http://127.0.0.1:8080/static/polaris-md.js|polaris-md.js|' \
  "$HERE/md-battery.html" > "$STAGE/md-battery.html"

node - "$STAGE" "$CHROME" <<'EOF'
const [STAGE, CHROME] = process.argv.slice(2);
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const udd = fs.mkdtempSync(path.join(require('node:os').tmpdir(), 'polaris-battery-'));
  const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox',
    '--remote-debugging-port=0', `--user-data-dir=${udd}`, 'about:blank']);
  const die = (msg, code) => { console.log(msg); chrome.kill(); process.exit(code); };

  // port 0 → Chrome writes the actual port to DevToolsActivePort
  const portFile = path.join(udd, 'DevToolsActivePort');
  let port = null;
  for (let i = 0; i < 50 && !port; i++) {
    await sleep(200);
    try { port = fs.readFileSync(portFile, 'utf8').split('\n')[0].trim(); } catch {}
  }
  if (!port) die('run.sh: chrome devtools port never appeared', 1);

  const target = (await (await fetch(`http://127.0.0.1:${port}/json`)).json())
    .find(t => t.type === 'page');
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise(r => ws.addEventListener('open', r, { once: true }));
  let n = 0; const pend = new Map();
  ws.addEventListener('message', e => {
    const m = JSON.parse(e.data);
    if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
  });
  const cmd = (method, params = {}) => {
    const i = ++n; ws.send(JSON.stringify({ id: i, method, params }));
    return new Promise(r => pend.set(i, r));
  };
  await cmd('Page.enable'); await cmd('Runtime.enable');
  await cmd('Page.navigate', { url: `file://${STAGE}/md-battery.html` });

  // the battery script is synchronous; poll #out until it moves off "running"
  let out = 'running';
  for (let i = 0; i < 60 && out === 'running'; i++) {
    await sleep(500);
    const r = await cmd('Runtime.evaluate', {
      expression: `(document.getElementById('out')||{}).textContent || 'NO OUT'`,
      returnByValue: true });
    out = r.result?.result?.value ?? 'running';
  }
  if (out === 'running' || out === 'NO OUT')
    die('run.sh: no test output captured (chrome failed?)', 1);

  const lines = out.split('\n');
  const fails = lines.filter(l => l.startsWith('FAIL'));
  const total = lines.filter(l => /^(ok|FAIL)/.test(l)).length;
  if (fails.length || !out.includes('ALL PASS')) {
    console.log(fails.join('\n') || out);
    die(`${fails.length} FAILURES / ${total} cases`, 1);
  }
  die(`ALL PASS (${total} cases)`, 0);
})().catch(e => { console.log('run.sh: ' + e.message); process.exit(1); });
EOF
