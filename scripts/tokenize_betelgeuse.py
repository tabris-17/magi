#!/usr/bin/env python3
"""Tokenize + recolor betelgeuse's hardcoded colors to the magi shell palette (M3b + dark-align).

Two passes, both idempotent:

1. **Tokenize <style> blocks** — replace betelgeuse's original slate-blue literals
   inside each template's <style> blocks with var(--bt-*) references defined in
   functions/betelgeuse/static/betelgeuse-theme.css (whose DARK values now mirror the
   magi shell's GitHub-dark palette — see that file). rgba() overlays become the base
   triple with alpha kept inline: rgba(R,G,B,A) -> rgba(var(--bt-X-rgb), A).

2. **Recolor literals everywhere else** — inline style="..." attributes, JS-built CSS
   strings (incl. static/*.js), and URL-encoded hex in data-URIs can't reference CSS
   vars safely, so the <style> tokenizer skips them. This pass rewrites the same
   slate-blue palette to its GitHub-dark LITERAL equivalent (a plain hex/triple, valid
   in any context) so betelgeuse's dark mode matches the shell there too.

header.html is excluded (hand-authored magi shell — not re-copied from prod).
Run after re-copying betelgeuse from prod (with scripts/prefix_betelgeuse.py):

    python3 scripts/tokenize_betelgeuse.py
"""
import glob
import os
import re

PKG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "functions", "betelgeuse")
TPL = os.path.join(PKG, "templates")
STATIC = os.path.join(PKG, "static")
EXCLUDE = {"header.html"}

# --- pass 1: solid hex -> token (lowercase keys), for <style> blocks ---------
HEX = {
    "#0f172a": "--bt-bg", "#1e293b": "--bt-surface", "#0d1929": "--bt-bg-deep",
    "#334155": "--bt-surface-3",
    "#f1f5f9": "--bt-fg-bright", "#e2e8f0": "--bt-fg", "#cbd5e1": "--bt-fg-2",
    "#94a3b8": "--bt-fg-muted", "#64748b": "--bt-fg-subtle", "#475569": "--bt-fg-faint",
    "#4b5563": "--bt-line-strong",
    "#3b82f6": "--bt-accent", "#60a5fa": "--bt-accent-2", "#93c5fd": "--bt-accent-3",
    "#bfdbfe": "--bt-accent-4",
    "#4ade80": "--bt-up", "#22c55e": "--bt-up-strong", "#86efac": "--bt-up-soft",
    "#34d399": "--bt-up-teal", "#10b981": "--bt-green", "#059669": "--bt-green-deep",
    "#f87171": "--bt-down", "#ef4444": "--bt-down-strong", "#fca5a5": "--bt-down-soft",
    "#fbbf24": "--bt-warn", "#f59e0b": "--bt-warn-strong", "#fcd34d": "--bt-warn-soft",
    "#eab308": "--bt-warn-2",
}
# left as literals on purpose: #fff (button text), rare 1–3-use chart accents.

# rgba base triple -> token (alpha kept inline). (0,0,0) deliberately omitted (shadows).
RGB = {
    (148, 163, 184): "--bt-slate-rgb", (15, 23, 42): "--bt-bg-rgb",
    (30, 41, 59): "--bt-surface-rgb", (59, 130, 246): "--bt-accent-rgb",
    (96, 165, 250): "--bt-accent2-rgb", (34, 197, 94): "--bt-up-rgb",
    (74, 222, 128): "--bt-up2-rgb", (134, 239, 172): "--bt-up3-rgb",
    (248, 113, 113): "--bt-down-rgb", (239, 68, 68): "--bt-down2-rgb",
    (245, 158, 11): "--bt-warn-rgb", (251, 191, 36): "--bt-warn2-rgb",
    (100, 116, 139): "--bt-subtle-rgb",
}

