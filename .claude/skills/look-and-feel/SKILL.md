---
name: look-and-feel
description: Audit and fix magi's UI so it stays consistent and pleasing across themes (dark/light) and, in future, viewports (desktop/mobile). Use when adding or changing any UI, when a screen looks wrong in one theme, when colors/contrast look off, or when asked to "clean up the look and feel" / check theming. Knows magi's token systems (shell --fg/--canvas/--accent + betelgeuse --bt-*) and the common breakages.
---

# look-and-feel — cross-theme / cross-viewport UI consistency

magi must look intentional in **both** themes (and, soon, on mobile). The #1 rule:
**every color comes from a theme token — never a hardcoded hex.** A hardcoded color
is correct in at most one theme; in the other it's a bug (dark text on dark, an
invisible card, glare). This skill is the process for finding and fixing those.

## Token architecture (the single sources of truth)

- **Shell / host** — `static/shell.css` defines `:root,[data-theme="dark"]` and
  `[data-theme="light"]` blocks: `--canvas` (page bg), `--surface`/`--surface-2`
  (cards/fills), `--border`, `--fg`/`--fg-muted`/`--fg-subtle`, `--accent`,
  `--danger*`/`--success*`/`--warn*` (+ `--danger-rgb`), `--shadow`. Host page
  components live in `static/theme.css` and **must** use these.
- **Betelgeuse content** — `functions/betelgeuse/static/betelgeuse-theme.css` defines
  `--bt-*` (bg/surface/fg/accent/up/down/warn + `*-rgb` triples for `rgba()` overlays:
  `rgba(var(--bt-slate-rgb), 0.1)`). Both themes are defined; **light values exist** —
  if a betelgeuse panel looks dark in light mode it's because it bypassed the tokens.
- `data-theme` is set on `<html>` (pre-paint script in `base.html` and betelgeuse's
  `header.html`); `localStorage('magi-theme')` is the no-flash cache, the host DB is
  source of truth. Don't add theme logic — reuse the tokens.

## The recurring failure modes (check these first)

1. **Pure-white glare.** A light theme whose `--canvas` AND `--surface` are both
   `#ffffff` is one flat bright sheet — no depth, harsh. **Light mode is NOT plain
   white.** Use a layered neutral scale: a soft off-white **canvas** (`#f6f8fa`-ish)
   with **white surfaces/cards on top** for elevation (the GitHub-light / Linear /
   Notion pattern). Text is dark-grey (`#1f2328`), not pure black. Keep `--shadow`
   light-tuned (low-alpha grey, not the dark `rgba(1,4,9,.6)`).
2. **Betelgeuse inline-style literals.** `scripts/tokenize_betelgeuse.py` **pass 2**
   bakes GitHub-**dark** hex *literals* into inline `style="…"`, JS-built CSS strings,
   and data-URIs. Plain inline styles and JS `el.style.x=` **can** use `var()` and so
   should be mapped to `--bt-*` (theme-adaptive); only real data-URIs (`%23…` SVG in
   `url()`) and SVG `fill=`/canvas `fillStyle` must keep literals. The map:
   `#e6edf3→var(--bt-fg)` · `#c9d1d9→var(--bt-fg-2)` · `#7d8590→var(--bt-fg-muted)` ·
   `#6e7681→var(--bt-fg-subtle)` · `#484f58→var(--bt-fg-faint)`. Dark-bg overlays
   `rgba(13,17,23, α)` used as a panel/field bg → a **solid token** with proper light
   values (`var(--bt-surface)` for cards, `var(--bt-surface-3)` for inset fields), NOT
   a re-based `rgba` (the α was tuned for a dark base and turns invisible or muddy).
3. **Alpha-tuned overlays.** `rgba(<dark triple>, α)` only works on a dark base. Swap
   to a token whose light/dark values are both defined, or to `rgba(var(--bt-slate-rgb),
   α)` (neutral, reads in both) — never keep a one-theme triple.
4. **Borrowed/hardcoded status colors** (greens/reds for P&L, badges) — must come from
   `--up/--down/--warn` (betelgeuse) or `--success/--danger/--warn` (host).

## The scale contract (sizes drift even when colors don't)

Tokens fix color; **nothing fixes size but discipline.** The recurring bug is a page
inventing its own button/heading metrics, so it looks "almost right" next to every other
page. The canonical scale lives in `CLAUDE.md` ("The UI scale contract"); the short form:

- **One button primitive: `.btn`** (`9px 16px`/`14px`) + `.primary` + **`.sm`**
  (`6px 12px`/`13px`). A page class may set layout only — never its own padding/font/bg.
- **Inputs** match `input[type=text]` (`9px 12px`/`14px`). That base rule is specificity
  `(0,1,1)`, so a single-class override `(0,1,0)` **silently loses** — use two classes.
