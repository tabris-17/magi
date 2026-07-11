/* Polaris — Markdown ⇄ HTML for the journal's rich-text body.
 *
 * The body is EDITED as rich text (a contenteditable) but STORED as Markdown, so entries
 * stay plain, greppable, portable text (the LIKE search + tree snippets read them directly).
 *
 * This is a deliberately SMALL subset — not a CommonMark implementation:
 *   blocks   h1 h2 h3 · ul/li · ol/li · p
 *   inline   **bold** · *italic* · `code`
 * Anything else a browser might produce (pasted markup, colors, tables) never survives:
 * paste is forced to plain text, and htmlToMd() only emits the shapes above. Keeping the
 * set closed is what makes the round-trip md → html → md stable.
 *
 * Loaded as a plain script (no bundler in this repo); also exports for `node` so the
 * round-trip can be unit-tested.
 */
(function (root) {
  'use strict';

  const escHtml = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  // ---- markdown → html (load) ----------------------------------------------

  function inlineToHtml(src) {
    let s = escHtml(src);
    // Escaped literals (\* \_ \` \\) are parked FIRST, so they can never act as — or
    // terminate — a formatting delimiter. (The old code unescaped only at the end; the
    // italic regex then saw the backslash of "sync\_dell" as a non-word boundary and
    // matched the escaped underscores as <em> markers, corrupting a little more on
    // every save→load cycle.) Code spans are parked next, so their contents are never
    // re-parsed. NUL/SOH sentinels can't occur in real text.
    const lits = [];
    s = s.replace(/\\([\\*_`])/g, (_, c) => '\u0001' + (lits.push(c) - 1) + '\u0001');
    const codes = [];
    s = s.replace(/`([^`]+)`/g, (_, c) => '\u0000' + (codes.push(c) - 1) + '\u0000');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    s = s.replace(/(^|[^\w])_([^_\n]+)_/g, '$1<em>$2</em>');
    s = s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`);
    return s.replace(/\u0001(\d+)\u0001/g, (_, i) => escHtml(lits[+i]));
  }

  function mdToHtml(md) {
    const lines = String(md == null ? '' : md).replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    let para = [];        // buffered paragraph lines
    let list = null;      // {tag:'ul'|'ol', items:[]}

    const flushPara = () => {
      if (para.length) { out.push(`<p>${para.map(inlineToHtml).join('<br>')}</p>`); para = []; }
    };
    const flushList = () => {
      if (list) {
        out.push(`<${list.tag}>${list.items.map(i => `<li>${inlineToHtml(i)}</li>`).join('')}</${list.tag}>`);
        list = null;
      }
    };
    const flush = () => { flushPara(); flushList(); };

    for (const line of lines) {
      let m;
      if ((m = /^(#{1,3})\s+(.*)$/.exec(line))) {
        flush();
        const n = m[1].length;
        out.push(`<h${n}>${inlineToHtml(m[2])}</h${n}>`);
      } else if ((m = /^[-*]\s+(.*)$/.exec(line))) {
        flushPara();
        if (!list || list.tag !== 'ul') { flushList(); list = { tag: 'ul', items: [] }; }
        list.items.push(m[1]);
      } else if ((m = /^\d+[.)]\s+(.*)$/.exec(line))) {
        flushPara();
        if (!list || list.tag !== 'ol') { flushList(); list = { tag: 'ol', items: [] }; }
        list.items.push(m[1]);
      } else if (!line.trim()) {
        flush();
      } else {
        flushList();
        para.push(line);
      }
    }
    flush();
    return out.join('') || '';
  }

  // ---- html → markdown (save) ----------------------------------------------

  const escMd = s => s.replace(/([\\*_`])/g, '\\$1');

  /** Inline markdown for a node's children (strong/em/code/br/text; anything else unwraps). */
  function inlineToMd(node) {
    let out = '';
    for (const c of node.childNodes) {
      if (c.nodeType === 3) {                       // text
        out += escMd(c.nodeValue);
      } else if (c.nodeType !== 1) {
        continue;
      } else {
        const tag = c.tagName.toLowerCase();
        if (tag === 'br') out += '\n';
        else if (tag === 'strong' || tag === 'b') { const t = inlineToMd(c); out += t.trim() ? `**${t}**` : t; }
        else if (tag === 'em' || tag === 'i') { const t = inlineToMd(c); out += t.trim() ? `*${t}*` : t; }
        else if (tag === 'code') { const t = c.textContent; out += t.trim() ? '`' + t + '`' : t; }
        else if (/^(div|p|h[1-6]|ul|ol|li|blockquote|section|article)$/.test(tag)) {
          // A BLOCK nested inside a block (Safari writes lines as
          // <div>aa<div>bb</div><div>cc</div></div>) is a line break, never inline —
          // recursing without one is what silently glued "aa"+"bb"+"cc" together.
          if (out && !out.endsWith('\n')) out += '\n';
          out += inlineToMd(c);
        }
        else out += inlineToMd(c);                  // unknown INLINE wrapper → keep its text
      }
    }
    return out;
  }

  const collapse = s => s.replace(/[ \t]+/g, ' ').trim();

  const BLOCK_TAGS = /^(div|p|h[1-6]|ul|ol|blockquote|section|article)$/;

  /** Serialize a container's children into md blocks — RECURSIVE, because a
   * contenteditable's editing history produces block soup: lists wrapped in divs,
   * divs in divs, headings next to text runs. The 1.7.1 code special-cased exactly
   * two levels, so a <ul> nested inside a <div> fell into the INLINE serializer and
   * lost its bullets (the "lists vanish after reopening" bug). Rules:
   *   text / inline elements / <br> accumulate into a paragraph run;
   *   h1-h6 → "#"-headings (clamped to h3 — the subset's ceiling);
   *   ul/ol → "- " / "n. " items (an item's inner line breaks collapse to spaces);
   *   any other block → recurse, its blocks join the stream. */
  function blocksOf(container) {
    const blocks = [];
    let run = '';
    const flush = () => {
      const t = run.replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n')
        .replace(/^\n+|\n+$/g, '').trim();
      if (t) blocks.push(t);
      run = '';
    };
    for (const node of container.childNodes) {
      if (node.nodeType === 3) { run += escMd(node.nodeValue); continue; }
      if (node.nodeType !== 1) continue;
      const tag = node.tagName.toLowerCase();
      if (/^h[1-6]$/.test(tag)) {
        flush();
        const t = collapse(inlineToMd(node).replace(/\n+/g, ' '));
        if (t) blocks.push('#'.repeat(Math.min(+tag[1], 3)) + ' ' + t);
      } else if (tag === 'ul' || tag === 'ol') {
        flush();
        const items = [];
        let n = 1;
        for (const li of node.children) {
          if (li.tagName.toLowerCase() !== 'li') continue;
          const t = collapse(inlineToMd(li).replace(/\n+/g, ' '));
          if (t) items.push(tag === 'ul' ? `- ${t}` : `${n++}. ${t}`);
        }
        if (items.length) blocks.push(items.join('\n'));
      } else if (tag === 'br') {
        run += '\n';
      } else if (BLOCK_TAGS.test(tag)) {
        flush();
        blocks.push(...blocksOf(node));
      } else {
        // a single inline element (strong/em/code/span…): inlineToMd serializes a
        // container's CHILDREN, so wrap it — passed directly it would lose its own markers
        const wrap = container.ownerDocument.createElement('span');
        wrap.appendChild(node.cloneNode(true));
        run += inlineToMd(wrap);
      }
    }
    flush();
    return blocks;
  }

  /** Serialize a contenteditable root to Markdown. */
  function htmlToMd(rootEl) {
    return blocksOf(rootEl).join('\n\n');
  }

  /** Plain text of markdown — for word counts. */
  function mdToText(md) {
    return String(md || '')
      .replace(/^#{1,3}\s+/gm, '')
      .replace(/^\s*(?:[-*]|\d+[.)])\s+/gm, '')
      .replace(/\*\*|\*|`|_/g, '');
  }

  const api = { mdToHtml, htmlToMd, mdToText, inlineToHtml };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.PolarisMD = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
