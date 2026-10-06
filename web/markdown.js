/* TodoTracker Markdown: a small renderer that escapes all HTML first.
 *
 * Blocks: # ## ### headings, paragraphs (line breaks kept), - * + 1. lists,
 * - [ ] / - [x] checklist items, > quotes, --- rules, fenced code.
 * Inline: `code`, **bold**, *italic* / _italic_, ~~strike~~, [links](url),
 * ![images](url) and bare http(s) links. Only http(s):, mailto: and local
 * /images/ URLs are allowed. Code spans and generated <a>/<img> tags are held
 * aside while emphasis is applied, so underscores in URLs never become <em>.
 *
 * Also used for checklist conversion: findChecklist(), removeLines() and
 * stripEmphasis() (reads emphasis exactly like the renderer).
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.TTMarkdown = api;
})(typeof self !== 'undefined' ? self : globalThis, function () {
  'use strict';

  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ESC[c]);

  // Placeholders use private-use characters, removed from the input first.
  const HOLD_OPEN = '\uE000';
  const HOLD_CLOSE = '\uE001';
  const HOLD_RE = /\uE000(\d+)\uE001/g;

  function hold(holds, html) {
    holds.push(html);
    return HOLD_OPEN + (holds.length - 1) + HOLD_CLOSE;
  }

  function restore(text, holds) {
    for (let i = 0; i < 6 && text.indexOf(HOLD_OPEN) >= 0; i++) {
      text = text.replace(HOLD_RE, (m, n) => holds[+n]);
    }
    return text;
  }

  function okUrl(url, image) {
    const u = url.trim().toLowerCase();
    if (u.startsWith('http://') || u.startsWith('https://')) return true;
    if (u.startsWith('/images/') && !u.includes('..')) return true;
    if (!image && u.startsWith('mailto:')) return true;
    return false;
  }

  // Emphasis rules (CommonMark-like): an opener must be followed by a
  // non-space and a closer must follow a non-space, so "*.pyc and *.log"
  // stays plain; "_" never opens or closes inside a word.
  const EMPHASIS = [
    [/\*\*\*(?=\S)([\s\S]*?\S)\*\*\*/g, '<strong><em>', '</em></strong>'],
    [/~~(?=\S)([\s\S]*?\S)~~/g, '<del>', '</del>'],
    [/\*\*(?=\S)([\s\S]*?\S)\*\*/g, '<strong>', '</strong>'],
    [/(^|[^\p{L}\p{N}_])__(?=\S)([\s\S]*?\S)__(?![\p{L}\p{N}_])/gu, '<strong>', '</strong>', true],
    [/\*(?=[^\s*])([\s\S]*?[^\s*])\*/g, '<em>', '</em>'],
    [/(^|[^\p{L}\p{N}_])_(?=[^\s_])([\s\S]*?[^\s_])_(?![\p{L}\p{N}_])/gu, '<em>', '</em>', true],
  ];

  function applyEmphasis(text, strip) {
    for (const [re, open, close, lead] of EMPHASIS) {
      text = text.replace(re, (m, a, b) => {
        const before = lead ? a : '';
        const inner = lead ? b : a;
        return before + (strip ? inner : open + inner + close);
      });
    }
    return text;
  }

  const CODE_RE = /(`+)([^`\n]|[^`\n][^\n]*?[^`\n])\1(?!`)/g;
  const IMAGE_RE = /!\[([^\]\n]*)\]\(\s*([^\s()]+)\s*\)/g;
  const LINK_RE = /\[([^\]\n]+)\]\(\s*([^\s()]+)\s*\)/g;
  // In escaped text an URL ends before an entity for < > " '.
  const BARE_URL_ESCAPED_RE = /\bhttps?:\/\/(?:(?!&(?:lt|gt|quot|#39);)[^\s\uE000\uE001])+/gi;
  const BARE_URL_RAW_RE = /\bhttps?:\/\/[^\s<>"\uE000\uE001]+/gi;

  function splitTrailing(url) {
    let end = url.length;
    for (;;) {
      const ch = url[end - 1];
      if ('.,;:!?\'"'.includes(ch)) { end--; continue; }
      if (ch === ')') {
        const part = url.slice(0, end);
        if ((part.match(/\(/g) || []).length < (part.match(/\)/g) || []).length) { end--; continue; }
      }
      break;
    }
    return [url.slice(0, end), url.slice(end)];
  }

  // `text` is already HTML-escaped.
  function inline(text) {
    const holds = [];
    text = text.replace(CODE_RE, (m, ticks, code) => hold(holds, '<code>' + code + '</code>'));
    text = text.replace(IMAGE_RE, (m, alt, url) => (okUrl(url, true)
      ? hold(holds, '<img src="' + url + '" alt="' + alt + '" loading="lazy">')
      : hold(holds, m)));
    text = text.replace(LINK_RE, (m, label, url) => (okUrl(url, false)
      ? hold(holds, '<a href="' + url + '" target="_blank" rel="noopener noreferrer">'
        + applyEmphasis(label, false) + '</a>')
      : hold(holds, m)));
    text = text.replace(BARE_URL_ESCAPED_RE, (m) => {
      const [url, rest] = splitTrailing(m);
      return hold(holds, '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + url + '</a>') + rest;
    });
    text = applyEmphasis(text, false);
    return restore(text, holds);
  }

  /** Plain-text title: emphasis markers removed exactly where the renderer
   *  would apply them; code spans, links and URLs are kept as written. */
  function stripEmphasis(text) {
    const holds = [];
    let s = String(text || '').replace(/[\uE000\uE001]/g, '');
    s = s.replace(CODE_RE, (m) => hold(holds, m));
    s = s.replace(IMAGE_RE, (m) => hold(holds, m));
    s = s.replace(LINK_RE, (m) => hold(holds, m));
    s = s.replace(BARE_URL_RAW_RE, (m) => hold(holds, m));
    s = applyEmphasis(s, true);
    return restore(s, holds);
  }

  const FENCE_RE = /^ {0,3}(`{3,}|~{3,})(.*)$/;
  const HEADING_RE = /^ {0,3}(#{1,3})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$/;
  const HR_RE = /^ {0,3}(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$/;
  const QUOTE_RE = /^ {0,3}&gt;/;
  const LIST_RE = /^([ \t]*)([-*+]|\d{1,9}[.)])[ \t]+(.*)$/;

  function startsBlock(line) {
    return FENCE_RE.test(line) || HEADING_RE.test(line) || HR_RE.test(line)
      || QUOTE_RE.test(line) || LIST_RE.test(line);
  }

  function indentOf(s) {
    return s.replace(/\t/g, '    ').length;
  }

  function renderList(lines, start) {
    const first = LIST_RE.exec(lines[start]);
    const base = indentOf(first[1]);
    const ordered = /\d/.test(first[2]);
    const items = [];
    let i = start;
    while (i < lines.length) {
      const line = lines[i];
      const m = LIST_RE.exec(line);
      if (m && !HR_RE.test(line)) {
        const ind = indentOf(m[1]);
        if (ind < base) break;
        if (ind <= base + 1) {
          if (/\d/.test(m[2]) !== ordered) break;
          items.push({ text: m[3], marker: m[2], children: [] });
          i++;
          continue;
        }
        if (items.length) {
          const [html, next] = renderList(lines, i);
          items[items.length - 1].children.push(html);
          i = next;
          continue;
        }
        break;
      }
      if (items.length && line.trim() && /^[ \t]+\S/.test(line) && !startsBlock(line.trim())) {
        items[items.length - 1].text += '\n' + line.trim();
        i++;
        continue;
      }
      break;
    }
    const startNum = ordered ? parseInt(items[0].marker, 10) : 1;
    let html = ordered ? (startNum !== 1 ? '<ol start="' + startNum + '">' : '<ol>') : '<ul>';
    for (const it of items) {
      const check = /^\[([ xX])\][ \t]+([\s\S]*)$/.exec(it.text);
      const body = check ? check[2] : it.text;
      const content = inline(body).replace(/\n/g, '<br>');
      if (check) {
        const done = check[1] !== ' ';
        html += '<li class="md-check' + (done ? ' done' : '') + '"><input type="checkbox" disabled'
          + (done ? ' checked' : '') + ' tabindex="-1" aria-label="' + (done ? 'done' : 'not done') + '"> '
          + content + it.children.join('') + '</li>';
      } else {
        html += '<li>' + content + it.children.join('') + '</li>';
      }
    }
    return [html + (ordered ? '</ol>' : '</ul>'), i];
  }

  function renderBlocks(lines) {
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      let m = FENCE_RE.exec(line);
      if (m && !(m[1][0] === '`' && m[2].includes('`'))) {
        const fence = m[1];
        const closeRe = new RegExp('^ {0,3}' + (fence[0] === '`' ? '`' : '~') + '{' + fence.length + ',}[ \\t]*$');
        const body = [];
        i++;
        while (i < lines.length && !closeRe.test(lines[i])) { body.push(lines[i]); i++; }
        i++;
        out.push('<pre><code>' + body.join('\n') + '</code></pre>');
        continue;
      }
      m = HEADING_RE.exec(line);
      if (m) {
        const n = m[1].length;
        out.push('<h' + n + '>' + inline(m[2]) + '</h' + n + '>');
        i++;
        continue;
      }
      if (HR_RE.test(line)) { out.push('<hr>'); i++; continue; }
      if (QUOTE_RE.test(line)) {
        const inner = [];
        while (i < lines.length && QUOTE_RE.test(lines[i])) {
          inner.push(lines[i].replace(/^ {0,3}&gt; ?/, ''));
          i++;
        }
        out.push('<blockquote>' + renderBlocks(inner) + '</blockquote>');
        continue;
      }
      if (LIST_RE.test(line)) {
        const [html, next] = renderList(lines, i);
        out.push(html);
        i = next;
        continue;
      }
      const para = [];
      while (i < lines.length && lines[i].trim() && (para.length === 0 || !startsBlock(lines[i]))) {
        para.push(lines[i].trim());
        i++;
      }
      out.push('<p>' + inline(para.join('\n')).replace(/\n/g, '<br>') + '</p>');
    }
    return out.join('\n');
  }

  function normalize(src) {
    return String(src == null ? '' : src).replace(/\r\n?|\u2028|\u2029/g, '\n').replace(/[\uE000\uE001]/g, '');
  }

  function render(src) {
    return renderBlocks(escapeHtml(normalize(src)).split('\n'));
  }

  // ---- checklist conversion -------------------------------------------

  const CHECK_LINE_RE = /^[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+\[([ xX])\][ \t]+(.*\S)[ \t]*$/;

  /** Checklist lines outside fenced code: [{line, raw, title, done}]. */
  function findChecklist(desc) {
    const lines = normalize(desc).split('\n');
    const items = [];
    let fence = null;
    lines.forEach((line, idx) => {
      const f = FENCE_RE.exec(line);
      if (fence) {
        if (f && f[1][0] === fence[0] && f[1].length >= fence.length && !f[2].trim()) fence = null;
        return;
      }
      if (f && !(f[1][0] === '`' && f[2].includes('`'))) { fence = f[1]; return; }
      const m = CHECK_LINE_RE.exec(line);
      if (!m) return;
      const title = stripEmphasis(m[2]).replace(/\s+/g, ' ').trim().slice(0, 500);
      if (title) items.push({ line: idx, raw: line, title, done: m[1] !== ' ' });
    });
    return items;
  }

  /** Remove exactly the converted lines; everything else stays as it is. */
  function removeLines(desc, items) {
    const lines = normalize(desc).split('\n');
    const drop = new Set();
    for (const it of items) {
      if (lines[it.line] === it.raw && !drop.has(it.line)) { drop.add(it.line); continue; }
      const j = lines.findIndex((l, k) => l === it.raw && !drop.has(k));
      if (j >= 0) drop.add(j);
    }
    return lines.filter((_, k) => !drop.has(k)).join('\n');
  }

  return { render, inline: (s) => inline(escapeHtml(normalize(s))), escapeHtml, stripEmphasis,
    findChecklist, removeLines, okUrl };
});
