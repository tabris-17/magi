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
    // Code spans are parked behind a sentinel first, so their contents are never
    // re-parsed as bold/italic. NUL can't occur in real text.
    const codes = [];
    s = s.replace(/`([^`]+)`/g, (_, c) => '\u0000' + (codes.push(c) - 1) + '\u0000');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    s = s.replace(/(^|[^\w])_([^_\n]+)_/g, '$1<em>$2</em>');
    s = s.replace(/\\([\\*_`])/g, '$1');                       // unescape \* \_ \` \\
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`);
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
        else if (tag === 'strong' || tag === 'b') out += `**${inlineToMd(c)}**`;
        else if (tag === 'em' || tag === 'i') out += `*${inlineToMd(c)}*`;
        else if (tag === 'code') out += '`' + c.textContent + '`';
        else out += inlineToMd(c);                  // unknown wrapper → keep its text
      }
    }
    return out;
  }

  const collapse = s => s.replace(/[ \t]+/g, ' ').trim();

  /** Serialize a contenteditable root to Markdown. */
  function htmlToMd(rootEl) {
    const blocks = [];
    for (const node of rootEl.childNodes) {
      if (node.nodeType === 3) {                    // stray text → its own paragraph
        const t = collapse(node.nodeValue);
        if (t) blocks.push(escMd(t));
        continue;
      }
      if (node.nodeType !== 1) continue;
      const tag = node.tagName.toLowerCase();

      if (/^h[1-3]$/.test(tag)) {
        const t = collapse(inlineToMd(node));
        if (t) blocks.push(`${'#'.repeat(+tag[1])} ${t}`);
      } else if (tag === 'ul' || tag === 'ol') {
        const items = [];
        let n = 1;
        for (const li of node.children) {
          if (li.tagName.toLowerCase() !== 'li') continue;
          const t = collapse(inlineToMd(li));
          if (t) items.push(tag === 'ul' ? `- ${t}` : `${n++}. ${t}`);
        }
        if (items.length) blocks.push(items.join('\n'));
      } else if (tag === 'br') {
        continue;
      } else {                                      // p, div, and anything else → paragraph
        const t = inlineToMd(node).replace(/[ \t]+/g, ' ').replace(/^\n+|\n+$/g, '').trim();
        if (t) blocks.push(t);
      }
    }
    return blocks.join('\n\n');
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
