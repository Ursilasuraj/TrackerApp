/* TodoTracker without a server: the same JSON API, kept on the device.
 *
 * The Android app (and any page opened in local mode) runs TodoTracker
 * entirely in the browser. This file is a port of db.py and of the API part
 * of server.py: same validation, messages, ordering and JSON. The
 * conformance tests (tests/test_localdb.py) send the same requests to both
 * and compare the answers, so the phone and the PC behave alike.
 *
 * Data lives in memory and is written through to IndexedDB (in Node, for
 * the tests, to nothing or a JSON file). Every write runs in a small
 * transaction: changes go to an overlay first and reach the data only when
 * the write succeeds, like a database rollback.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.TTLocal = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const API = 5;
  const STATUSES = ['open', 'in_progress', 'done'];
  const PRIORITIES = ['high', 'medium', 'low'];
  const PRIORITY_RANK = { high: 0, medium: 1, low: 2 };
  const VIEWS = ['open', 'overdue', 'today', 'week', 'nodate', 'done', 'all'];
  const SORTS = ['newest', 'due', 'priority'];
  const PALETTE = ['#e11d48', '#2563eb', '#16a34a', '#d97706', '#7c3aed', '#0891b2',
    '#db2777', '#65a30d', '#ea580c', '#4f46e5', '#0d9488', '#9333ea'];
  const MAX_TITLE = 500;
  const MAX_DESCRIPTION = 200000;
  const MAX_LABEL = 40;
  const MAX_LABELS_PER_TASK = 50;
  const MAX_SUBTASKS_PER_REQUEST = 500;
  const MAX_NOTES = 20000;
  const MAX_QUERY = 200;
  const MAX_IMAGE = 25 * 1024 * 1024;
  const REMIND_DATE_ONLY_AT = '09:00';

  // Python's str.isspace() characters (split(), strip(), \s in re).
  const WS = '\\t\\n\\x0b\\x0c\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000';
  const WS_RUN = new RegExp('[' + WS + ']+', 'g');
  const WS_ONE = new RegExp('[' + WS + ']');
  const WS_LEAD = new RegExp('^[' + WS + ']+');
  const WS_TRAIL = new RegExp('[' + WS + ']+$');

  const DATE_RE = /^([0-9]{4})-([0-9]{2})-([0-9]{2})$/;
  const DATETIME_RE = /^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2})$/;
  const COLOR_RE = /^#[0-9a-fA-F]{6}$/;
  const KEY_RE = /^([ts])([1-9][0-9]{0,11})$/;
  const STAMP_RE = /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}$/;
  const IMG_RE = new RegExp('!\\[[^\\]\\n]*\\]\\([' + WS + ']*(?:/images/[^' + WS + ')]+|https?://[^' + WS + ')]+)[^)\\n]*\\)', 'g');
  const WORD_RE = /[\p{L}\p{N}]+/gu;
  const IMAGE_NAME_RE = /\/images\/([0-9a-f]{32}\.(?:png|jpg|gif|webp|bmp))/g;

  class ValidationError extends Error {}
  class NotFound extends Error {}
  class ApiError extends Error {
    constructor(status, message) { super(message); this.status = status; }
  }

  // ------------------------------------------------------------- Python-alike helpers

  const strip = (s) => s.replace(WS_LEAD, '').replace(WS_TRAIL, '');
  const rstrip = (s) => s.replace(WS_TRAIL, '');
  const splitWords = (s) => s.split(WS_RUN).filter(Boolean);
  const cpLen = (s) => { let n = 0; for (const _ of s) n++; return n; };       // len() counts code points
  const cpSlice = (s, a, b) => Array.from(s).slice(a, b).join('');
  const nocase = (s) => s.replace(/[A-Z]+/g, (m) => m.toLowerCase());          // SQLite NOCASE folds ASCII only
  const isStr = (v) => typeof v === 'string';
  const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
  const pad = (n, w = 2) => String(n).padStart(w, '0');

  function cmpCodePoints(a, b) {
    const ia = a[Symbol.iterator]();
    const ib = b[Symbol.iterator]();
    for (;;) {
      const x = ia.next();
      const y = ib.next();
      if (x.done || y.done) return x.done === y.done ? 0 : (x.done ? -1 : 1);
      const cx = x.value.codePointAt(0);
      const cy = y.value.codePointAt(0);
      if (cx !== cy) return cx < cy ? -1 : 1;
    }
  }

  /** Python truthiness of a JSON value. */
  function truthy(v) {
    if (Array.isArray(v)) return v.length > 0;
    if (isObj(v)) return Object.keys(v).length > 0;
    return !!v;
  }

  /** Python's float %: the result has the divisor's sign. */
  function pyMod(x, y) {
    let m = x % y;
    if (m !== 0) { if ((y < 0) !== (m < 0)) m += y; } else m = y < 0 ? -0 : 0;
    return m;
  }

  /** Python's round(x): halves go to the even neighbour. */
  function pyRoundInt(x) {
    const f = Math.floor(x);
    const diff = x - f;
    if (diff > 0.5) return f + 1;
    if (diff < 0.5) return f;
    return f % 2 === 0 ? f : f + 1;
  }

  /** Python's round(x, 4) for 0 <= x <= 1 (exact ties go to the even digit). */
  function round4(x) {
    const exact = x.toFixed(20);
    const m = /^(\d+)\.(\d{4})5(0*)$/.exec(exact);
    if (m) {
      const last = Number(m[2][3]);
      const base = Number(m[1] + '.' + m[2]);
      return last % 2 === 0 ? base : Number((base + 0.0001).toFixed(4));
    }
    return Number(x.toFixed(4));
  }

  function str(v) {
    // Python str() of a JSON value, for the lenient import.
    if (v === null || v === undefined) return 'None';
    if (v === true) return 'True';
    if (v === false) return 'False';
    if (Array.isArray(v) || isObj(v)) return JSON.stringify(v);
    return String(v);
  }

  // ------------------------------------------------------------- time

  function stamp(d) {
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
      + 'T' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }
  const ymd = (d) => stamp(d).slice(0, 10);
  const minute = (d) => stamp(d).slice(0, 16);

  function parseStamp(s) {
    return new Date(Number(s.slice(0, 4)), Number(s.slice(5, 7)) - 1, Number(s.slice(8, 10)),
      Number(s.slice(11, 13) || 0), Number(s.slice(14, 16) || 0), Number(s.slice(17, 19) || 0));
  }

  function addDays(d, n) {
    return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n, 12);
  }

  function validYMD(y, m, d) {
    y = Number(y); m = Number(m); d = Number(d);
    if (y < 1 || m < 1 || m > 12 || d < 1) return false;
    const days = [31, (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0 ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    return d <= days[m - 1];
  }

  // ------------------------------------------------------------- validation (db.py)

  function cleanDue(value) {
    if (value === null || value === undefined || value === '') return null;
    if (!isStr(value)) throw new ValidationError('Target date must be a string like 2026-10-01 or 2026-10-01T14:30.');
    let m = DATE_RE.exec(value);
    if (m && validYMD(m[1], m[2], m[3])) return value;
    m = DATETIME_RE.exec(value);
    if (m && validYMD(m[1], m[2], m[3]) && Number(m[4]) < 24 && Number(m[5]) < 60) return value;
    throw new ValidationError('Invalid target date "' + cpSlice(value, 0, 40) + '". Use YYYY-MM-DD or YYYY-MM-DDTHH:MM.');
  }

  function cleanTitle(value, what) {
    what = what || 'Title';
    if (!isStr(value)) throw new ValidationError(what + ' must be text.');
    value = splitWords(value).join(' ');
    if (!value) throw new ValidationError(what + ' cannot be empty.');
    if (cpLen(value) > MAX_TITLE) throw new ValidationError(what + ' is too long (at most ' + MAX_TITLE + ' characters).');
    return value;
  }

  function cleanDescription(value) {
    if (!isStr(value)) throw new ValidationError('Description must be text.');
    if (cpLen(value) > MAX_DESCRIPTION) throw new ValidationError('Description is too long (at most ' + MAX_DESCRIPTION + ' characters).');
    return value.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  }

  function cleanStamp(value, what) {
    what = what || 'Timestamp';
    if (!isStr(value) || !STAMP_RE.test(value)) throw new ValidationError(what + ' must look like 2026-10-06T14:30:00.');
    if (!validStamp(value)) throw new ValidationError(what + ' is not a valid date and time.');
    return value;
  }

  function validStamp(v) {
    return validYMD(v.slice(0, 4), v.slice(5, 7), v.slice(8, 10)) && Number(v.slice(11, 13)) < 24
      && Number(v.slice(14, 16)) < 60 && Number(v.slice(17, 19)) < 60;
  }

  function cleanNotes(value) {
    if (!isStr(value)) throw new ValidationError('Notes must be text.');
    value = rstrip(value.replace(/\r\n/g, '\n').replace(/\r/g, '\n'));
    if (cpLen(value) > MAX_NOTES) throw new ValidationError('Notes are too long (at most ' + MAX_NOTES + ' characters).');
    return value;
  }

  function firstLine(text, limit) {
    limit = limit || 120;
    for (let line of (text || '').split('\n')) {
      line = strip(line);
      if (line) return cpLen(line) <= limit ? line : rstrip(cpSlice(line, 0, limit - 1)) + '…';
    }
    return '';
  }

  function parseKey(key) {
    const m = isStr(key) ? KEY_RE.exec(key) : null;
    if (!m) throw new ValidationError('Invalid item key ' + pyRepr(cpSlice(key == null ? 'None' : String(key), 0, 20)) + ' (use t<id> or s<id>).');
    return [m[1], Number(m[2])];
  }

  function pyRepr(s) {
    // repr() of a short string, as in the Python messages.
    if (s.includes("'") && !s.includes('"')) return '"' + s.replace(/\\/g, '\\\\') + '"';
    return "'" + s.replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n').replace(/\r/g, '\\r').replace(/\t/g, '\\t') + "'";
  }

  function cleanMatrix(value) {
    if (value === null || value === undefined) return null;
    if (!Array.isArray(value) || value.length !== 2 || value.some((v) => typeof v !== 'number')) {
      throw new ValidationError('A matrix position is [urgency, importance] with numbers from 0 to 1.');
    }
    const [x, y] = value;
    if (!(x >= 0 && x <= 1 && y >= 0 && y <= 1)) throw new ValidationError('Matrix positions must lie between 0 and 1.');
    return [round4(x), round4(y)];
  }

  const inDo = (m) => !!m && m[0] >= 0.5 && m[1] >= 0.5;

  function cleanStatus(value) {
    if (!STATUSES.includes(value)) throw new ValidationError('Status must be one of: open, in_progress, done.');
    return value;
  }

  function cleanPriority(value) {
    if (!PRIORITIES.includes(value)) throw new ValidationError('Priority must be one of: high, medium, low.');
    return value;
  }

  function cleanLabelName(value) {
    if (!isStr(value)) throw new ValidationError('Label names must be text.');
    let name = strip(value);
    if (name.startsWith('#')) name = name.slice(1);
    if (!name) throw new ValidationError('Label name cannot be empty.');
    if (cpLen(name) > MAX_LABEL) throw new ValidationError('Label "' + cpSlice(name, 0, 40) + '…" is too long (at most ' + MAX_LABEL + ' characters).');
    if (WS_ONE.test(name) || name.includes(',') || name.includes('#')) {
      throw new ValidationError('Label "' + name + '" cannot contain spaces, commas or #.');
    }
    return name;
  }

  function cleanColor(value) {
    if (!isStr(value) || !COLOR_RE.test(value)) throw new ValidationError('Colour must look like #1a2b3c.');
    return value.toLowerCase();
  }

  const dueMoment = (due) => (due == null ? null : (due.length > 10 ? due : due + 'T' + REMIND_DATE_ONLY_AT));

  /** reminded_at for a freshly set target date (one that has already
   *  arrived needs no reminder). */
  function rearmValue(due, now) {
    const moment = dueMoment(due);
    if (moment === null) return null;
    return moment <= minute(now) ? stamp(now) : null;
  }

  function isOverdue(due, now) {
    if (due == null) return false;
    return due.length === 10 ? due < ymd(now) : due < minute(now);
  }

  const RULE_RE = new RegExp('^(?:[-*_][' + WS + ']*){3,}$');
  const LEAD_RE = new RegExp('^(?:#{1,6}[' + WS + ']+|(?:>[' + WS + ']*)+|(?:[-*+]|\\p{Nd}+[.)])[' + WS + ']+(?:\\[[ xX]\\][' + WS + ']+)?)', 'u');

  function snippet(desc, limit) {
    limit = limit || 160;
    let inFence = false;
    for (const line of desc.split('\n')) {
      let s = strip(line);
      if (s.startsWith('```') || s.startsWith('~~~')) { inFence = !inFence; continue; }
      if (inFence || !s || RULE_RE.test(s)) continue;
      s = s.replace(LEAD_RE, '');
      s = s.replace(/!\[[^\]]*\]\([^)]*\)/g, '');
      s = s.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1');
      s = s.replace(/\*\*|__|~~|`/g, '');
      s = splitWords(s).join(' ');
      if (s) return cpLen(s) <= limit ? s : rstrip(cpSlice(s, 0, limit - 1)) + '…';
    }
    return '';
  }

  const imageCount = (desc) => ((desc || '').match(IMG_RE) || []).length;

  function searchWords(q) {
    return ((q || '').match(WORD_RE) || []).slice(0, 12);
  }

  const fold = (text) => (text || '').normalize('NFKD').replace(/\p{Mn}/gu, '').toLowerCase();

  function textMatchesAny(words, text) {
    if (!words.length) return false;
    const tokens = fold(text).match(WORD_RE) || [];
    const folded = words.map(fold);
    return folded.some((w) => tokens.some((t) => t.startsWith(w)));
  }

  function textMatchesAll(words, text) {
    const tokens = fold(text).match(WORD_RE) || [];
    return words.every((w) => { const f = fold(w); return tokens.some((t) => t.startsWith(f)); });
  }

  function hexLuminance(hex) {
    const chan = (c) => { c /= 255; return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b);
  }

  function hlsToRgb(h, l, s) {
    // colorsys.hls_to_rgb
    if (s === 0) return [l, l, l];
    const m2 = l <= 0.5 ? l * (1 + s) : l + s - (l * s);
    const m1 = 2 * l - m2;
    const v = (hue) => {
      hue = pyMod(hue, 1);
      if (hue < 1 / 6) return m1 + (m2 - m1) * hue * 6;
      if (hue < 0.5) return m2;
      if (hue < 2 / 3) return m1 + (m2 - m1) * (2 / 3 - hue) * 6;
      return m1;
    };
    return [v(h + 1 / 3), v(h), v(h - 1 / 3)];
  }

  function goldenColor(n) {
    const hue = pyMod(n * 137.508 + 20, 360) / 360;
    let col = null;
    for (let lightness = 62; lightness > 20; lightness--) {
      const rgb = hlsToRgb(hue, lightness / 100, 0.72);
      col = '#' + rgb.map((c) => pyRoundInt(c * 255).toString(16).padStart(2, '0')).join('');
      if (hexLuminance(col) <= 0.2) return col;
    }
    return col;
  }

  // ------------------------------------------------------------- import helpers (lenient)

  function importStamp(value) {
    if (!isStr(value)) return null;
    const v = strip(value).replace(' ', 'T');
    const m = /^([0-9]{4}-[0-9]{2}-[0-9]{2})(?:T([0-9]{2}):([0-9]{2})(?::([0-9]{2}))?)?/.exec(v);
    if (!m) return null;
    const out = m[1] + 'T' + (m[2] || '00') + ':' + (m[3] || '00') + ':' + (m[4] || '00');
    return validStamp(out) ? out : null;
  }

  function importDue(value) {
    if (!isStr(value)) return null;
    const v = strip(value).replace(' ', 'T');
    for (const candidate of [v, cpSlice(v, 0, 16), cpSlice(v, 0, 10)]) {
      try { return cleanDue(candidate); } catch (e) { if (!(e instanceof ValidationError)) throw e; }
    }
    return null;
  }

  function importStatus(value, done) {
    const v = strip(truthy(value) ? str(value) : '').toLowerCase().replace(/-/g, '_').replace(/ /g, '_');
    if (['done', 'completed', 'complete', 'closed', 'finished'].includes(v) || done === true) return 'done';
    if (['in_progress', 'inprogress', 'progress', 'doing', 'started', 'active', 'wip'].includes(v)) return 'in_progress';
    return 'open';
  }

  function importPriority(value) {
    const v = strip(truthy(value) ? str(value) : '').toLowerCase();
    if (['high', 'h', '1', 'urgent', 'important'].includes(v)) return 'high';
    if (['low', 'l', '3'].includes(v)) return 'low';
    return 'medium';
  }

  function importLabelName(value) {
    if (!isStr(value)) return null;
    let name = strip(value).replace(/^#+/, '');
    name = name.replace(new RegExp('[' + WS + ',#]+', 'g'), '-').replace(/^-+|-+$/g, '');
    name = cpSlice(name, 0, MAX_LABEL);
    return name || null;
  }

  function importMatrix(item) {
    let m = item.matrix;
    if ((m === undefined || m === null) && item.mx != null && item.my != null) m = [item.mx, item.my];
    try { m = cleanMatrix(m === undefined ? null : m); } catch (e) { m = null; }
    return m ? [m[0], m[1]] : [null, null];
  }

  function importDepKeys(d) {
    if (!isObj(d)) return [null, null];
    if (isStr(d.before) && isStr(d.after)) return [d.before, d.after];
    const before = d.before_task_id != null ? 't' + str(d.before_task_id) : (d.before_subtask_id != null ? 's' + str(d.before_subtask_id) : null);
    const after = d.after_task_id != null ? 't' + str(d.after_task_id) : (d.after_subtask_id != null ? 's' + str(d.after_subtask_id) : null);
    return [before, after];
  }

  function sniffImage(bytes) {
    const b = (i) => bytes[i];
    const starts = (arr) => arr.every((v, i) => b(i) === v);
    if (starts([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])) return 'png';
    if (starts([0xff, 0xd8, 0xff])) return 'jpg';
    const head = String.fromCharCode.apply(null, Array.from(bytes.slice(0, 12)));
    if (head.startsWith('GIF87a') || head.startsWith('GIF89a')) return 'gif';
    if (head.slice(0, 4) === 'RIFF' && head.slice(8, 12) === 'WEBP') return 'webp';
    if (head.slice(0, 2) === 'BM' && bytes.length > 26) return 'bmp';
    return null;
  }

  // ------------------------------------------------------------- the data

  const STORES = ['tasks', 'subtasks', 'labels', 'deps'];

  function emptyData() {
    return {
      tasks: new Map(), subtasks: new Map(), labels: new Map(), deps: new Map(),
      seq: { tasks: 0, subtasks: 0, labels: 0, deps: 0 },
      changes: 0,
      epoch: Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8),
    };
  }

  /** A write in progress: records changed so far live in an overlay until
   *  commit (a failed write leaves the data as it was). */
  class Tx {
    constructor(data) {
      this.data = data;
      this.over = { tasks: new Map(), subtasks: new Map(), labels: new Map(), deps: new Map() };
      this.seq = Object.assign({}, data.seq);
    }

    get(store, id) {
      const o = this.over[store];
      if (o.has(id)) return o.get(id);
      return this.data[store].get(id) || null;
    }

    put(store, rec) { this.over[store].set(rec.id, rec); return rec; }

    del(store, id) { this.over[store].set(id, null); }

    /** Records in id order (like a table scan by rowid). */
    all(store) {
      const o = this.over[store];
      const out = [];
      for (const [id, rec] of this.data[store]) {
        if (o.has(id)) { const r = o.get(id); if (r) out.push(r); } else out.push(rec);
      }
      const fresh = [];
      for (const [id, rec] of o) if (rec && !this.data[store].has(id)) fresh.push(rec);
      fresh.sort((a, b) => a.id - b.id);
      return out.concat(fresh);
    }

    nextId(store) { this.seq[store] += 1; return this.seq[store]; }

    get dirty() { return STORES.some((s) => this.over[s].size > 0) || STORES.some((s) => this.seq[s] !== this.data.seq[s]); }
  }

  /** Reads go straight to the data. */
  class View extends Tx {
    constructor(data) { super(data); }
    all(store) { return Array.from(this.data[store].values()); }
    get(store, id) { return this.data[store].get(id) || null; }
  }

  function commit(data, tx) {
    for (const s of STORES) {
      for (const [id, rec] of tx.over[s]) {
        if (rec) data[s].set(id, rec); else data[s].delete(id);
      }
    }
    data.seq = Object.assign({}, tx.seq);
  }

  const keyOf = (kind, id) => kind + id;

  // ------------------------------------------------------------- the store (db.py)

  class Store {
    constructor(opts) {
      opts = opts || {};
      this.data = emptyData();
      this.nowFn = opts.now || (() => new Date());
      this.persist = opts.persist || null;      // async (tx, data) => void
      this.onChange = null;
    }

    now() { return this.nowFn(); }

    read(fn) { return fn(new View(this.data)); }

    /** Run fn on a transaction; commit if it returns, roll back if it throws. */
    write(fn) {
      const tx = new Tx(this.data);
      const result = fn(tx);
      commit(this.data, tx);
      this.data.changes += 1;
      if (this.persist) {
        // Saves run one after another; a failed one is reported by the request
        // that waits for it and does not hold up the next. With nothing else
        // being saved the save starts right here (not a tick later), so a
        // write made while the page goes away still reaches storage.
        const data = this.data;
        const run = () => this.persist(tx, data);
        this.saving = (this.saving || 0) + 1;
        const done = () => { this.saving -= 1; };
        this.pending = (this.saving === 1 ? run() : this.pending.catch(() => {}).then(run));
        this.pending.then(done, done);
      }
      if (this.onChange) this.onChange(this.data.changes);
      return result;
    }

    // -- labels

    nextColor(tx) {
      const used = new Set(tx.all('labels').map((l) => l.color.toLowerCase()));
      for (const col of PALETTE) if (!used.has(col)) return col;
      for (let n = 0; n < 2000; n++) { const col = goldenColor(n); if (!used.has(col)) return col; }
      return goldenColor(used.size);
    }

    findLabel(tx, name) {
      const k = nocase(name);
      return tx.all('labels').find((l) => nocase(l.name) === k) || null;
    }

    labelId(tx, name, create) {
      name = cleanLabelName(name);
      const found = this.findLabel(tx, name);
      if (found) return found.id;
      if (create === false) return null;
      const id = tx.nextId('labels');
      tx.put('labels', { id, name, color: this.nextColor(tx), created_at: stamp(this.now()) });
      return id;
    }

    setTaskLabels(tx, taskId, names) {
      if (!Array.isArray(names)) throw new ValidationError('Labels must be a list of names.');
      if (names.length > MAX_LABELS_PER_TASK) throw new ValidationError('A task can have at most ' + MAX_LABELS_PER_TASK + ' labels.');
      const wanted = [];
      for (const name of names) {
        const lid = this.labelId(tx, name);
        if (!wanted.includes(lid)) wanted.push(lid);
      }
      const task = tx.get('tasks', taskId);
      const current = task.labels;
      if (current.length === wanted.length && current.every((x, i) => x === wanted[i])) return false;
      const kept = current.filter((lid) => wanted.includes(lid));
      const removed = current.filter((lid) => !wanted.includes(lid));
      tx.put('tasks', Object.assign({}, task, { labels: kept.concat(wanted.filter((lid) => !current.includes(lid))) }));
      if (removed.length) {
        // Taking a label off a task takes it off the task's subtasks.
        for (const s of this.subtasksOf(tx, taskId)) {
          if (s.labels.some((lid) => removed.includes(lid))) {
            tx.put('subtasks', Object.assign({}, s, { labels: s.labels.filter((lid) => !removed.includes(lid)) }));
          }
        }
      }
      return true;
    }

    subtaskLabelIds(tx, taskId, names) {
      if (!Array.isArray(names)) throw new ValidationError('Labels must be a list of names.');
      const task = tx.get('tasks', taskId);
      const byName = new Map();
      for (const lid of task.labels) byName.set(tx.get('labels', lid).name.toLowerCase(), lid);
      const ids = [];
      for (const name of names) {
        if (!isStr(name)) throw new ValidationError('Labels must be a list of names.');
        const key = strip(name).replace(/^#+/, '').toLowerCase();
        if (!byName.has(key)) {
          throw new ValidationError('Label "' + cpSlice(strip(name), 0, 40) + '" is not on the task; a subtask can only use its task\'s labels.');
        }
        if (!ids.includes(byName.get(key))) ids.push(byName.get(key));
      }
      return ids;
    }

    setSubtaskLabels(tx, subId, taskId, names) {
      const wanted = this.subtaskLabelIds(tx, taskId, names);
      const sub = tx.get('subtasks', subId);
      if (sub.labels.length === wanted.length && sub.labels.every((x, i) => x === wanted[i])) return false;
      tx.put('subtasks', Object.assign({}, sub, { labels: wanted }));
      return true;
    }

    labelsWithCounts(tx) {
      const tasks = tx.all('tasks');
      return tx.all('labels').slice().sort((a, b) => cmpCodePoints(nocase(a.name), nocase(b.name)) || (a.id - b.id))
        .map((l) => ({ id: l.id, name: l.name, color: l.color,
          open: tasks.filter((t) => t.status !== 'done' && t.labels.includes(l.id)).length }));
    }

    listLabels() { return this.read((tx) => this.labelsWithCounts(tx)); }

    updateLabel(labelId, name, color) {
      return this.write((tx) => {
        const row = tx.get('labels', labelId);
        if (!row) throw new NotFound('Label not found.');
        let resultId = labelId;
        if (color !== undefined && color !== null) tx.put('labels', Object.assign({}, tx.get('labels', labelId), { color: cleanColor(color) }));
        if (name !== undefined && name !== null) {
          name = cleanLabelName(name);
          const other = tx.all('labels').find((l) => l.id !== labelId && nocase(l.name) === nocase(name));
          if (other) resultId = this.mergeLabels(tx, labelId, other.id);
          else if (name !== row.name) tx.put('labels', Object.assign({}, tx.get('labels', labelId), { name }));
        }
        const r = tx.get('labels', resultId);
        return { id: r.id, name: r.name, color: r.color, merged: resultId !== labelId };
      });
    }

    mergeLabels(tx, source, target) {
      for (const t of tx.all('tasks')) {
        if (!t.labels.includes(source)) continue;
        const labels = t.labels.includes(target) ? t.labels.slice() : t.labels.concat([target]);
        tx.put('tasks', Object.assign({}, t, { labels: labels.filter((x) => x !== source) }));
      }
      for (const s of tx.all('subtasks')) {
        if (!s.labels.includes(source)) continue;
        const labels = s.labels.includes(target) ? s.labels.slice() : s.labels.concat([target]);
        tx.put('subtasks', Object.assign({}, s, { labels: labels.filter((x) => x !== source) }));
      }
      tx.del('labels', source);
      return target;
    }

    deleteLabel(labelId) {
      return this.write((tx) => {
        if (!tx.get('labels', labelId)) throw new NotFound('Label not found.');
        for (const t of tx.all('tasks')) {
          if (t.labels.includes(labelId)) tx.put('tasks', Object.assign({}, t, { labels: t.labels.filter((x) => x !== labelId) }));
        }
        for (const s of tx.all('subtasks')) {
          if (s.labels.includes(labelId)) tx.put('subtasks', Object.assign({}, s, { labels: s.labels.filter((x) => x !== labelId) }));
        }
        tx.del('labels', labelId);
      });
    }

    // -- tasks

    createTask(f) {
      const title = cleanTitle(f.title);
      const description = cleanDescription(f.description || '');
      const status = cleanStatus(f.status === undefined ? 'open' : f.status);
      const priority = cleanPriority(f.priority === undefined ? 'medium' : f.priority);
      const due = cleanDue(f.due_at);
      const now = this.now();
      const st = stamp(now);
      return this.write((tx) => {
        const id = tx.nextId('tasks');
        tx.put('tasks', { id, title, description, status, priority, created_at: st, updated_at: st, due_at: due,
          completed_at: status === 'done' ? st : null, reminded_at: rearmValue(due, now), mx: null, my: null, labels: [] });
        if (f.labels && f.labels.length) this.setTaskLabels(tx, id, f.labels);
        return this.taskJSON(tx, id, true);
      });
    }

    getTask(id) { return this.read((tx) => this.taskJSON(tx, id, true)); }

    updateTask(id, fields) {
      const now = this.now();
      const st = stamp(now);
      return this.write((tx) => {
        const row = tx.get('tasks', id);
        if (!row) throw new NotFound('Task not found.');
        const sets = {};
        if ('title' in fields) { const v = cleanTitle(fields.title); if (v !== row.title) sets.title = v; }
        if ('description' in fields) { const v = cleanDescription(fields.description); if (v !== row.description) sets.description = v; }
        if ('status' in fields) {
          const v = cleanStatus(fields.status);
          if (v !== row.status) {
            sets.status = v;
            if (v === 'done') sets.completed_at = st;
            else if (row.status === 'done') sets.completed_at = null;
          }
        }
        if ('priority' in fields) { const v = cleanPriority(fields.priority); if (v !== row.priority) sets.priority = v; }
        if ('due_at' in fields) {
          const v = cleanDue(fields.due_at);
          if (v !== row.due_at) { sets.due_at = v; sets.reminded_at = rearmValue(v, now); }
        }
        let changed = Object.keys(sets).length > 0;
        if ('labels' in fields) changed = this.setTaskLabels(tx, id, fields.labels) || changed;
        if (changed) {
          sets.updated_at = st;
          tx.put('tasks', Object.assign({}, tx.get('tasks', id), sets));
        }
        return this.taskJSON(tx, id, true);
      });
    }

    deleteTask(id) {
      return this.write((tx) => {
        if (!tx.get('tasks', id)) throw new NotFound('Task not found.');
        const subs = this.subtasksOf(tx, id);
        const keys = new Set([keyOf('t', id)].concat(subs.map((s) => keyOf('s', s.id))));
        for (const s of subs) tx.del('subtasks', s.id);
        for (const d of tx.all('deps')) if (keys.has(d.before) || keys.has(d.after)) tx.del('deps', d.id);
        tx.del('tasks', id);
      });
    }

    // -- subtasks

    subtasksOf(tx, taskId) {
      return tx.all('subtasks').filter((s) => s.task_id === taskId).sort((a, b) => (a.position - b.position) || (a.id - b.id));
    }

    cleanSubtaskItem(item) {
      if (isStr(item)) item = { title: item };
      if (!isObj(item)) throw new ValidationError('Each subtask must be a title or an object.');
      const allowed = ['title', 'done', 'position', 'created_at', 'completed_at', 'due_at', 'labels', 'notes', 'matrix'];
      for (const key of Object.keys(item)) if (!allowed.includes(key)) throw new ValidationError('Unknown subtask field "' + key + '".');
      const out = { title: cleanTitle(item.title === undefined ? null : item.title, 'Subtask title') };
      const done = item.done === undefined ? false : item.done;
      if (typeof done !== 'boolean') throw new ValidationError('Subtask "done" must be true or false.');
      out.done = done;
      const pos = item.position;
      if (pos !== undefined && pos !== null && (!Number.isInteger(pos) || pos < 0)) {
        throw new ValidationError('Subtask position must be a whole number ≥ 0.');
      }
      out.position = pos === undefined ? null : pos;
      out.created_at = truthy(item.created_at) ? cleanStamp(item.created_at, 'created_at') : null;
      out.completed_at = truthy(item.completed_at) ? cleanStamp(item.completed_at, 'completed_at') : null;
      out.due_at = cleanDue(item.due_at);
      out.notes = cleanNotes(truthy(item.notes) ? item.notes : '');
      out.matrix = cleanMatrix(item.matrix === undefined ? null : item.matrix);
      const labels = truthy(item.labels) ? item.labels : [];
      if (!Array.isArray(labels)) throw new ValidationError('Labels must be a list of names.');
      out.labels = labels;
      return out;
    }

    renumber(tx, taskId) {
      this.subtasksOf(tx, taskId).forEach((s, pos) => {
        if (s.position !== pos) tx.put('subtasks', Object.assign({}, s, { position: pos }));
      });
    }

    addSubtasks(taskId, items) {
      if (!Array.isArray(items) || !items.length) throw new ValidationError('No subtasks given.');
      if (items.length > MAX_SUBTASKS_PER_REQUEST) throw new ValidationError('At most ' + MAX_SUBTASKS_PER_REQUEST + ' subtasks at once.');
      const clean = items.map((it) => this.cleanSubtaskItem(it));
      const now = this.now();
      const st = stamp(now);
      return this.write((tx) => {
        const task = tx.get('tasks', taskId);
        if (!task) throw new NotFound('Task not found.');
        tx.put('tasks', Object.assign({}, task, { updated_at: st }));
        const ids = [];
        for (const it of clean) {
          const subs = this.subtasksOf(tx, taskId);
          const maxPos = subs.length ? Math.max.apply(null, subs.map((s) => s.position)) + 1 : 0;
          const end = Math.max(subs.length, maxPos);
          const pos = it.position === null ? end : Math.min(it.position, end);
          if (pos < end) {
            for (const s of subs) if (s.position >= pos) tx.put('subtasks', Object.assign({}, s, { position: s.position + 1 }));
          }
          const sid = tx.nextId('subtasks');
          const rec = { id: sid, task_id: taskId, title: it.title, done: it.done, position: pos,
            created_at: it.created_at || st, completed_at: it.done ? (it.completed_at || st) : null,
            due_at: null, reminded_at: null, notes: '', mx: null, my: null, labels: [] };
          if (it.due_at || it.notes) {
            rec.due_at = it.due_at;
            rec.reminded_at = rearmValue(it.due_at, now);
            rec.notes = it.notes || '';
          }
          tx.put('subtasks', rec);
          if (it.labels && it.labels.length) this.setSubtaskLabels(tx, sid, taskId, it.labels);
          if (it.matrix) tx.put('subtasks', Object.assign({}, tx.get('subtasks', sid), { mx: it.matrix[0], my: it.matrix[1] }));
          ids.push(sid);
        }
        this.renumber(tx, taskId);
        return [this.taskJSON(tx, taskId, true), ids];
      });
    }

    updateSubtask(subId, fields) {
      const now = this.now();
      const st = stamp(now);
      return this.write((tx) => {
        const row = tx.get('subtasks', subId);
        if (!row) throw new NotFound('Subtask not found.');
        const sets = {};
        if ('title' in fields) { const v = cleanTitle(fields.title, 'Subtask title'); if (v !== row.title) sets.title = v; }
        if ('done' in fields) {
          const done = fields.done;
          if (typeof done !== 'boolean') throw new ValidationError('Subtask "done" must be true or false.');
          if (done !== row.done) { sets.done = done; sets.completed_at = done ? st : null; }
        }
        if ('due_at' in fields) {
          const v = cleanDue(fields.due_at);
          if (v !== row.due_at) { sets.due_at = v; sets.reminded_at = rearmValue(v, now); }
        }
        if ('notes' in fields) { const v = cleanNotes(fields.notes); if (v !== row.notes) sets.notes = v; }
        let changed = false;
        if ('labels' in fields) changed = this.setSubtaskLabels(tx, subId, row.task_id, fields.labels);
        if (Object.keys(sets).length) tx.put('subtasks', Object.assign({}, tx.get('subtasks', subId), sets));
        if (Object.keys(sets).length || changed) {
          tx.put('tasks', Object.assign({}, tx.get('tasks', row.task_id), { updated_at: st }));
        }
        return this.taskJSON(tx, row.task_id, true);
      });
    }

    deleteSubtask(subId) {
      return this.write((tx) => {
        const row = tx.get('subtasks', subId);
        if (!row) throw new NotFound('Subtask not found.');
        tx.del('subtasks', subId);
        const key = keyOf('s', subId);
        for (const d of tx.all('deps')) if (d.before === key || d.after === key) tx.del('deps', d.id);
        this.renumber(tx, row.task_id);
        tx.put('tasks', Object.assign({}, tx.get('tasks', row.task_id), { updated_at: stamp(this.now()) }));
        return this.taskJSON(tx, row.task_id, true);
      });
    }

    reorderSubtasks(taskId, ids) {
      if (!Array.isArray(ids) || !ids.every((i) => Number.isInteger(i))) throw new ValidationError('ids must be a list of subtask ids.');
      if (new Set(ids).size !== ids.length) throw new ValidationError('A subtask is listed twice.');
      return this.write((tx) => {
        if (!tx.get('tasks', taskId)) throw new NotFound('Task not found.');
        const current = this.subtasksOf(tx, taskId).map((s) => s.id);
        const foreign = ids.filter((i) => !current.includes(i));
        if (foreign.length) throw new ValidationError('Subtask ' + foreign[0] + ' does not belong to this task.');
        const order = ids.concat(current.filter((i) => !ids.includes(i)));
        if (order.some((sid, i) => sid !== current[i])) {
          order.forEach((sid, pos) => tx.put('subtasks', Object.assign({}, tx.get('subtasks', sid), { position: pos })));
          tx.put('tasks', Object.assign({}, tx.get('tasks', taskId), { updated_at: stamp(this.now()) }));
        }
        return this.taskJSON(tx, taskId, true);
      });
    }

    // -- matrix

    setMatrix(items) {
      if (!Array.isArray(items) || !items.length) throw new ValidationError('No items given.');
      if (items.length > 5000) throw new ValidationError('Too many items at once.');
      const clean = [];
      for (const it of items) {
        if (!isObj(it)) throw new ValidationError('Each item must be an object.');
        for (const key of Object.keys(it)) if (!['key', 'matrix', 'priority'].includes(key)) throw new ValidationError('Unknown field "' + key + '".');
        if (!('matrix' in it)) throw new ValidationError('Missing field "matrix".');
        const [kind, itemId] = parseKey(it.key);
        let prio = it.priority === undefined ? null : it.priority;
        if (prio !== null) {
          if (kind !== 't') throw new ValidationError('Subtasks have no priority.');
          prio = cleanPriority(prio);
        }
        clean.push([kind, itemId, cleanMatrix(it.matrix), prio]);
      }
      const st = stamp(this.now());
      return this.write((tx) => {
        const touched = [];
        for (const [kind, itemId, pos, prio] of clean) {
          const store = kind === 't' ? 'tasks' : 'subtasks';
          const rec = tx.get(store, itemId);
          if (!rec) throw new NotFound((kind === 't' ? 'Task' : 'Subtask') + ' ' + itemId + ' not found.');
          tx.put(store, Object.assign({}, rec, { mx: pos ? pos[0] : null, my: pos ? pos[1] : null }));
          const taskId = kind === 't' ? itemId : rec.task_id;
          if (prio) {
            const t = tx.get('tasks', taskId);
            if (t.priority !== prio) tx.put('tasks', Object.assign({}, t, { priority: prio, updated_at: st }));
          }
          if (!touched.includes(taskId)) touched.push(taskId);
        }
        return touched.map((tid) => this.taskJSON(tx, tid, false));
      });
    }

    // -- dependencies

    itemOf(tx, key) {
      const [kind, id] = parseKey(key);
      return tx.get(kind === 't' ? 'tasks' : 'subtasks', id);
    }

    deps(tx) {
      const out = [];
      const info = (key) => {
        const kind = key[0];
        const id = Number(key.slice(1));
        if (kind === 't') {
          const t = tx.get('tasks', id);
          return t ? { title: t.title, done: t.status === 'done' } : { title: null, done: false };
        }
        const s = tx.get('subtasks', id);
        if (!s) return { title: null, done: false };
        const t = tx.get('tasks', s.task_id);
        return { title: s.title, done: s.done || (t && t.status === 'done') };
      };
      for (const d of tx.all('deps')) {
        const b = info(d.before);
        const a = info(d.after);
        out.push({ id: d.id, before: d.before, after: d.after, created_at: d.created_at,
          before_title: b.title, after_title: a.title, before_done: !!b.done, after_done: !!a.done });
      }
      return out;
    }

    listDeps() { return this.read((tx) => this.deps(tx)); }

    itemTitle(tx, key) {
      const [kind, id] = parseKey(key);
      const rec = tx.get(kind === 't' ? 'tasks' : 'subtasks', id);
      if (!rec) throw new NotFound((kind === 't' ? 'Task' : 'Subtask') + ' ' + id + ' not found.');
      return rec.title;
    }

    addDep(before, after) {
      parseKey(before);
      parseKey(after);
      if (before === after) throw new ValidationError('An item cannot wait for itself.');
      // A duplicate is answered without writing.
      const existing = this.read((tx) => {
        this.itemTitle(tx, before);
        this.itemTitle(tx, after);
        const deps = this.deps(tx);
        const d = deps.find((x) => x.before === before && x.after === after);
        return d ? [d.id, false, deps] : null;
      });
      if (existing) return existing;
      return this.write((tx) => {
        const beforeTitle = this.itemTitle(tx, before);
        const afterTitle = this.itemTitle(tx, after);
        const deps = this.deps(tx);
        const following = new Map();
        for (const d of deps) {
          if (!following.has(d.before)) following.set(d.before, []);
          following.get(d.before).push(d.after);
        }
        const stack = [after];
        const seen = new Set();
        while (stack.length) {
          const node = stack.pop();
          if (node === before) {
            throw new ValidationError('“' + afterTitle + '” already comes before “' + beforeTitle + '”, so this link would make a loop.');
          }
          if (seen.has(node)) continue;
          seen.add(node);
          stack.push(...(following.get(node) || []));
        }
        const id = tx.nextId('deps');
        tx.put('deps', { id, before, after, created_at: stamp(this.now()) });
        return [id, true, this.deps(tx)];
      });
    }

    deleteDep(depId) {
      return this.write((tx) => {
        if (!tx.get('deps', depId)) throw new NotFound('Link not found.');
        tx.del('deps', depId);
        return this.deps(tx);
      });
    }

    // -- reading tasks

    labelObjs(tx, ids) {
      return ids.map((lid) => { const l = tx.get('labels', lid); return { id: l.id, name: l.name, color: l.color }; });
    }

    subtaskJSON(tx, s, full) {
      const d = {
        id: s.id, task_id: s.task_id, title: s.title, done: !!s.done, position: s.position,
        created_at: s.created_at, completed_at: s.completed_at, due_at: s.due_at,
        labels: this.labelObjs(tx, s.labels), note1: firstLine(s.notes), has_notes: !!s.notes,
        matrix: s.mx !== null && s.my !== null ? [s.mx, s.my] : null,
      };
      if (full) d.notes = s.notes;
      return d;
    }

    taskJSON(tx, id, full, subsOf) {
      const t = tx.get('tasks', id);
      if (!t) throw new NotFound('Task not found.');
      const desc = t.description || '';
      const d = {
        id: t.id, title: t.title, status: t.status, priority: t.priority,
        created_at: t.created_at, updated_at: t.updated_at, due_at: t.due_at, completed_at: t.completed_at,
        labels: this.labelObjs(tx, t.labels), snippet: snippet(desc), images: imageCount(desc),
        matrix: t.mx !== null && t.my !== null ? [t.mx, t.my] : null,
      };
      if (full) d.description = desc;
      const subs = (subsOf ? subsOf.get(id) || [] : this.subtasksOf(tx, id)).map((s) => this.subtaskJSON(tx, s, full));
      d.subtasks = subs;
      d.progress = [subs.filter((s) => s.done).length, subs.length];
      d.eff_due = effectiveDue(d);
      return d;
    }

    childrenByTask(tx) {
      const map = new Map();
      for (const s of tx.all('subtasks')) {
        if (!map.has(s.task_id)) map.set(s.task_id, []);
        map.get(s.task_id).push(s);
      }
      for (const list of map.values()) list.sort((a, b) => (a.position - b.position) || (a.id - b.id));
      return map;
    }

    listTasks(view, labelIds, match, q, sort) {
      view = view === undefined ? 'open' : view;
      sort = sort === undefined ? 'newest' : sort;
      match = match === undefined ? 'any' : match;
      labelIds = labelIds || [];
      if (!VIEWS.includes(view)) throw new ValidationError('Unknown view "' + view + '".');
      if (!SORTS.includes(sort)) throw new ValidationError('Unknown sort "' + sort + '".');
      if (match !== 'any' && match !== 'all') throw new ValidationError('match must be "any" or "all".');
      const now = this.now();
      const words = searchWords(q);
      return this.read((tx) => {
        let rows = tx.all('tasks').filter((t) => (view === 'done' ? t.status === 'done' : view === 'all' ? true : t.status !== 'done'));
        const children = this.childrenByTask(tx);
        if (words.length) {
          rows = rows.filter((t) => textMatchesAll(words, [t.title, t.description]
            .concat((children.get(t.id) || []).map((s) => (s.title + ' ' + s.notes).trim())).join(' ')));
        }
        if (labelIds.length) {
          const wanted = new Set(labelIds);
          rows = rows.filter((t) => {
            const have = new Set(t.labels);
            return match === 'all' ? Array.from(wanted).every((x) => have.has(x)) : Array.from(wanted).some((x) => have.has(x));
          });
        }
        const tasks = [];
        for (const r of rows) {
          const t = this.taskJSON(tx, r.id, false, children);
          if (inView(view, t, now)) {
            markMatches(t, children.get(r.id) || [], words, labelIds, match);
            tasks.push(t);
          }
        }
        sortTasks(tasks, sort, view);
        return tasks;
      });
    }

    meta() {
      const now = this.now();
      return this.read((tx) => {
        const counts = {};
        for (const v of VIEWS) counts[v] = 0;
        counts.do = 0;
        const children = this.childrenByTask(tx);
        let done = 0;
        for (const r of tx.all('tasks')) {
          if (r.status === 'done') { done++; continue; }
          const t = this.taskJSON(tx, r.id, false, children);
          for (const v of ['open', 'overdue', 'today', 'week', 'nodate']) if (inView(v, t, now)) counts[v] += 1;
          if (inDo(t.matrix)) counts.do += 1;
          for (const s of t.subtasks) if (!s.done && inDo(s.matrix)) counts.do += 1;
        }
        counts.done = done;
        counts.all = counts.open + counts.done;
        return { labels: this.labelsWithCounts(tx), counts, search: 'local' };
      });
    }

    // -- reminders

    /** Claim the items whose target time has arrived and that were not
     *  reminded yet (marked as reminded in the same write). */
    dueReminders(now) {
      now = now || this.now();
      const current = minute(now);
      const st = stamp(now);
      const due = this.read((tx) => {
        const items = [];
        const tasks = tx.all('tasks').filter((t) => t.status !== 'done' && t.due_at !== null && t.reminded_at === null && dueMoment(t.due_at) <= current);
        tasks.sort((a, b) => cmpStr(dueMoment(a.due_at), dueMoment(b.due_at)) || (a.id - b.id));
        for (const t of tasks) items.push({ kind: 'task', id: t.id, title: t.title, due_at: t.due_at });
        const subs = tx.all('subtasks').filter((s) => {
          const t = tx.get('tasks', s.task_id);
          return !s.done && t.status !== 'done' && s.due_at !== null && s.reminded_at === null && dueMoment(s.due_at) <= current;
        });
        subs.sort((a, b) => cmpStr(dueMoment(a.due_at), dueMoment(b.due_at)) || (a.id - b.id));
        for (const s of subs) {
          const t = tx.get('tasks', s.task_id);
          items.push({ kind: 'subtask', id: s.id, title: s.title, due_at: s.due_at, task_id: t.id, task_title: t.title });
        }
        return items;
      });
      if (!due.length) return due;
      this.write((tx) => {
        for (const it of due) {
          const store = it.kind === 'task' ? 'tasks' : 'subtasks';
          tx.put(store, Object.assign({}, tx.get(store, it.id), { reminded_at: st }));
        }
      });
      return due;
    }

    /** Reminders still to come (for the phone's alarm clock): [{key, at, title, body}]. */
    upcomingReminders(limit) {
      const now = this.now();
      const current = minute(now);
      return this.read((tx) => {
        const out = [];
        for (const t of tx.all('tasks')) {
          if (t.status === 'done' || t.due_at === null || t.reminded_at !== null) continue;
          out.push({ key: 't' + t.id, at: dueMoment(t.due_at), title: t.title, due_at: t.due_at });
        }
        for (const s of tx.all('subtasks')) {
          const t = tx.get('tasks', s.task_id);
          if (s.done || t.status === 'done' || s.due_at === null || s.reminded_at !== null) continue;
          out.push({ key: 's' + s.id, at: dueMoment(s.due_at), title: s.title, due_at: s.due_at, task_title: t.title });
        }
        return out.filter((r) => r.at > current).sort((a, b) => cmpStr(a.at, b.at) || cmpStr(a.key, b.key)).slice(0, limit || 50);
      });
    }

    // -- import / export

    importData(data) {
      if (!isObj(data) || !Array.isArray(data.tasks)) throw new ValidationError('This file is not a TodoTracker export (no task list found).');
      const now = this.now();
      const st = stamp(now);
      const summary = { tasks: 0, skipped: 0, invalid: 0, subtasks: 0, labels: 0, dependencies: 0, dependencies_skipped: 0 };
      const keyMap = new Map();
      return this.write((tx) => {
        const colors = new Map();
        for (const l of (Array.isArray(data.labels) ? data.labels : [])) {
          if (isObj(l) && isStr(l.name)) {
            const name = importLabelName(l.name);
            const color = l.color;
            if (name && isStr(color) && COLOR_RE.test(color)) colors.set(name.toLowerCase(), color.toLowerCase());
          }
        }
        const labelIds = new Map();
        const labelId = (raw) => {
          const name = importLabelName(isObj(raw) ? raw.name : raw);
          if (!name) return null;
          if (labelIds.has(name.toLowerCase())) return labelIds.get(name.toLowerCase());
          let lid;
          const row = this.findLabel(tx, name);
          if (row) lid = row.id;
          else {
            let color = colors.get(name.toLowerCase());
            const used = new Set(tx.all('labels').map((l) => l.color.toLowerCase()));
            if (!color || used.has(color)) color = this.nextColor(tx);
            lid = tx.nextId('labels');
            tx.put('labels', { id: lid, name, color, created_at: st });
            summary.labels += 1;
          }
          labelIds.set(name.toLowerCase(), lid);
          return lid;
        };

        for (const t of data.tasks) {
          if (!isObj(t)) { summary.invalid += 1; continue; }
          let title;
          try { title = cleanTitle(truthy(t.title) ? str(t.title) : ''); } catch (e) { summary.invalid += 1; continue; }
          const created = importStamp(t.created_at) || st;
          const oldId = t.id;
          const subsIn = (truthy(t.subtasks) && Array.isArray(t.subtasks) ? t.subtasks : []).filter(isObj);
          const existing = tx.all('tasks').find((x) => x.title === title && x.created_at === created);
          if (existing) {
            summary.skipped += 1;
            if (oldId !== undefined && oldId !== null) keyMap.set('t' + str(oldId), 't' + existing.id);
            for (const sub of subsIn) {
              const subTitle = strip(truthy(sub.title) ? str(sub.title) : '');
              const subCreated = importStamp(sub.created_at) || '';
              const row = this.subtasksOf(tx, existing.id).find((s) => s.title === subTitle && s.created_at === subCreated);
              if (row && sub.id !== undefined && sub.id !== null) keyMap.set('s' + str(sub.id), 's' + row.id);
            }
            continue;
          }
          const status = importStatus(t.status, t.done);
          const due = importDue(truthy(t.due_at) ? t.due_at : t.due);
          const completed = importStamp(t.completed_at) || (status === 'done' ? st : null);
          const [mx, my] = importMatrix(t);
          const reminded = importStamp(t.reminded_at) || rearmValue(due, now);
          let desc = isStr(t.description) ? t.description : '';
          try { desc = cleanDescription(desc); } catch (e) { desc = cpSlice(desc, 0, MAX_DESCRIPTION); }
          const taskId = tx.nextId('tasks');
          const taskLabels = [];
          const rawLabels = truthy(t.labels) && Array.isArray(t.labels) ? t.labels.slice(0, MAX_LABELS_PER_TASK) : [];
          tx.put('tasks', { id: taskId, title, description: desc, status, priority: importPriority(t.priority),
            created_at: created, updated_at: importStamp(t.updated_at) || created, due_at: due,
            completed_at: status === 'done' ? completed : null, reminded_at: reminded, mx, my, labels: [] });
          summary.tasks += 1;
          if (oldId !== undefined && oldId !== null) keyMap.set('t' + str(oldId), 't' + taskId);
          for (const raw of rawLabels) {
            const lid = labelId(raw);
            if (lid && !taskLabels.includes(lid)) taskLabels.push(lid);
          }
          tx.put('tasks', Object.assign({}, tx.get('tasks', taskId), { labels: taskLabels.slice() }));
          const sorted = subsIn.map((x, i) => [x, i]).sort((a, b) => {
            const pa = Number.isInteger(a[0].position) ? a[0].position : (typeof a[0].position === 'boolean' ? Number(a[0].position) : 1e9);
            const pb = Number.isInteger(b[0].position) ? b[0].position : (typeof b[0].position === 'boolean' ? Number(b[0].position) : 1e9);
            return (pa - pb) || (a[1] - b[1]);
          }).map((x) => x[0]);
          sorted.forEach((sub, pos) => {
            let subTitle;
            try { subTitle = cleanTitle(truthy(sub.title) ? str(sub.title) : '', 'Subtask title'); } catch (e) { summary.invalid += 1; return; }
            const done = truthy(sub.done);
            const sCreated = importStamp(sub.created_at) || created;
            const sDue = importDue(truthy(sub.due_at) ? sub.due_at : sub.due);
            let notes = isStr(sub.notes) ? sub.notes : '';
            notes = cpSlice(rstrip(notes.replace(/\r\n/g, '\n').replace(/\r/g, '\n')), 0, MAX_NOTES);
            const [smx, smy] = importMatrix(sub);
            const sid = tx.nextId('subtasks');
            const subLabels = [];
            for (const raw of (truthy(sub.labels) && Array.isArray(sub.labels) ? sub.labels : [])) {
              const lid = labelId(raw);
              if (taskLabels.includes(lid) && !subLabels.includes(lid)) subLabels.push(lid);
            }
            tx.put('subtasks', { id: sid, task_id: taskId, title: subTitle, done, position: pos, created_at: sCreated,
              completed_at: done ? (importStamp(sub.completed_at) || st) : null, due_at: sDue,
              reminded_at: importStamp(sub.reminded_at) || rearmValue(sDue, now), notes, mx: smx, my: smy, labels: subLabels });
            summary.subtasks += 1;
            if (sub.id !== undefined && sub.id !== null) keyMap.set('s' + str(sub.id), 's' + sid);
          });
        }

        const existingDeps = new Set(this.deps(tx).map((d) => d.before + '>' + d.after));
        const following = new Map();
        for (const d of this.deps(tx)) {
          if (!following.has(d.before)) following.set(d.before, []);
          following.get(d.before).push(d.after);
        }
        const depsIn = truthy(data.dependencies) ? data.dependencies : (truthy(data.deps) ? data.deps : []);
        for (const d of (Array.isArray(depsIn) ? depsIn : [])) {
          let [before, after] = importDepKeys(d);
          before = keyMap.get(before) || null;
          after = keyMap.get(after) || null;
          if (!before || !after || before === after || existingDeps.has(before + '>' + after)) { summary.dependencies_skipped += 1; continue; }
          const stack = [after];
          const seen = new Set();
          let loop = false;
          while (stack.length) {
            const node = stack.pop();
            if (node === before) { loop = true; break; }
            if (!seen.has(node)) { seen.add(node); stack.push(...(following.get(node) || [])); }
          }
          if (loop) { summary.dependencies_skipped += 1; continue; }
          const id = tx.nextId('deps');
          tx.put('deps', { id, before, after, created_at: importStamp(isObj(d) ? d.created_at : null) || st });
          existingDeps.add(before + '>' + after);
          if (!following.has(before)) following.set(before, []);
          following.get(before).push(after);
          summary.dependencies += 1;
        }
        return summary;
      });
    }

    export() {
      return this.read((tx) => {
        const labels = tx.all('labels').map((l) => ({ id: l.id, name: l.name, color: l.color, created_at: l.created_at }));
        const children = this.childrenByTask(tx);
        const tasks = tx.all('tasks').map((r) => ({
          id: r.id, title: r.title, description: r.description, status: r.status, priority: r.priority,
          created_at: r.created_at, updated_at: r.updated_at, due_at: r.due_at, completed_at: r.completed_at,
          reminded_at: r.reminded_at,
          labels: r.labels.map((lid) => tx.get('labels', lid).name),
          matrix: r.mx !== null && r.my !== null ? [r.mx, r.my] : null,
          subtasks: (children.get(r.id) || []).map((s) => ({
            id: s.id, title: s.title, position: s.position, created_at: s.created_at, completed_at: s.completed_at,
            due_at: s.due_at, reminded_at: s.reminded_at, notes: s.notes, done: !!s.done,
            labels: s.labels.map((lid) => tx.get('labels', lid).name),
            matrix: s.mx !== null && s.my !== null ? [s.mx, s.my] : null,
          })),
        }));
        return { app: 'TodoTracker', format: 1, exported_at: stamp(this.now()), labels, tasks,
          dependencies: this.deps(tx).map((d) => ({ id: d.id, before: d.before, after: d.after, created_at: d.created_at })) };
      });
    }

    counts() {
      return { tasks: this.data.tasks.size, labels: this.data.labels.size, subtasks: this.data.subtasks.size, dependencies: this.data.deps.size };
    }
  }

  const cmpStr = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

  function relevantDues(t) {
    if (t.status === 'done') return t.due_at ? [t.due_at] : [];
    const dues = t.due_at ? [t.due_at] : [];
    for (const s of t.subtasks || []) if (!s.done && s.due_at) dues.push(s.due_at);
    return dues;
  }

  function effectiveDue(t) {
    const dues = relevantDues(t);
    return dues.length ? dues.reduce((a, b) => (b < a ? b : a)) : null;
  }

  function inView(view, t, now) {
    if (view === 'all') return true;
    if (view === 'done') return t.status === 'done';
    if (t.status === 'done') return false;
    if (view === 'open') return true;
    const dues = relevantDues(t);
    const today = ymd(now);
    if (view === 'overdue') return dues.some((d) => isOverdue(d, now));
    if (view === 'today') return dues.some((d) => d.slice(0, 10) === today);
    if (view === 'week') {
      const last = ymd(addDays(now, 6));
      return dues.some((d) => today <= d.slice(0, 10) && d.slice(0, 10) <= last);
    }
    if (view === 'nodate') return !dues.length;
    return true;
  }

  function markMatches(t, rows, words, labelIds, match) {
    if (!words.length && !labelIds.length) return;
    const wanted = new Set(labelIds);
    const text = (r) => r.title + ' ' + (r.notes || '');
    if (words.length) t.text_subs = rows.filter((r) => textMatchesAny(words, text(r))).map((r) => r.id);
    const hits = [];
    for (const r of rows) {
      if (words.length && !textMatchesAny(words, text(r))) continue;
      if (wanted.size) {
        const own = new Set(r.labels);
        const ok = match === 'all' ? Array.from(wanted).every((x) => own.has(x)) : Array.from(wanted).some((x) => own.has(x));
        if (!ok) continue;
      }
      hits.push(r.id);
    }
    t.match_subs = hits;
  }

  function sortTasks(tasks, sort, view) {
    const desc = (ka, kb) => (ka < kb ? 1 : ka > kb ? -1 : 0);
    if (sort === 'newest') {
      const k = (t) => (view === 'done' && t.completed_at ? t.completed_at : t.created_at);
      tasks.sort((a, b) => desc(k(a), k(b)) || (b.id - a.id));
      return;
    }
    tasks.sort((a, b) => desc(a.created_at, b.created_at) || (b.id - a.id));
    const rank = (t) => (PRIORITY_RANK[t.priority] === undefined ? 1 : PRIORITY_RANK[t.priority]);
    const due = (a, b) => ((a.eff_due === null) - (b.eff_due === null)) || cmpStr(a.eff_due || '', b.eff_due || '');
    if (sort === 'due') tasks.sort((a, b) => due(a, b) || (rank(a) - rank(b)));
    else tasks.sort((a, b) => (rank(a) - rank(b)) || due(a, b));
  }

  // ------------------------------------------------------------- reminder texts (reminders.py)

  const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const MAX_SINGLE = 3;

  function describeDue(due, now) {
    const day = new Date(Number(due.slice(0, 4)), Number(due.slice(5, 7)) - 1, Number(due.slice(8, 10)), 12);
    const today = ymd(now);
    let text;
    if (due.slice(0, 10) === today) text = 'today';
    else if (due.slice(0, 10) === ymd(addDays(now, -1))) text = 'yesterday';
    else if (due.slice(0, 10) === ymd(addDays(now, 1))) text = 'tomorrow';
    else {
      text = WEEKDAYS[(day.getDay() + 6) % 7] + ' ' + day.getDate() + ' ' + MONTHS[day.getMonth()];
      if (day.getFullYear() !== now.getFullYear()) text += ' ' + day.getFullYear();
    }
    if (due.length > 10) text += ' ' + due.slice(11, 16);
    return text;
  }

  /** [title, body] pairs to show (more than three become one summary). */
  function compose(items, now) {
    if (items.length > MAX_SINGLE) {
      const names = items.slice(0, 5).map((it) => it.title);
      const body = names.join(' · ') + (items.length > 5 ? ' … and ' + (items.length - 5) + ' more' : '');
      return [[items.length + ' TodoTracker items are due', body]];
    }
    return items.map((it) => {
      const when = describeDue(it.due_at, now);
      return it.kind === 'subtask' ? [it.title, 'Subtask of “' + it.task_title + '” · due ' + when] : [it.title, 'Due ' + when];
    });
  }

  // ------------------------------------------------------------- the API (server.py)

  const TASK_FIELDS = { title: ['string'], description: ['string'], status: ['string'], priority: ['string'],
    due_at: ['string', 'null'], labels: ['list'] };
  const SUBTASK_FIELDS = { title: ['string'], done: ['bool'], due_at: ['string', 'null'], labels: ['list'], notes: ['string'] };

  function typeOf(v) {
    if (v === null) return 'null';
    if (Array.isArray(v)) return 'list';
    if (typeof v === 'boolean') return 'bool';
    if (typeof v === 'number') return Number.isInteger(v) ? 'int' : 'float';
    if (typeof v === 'string') return 'string';
    return 'object';
  }

  function checkFields(body, allowed, required) {
    if (!isObj(body)) throw new ApiError(400, 'Expected a JSON object.');
    for (const key of Object.keys(body)) if (!(key in allowed)) throw new ApiError(400, 'Unknown field "' + key + '".');
    for (const key of required || []) if (!(key in body)) throw new ApiError(400, 'Missing field "' + key + '".');
    for (const key of Object.keys(body)) {
      if (!allowed[key].includes(typeOf(body[key]))) throw new ApiError(400, 'Field "' + key + '" has the wrong type.');
    }
    return body;
  }

  function parseIdList(text, what) {
    if (!text) return [];
    const out = [];
    for (let part of text.split(',')) {
      part = strip(part);
      if (!part) continue;
      if (!/^[0-9]+$/.test(part) || part.length > 12) throw new ApiError(400, 'Invalid ' + (what || 'labels') + ' list.');
      out.push(Number(part));
    }
    return out;
  }

  function parseQuery(query) {
    // parse_qs(keep_blank_values=True), last value wins
    const out = {};
    if (!query) return out;
    for (const part of query.split('&')) {
      if (!part) continue;
      const i = part.indexOf('=');
      const k = i < 0 ? part : part.slice(0, i);
      const v = i < 0 ? '' : part.slice(i + 1);
      const dec = (s) => { try { return decodeURIComponent(s.replace(/\+/g, ' ')); } catch (e) { return s; } };
      out[dec(k)] = dec(v);
    }
    return out;
  }

  const ROUTES = [
    ['GET', /^\/api\/ping$/, 'ping'],
    ['GET', /^\/api\/state$/, 'state'],
    ['GET', /^\/api\/meta$/, 'meta'],
    ['GET', /^\/api\/tasks$/, 'listTasks'],
    ['GET', /^\/api\/tasks\/([0-9]+)$/, 'getTask'],
    ['GET', /^\/api\/export$/, 'export'],
    ['GET', /^\/api\/deps$/, 'listDeps'],
    ['POST', /^\/api\/deps$/, 'addDep'],
    ['DELETE', /^\/api\/deps\/([0-9]+)$/, 'deleteDep'],
    ['POST', /^\/api\/matrix$/, 'matrix'],
    ['POST', /^\/api\/import$/, 'import'],
    ['POST', /^\/api\/tasks$/, 'createTask'],
    ['POST', /^\/api\/tasks\/([0-9]+)\/subtasks$/, 'addSubtasks'],
    ['POST', /^\/api\/tasks\/([0-9]+)\/subtasks\/order$/, 'orderSubtasks'],
    ['PATCH', /^\/api\/subtasks\/([0-9]+)$/, 'updateSubtask'],
    ['DELETE', /^\/api\/subtasks\/([0-9]+)$/, 'deleteSubtask'],
    ['PATCH', /^\/api\/tasks\/([0-9]+)$/, 'updateTask'],
    ['DELETE', /^\/api\/tasks\/([0-9]+)$/, 'deleteTask'],
    ['PATCH', /^\/api\/labels\/([0-9]+)$/, 'updateLabel'],
    ['DELETE', /^\/api\/labels\/([0-9]+)$/, 'deleteLabel'],
    ['POST', /^\/api\/images$/, 'uploadImage'],
    ['POST', /^\/api\/backup$/, 'backup'],
  ];

  /** The JSON API on top of a Store. handle() answers like server.py:
   *  {status, data, changes} (data parsed). */
  class Api {
    constructor(store, opts) {
      this.store = store;
      this.opts = opts || {};
      this.images = this.opts.images || null;     // {put(name, bytes, type), has(name)}
    }

    async handle(method, url, body) {
      const before = this.store.data.changes;
      try {
        const q = url.indexOf('?');
        const path = q < 0 ? url : url.slice(0, q);
        const query = q < 0 ? '' : url.slice(q + 1);
        if (!path.startsWith('/api/')) throw new ApiError(404, 'Not found.');
        let allowed = false;
        for (const [m, rx, name] of ROUTES) {
          const match = rx.exec(path);
          if (!match) continue;
          if (m !== method) { allowed = true; continue; }
          const args = match.slice(1).map(Number);
          const res = await this[name](...args, parseQuery(query), body);
          if (this.store.pending && this.store.data.changes !== before) {
            try { await this.store.pending; } catch (e) {
              if (this.opts.onStorageError) await this.opts.onStorageError(e);
              throw new ApiError(500, 'The change could not be saved on this device (' + (e && e.name || 'storage error') + ').');
            }
          }
          return this.answer(res[0], res[1], before);
        }
        if (allowed) throw new ApiError(405, 'Method not allowed.');
        throw new ApiError(404, 'Not found.');
      } catch (e) {
        let status = 500;
        let message = 'Internal error.';
        if (e instanceof ApiError) { status = e.status; message = e.message; }
        else if (e instanceof ValidationError) { status = 400; message = e.message; }
        else if (e instanceof NotFound) { status = 404; message = e.message; }
        else if (typeof console !== 'undefined') console.error('TodoTracker local API:', e);
        return this.answer(status, { error: message }, before);
      }
    }

    answer(status, data, before) {
      const changes = [];
      for (let n = before + 1; n <= this.store.data.changes; n++) changes.push(n);
      return { status, data, changes };
    }

    json(body) {
      if (body === undefined || body === null || body === '') return {};
      if (!isObj(body)) throw new ApiError(400, 'Expected a JSON object.');
      return body;
    }

    ping() { return [200, { app: 'TodoTracker', api: API, build: this.opts.build || '', pid: 0, app_dir: '', data_dir: '' }]; }

    state() {
      return [200, { api: this.opts.api || API, build: this.opts.build || '', epoch: this.store.data.epoch,
        changes: this.store.data.changes, hotkey: 0 }];
    }

    meta() { return [200, this.store.meta()]; }

    listTasks(qs) {
      const one = (name, dflt) => (name in qs ? qs[name] : dflt);
      const q = one('q', '');
      if (cpLen(q) > MAX_QUERY) throw new ApiError(400, 'Search text is too long.');
      return [200, { tasks: this.store.listTasks(one('view', 'open'), parseIdList(one('labels', '')), one('match', 'any'), q, one('sort', 'newest')) }];
    }

    getTask(id) { return [200, this.store.getTask(id)]; }

    createTask(qs, body) {
      const b = checkFields(this.json(body), TASK_FIELDS, ['title']);
      return [201, this.store.createTask(b)];
    }

    updateTask(id, qs, body) {
      const b = checkFields(this.json(body), TASK_FIELDS);
      return [200, this.store.updateTask(id, b)];
    }

    deleteTask(id, qs, body) {
      this.json(body);
      this.store.deleteTask(id);
      return [200, { ok: true }];
    }

    addSubtasks(id, qs, body) {
      const b = checkFields(this.json(body), { title: ['string'], items: ['list'] });
      if (('title' in b) === ('items' in b)) throw new ApiError(400, 'Send either "title" or "items".');
      const [task, ids] = this.store.addSubtasks(id, 'title' in b ? [b.title] : b.items);
      task.created_ids = ids;
      return [201, task];
    }

    orderSubtasks(id, qs, body) {
      const b = checkFields(this.json(body), { ids: ['list'] }, ['ids']);
      return [200, this.store.reorderSubtasks(id, b.ids)];
    }

    updateSubtask(id, qs, body) {
      const b = checkFields(this.json(body), SUBTASK_FIELDS);
      if (!Object.keys(b).length) throw new ApiError(400, 'Nothing to change.');
      return [200, this.store.updateSubtask(id, b)];
    }

    deleteSubtask(id, qs, body) {
      this.json(body);
      return [200, this.store.deleteSubtask(id)];
    }

    matrix(qs, body) {
      const b = checkFields(this.json(body), { items: ['list'] }, ['items']);
      return [200, { tasks: this.store.setMatrix(b.items) }];
    }

    listDeps() { return [200, { deps: this.store.listDeps() }]; }

    addDep(qs, body) {
      const b = checkFields(this.json(body), { before: ['string'], after: ['string'] }, ['before', 'after']);
      const [id, created, deps] = this.store.addDep(b.before, b.after);
      return [created ? 201 : 200, { id, created, deps }];
    }

    deleteDep(id, qs, body) {
      this.json(body);
      return [200, { deps: this.store.deleteDep(id) }];
    }

    updateLabel(id, qs, body) {
      const b = checkFields(this.json(body), { name: ['string'], color: ['string'] });
      if (!Object.keys(b).length) throw new ApiError(400, 'Nothing to change.');
      return [200, this.store.updateLabel(id, b.name, b.color)];
    }

    deleteLabel(id, qs, body) {
      this.json(body);
      this.store.deleteLabel(id);
      return [200, { ok: true }];
    }

    async uploadImage(qs, body) {
      const bytes = body instanceof Uint8Array ? body : (body && typeof body.arrayBuffer === 'function' ? new Uint8Array(await body.arrayBuffer()) : null);
      if (!bytes || !bytes.length) throw new ApiError(400, 'No image data.');
      if (bytes.length > MAX_IMAGE) throw new ApiError(413, 'Too large (limit 25 MB).');
      const ext = sniffImage(bytes);
      if (!ext) throw new ApiError(400, 'Unsupported image type (use PNG, JPEG, GIF, WebP or BMP).');
      if (!this.images) throw new ApiError(503, 'Pictures cannot be stored here.');
      const name = randomHex(16) + '.' + ext;
      await this.images.put(name, bytes, ext === 'jpg' ? 'image/jpeg' : 'image/' + ext);
      return [201, { url: '/images/' + name, size: bytes.length }];
    }

    async import(qs, body) {
      let data = body;
      if (body && typeof body.text === 'function') {
        try { data = JSON.parse(await body.text()); } catch (e) { throw new ApiError(400, 'Request body is not valid JSON.'); }
      } else if (isStr(body)) {
        try { data = JSON.parse(body); } catch (e) { throw new ApiError(400, 'Request body is not valid JSON.'); }
      }
      data = this.json(data);
      const summary = this.store.importData(data);
      const names = new Set();
      const text = JSON.stringify(data);
      let m;
      IMAGE_NAME_RE.lastIndex = 0;
      while ((m = IMAGE_NAME_RE.exec(text))) names.add(m[1]);
      let missing = 0;
      for (const n of names) if (!this.images || !(await this.images.has(n))) missing++;
      summary.missing_images = missing;
      return [200, { summary }];
    }

    export() {
      const data = this.store.export();
      data.api = API;
      return [200, data];
    }

    backup() { throw new ApiError(503, 'Backups are not available.'); }
  }

  function randomHex(bytes) {
    const a = new Uint8Array(bytes);
    if (typeof crypto !== 'undefined' && crypto.getRandomValues) crypto.getRandomValues(a);
    else for (let i = 0; i < bytes; i++) a[i] = Math.floor(Math.random() * 256);
    return Array.from(a, (b) => b.toString(16).padStart(2, '0')).join('');
  }

  // ------------------------------------------------------------- persistence

  /** Plain records for storage (and back). */
  function snapshot(data) {
    return {
      tasks: Array.from(data.tasks.values()), subtasks: Array.from(data.subtasks.values()),
      labels: Array.from(data.labels.values()), deps: Array.from(data.deps.values()),
      meta: { seq: data.seq, changes: data.changes, epoch: data.epoch },
    };
  }

  function restore(store, snap) {
    const data = emptyData();
    for (const s of STORES) {
      const recs = (snap[s] || []).slice().sort((a, b) => a.id - b.id);
      for (const r of recs) data[s].set(r.id, r);
    }
    if (snap.meta) {
      data.seq = Object.assign(data.seq, snap.meta.seq || {});
      data.changes = snap.meta.changes || 0;
      data.epoch = snap.meta.epoch || data.epoch;
    }
    store.data = data;
  }

  const DB_NAME = 'todotracker';
  const DB_VERSION = 1;

  function idbRequest(req) {
    return new Promise((resolve, reject) => {
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  /** IndexedDB: one object store per table, plus meta and images. */
  async function openIndexedDB(indexedDB) {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      for (const s of STORES) if (!db.objectStoreNames.contains(s)) db.createObjectStore(s, { keyPath: 'id' });
      if (!db.objectStoreNames.contains('meta')) db.createObjectStore('meta', { keyPath: 'key' });
      if (!db.objectStoreNames.contains('images')) db.createObjectStore('images', { keyPath: 'name' });
    };
    const db = await idbRequest(req);
    db.onversionchange = () => db.close();
    return db;
  }

  async function loadIndexedDB(db) {
    const tx = db.transaction(STORES.concat(['meta']), 'readonly');
    const snap = {};
    for (const s of STORES) snap[s] = await idbRequest(tx.objectStore(s).getAll());
    const meta = await idbRequest(tx.objectStore('meta').get('state'));
    snap.meta = meta ? meta.value : null;
    return snap;
  }

  function idbPersist(db) {
    return (tx, data) => new Promise((resolve, reject) => {
      const t = db.transaction(STORES.concat(['meta']), 'readwrite');
      for (const s of STORES) {
        const os = t.objectStore(s);
        for (const [id, rec] of tx.over[s]) { if (rec) os.put(rec); else os.delete(id); }
      }
      t.objectStore('meta').put({ key: 'state', value: { seq: data.seq, changes: data.changes, epoch: data.epoch } });
      if (t.commit) t.commit();              // do not wait for the end of this task
      t.oncomplete = () => resolve();
      t.onerror = () => reject(t.error);
      t.onabort = () => reject(t.error || new Error('IndexedDB write aborted'));
    });
  }

  function idbImages(db) {
    return {
      async put(name, bytes, type) {
        const t = db.transaction(['images'], 'readwrite');
        t.objectStore('images').put({ name, type, blob: new Blob([bytes], { type }) });
        await new Promise((resolve, reject) => { t.oncomplete = resolve; t.onerror = () => reject(t.error); });
      },
      async get(name) {
        const rec = await idbRequest(db.transaction(['images'], 'readonly').objectStore('images').get(name));
        return rec ? rec.blob : null;
      },
      async has(name) {
        const key = await idbRequest(db.transaction(['images'], 'readonly').objectStore('images').getKey(name));
        return key !== undefined;
      },
    };
  }

  /** Open the browser's data: {store, api, images, reload()}. */
  async function openBrowser(opts) {
    opts = opts || {};
    const idb = await openIndexedDB(opts.indexedDB || indexedDB);
    const store = new Store({ now: opts.now });
    restore(store, await loadIndexedDB(idb));
    store.persist = idbPersist(idb);
    const images = idbImages(idb);
    const api = new Api(store, { build: opts.build, api: opts.api, images });
    // Another tab may have written: the counter in IndexedDB is ahead of ours.
    async function sync() {
      const meta = await idbRequest(idb.transaction(['meta'], 'readonly').objectStore('meta').get('state'));
      const theirs = meta ? meta.value.changes : 0;
      if (theirs !== store.data.changes) restore(store, await loadIndexedDB(idb));
    }
    return { store, api, images, sync, idb };
  }

  /** In memory (tests, Node). */
  function openMemory(opts) {
    opts = opts || {};
    const store = new Store({ now: opts.now });
    const files = new Map();
    const images = {
      async put(name, bytes) { files.set(name, bytes); },
      async get(name) { return files.get(name) || null; },
      async has(name) { return files.has(name); },
    };
    return { store, api: new Api(store, { build: opts.build, api: opts.api, images }), images };
  }

  return {
    API, Store, Api, ValidationError, NotFound, ApiError, openBrowser, openMemory, snapshot, restore, compose, describeDue,
    // exposed for tests
    _: { cleanDue, cleanTitle, cleanLabelName, cleanMatrix, round4, goldenColor, snippet, imageCount, searchWords,
      textMatchesAny, textMatchesAll, fold, firstLine, importStamp, importDue, importLabelName, importStatus,
      importPriority, sniffImage, stamp, rearmValue, cmpCodePoints, nocase, pyRoundInt,
      compose: (items, now) => compose(items, parseStamp(now)), describeDue: (due, now) => describeDue(due, parseStamp(now)) },
  };
});
