/* WCAG 2.1 AA contrast scan, injected into the page by tests/test_ui.py.
 *
 * window.__ttContrastScan() -> list of failures (strings) for what is on
 * screen now: text (4.5:1, large text 3:1), placeholders, and meaningful
 * non-text UI (3:1): field borders, priority edges, label dots, check circles,
 * quadrant edges of notes, link arrows.
 * window.__ttFocusScan(elements) -> failures of the :focus-visible outline.
 */
(function () {
  'use strict';

  const canvas = document.createElement('canvas').getContext('2d');

  function parse(str) {
    if (!str || str === 'transparent' || str === 'none') return [0, 0, 0, 0];
    let m = /^rgba?\(([^)]+)\)$/.exec(str);
    if (m) {
      const p = m[1].replace(/\//g, ' ').replace(/,/g, ' ').split(/\s+/).filter(Boolean);
      const a = p.length > 3 ? (p[3].endsWith('%') ? parseFloat(p[3]) / 100 : Number(p[3])) : 1;
      return [Number(p[0]), Number(p[1]), Number(p[2]), a];
    }
    m = /^color\(srgb ([^)]+)\)$/.exec(str);
    if (m) {
      const p = m[1].replace(/\//g, ' ').split(/\s+/).filter(Boolean);
      const a = p.length > 3 ? Number(p[3]) : 1;
      return [Number(p[0]) * 255, Number(p[1]) * 255, Number(p[2]) * 255, a];
    }
    canvas.fillStyle = '#000';
    canvas.fillStyle = str;
    const out = canvas.fillStyle;
    if (out === str) return null;
    if (/^#[0-9a-f]{6}$/i.test(out)) return [parseInt(out.slice(1, 3), 16), parseInt(out.slice(3, 5), 16), parseInt(out.slice(5, 7), 16), 1];
    return parse(out);
  }

  function blend(top, bottom) {
    const a = top[3];
    return [top[0] * a + bottom[0] * (1 - a), top[1] * a + bottom[1] * (1 - a), top[2] * a + bottom[2] * (1 - a), 1];
  }

  function lum(c) {
    const f = (v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
  }

  function ratio(a, b) {
    const la = lum(a);
    const lb = lum(b);
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  }

  /** The colour actually behind an element (backgrounds composited). */
  function background(el) {
    const layers = [];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const c = parse(getComputedStyle(n).backgroundColor);
      if (c && c[3] > 0) {
        layers.push(c);
        if (c[3] >= 0.999) break;
      }
    }
    let bg = layers.length && layers[layers.length - 1][3] >= 0.999 ? layers.pop() : [255, 255, 255, 1];
    while (layers.length) bg = blend(layers.pop(), bg);
    return bg;
  }

  function opacity(el) {
    let o = 1;
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) o *= Number(getComputedStyle(n).opacity);
    return o;
  }

  function visible(el) {
    if (!el.getClientRects().length) return false;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') return false;
    const r = el.getBoundingClientRect();
    if (r.width <= 1 && r.height <= 1) return false;     // .sr-only
    return opacity(el) > 0.1;
  }

  function describe(el) {
    const k = el.dataset && el.dataset.k ? '[' + el.dataset.k + ']' : '';
    const cls = typeof el.className === 'string' && el.className ? '.' + el.className.trim().split(/\s+/).join('.') : '';
    const text = (el.textContent || el.value || '').trim().replace(/\s+/g, ' ').slice(0, 30);
    return el.tagName.toLowerCase() + cls + k + (text ? ' "' + text + '"' : '');
  }

  function isLarge(st) {
    const size = parseFloat(st.fontSize);
    return size >= 24 || (size >= 18.66 && Number(st.fontWeight) >= 700);
  }

  const DISABLED = (el) => el.disabled || !!el.closest('[disabled]');

  function checkText(failures) {
    const all = document.body.querySelectorAll('*');
    for (const el of all) {
      if (el.closest('svg') || el.tagName === 'SCRIPT' || el.tagName === 'STYLE' || el.tagName === 'OPTION') continue;
      let hasText = false;
      for (const n of el.childNodes) if (n.nodeType === 3 && n.textContent.trim()) { hasText = true; break; }
      const field = el.matches('input[type=text], input[type=search], input:not([type]), input[type=date], input[type=time], textarea, select');
      if (field && el.value) hasText = true;
      if (!hasText || !visible(el) || DISABLED(el)) continue;
      const st = getComputedStyle(el);
      const bg = background(el);
      const fg = parse(st.color);
      if (!fg) continue;
      const color = blend([fg[0], fg[1], fg[2], fg[3] * opacity(el)], bg);
      const need = isLarge(st) ? 3 : 4.5;
      const r = ratio(color, bg);
      if (r < need) failures.push('text ' + r.toFixed(2) + ' < ' + need + ': ' + describe(el));
    }
    for (const el of document.querySelectorAll('input[placeholder], textarea[placeholder]')) {
      if (el.value || !visible(el) || DISABLED(el) || !el.placeholder) continue;
      const ph = parse(getComputedStyle(el, '::placeholder').color);
      const bg = background(el);
      const r = ratio(blend(ph, bg), bg);
      if (r < 4.5) failures.push('placeholder ' + r.toFixed(2) + ' < 4.5: ' + describe(el));
    }
  }

  function needUI(failures, what, color, bgs, el) {
    const c = parse(color);
    if (!c || c[3] === 0) { failures.push(what + ' missing: ' + describe(el)); return; }
    for (const bg of bgs) {
      const r = ratio(blend(c, bg), bg);
      if (r < 3) { failures.push(what + ' ' + r.toFixed(2) + ' < 3: ' + describe(el)); return; }
    }
  }

  // Inline-editable titles show their border on hover/focus; the label box
  // carries the border for its input.
  const BORDERLESS = ['ed-title', 'sub-title', 'label-input'];

  function checkUI(failures) {
    for (const el of document.querySelectorAll('input[type=text], input[type=search], input[type=date], input[type=time], textarea, select, .ed-labels')) {
      if (!visible(el) || DISABLED(el) || BORDERLESS.some((c) => el.classList.contains(c))) continue;
      needUI(failures, 'field border', getComputedStyle(el).borderTopColor, [background(el.parentElement)], el);
    }
    for (const el of document.querySelectorAll('.check')) {
      if (visible(el)) needUI(failures, 'check circle', getComputedStyle(el).borderTopColor, [background(el.parentElement)], el);
    }
    for (const el of document.querySelectorAll('.row')) {
      if (visible(el)) needUI(failures, 'priority edge', getComputedStyle(el).borderLeftColor, [background(el), background(el.parentElement)], el);
    }
    for (const el of document.querySelectorAll('.chip-dot')) {
      if (visible(el)) needUI(failures, 'label dot', getComputedStyle(el).backgroundColor, [background(el.parentElement)], el);
    }
    for (const el of document.querySelectorAll('.label-color')) {
      if (visible(el)) needUI(failures, 'label dot', getComputedStyle(el, '::before').backgroundColor, [background(el)], el);
    }
    for (const el of document.querySelectorAll('#mx-board .note')) {
      if (visible(el)) needUI(failures, 'quadrant edge', getComputedStyle(el).borderLeftColor, [background(el)], el);
    }
    const tints = Array.from(document.querySelectorAll('#mx-board .mx-q')).map((q) => background(q));
    for (const el of document.querySelectorAll('#mx-board .link-line, #mx-board .arrow-head')) {
      const st = getComputedStyle(el);
      const color = el.classList.contains('arrow-head') ? st.fill : st.stroke;
      if (el.closest('defs') && !document.querySelector('#mx-board .link')) continue;
      needUI(failures, 'link arrow', color, tints, el);
    }
  }

  window.__ttContrastScan = function () {
    const failures = [];
    checkText(failures);
    checkUI(failures);
    return Array.from(new Set(failures));
  };

  /** Focus each element (keyboard modality must be on) and check the ring. */
  window.__ttFocusScan = function (selector) {
    const failures = [];
    const els = Array.from(document.querySelectorAll(selector || 'button, input, select, textarea, [tabindex]'))
      .filter((el) => visible(el) && !DISABLED(el) && el.tabIndex >= 0 && !el.closest('svg'));
    for (const el of els) {
      el.focus({ preventScroll: true });
      if (document.activeElement !== el) continue;
      let ring = null;
      for (let n = el, i = 0; n && i < 3; n = n.parentElement, i++) {
        const st = getComputedStyle(n);
        if (st.outlineStyle !== 'none' && parseFloat(st.outlineWidth) >= 2) { ring = { el: n, color: st.outlineColor }; break; }
        if (n !== el && !n.matches(':focus-within')) break;
      }
      if (!el.matches(':focus-visible')) { failures.push('no :focus-visible: ' + describe(el)); continue; }
      if (!ring) { failures.push('no focus ring: ' + describe(el)); continue; }
      const bg = background(ring.el.parentElement || ring.el);
      const r = ratio(blend(parse(ring.color), bg), bg);
      if (r < 3) failures.push('focus ring ' + r.toFixed(2) + ' < 3: ' + describe(el));
    }
    return failures;
  };
})();