# --- pass 2: old slate-blue literal -> new GitHub-dark literal (== the dark value
#     of each --bt-* token in betelgeuse-theme.css). Applied outside <style> blocks. ---
RECOLOR_HEX = {
    "#0f172a": "#0d1117", "#1e293b": "#161b22", "#0d1929": "#010409", "#334155": "#21262d",
    "#f1f5f9": "#f0f6fc", "#e2e8f0": "#e6edf3", "#cbd5e1": "#c9d1d9", "#94a3b8": "#7d8590",
    "#64748b": "#6e7681", "#475569": "#484f58", "#4b5563": "#30363d",
    "#3b82f6": "#2f81f7", "#60a5fa": "#58a6ff", "#93c5fd": "#79c0ff", "#bfdbfe": "#a5d6ff",
    "#4ade80": "#3fb950", "#22c55e": "#2ea043", "#86efac": "#56d364", "#34d399": "#3fb950",
    "#10b981": "#2ea043", "#059669": "#238636",
    "#f87171": "#f85149", "#ef4444": "#da3633", "#fca5a5": "#ff7b72",
    "#fbbf24": "#d29922", "#f59e0b": "#bb8009", "#fcd34d": "#e3b341", "#eab308": "#d29922",
}
RECOLOR_RGB = {
    (148, 163, 184): (125, 133, 144), (15, 23, 42): (13, 17, 23), (30, 41, 59): (22, 27, 34),
    (59, 130, 246): (47, 129, 247), (96, 165, 250): (88, 166, 255), (34, 197, 94): (46, 160, 67),
    (74, 222, 128): (63, 185, 80), (134, 239, 172): (86, 211, 100), (248, 113, 113): (248, 81, 73),
    (239, 68, 68): (218, 54, 51), (245, 158, 11): (187, 128, 9), (251, 191, 36): (210, 153, 34),
    (100, 116, 139): (110, 118, 129),
}

HEX_RE = re.compile(r"#[0-9a-fA-F]{6}(?![0-9a-fA-F])")
ENC_RE = re.compile(r"%23[0-9a-fA-F]{6}(?![0-9a-fA-F])")  # URL-encoded #hex in data-URIs
RGBA_RE = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([0-9.]+)\s*\)")
STYLE_RE = re.compile(r"(<style[^>]*>)(.*?)(</style>)", re.S | re.I)


def _hex(m):
    return f"var({HEX[m.group(0).lower()]})" if m.group(0).lower() in HEX else m.group(0)


def _rgba(m):
    rgb = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return f"rgba(var({RGB[rgb]}), {m.group(4)})" if rgb in RGB else m.group(0)


def tokenize_css(css):
    return RGBA_RE.sub(_rgba, HEX_RE.sub(_hex, css))


def _recolor_hex(m):
    return RECOLOR_HEX.get(m.group(0).lower(), m.group(0))


def _recolor_enc(m):
    lit = "#" + m.group(0)[3:].lower()
    return ("%23" + RECOLOR_HEX[lit][1:]) if lit in RECOLOR_HEX else m.group(0)


def _recolor_rgba(m):
    rgb = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if rgb not in RECOLOR_RGB:
        return m.group(0)
    r, g, b = RECOLOR_RGB[rgb]  # RGBA_RE always captures an alpha (group 4)
    return f"rgba({r},{g},{b}, {m.group(4)})"


def recolor_literals(text):
    """Recolor the slate-blue palette outside <style> blocks (inline styles / JS / data-URIs)."""
    text = HEX_RE.sub(_recolor_hex, text)
    text = ENC_RE.sub(_recolor_enc, text)
    text = RGBA_RE.sub(_recolor_rgba, text)
    return text


def _write_if_changed(path, before, after, results):
    if after != before:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(after)
        results.append(os.path.basename(path))


def main():
    results = []
    for fn in sorted(os.listdir(TPL)):
        if not fn.endswith(".html") or fn in EXCLUDE:
            continue
        p = os.path.join(TPL, fn)
        with open(p, encoding="utf-8") as fh:
            src = before = fh.read()
        src = STYLE_RE.sub(lambda m: m.group(1) + tokenize_css(m.group(2)) + m.group(3), src)
        src = recolor_literals(src)
        _write_if_changed(p, before, src, results)

    for p in sorted(glob.glob(os.path.join(STATIC, "*.js"))):
        with open(p, encoding="utf-8") as fh:
            src = before = fh.read()
        src = recolor_literals(src)  # JS-built CSS strings — recolor only, never tokenize
        _write_if_changed(p, before, src, results)

    print("updated:", ", ".join(results) if results else "(nothing — already aligned)")


if __name__ == "__main__":
    main()
