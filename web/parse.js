/* TodoTracker parsers and date helpers (pure functions, tested with Node).
 *
 * Quick-add tokens:  #label  !high !h !med !m !low !l  ^date[@time]
 * Dates: ^today ^tod ^tomorrow ^tom ^mon..^sun (next occurrence, never today)
 *        ^+3d ^+2w ^2026-10-01 ^24.12. ^24.12.2026 (a past day.month without
 *        a year means next year), optional time: ^fri@14:30
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.TTParse = api;
})(typeof self !== 'undefined' ? self : globalThis, function () {
  'use strict';

  const WEEKDAYS = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'];
  const WEEKDAY_FULL = ['sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday'];
  const WEEKDAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const PRIORITY_TOKENS = { high: 'high', h: 'high', medium: 'medium', med: 'medium', m: 'medium', low: 'low', l: 'low' };
  const MAX_LABEL = 40;

  const pad = (n) => String(n).padStart(2, '0');
  const ymd = (d) => d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);

  function makeDate(y, m, d) {
    const x = new Date(y, m - 1, d);
    if (x.getFullYear() !== y || x.getMonth() !== m - 1 || x.getDate() !== d) return null;
    return x;
  }

  function parseYMD(s) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || '');
    return m ? makeDate(+m[1], +m[2], +m[3]) : null;
  }

  /** Whole days from a to b (local calendar days, DST-safe). */
  function dayDiff(a, b) {
    return Math.round((Date.UTC(b.getFullYear(), b.getMonth(), b.getDate())
      - Date.UTC(a.getFullYear(), a.getMonth(), a.getDate())) / 86400000);
  }

  function parseTime(t) {
    const m = /^(\d{1,2})(?::(\d{2}))?$/.exec(t);
    if (!m) return null;
    const h = +m[1];
    const mi = m[2] === undefined ? 0 : +m[2];
    if (h > 23 || mi > 59) return null;
    return pad(h) + ':' + pad(mi);
  }

  /** Text after "^" -> 'YYYY-MM-DD' | 'YYYY-MM-DDTHH:MM' | null. */
  function parseDue(spec, now) {
    now = now || new Date();
    const today = startOfDay(now);
    let s = String(spec || '').trim().toLowerCase();
    let time = null;
    const at = s.indexOf('@');
    if (at >= 0) {
      time = parseTime(s.slice(at + 1));
      if (!time) return null;
      s = s.slice(0, at);
    }
    let d = null;
    let m;
    if (s === '') d = time ? today : null;
    else if (s === 'today' || s === 'tod') d = today;
    else if (s === 'tomorrow' || s === 'tom') d = addDays(today, 1);
    else if (WEEKDAYS.includes(s) || WEEKDAY_FULL.includes(s)) {
      const wd = WEEKDAYS.includes(s) ? WEEKDAYS.indexOf(s) : WEEKDAY_FULL.indexOf(s);
      let diff = (wd - today.getDay() + 7) % 7;
      if (diff === 0) diff = 7;
      d = addDays(today, diff);
    } else if ((m = /^\+(\d{1,3})([dw])$/.exec(s))) {
      d = addDays(today, +m[1] * (m[2] === 'w' ? 7 : 1));
    } else if ((m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(s))) {
      d = makeDate(+m[1], +m[2], +m[3]);
    } else if ((m = /^(\d{1,2})\.(\d{1,2})\.(\d{4})?$/.exec(s)) || (m = /^(\d{1,2})\.(\d{1,2})$/.exec(s))) {
      if (m[3]) d = makeDate(+m[3], +m[2], +m[1]);
      else {
        d = makeDate(today.getFullYear(), +m[2], +m[1]);
        if (d && d < today) d = makeDate(today.getFullYear() + 1, +m[2], +m[1]);
      }
    }
    if (!d) return null;
    return ymd(d) + (time ? 'T' + time : '');
  }

  /** "#name" token -> label name, or null when it is not a label. */
  function labelFromToken(tok) {
    if (tok.length < 2 || tok[0] !== '#') return null;
    const name = tok.slice(1).replace(/[.,;:!?)]+$/, '');
    if (!name || name.length > MAX_LABEL) return null;
    if (/^\d/.test(name) || /[#,\s]/.test(name)) return null;
    return name;
  }

  function addUnique(list, name, known) {
    const lower = name.toLowerCase();
    const canon = (known || []).find((k) => k.toLowerCase() === lower) || name;
    if (!list.some((x) => x.toLowerCase() === lower)) list.push(canon);
  }

  /** Quick-add text -> {title, labels, priority, due, errors}. */
  function parseQuickAdd(text, opts) {
    opts = opts || {};
    const now = opts.now || new Date();
    const title = [];
    const labels = [];
    const errors = [];
    let priority = null;
    let due = null;
    for (const tok of String(text || '').split(/\s+/).filter(Boolean)) {
      const label = labelFromToken(tok);
      if (label) { addUnique(labels, label, opts.labels); continue; }
      const p = /^!([a-z]+)$/i.exec(tok);
      if (p && PRIORITY_TOKENS[p[1].toLowerCase()]) { priority = PRIORITY_TOKENS[p[1].toLowerCase()]; continue; }
      if (tok.length > 1 && tok[0] === '^') {
        const d = parseDue(tok.slice(1), now);
        if (d) { due = d; continue; }
        errors.push(tok);
      }
      title.push(tok);
    }
    return { title: title.join(' '), labels, priority, due, errors };
  }

  /** Subtask title: #label only if the task has it, and ^date; others stay. */
  function parseSubtaskTitle(text, taskLabels, now) {
    const title = [];
    const labels = [];
    let due = null;
    const known = taskLabels || [];
    for (const tok of String(text || '').split(/\s+/).filter(Boolean)) {
      const label = labelFromToken(tok);
      if (label && known.some((k) => k.toLowerCase() === label.toLowerCase())) {
        addUnique(labels, label, known);
        continue;
      }
      if (tok.length > 1 && tok[0] === '^') {
        const d = parseDue(tok.slice(1), now);
        if (d) { due = d; continue; }
      }
      title.push(tok);
    }
    return { title: title.join(' '), labels, due };
  }

  /** The "#word" being typed at the caret: {start, end, prefix} or null. */
  function labelQueryAt(text, caret) {
    text = String(text || '');
    let start = caret;
    while (start > 0 && !/\s/.test(text[start - 1])) start--;
    let end = caret;
    while (end < text.length && !/\s/.test(text[end])) end++;
    const tok = text.slice(start, caret);
    if (tok[0] !== '#' || /[#,]/.test(tok.slice(1)) || /^\d/.test(tok.slice(1))) return null;
    return { start, end, prefix: tok.slice(1) };
  }

  /** Labels matching a typed prefix: prefix matches first, then substrings. */
  function suggestLabels(prefix, names, exclude, limit) {
    const p = (prefix || '').toLowerCase();
    const ex = new Set((exclude || []).map((x) => x.toLowerCase()));
    const pool = names.filter((n) => !ex.has(n.toLowerCase()));
    const starts = pool.filter((n) => n.toLowerCase().startsWith(p));
    const contains = p ? pool.filter((n) => !n.toLowerCase().startsWith(p) && n.toLowerCase().includes(p)) : [];
    const byName = (a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' });
    return starts.sort(byName).concat(contains.sort(byName)).slice(0, limit || 8);
  }

  /** Pasted text -> one subtask per line: [{title, done}].
   *  A list marker or checkbox counts only when followed by a space, so
   *  "3.5 kg flour" and "-v flag" stay intact; empty bullets and rules go. */
  function splitPastedLines(text) {
    const out = [];
    for (const raw of String(text || '').split(/\r\n|\r|\n|\u2028|\u2029/)) {
      let s = raw.trim();
      if (!s) continue;
      if (/^(?:(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,})$/.test(s)) continue;
      if (/^(?:[-*+•]|\d{1,9}[.)])(?:\s+\[[ xX]\])?$/.test(s) || /^\[[ xX]\]$/.test(s)) continue;
      let done = false;
      const marker = /^(?:[-*+•]|\d{1,9}[.)])\s+/.exec(s);
      if (marker) s = s.slice(marker[0].length);
      const box = /^\[([ xX])\]\s+/.exec(s);
      if (box) { done = box[1] !== ' '; s = s.slice(box[0].length); }
      s = s.replace(/\s+/g, ' ').trim();
      if (s) out.push({ title: s.slice(0, 500), done });
    }
    return out;
  }

  // ---- display helpers -------------------------------------------------

  function isOverdue(due, now) {
    if (!due) return false;
    now = now || new Date();
    const today = ymd(now);
    if (due.length <= 10) return due < today;
    return due < today + 'T' + pad(now.getHours()) + ':' + pad(now.getMinutes());
  }

  /** 'overdue' | 'today' | 'future' | null */
  function dueState(due, now) {
    if (!due) return null;
    now = now || new Date();
    if (isOverdue(due, now)) return 'overdue';
    if (due.slice(0, 10) === ymd(now)) return 'today';
    return 'future';
  }

  /** "Today", "Tomorrow", a weekday within a week, or "16 Oct" (+ time). */
  function dueLabel(due, now) {
    const d = parseYMD(due);
    if (!d) return due || '';
    now = now || new Date();
    const diff = dayDiff(now, d);
    let text;
    if (diff === 0) text = 'Today';
    else if (diff === 1) text = 'Tomorrow';
    else if (diff > 1 && diff < 7) text = WEEKDAY_NAMES[d.getDay()];
    else text = d.getDate() + ' ' + MONTHS[d.getMonth()] + (d.getFullYear() !== now.getFullYear() ? ' ' + d.getFullYear() : '');
    if (due.length > 10) text += ' ' + due.slice(11, 16);
    return text;
  }

  /** Long form for tooltips: "Fri 9 Oct 2026, 14:30". */
  function dueLong(due) {
    const d = parseYMD(due);
    if (!d) return due || '';
    return WEEKDAY_NAMES[d.getDay()] + ' ' + d.getDate() + ' ' + MONTHS[d.getMonth()] + ' ' + d.getFullYear()
      + (due.length > 10 ? ', ' + due.slice(11, 16) : '');
  }

  /** Created/completed stamps: "Today 14:30", "Yesterday 09:12", "6 Oct 14:30". */
  function stampLabel(iso, now) {
    const d = parseYMD(iso);
    if (!d) return '';
    now = now || new Date();
    const time = iso.length >= 16 ? ' ' + iso.slice(11, 16) : '';
    const diff = dayDiff(now, d);
    if (diff === 0) return 'Today' + time;
    if (diff === -1) return 'Yesterday' + time;
    return d.getDate() + ' ' + MONTHS[d.getMonth()] + (d.getFullYear() !== now.getFullYear() ? ' ' + d.getFullYear() : '') + time;
  }

  /** Heading group when sorted by target date. */
  function dueGroup(due, now) {
    if (!due) return 'none';
    now = now || new Date();
    if (isOverdue(due, now)) return 'overdue';
    const diff = dayDiff(now, parseYMD(due));
    if (diff <= 0) return 'today';
    if (diff <= 6) return 'week';
    return 'later';
  }

  // ---- Eisenhower matrix rules -------------------------------------------

  /** 'do' | 'schedule' | 'delegate' | 'eliminate' for [urgency, importance]. */
  function quadrantOf(pos) {
    if (!pos) return null;
    return pos[1] >= 0.5 ? (pos[0] >= 0.5 ? 'do' : 'schedule') : (pos[0] >= 0.5 ? 'delegate' : 'eliminate');
  }

  /** A small deterministic offset (-1..1, -1..1) from an item key. */
  function spread(key) {
    let h = 2166136261;
    for (const ch of String(key)) { h ^= ch.charCodeAt(0); h = Math.imul(h, 16777619); }
    const a = ((h >>> 0) % 1000) / 999;
    const b = ((Math.imul(h ^ (h >>> 15), 2246822507) >>> 0) % 1000) / 999;
    return [a * 2 - 1, b * 2 - 1];
  }

  /** Where an item should go: due within 2 days (or overdue) is urgent; Low
   *  priority is not important. Earlier dates sit further right, higher
   *  priority further up, plus a small deterministic spread.
   *  item: {key, due (own date, or the task's for a subtask), priority}. */
  function suggestPlacement(item, now) {
    now = now || new Date();
    const due = item.due ? parseYMD(item.due) : null;
    const days = due ? dayDiff(now, due) : null;
    const urgent = days !== null && days <= 2;
    const important = item.priority !== 'low';
    let x;
    if (urgent) x = 0.85 - 0.1 * Math.max(-1, Math.min(days, 2));
    else if (days !== null) x = 0.42 - 0.32 * Math.min(days - 3, 30) / 30;
    else x = 0.1;
    let y = important ? (item.priority === 'high' ? 0.85 : 0.66) : 0.26;
    const [dx, dy] = spread(item.key);
    x += dx * 0.035;
    y += dy * 0.035;
    const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
    x = urgent ? clamp(x, 0.54, 0.96) : clamp(x, 0.04, 0.46);
    y = important ? clamp(y, 0.54, 0.96) : clamp(y, 0.04, 0.46);
    const quad = important ? (urgent ? 'do' : 'schedule') : (urgent ? 'delegate' : 'eliminate');
    return { quad, pos: [Math.round(x * 1000) / 1000, Math.round(y * 1000) / 1000] };
  }

  /** Priority that follows a manual move of a task note (null: unchanged).
   *  Crossing into the top half sets High, into the bottom half Low; moving
   *  within a half never changes it. From the tray, only a contradiction is
   *  fixed (Low in the top half -> High; High/Medium in the bottom -> Low). */
  function priorityForMove(kind, priority, from, to) {
    if (kind !== 'task' || !to) return null;
    const toTop = to[1] >= 0.5;
    let want;
    if (from) {
      if ((from[1] >= 0.5) === toTop) return null;
      want = toTop ? 'high' : 'low';
    } else if (toTop && priority === 'low') want = 'high';
    else if (!toTop && priority !== 'low') want = 'low';
    else return null;
    return want === priority ? null : want;
  }

  return {
    quadrantOf, spread, suggestPlacement, priorityForMove,
    parseDue, parseTime, parseQuickAdd, parseSubtaskTitle, labelFromToken, labelQueryAt,
    suggestLabels, splitPastedLines, isOverdue, dueState, dueLabel, dueLong, stampLabel,
    dueGroup, dayDiff, parseYMD, ymd, addDays, startOfDay, pad, MONTHS, WEEKDAY_NAMES,
  };
});