- **Type:** 22/600 page title · 17/600 section heading · 15/600 card title · **14 body** ·
  13 secondary · 12/700 uppercase label · 11–11.5 micro. No 14.5px, no 21px.
- Tree/sidebar rows are navigation: ~26px tall, 12.5px — not body text.

**Scale audit** (run on the files you touched, before the theme screenshots):

```bash
# 1. one-off buttons: cursor:pointer AND its own padding AND font-size, not named .btn*.
#    Requiring BOTH is what keeps the signal clean — icon buttons (.pol-att-del) and nav
#    rows (.pol-node, .pol-entry) set only one of the two and are correctly not flagged.
python3 - <<'PY'
import re, pathlib
css = pathlib.Path("static/theme.css").read_text()
for m in re.finditer(r'^(\.[\w\-\. ]+)\s*\{([^}]*)\}', css, re.M):
    sel, body = m.group(1).strip(), m.group(2)
    # only the primitive itself is exempt — `.btn-env-save` is a one-off NAMED like it
    if re.match(r'^\.btn(\.|:|\s|$)', sel): continue
    if 'cursor: pointer' in body and 'padding:' in body and 'font-size:' in body:
        print('one-off button?', sel, '→ should this be `.btn` / `.btn.sm`?')
PY
# 2. off-scale type
grep -nE "font-size: (14\.5|21|19|17\.5)px" static/theme.css
```

Then: screenshot the new page **and an existing one** (`/youtube/`) at the same viewport and
confirm headings, buttons and inputs line up. A component that needs a size the primitives
lack gets a **new `.btn` modifier** in the "generic UI bits" block — never a page-local fork.

## Method (audit → fix → verify)

1. **Find the bypasses.** `grep -nE '#[0-9a-fA-F]{6}' <files>` over the changed/target
   templates + CSS; separate CSS-value hits (fixable → `var()`) from data-URI/`fill=`
   (leave). For betelgeuse: `grep -oE '#(c9d1d9|7d8590|6e7681|e6edf3|484f58)'` and
   `rgba\(13,17,23` show the literal debt. **Before any bulk replace**, grep the same
   colors in `fill="…"`, `fillStyle`, and `%23…` and confirm none (those can't take
   `var()`).
2. **Map to tokens** using the tables above. Prefer the closest semantic token
   (`fg-muted` for secondary text, `surface` for cards) over an exact hex match.
3. **Verify in BOTH themes — always screenshot, never eyeball the diff.** Run the dev
   server (`./magi run --env dev`), then headless Chrome for each theme. Set the theme
   by seeding `localStorage` before load (the pre-paint script reads `magi-theme`):

   ```bash
   CH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
   # quick path: the page defaults to the DB theme; to force one, use a CDP harness
   # (see scripts in this repo's session scratchpad) that does, before navigate:
   #   Page.addScriptToEvaluateOnNewDocument: localStorage.setItem('magi-theme','light')
   "$CH" --headless --force-device-scale-factor=2 --window-size=1240,900 \
     --virtual-time-budget=1800 --screenshot=out.png "http://127.0.0.1:8080/<path>"
   ```

   Capture the SAME screen in dark and light; read both. Check: text contrast (no
   ghost text), cards visibly elevated (not invisible, not a dark slab on light),
   inputs/buttons legible, accent/links visible, no glare. Crop with PIL to inspect.
4. **Templates are cached** (not debug) — restart the dev server after editing any
   `.html`; `shell.css`/`theme.css`/`*.css` are static and reload live.
5. **Guard betelgeuse:** `cd functions/betelgeuse && python3 -m pytest -q` must stay
   green; never touch `app.py`.

## Mobile (future viewport pass)

Same discipline, for width. The shell already has a `≤860px` drawer (`magiSidebar`/
`magiBackdrop`/`magiMenuBtn` in `shell.js`, `body{padding-left:260px}` desktop offset in
betelgeuse `header.html`). When doing a mobile pass: verify at `--window-size=390,844`
(iPhone) AND desktop; check the drawer opens/closes, tap targets ≥ 40px, tables scroll
(don't overflow the viewport), no fixed-width content wider than the screen, and the
`viewport-fit=cover` safe-area insets. Add breakpoints with `@media (max-width:860px)`,
reusing tokens.

## Caveats

- **Betelgeuse is vendored-only.** Direct fixes to `functions/betelgeuse/**` are
  clobbered when betelgeuse is re-vendored from upstream + re-run through
  `prefix_/tokenize_betelgeuse.py`. The **durable** fix for the inline-literal debt is
  to teach `scripts/tokenize_betelgeuse.py` pass 2 to emit `var(--bt-*)` for inline
  `style=`/JS-`.style` contexts (keeping literals only for data-URIs/`fill=`). Note this
  whenever you hand-fix vendored betelgeuse.
- Don't invent new tokens for a one-off; reuse the closest existing one. Only add a
  token (to BOTH theme blocks) when a genuinely new semantic role appears.
