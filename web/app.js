/* TodoTracker: list page, details editor and the shared page machinery.
 *
 * Rules that keep the UI robust (see README, "Robustness"):
 *  - DOM rebuilds never happen while a mouse button is held: they are queued
 *    and run in a setTimeout(0) after pointerup/pointercancel/dragstart/blur.
 *  - Redraws keep focus on the control with the same data-k key, keep typed
 *    but unsaved text and the caret, and never scroll. A focused text field is
 *    kept as the very same node (the page is rebuilt around it), so undo
 *    history, a manual resize and IME composition survive autosaves.
 *  - Field saves are chained and remember which task they belong to.
 */
(function () {
  'use strict';

  const P = window.TTParse;
  const MD = window.TTMarkdown;
  const ROOT = document.documentElement;
  const BOOT = { api: Number(ROOT.dataset.api) || 0, build: ROOT.dataset.build || '' };
  const CLIENT_API = 4;            // the server API this page is written for
  const FEATURE_API = { subtasks: 2, subtaskDetails: 3 };   // feature -> minimum server API
  const KEEPALIVE_BUDGET = 60000;  // Chromium allows 64 KB of keepalive bodies in flight
  const STATUS_NAMES = { open: 'Open', in_progress: 'In progress', done: 'Done' };
  const PRIORITY_NAMES = { high: 'High', medium: 'Medium', low: 'Low' };
  const VIEWS = [
    ['open', 'All open', '☰'], ['overdue', 'Overdue', '!'], ['today', 'Today', '◉'],
    ['week', 'Next 7 days', '▦'], ['nodate', 'No target date', '∅'], ['done', 'Done', '✓'],
    ['all', 'Everything', '≡'],
  ];
  const VIEW_NAMES = Object.fromEntries(VIEWS.map((v) => [v[0], v[1]]));
  const GROUPS = [['overdue', 'Overdue'], ['today', 'Today'], ['week', 'Next 7 days'], ['later', 'Later'], ['none', 'No target date']];

  // ------------------------------------------------------------- helpers

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ESC[c]);
  const kq = (k) => '[data-k="' + CSS.escape(k) + '"]';

  function load(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch (e) { return fallback; }
  }
  function save(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* private mode */ }
  }
  function supports(feature) {
    return !FEATURE_API[feature] || BOOT.api >= FEATURE_API[feature];
  }

  // ------------------------------------------------------------- API

  const net = { inflight: 0, own: new Set(), lastChanges: null, epoch: null, failures: 0 };

  async function api(method, path, body, opts) {
    const init = { method, headers: {} };
    const write = method !== 'GET';
    if (write) init.headers['X-Todo'] = '1';
    if (body !== undefined) {
      if (body instanceof Blob) {
        init.body = body;
        init.headers['Content-Type'] = body.type || 'application/octet-stream';
      } else {
        init.body = JSON.stringify(body);
        init.headers['Content-Type'] = 'application/json';
      }
    }
    if (opts && opts.keepalive) init.keepalive = true;
    if (write) net.inflight++;
    let res;
    try {
      res = await fetch(path, init);
    } catch (e) {
      if (write) net.inflight--;
      const err = new Error('TodoTracker is not reachable.');
      err.network = true;
      throw err;
    }
    const changes = res.headers.get('X-Todo-Changes');
    if (changes) for (const n of changes.split(',')) net.own.add(Number(n));
    if (write) net.inflight--;
    const text = await res.text();
    let data = null;
    if (text) { try { data = JSON.parse(text); } catch (e) { data = null; } }
    if (!res.ok) {
      const err = new Error((data && data.error) || ('Request failed (' + res.status + ').'));
      err.status = res.status;
      throw err;
    }
    return data;
  }

  // ------------------------------------------------- deferred redraws

  // A redraw between mousedown and mouseup swallows the click, so rebuilds
  // wait while a pointer is held (or while one of our drags is running).
  const hold = { pointer: false, pressed: null, timer: 0, drags: 0, queue: new Map() };

  function held() { return hold.pointer || hold.drags > 0; }

  function schedule(key, fn) {
    if (held()) { hold.queue.set(key, fn); return; }
    fn();
  }

  function flushHeld() {
    if (held()) return;
    const fns = Array.from(hold.queue.values());
    hold.queue.clear();
    for (const fn of fns) fn();
  }

  function releasePointer() {
    clearTimeout(hold.timer);
    if (!hold.pointer) return;
    hold.pointer = false;
    hold.pressed = null;
    setTimeout(flushHeld, 0);
  }

  function beginDrag() { hold.drags++; }
  function endDrag() {
    hold.drags = Math.max(0, hold.drags - 1);
    setTimeout(flushHeld, 0);
  }

  function bindPointerHold() {
    window.addEventListener('pointerdown', (e) => {
      hold.pointer = true;
      hold.pressed = e.target;
      clearTimeout(hold.timer);
      hold.timer = setTimeout(releasePointer, 1500);   // safety net
    }, true);
    window.addEventListener('pointerup', releasePointer, true);
    window.addEventListener('pointercancel', releasePointer, true);
    window.addEventListener('dragstart', releasePointer, true);
    window.addEventListener('blur', releasePointer);
    // A <select> or a date popup can swallow the pointerup; its own change
    // ends the press. A text field that is being left also fires change
    // during the press, and that one must not release.
    document.addEventListener('change', (e) => {
      const p = hold.pressed;
      if (p && (e.target === p || (e.target.contains && e.target.contains(p)))) releasePointer();
    }, true);
  }

  // ------------------------------------------- focus-keeping redraws

  const TEXT_INPUT = /^(text|search|url|email|tel|number|password)$/;
  const KEPT_INPUT = /^(text|search|url|email|tel|number|password|date|time)$/;

  function isTextField(el) {
    return !!el && (el.tagName === 'TEXTAREA' || (el.tagName === 'INPUT' && TEXT_INPUT.test(el.type)));
  }

  // Fields kept as the same node across redraws while focused (text keeps
  // its undo history; a date field keeps the segment being typed).
  function isKeptField(el) {
    return !!el && (el.tagName === 'TEXTAREA' || (el.tagName === 'INPUT' && KEPT_INPUT.test(el.type)));
  }

  function snapshotFocus(container) {
    const el = document.activeElement;
    if (!el || el === document.body || !container.contains(el)) return null;
    const s = { el, k: el.dataset ? el.dataset.k || null : null, typed: null, start: null, end: null, dir: null };
    if (isTextField(el) || el.tagName === 'INPUT') {
      if (el.value !== el.defaultValue) s.typed = el.value;
      try {
        s.start = el.selectionStart; s.end = el.selectionEnd; s.dir = el.selectionDirection;
      } catch (e) { /* no selection on this input type */ }
    }
    return s;
  }

  function restoreFocus(container, s, fallback) {
    let el = s.k ? container.querySelector(kq(s.k)) : null;
    if (!el && fallback) el = fallback(s);
    if (!el) return;
    if (s.typed != null && 'value' in el && el.value !== s.typed) el.value = s.typed;
    el.focus({ preventScroll: true });
    if (s.start != null) {
      try { el.setSelectionRange(s.start, s.end, s.dir || 'none'); } catch (e) { /* ignore */ }
    }
  }

  function syncAttributes(live, fresh) {
    for (const a of Array.from(live.attributes)) {
      if (!fresh.hasAttribute(a.name)) live.removeAttribute(a.name);
    }
    for (const a of Array.from(fresh.attributes)) {
      if (live.getAttribute(a.name) !== a.value) live.setAttribute(a.name, a.value);
    }
  }

  // Rebuild `container` from html but keep the focused text field `keep`
  // (and its ancestors) as the same nodes. Returns false when the new
  // markup has no matching field, so the caller does a plain rebuild.
  function rebuildAround(container, html, keep) {
    const tpl = document.createElement('template');
    tpl.innerHTML = html;
    const frag = tpl.content;
    const ph = frag.querySelector(kq(keep.dataset.k));
    if (!ph || ph.tagName !== keep.tagName || ph.type !== keep.type) return false;
    const livePath = [];
    for (let n = keep; n && n !== container; n = n.parentNode) livePath.push(n);
    const freshPath = [];
    for (let n = ph; n && n !== frag; n = n.parentNode) freshPath.push(n);
    if (livePath.length !== freshPath.length) return false;
    for (let i = 0; i < livePath.length; i++) {
      if (livePath[i].tagName !== freshPath[i].tagName) return false;
    }
    const typed = keep.value !== keep.defaultValue;
    let sel = null;
    try { sel = [keep.selectionStart, keep.selectionEnd, keep.selectionDirection]; } catch (e) { sel = null; }
    let liveParent = container;
    let freshParent = frag;
    for (let i = livePath.length - 1; i >= 0; i--) {
      const liveNode = livePath[i];
      const freshNode = freshPath[i];
      const kids = Array.from(freshParent.childNodes);
      const idx = kids.indexOf(freshNode);
      for (const c of Array.from(liveParent.childNodes)) if (c !== liveNode) liveParent.removeChild(c);
      for (let j = 0; j < idx; j++) liveParent.insertBefore(kids[j], liveNode);
      for (let j = idx + 1; j < kids.length; j++) liveParent.appendChild(kids[j]);
      if (i > 0) syncAttributes(liveNode, freshNode);
      liveParent = liveNode;
      freshParent = freshNode;
    }
    // The kept field itself: take the new attributes and saved value, but
    // keep what the user typed since the last render.
    const freshValue = ph.tagName === 'TEXTAREA' ? ph.defaultValue : ph.getAttribute('value') || '';
    for (const a of Array.from(ph.attributes)) {
      if (a.name !== 'value' && keep.getAttribute(a.name) !== a.value) keep.setAttribute(a.name, a.value);
    }
    if (keep.defaultValue !== freshValue) keep.defaultValue = freshValue;
    if (!typed && keep.value !== freshValue) keep.value = freshValue;
    if (sel && document.activeElement === keep) {
      try { keep.setSelectionRange(Math.min(sel[0], keep.value.length), Math.min(sel[1], keep.value.length), sel[2]); } catch (e) { /* ignore */ }
    }
    return true;
  }

  /** Replace a container's content, keeping focus, typed text, caret and scroll. */
  function renderInto(container, html, opts) {
    opts = opts || {};
    const scroller = opts.scroller || null;
    const top = scroller ? scroller.scrollTop : 0;
    const snap = snapshotFocus(container);
    let kept = false;
    if (snap && snap.k && isKeptField(snap.el)) kept = rebuildAround(container, html, snap.el);
    if (!kept) {
      container.innerHTML = html;
      if (snap) restoreFocus(container, snap, opts.fallback);
    }
    if (scroller && scroller.scrollTop !== top) scroller.scrollTop = top;
  }

  // ------------------------------------------------------------- toasts

  function toast(message, opts) {
    opts = opts || {};
    const box = $('#toasts');
    while (box.children.length >= 4) box.firstElementChild.remove();
    const el = document.createElement('div');
    el.className = 'toast' + (opts.kind === 'error' ? ' error' : '');
    el.setAttribute('role', opts.kind === 'error' ? 'alert' : 'status');
    const msg = document.createElement('span');
    msg.className = 'toast-msg';
    msg.textContent = message;
    el.append(msg);
    let timer = 0;
    const dismiss = () => { clearTimeout(timer); el.remove(); };
    if (opts.action) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = opts.action;
      b.addEventListener('click', () => { dismiss(); opts.onAction(); });
      el.append(b);
    }
    const x = document.createElement('button');
    x.type = 'button';
    x.className = 'toast-close';
    x.setAttribute('aria-label', 'Dismiss');
    x.textContent = '✕';
    x.addEventListener('click', dismiss);
    el.append(x);
    box.append(el);
    timer = setTimeout(dismiss, opts.timeout || (opts.action ? 8000 : opts.kind === 'error' ? 7000 : 3500));
    return { dismiss, el };
  }

  function errorToast(prefix, e) {
    toast(prefix + (e && e.message ? ' ' + e.message : ''), { kind: 'error' });
  }

  // ------------------------------------------------------------- state

  const S = {
    page: 'list',
    view: load('tt-view', 'open'),
    sort: load('tt-sort', 'newest'),
    labelIds: load('tt-labels', []),
    match: load('tt-match', 'any'),
    q: '',
    tasks: [],
    meta: { labels: [], counts: {} },
    loaded: false,
    openId: null,
    editingLabel: null,
    unfolded: new Set(),
    flashId: null,
  };
  if (!VIEW_NAMES[S.view]) S.view = 'open';
  if (!['newest', 'due', 'priority'].includes(S.sort)) S.sort = 'newest';
  if (!Array.isArray(S.labelIds)) S.labelIds = [];
  if (S.match !== 'all') S.match = 'any';

  const listeners = { refresh: [], task: [] };
  function on(event, fn) { listeners[event].push(fn); }
  function emit(event, arg) { for (const fn of listeners[event]) fn(arg); }

  function labelById(id) { return S.meta.labels.find((l) => l.id === id) || null; }
  function labelByName(name) {
    const lower = String(name).toLowerCase();
    return S.meta.labels.find((l) => l.name.toLowerCase() === lower) || null;
  }
  function labelNames() { return S.meta.labels.map((l) => l.name); }
  function taskById(id) { return S.tasks.find((t) => t.id === id) || null; }

  let refreshSeq = 0;
  let refreshTimer = 0;

  function scheduleRefresh(delay) {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, delay == null ? 150 : delay);
  }

  function listQuery(view) {
    const params = new URLSearchParams({ view: view || S.view, sort: S.sort, match: S.match });
    if (S.labelIds.length) params.set('labels', S.labelIds.join(','));
    if (S.q.trim()) params.set('q', S.q.trim());
    return params;
  }

  async function refresh() {
    clearTimeout(refreshTimer);
    const seq = ++refreshSeq;
    try {
      const view = S.page === 'matrix' && TT.matrixView ? TT.matrixView() : S.view;
      const [list, meta] = await Promise.all([
        api('GET', '/api/tasks?' + listQuery(view)), api('GET', '/api/meta')]);
      if (seq !== refreshSeq) return;
      S.meta = meta;
      const known = new Set(meta.labels.map((l) => l.id));
      const kept = S.labelIds.filter((id) => known.has(id));
      if (kept.length !== S.labelIds.length) { S.labelIds = kept; save('tt-labels', kept); }
      S.tasks = mergeLocal(list.tasks);
      S.loaded = true;
      renderSidebar();
      renderList();
      emit('refresh');
    } catch (e) {
      if (seq === refreshSeq && !e.network) errorToast('Could not load tasks:', e);
    }
  }

  // Later phases keep optimistic local data while their writes are queued.
  function mergeLocal(tasks) {
    let out = TT.mergeLocal ? TT.mergeLocal(tasks) : tasks;
    if (TT.mergeMatrixLocal) out = TT.mergeMatrixLocal(out);
    return out;
  }

  /** A full task arrived from the server (after a write): update everything. */
  function applyTask(task) {
    const i = S.tasks.findIndex((t) => t.id === task.id);
    if (i >= 0) S.tasks[i] = Object.assign({}, S.tasks[i], task);
    if (E.task && E.task.id === task.id) E.task = task;
    emit('task', task);
  }

  // ------------------------------------------------------------- sidebar

  function renderSidebar() {
    schedule('sidebar', () => {
      const c = S.meta.counts || {};
      const nav = VIEWS.map(([v, name, icon]) => {
        const n = c[v] == null ? '' : c[v];
        const alert = v === 'overdue' && n > 0 ? ' alert' : '';
        const current = S.page === 'list' && S.view === v ? ' aria-current="page"' : '';
        return '<button type="button" class="nav-item" data-act="view" data-view="' + v + '" data-k="nav:' + v + '"' + current + '>'
          + '<span class="nav-icon" aria-hidden="true">' + icon + '</span><span>' + esc(name) + '</span>'
          + '<span class="count' + alert + '"' + (alert ? ' aria-label="' + n + ' overdue"' : '') + '>' + n + '</span></button>';
      }).join('');
      const matrix = TT.sidebarMatrixEntry ? TT.sidebarMatrixEntry() : '';
      const labels = S.meta.labels.map(labelItemHTML).join('');
      const html = matrix
        + '<nav class="nav" aria-label="Views">' + nav + '</nav>'
        + '<div class="side-head"><span id="labels-head">Labels</span>'
        + '<span class="seg" role="group" aria-label="Show tasks with">'
        + '<button type="button" data-act="match" data-match="any" data-k="match:any" aria-pressed="' + (S.match === 'any') + '" title="Tasks with any selected label">Any</button>'
        + '<button type="button" data-act="match" data-match="all" data-k="match:all" aria-pressed="' + (S.match === 'all') + '" title="Tasks with all selected labels">All</button>'
        + '</span></div>'
        + (labels ? '<ul class="label-list" aria-labelledby="labels-head">' + labels + '</ul>'
          : '<div class="side-empty">Labels appear when you type #name in a task.</div>')
        + (S.labelIds.length ? '<button type="button" class="link-btn clear-labels" data-act="clear-labels" data-k="side:clear">Clear label filter</button>' : '')
        + '<div class="side-foot">'
        + '<div class="side-actions">'
        + '<button type="button" class="btn" data-act="export" data-k="side:export">Export JSON</button>'
        + (supports('import') && TT.importJSON ? '<button type="button" class="btn" data-act="import" data-k="side:import">Import JSON</button>' : '')
        + '<button type="button" class="btn" data-act="backup" data-k="side:backup">Back up now</button>'
        + '</div>'
        + themeSwitchHTML()
        + '</div>';
      renderInto($('#side-content'), html, { scroller: $('#side-content') });
      const editing = S.editingLabel != null ? $(kq('lbl:' + S.editingLabel + ':rename')) : null;
      if (editing && document.activeElement !== editing) { editing.focus({ preventScroll: true }); editing.select(); }
    });
  }

  function labelItemHTML(l) {
    const selected = S.labelIds.includes(l.id);
    const k = 'lbl:' + l.id;
    const style = ' style="--lc:' + esc(l.color) + '"';
    const color = '<span class="label-color"' + style + ' data-act="label-color" role="button" tabindex="0" data-k="' + k + ':color"'
      + ' aria-label="Change colour of ' + esc(l.name) + '" title="Change colour">'
      + '<input type="color" tabindex="-1" aria-hidden="true" value="' + esc(l.color) + '" data-label="' + l.id + '"></span>';
    if (S.editingLabel === l.id) {
      return '<li class="label-item editing" data-label="' + l.id + '">' + color
        + '<input type="text" class="label-rename" maxlength="40" data-act="rename" data-label="' + l.id + '" data-k="' + k + ':rename"'
        + ' value="' + esc(l.name) + '" aria-label="Rename label ' + esc(l.name) + ' (Enter saves, Esc cancels)">'
        + '<button type="button" class="icon-btn" data-act="delete-label" data-label="' + l.id + '" data-k="' + k + ':delete"'
        + ' aria-label="Delete label ' + esc(l.name) + '" title="Delete label (tasks stay)">🗑</button></li>';
    }
    return '<li class="label-item' + (selected ? ' selected' : '') + '" data-label="' + l.id + '">' + color
      + '<button type="button" class="label-name" data-act="label" data-label="' + l.id + '" data-k="' + k + ':name"'
      + ' aria-pressed="' + selected + '" title="Filter by ' + esc(l.name) + '">' + esc(l.name) + '</button>'
      + '<span class="count" aria-label="' + l.open + ' open">' + l.open + '</span>'
      + '<button type="button" class="icon-btn label-edit" data-act="edit-label" data-label="' + l.id + '" data-k="' + k + ':edit"'
      + ' aria-label="Rename or delete label ' + esc(l.name) + '" title="Rename or delete">✎</button></li>';
  }

  function themeSwitchHTML() {
    const pref = ROOT.dataset.themePref || 'auto';
    const opt = (v, label, icon) => '<button type="button" role="radio" data-act="theme" data-theme="' + v + '" data-k="theme:' + v + '"'
      + ' aria-checked="' + (pref === v) + '" tabindex="' + (pref === v ? 0 : -1) + '"><span aria-hidden="true">' + icon + '</span> ' + label + '</button>';
    return '<div class="theme-switch" role="radiogroup" aria-label="Theme">'
      + opt('day', 'Day', '☀') + opt('night', 'Night', '☾') + opt('auto', 'Auto', '◐') + '</div>';
  }

  function setView(view) {
    if (S.page !== 'list') TT.showPage('list');
    S.view = view;
    save('tt-view', view);
    renderSidebar();
    updateHeader();
    refresh();
  }

  function toggleLabel(id) {
    const i = S.labelIds.indexOf(id);
    if (i >= 0) S.labelIds.splice(i, 1); else S.labelIds.push(id);
    save('tt-labels', S.labelIds);
    renderSidebar();
    updateHeader();
    refresh();
  }

  async function renameLabel(id, name) {
    const l = labelById(id);
    S.editingLabel = null;
    if (!l || !name.trim() || name.trim() === l.name) { renderSidebar(); return; }
    try {
      const res = await api('PATCH', '/api/labels/' + id, { name: name.trim() });
      if (res.merged) {
        const i = S.labelIds.indexOf(id);
        if (i >= 0) {
          S.labelIds.splice(i, 1);
          if (!S.labelIds.includes(res.id)) S.labelIds.push(res.id);
          save('tt-labels', S.labelIds);
        }
        toast('Merged into label “' + res.name + '”.');
      }
    } catch (e) { errorToast('Could not rename the label:', e); }
    await refresh();
    if (E.task) reloadEditor();
    const btn = $(kq('lbl:' + (id) + ':name')) || $('#side-content .label-name');
    if (btn) btn.focus({ preventScroll: true });
  }

  async function deleteLabel(id) {
    const l = labelById(id);
    if (!l) return;
    if (!window.confirm('Delete the label “' + l.name + '”? The tasks keep everything else.')) return;
    S.editingLabel = null;
    try {
      await api('DELETE', '/api/labels/' + id);
      S.labelIds = S.labelIds.filter((x) => x !== id);
      save('tt-labels', S.labelIds);
    } catch (e) { errorToast('Could not delete the label:', e); }
    await refresh();
    if (E.task) reloadEditor();
  }

  async function recolorLabel(id, color) {
    try {
      await api('PATCH', '/api/labels/' + id, { color });
    } catch (e) { errorToast('Could not change the colour:', e); }
    await refresh();
    if (E.task) reloadEditor();
  }

  function exportJSON() {
    const a = document.createElement('a');
    a.href = '/api/export';
    a.download = '';
    document.body.append(a);
    a.click();
    a.remove();
  }

  async function backupNow() {
    try {
      const res = await api('POST', '/api/backup', {});
      toast('Backup saved: ' + res.file);
    } catch (e) { errorToast('Backup failed:', e); }
  }

  function setTheme(pref) {
    try { localStorage.setItem('tt-theme', pref); } catch (e) { /* ignore */ }
    applyTheme();
    renderSidebar();
  }

  function applyTheme() {
    let pref = 'auto';
    try { pref = localStorage.getItem('tt-theme') || 'auto'; } catch (e) { /* ignore */ }
    if (pref !== 'day' && pref !== 'night') pref = 'auto';
    const dark = pref === 'night' || (pref === 'auto' && window.matchMedia('(prefers-color-scheme: dark)').matches);
    ROOT.dataset.theme = dark ? 'dark' : 'light';
    ROOT.dataset.themePref = pref;
    const c = getComputedStyle(ROOT).getPropertyValue('--theme-color').trim();
    const meta = $('meta[name="theme-color"]');
    if (c && meta) meta.setAttribute('content', c);
  }

  function bindSidebar() {
    const side = $('#side-content');
    side.addEventListener('click', (e) => {
      const el = e.target.closest('[data-act]');
      if (!el || !side.contains(el)) return;
      const act = el.dataset.act;
      if (act === 'view') setView(el.dataset.view);
      else if (act === 'label') toggleLabel(Number(el.dataset.label));
      else if (act === 'match') {
        S.match = el.dataset.match;
        save('tt-match', S.match);
        renderSidebar();
        updateHeader();
        refresh();
      } else if (act === 'clear-labels') {
        S.labelIds = [];
        save('tt-labels', []);
        renderSidebar();
        updateHeader();
        refresh();
        const first = $(kq('nav:' + S.view));
        if (first) first.focus({ preventScroll: true });
      } else if (act === 'edit-label') {
        S.editingLabel = Number(el.dataset.label);
        renderSidebar();
      } else if (act === 'delete-label') deleteLabel(Number(el.dataset.label));
      else if (act === 'label-color') openColorPicker(el);
      else if (act === 'export') exportJSON();
      else if (act === 'import') TT.importJSON();
      else if (act === 'backup') backupNow();
      else if (act === 'theme') setTheme(el.dataset.theme);
      else if (act === 'matrix') TT.showPage('matrix');
    });
    side.addEventListener('keydown', (e) => {
      const el = e.target;
      if (el.dataset.act === 'rename') {
        if (e.key === 'Enter') { e.preventDefault(); renameLabel(Number(el.dataset.label), el.value); }
        else if (e.key === 'Escape') {
          e.preventDefault();
          e.stopPropagation();
          const id = S.editingLabel;
          S.editingLabel = null;
          renderSidebar();
          const btn = $(kq('lbl:' + id + ':edit'));
          if (btn) btn.focus({ preventScroll: true });
        }
      } else if (el.dataset.act === 'label-color' && (e.key === 'Enter' || e.key === ' ')) {
        e.preventDefault();
        openColorPicker(el);
      } else if (el.dataset.act === 'theme' && /^Arrow(Left|Right|Up|Down)$/.test(e.key)) {
        e.preventDefault();
        const order = ['day', 'night', 'auto'];
        const i = order.indexOf(el.dataset.theme);
        const next = order[(i + (e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? 2 : 1)) % 3];
        setTheme(next);
        const b = $(kq('theme:' + next));
        if (b) b.focus({ preventScroll: true });
      }
    });
    side.addEventListener('focusout', (e) => {
      const el = e.target;
      if (el.dataset && el.dataset.act === 'rename' && S.editingLabel === Number(el.dataset.label)) {
        const next = e.relatedTarget;
        if (next && next.dataset && next.dataset.act === 'delete-label') return;
        renameLabel(Number(el.dataset.label), el.value);
      }
    });
    side.addEventListener('change', (e) => {
      const input = e.target;
      if (input.type === 'color' && input.dataset.label) recolorLabel(Number(input.dataset.label), input.value);
    });
  }

  function openColorPicker(host) {
    const input = host.querySelector('input[type="color"]');
    if (!input) return;
    try {
      if (input.showPicker) input.showPicker(); else input.click();
    } catch (e) { input.click(); }
  }

  // ------------------------------------------------------------- header

  function updateHeader() {
    const title = $('#view-title');
    const sub = $('#view-sub');
    if (S.page !== 'list') return;
    title.textContent = VIEW_NAMES[S.view];
    const parts = [];
    if (S.labelIds.length) {
      const names = S.labelIds.map((id) => (labelById(id) || {}).name).filter(Boolean).map((n) => '#' + n);
      parts.push((S.labelIds.length > 1 ? (S.match === 'all' ? 'all of ' : 'any of ') : '') + names.join(', '));
    }
    if (S.q.trim()) parts.push('matching “' + S.q.trim() + '”');
    if (S.loaded) parts.unshift(S.tasks.length + (S.tasks.length === 1 ? ' task' : ' tasks'));
    sub.textContent = parts.join(' · ');
  }

  // ------------------------------------------------------------- quick add

  const qaSug = { items: [], active: -1, arrowUsed: false, query: null };

  function quickAddDefaults(parsed) {
    const labels = parsed.labels.slice();
    const lower = labels.map((x) => x.toLowerCase());
    const fromFilter = [];
    for (const id of S.labelIds) {
      const l = labelById(id);
      if (l && !lower.includes(l.name.toLowerCase())) { labels.push(l.name); fromFilter.push(l.name); }
    }
    let due = parsed.due;
    let dueFromView = false;
    if (!due && S.page === 'list' && S.view === 'today') { due = P.ymd(new Date()); dueFromView = true; }
    return { labels, fromFilter, due, dueFromView };
  }

  function renderQuickChips() {
    const qa = $('#qa');
    const box = $('#qa-chips');
    if (!qa.value.trim()) { box.innerHTML = ''; return; }
    const r = P.parseQuickAdd(qa.value, { now: new Date(), labels: labelNames() });
    const d = quickAddDefaults(r);
    const chips = [];
    for (const name of d.labels) {
      const l = labelByName(name);
      const extra = d.fromFilter.includes(name) ? ' (filter)' : '';
      chips.push(l
        ? '<span class="chip" style="--lc:' + esc(l.color) + '"><span class="chip-dot" aria-hidden="true"></span><span class="chip-text">#' + esc(l.name) + extra + '</span></span>'
        : '<span class="chip new"><span class="chip-text">#' + esc(name) + ' (new label)</span></span>');
    }
    if (r.priority) chips.push('<span class="qa-chip">' + PRIORITY_NAMES[r.priority] + ' priority</span>');
    if (d.due) chips.push('<span class="qa-chip">⏱ ' + esc(P.dueLong(d.due)) + (d.dueFromView ? ' (Today view)' : '') + '</span>');
    for (const bad of r.errors) chips.push('<span class="qa-chip error">Not a date: ' + esc(bad) + '</span>');
    if (!r.title) chips.push('<span class="qa-chip hint">Type a title</span>');
    box.innerHTML = chips.join('');
  }

  function showQaSuggest() {
    const qa = $('#qa');
    const list = $('#qa-suggest');
    const q = document.activeElement === qa ? P.labelQueryAt(qa.value, qa.selectionStart) : null;
    const items = q ? P.suggestLabels(q.prefix, labelNames(), [], 8) : [];
    if (!items.length || (items.length === 1 && items[0].toLowerCase() === q.prefix.toLowerCase())) {
      hideQaSuggest();
      return;
    }
    qaSug.items = items;
    qaSug.query = q;
    if (qaSug.active >= items.length) qaSug.active = -1;
    list.innerHTML = items.map((name, i) => {
      const l = labelByName(name);
      return '<li role="option" id="qa-opt-' + i + '" data-i="' + i + '" aria-selected="' + (i === qaSug.active) + '">'
        + '<span class="chip" style="--lc:' + esc(l ? l.color : '#888888') + '"><span class="chip-dot" aria-hidden="true"></span>'
        + '<span class="chip-text">' + esc(name) + '</span></span>'
        + (i === 0 && qaSug.active < 0 ? '<span class="hint">Tab</span>' : '') + '</li>';
    }).join('');
    list.hidden = false;
    qa.setAttribute('aria-expanded', 'true');
    if (qaSug.active >= 0) qa.setAttribute('aria-activedescendant', 'qa-opt-' + qaSug.active);
    else qa.removeAttribute('aria-activedescendant');
  }

  function hideQaSuggest() {
    const list = $('#qa-suggest');
    list.hidden = true;
    list.innerHTML = '';
    qaSug.items = [];
    qaSug.active = -1;
    qaSug.arrowUsed = false;
    $('#qa').setAttribute('aria-expanded', 'false');
    $('#qa').removeAttribute('aria-activedescendant');
  }

  function pickQaSuggest(i) {
    const qa = $('#qa');
    const name = qaSug.items[i];
    const q = qaSug.query;
    if (!name || !q) return;
    const before = qa.value.slice(0, q.start);
    const after = qa.value.slice(q.end);
    const insert = '#' + name + (after.startsWith(' ') ? '' : ' ');
    qa.value = before + insert + after;
    const caret = before.length + insert.length;
    qa.setSelectionRange(caret, caret);
    hideQaSuggest();
    renderQuickChips();
  }

  async function submitQuickAdd(openAfter) {
    const qa = $('#qa');
    const text = qa.value;
    const r = P.parseQuickAdd(text, { now: new Date(), labels: labelNames() });
    if (!r.title) {
      renderQuickChips();
      if (text.trim()) toast('Type a title for the task.');
      return;
    }
    const d = quickAddDefaults(r);
    const body = { title: r.title };
    if (d.labels.length) body.labels = d.labels;
    if (r.priority) body.priority = r.priority;
    if (d.due) body.due_at = d.due;
    qa.value = '';
    hideQaSuggest();
    renderQuickChips();
    try {
      const task = await api('POST', '/api/tasks', body);
      S.flashId = task.id;
      if (openAfter) openEditor(task.id, task, { focus: 'title' });
      await refresh();
      if (!taskById(task.id) && !openAfter) toast('Added “' + task.title + '” (not shown in this view).');
    } catch (e) {
      if (!qa.value) { qa.value = text; renderQuickChips(); }
      errorToast('Could not add the task:', e);
    }
  }

  function focusQuickAdd() {
    const qa = $('#qa');
    qa.focus({ preventScroll: true });
    qa.select();
  }

  function bindQuickAdd() {
    const qa = $('#qa');
    qa.addEventListener('input', () => {
      qaSug.arrowUsed = false;
      qaSug.active = -1;
      renderQuickChips();
      showQaSuggest();
    });
    qa.addEventListener('click', showQaSuggest);
    qa.addEventListener('blur', () => setTimeout(() => { if (document.activeElement !== qa) hideQaSuggest(); }, 0));
    qa.addEventListener('keydown', (e) => {
      const open = !$('#qa-suggest').hidden && qaSug.items.length > 0;
      if (open && e.key === 'Tab' && !e.shiftKey) {
        e.preventDefault();
        pickQaSuggest(qaSug.active >= 0 ? qaSug.active : 0);
      } else if (open && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
        e.preventDefault();
        const n = qaSug.items.length;
        qaSug.active = e.key === 'ArrowDown' ? (qaSug.active + 1) % n : (qaSug.active <= 0 ? n - 1 : qaSug.active - 1);
        qaSug.arrowUsed = true;
        showQaSuggest();
      } else if (e.key === 'Enter') {
        e.preventDefault();
        if (open && qaSug.arrowUsed && qaSug.active >= 0) pickQaSuggest(qaSug.active);
        else if (!e.isComposing) submitQuickAdd(e.shiftKey);
      } else if (e.key === 'Escape' && open) {
        e.preventDefault();
        e.stopPropagation();
        hideQaSuggest();
      }
    });
    qa.addEventListener('keyup', (e) => {
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight' || e.key === 'Home' || e.key === 'End') showQaSuggest();
    });
    $('#qa-suggest').addEventListener('mousedown', (e) => {
      const li = e.target.closest('li[data-i]');
      if (!li) return;
      e.preventDefault();
      pickQaSuggest(Number(li.dataset.i));
    });
  }

  // ------------------------------------------------------------- search

  let searchTimer = 0;
  function bindSearch() {
    const input = $('#search');
    input.addEventListener('input', () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        S.q = input.value;
        updateHeader();
        refresh();
      }, 180);
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        if (input.value) { clearSearch(); } else if (S.openId) closeEditor(); else input.blur();
      } else if (e.key === 'Enter') {
        clearTimeout(searchTimer);
        S.q = input.value;
        updateHeader();
        refresh();
      }
    });
  }

  function clearSearch() {
    const input = $('#search');
    input.value = '';
    clearTimeout(searchTimer);
    if (S.q) { S.q = ''; updateHeader(); refresh(); }
  }

  // ------------------------------------------------------------- list

  function chipHTML(l, extraClass) {
    return '<span class="chip' + (extraClass ? ' ' + extraClass : '') + '" style="--lc:' + esc(l.color) + '">'
      + '<span class="chip-dot" aria-hidden="true"></span><span class="chip-text">' + esc(l.name) + '</span></span>';
  }

  function dueBadgeHTML(due, now, prefix) {
    const st = P.dueState(due, now);
    return '<span class="badge due ' + st + '" title="Target: ' + esc(P.dueLong(due)) + (st === 'overdue' ? ' (overdue)' : '') + '">'
      + (prefix || '⏱') + ' ' + esc(P.dueLabel(due, now)) + '</span>';
  }

  function rowHTML(t, now) {
    const k = 't' + t.id;
    const done = t.status === 'done';
    const badges = [];
    if (t.status === 'in_progress') badges.push('<span class="badge progress">In progress</span>');
    if (t.due_at) badges.push(dueBadgeHTML(t.due_at, now));
    if (TT.rowExtraBadges) badges.push(TT.rowExtraBadges(t, now));
    if (t.images) badges.push('<span class="badge" title="' + t.images + (t.images === 1 ? ' image' : ' images') + '">🖼 ' + t.images + '</span>');
    const labels = t.labels.map((l) => chipHTML(l)).join('');
    const stamp = done && t.completed_at
      ? '<span class="stamp" title="Completed ' + esc(t.completed_at.replace('T', ' ')) + '">Done ' + esc(P.stampLabel(t.completed_at, now)) + '</span>'
      : '<span class="stamp" title="Created ' + esc(t.created_at.replace('T', ' ')) + '">' + esc(P.stampLabel(t.created_at, now)) + '</span>';
    const prio = t.priority === 'medium' ? ''
      : '<span class="prio-badge ' + t.priority + '">' + PRIORITY_NAMES[t.priority] + '</span>';
    const cls = 'row prio-' + t.priority + (done ? ' is-done' : '') + (t.id === S.openId ? ' is-open' : '')
      + (t.id === S.flashId ? ' flash' : '');
    return '<div class="' + cls + '" data-id="' + t.id + '" role="listitem">'
      + '<button type="button" class="check" data-act="check" data-k="' + k + ':check" aria-pressed="' + done + '"'
      + ' aria-label="' + (done ? 'Reopen' : 'Mark done') + ': ' + esc(t.title) + '" title="' + (done ? 'Reopen' : 'Mark done') + '"></button>'
      + '<div class="row-main" data-act="open">'
      + '<button type="button" class="row-title" data-act="open" data-k="' + k + ':open"'
      + ' aria-label="' + esc(t.title) + (t.status === 'in_progress' ? ', in progress' : '') + (t.due_at ? ', due ' + esc(P.dueLong(t.due_at)) : '') + '">'
      + esc(t.title) + '</button>'
      + (t.snippet ? '<div class="row-snippet">' + esc(t.snippet) + '</div>' : '')
      + '<div class="row-meta">' + badges.join('') + labels + stamp + '</div>'
      + '</div>' + prio
      + (TT.rowExtra ? TT.rowExtra(t, now) : '')
      + '</div>';
  }

  function emptyHTML() {
    if (!S.loaded) return '<div class="empty">Loading…</div>';
    if (S.q.trim()) return '<div class="empty"><strong>Nothing matches “' + esc(S.q.trim()) + '”.</strong>Search looks at titles, descriptions and subtasks.</div>';
    if (S.labelIds.length) return '<div class="empty"><strong>No tasks with these labels here.</strong>New tasks you add now get the selected labels.</div>';
    const msg = {
      open: ['Nothing to do.', 'Type a task above and press Enter.'],
      overdue: ['Nothing is overdue.', 'Well done.'],
      today: ['Nothing due today.', 'Tasks you add in this view are due today.'],
      week: ['Nothing due in the next 7 days.', ''],
      nodate: ['Every open task has a target date.', ''],
      done: ['No finished tasks yet.', ''],
      all: ['No tasks yet.', 'Type a task above and press Enter.'],
    }[S.view];
    return '<div class="empty"><strong>' + esc(msg[0]) + '</strong>' + esc(msg[1]) + '</div>';
  }

  function listFallback(snap) {
    // The focused row is gone (e.g. done in the Open view): focus the same
    // control in the row that took its place, so focus never drops to <body>.
    const list = $('#list');
    const part = snap.k && snap.k.includes(':') ? snap.k.slice(snap.k.indexOf(':') + 1) : 'open';
    const rows = $$('.row', list);
    const idx = Math.min(snap.rowIndex == null ? 0 : snap.rowIndex, rows.length - 1);
    if (idx >= 0) {
      const target = rows[idx].querySelector('[data-k$=":' + part + '"]') || rows[idx].querySelector('.row-title');
      if (target) return target;
    }
    return list;
  }

  function renderList() {
    schedule('list', () => {
      if (S.page !== 'list') return;
      const now = new Date();
      let html;
      if (!S.tasks.length) html = emptyHTML();
      else if (S.sort === 'due') {
        const groups = new Map(GROUPS.map(([g]) => [g, []]));
        for (const t of S.tasks) groups.get(t.status === 'done' && !t.eff_due ? 'none' : P.dueGroup(t.eff_due, now)).push(t);
        html = GROUPS.filter(([g]) => groups.get(g).length).map(([g, name]) =>
          '<h2 class="group-head ' + g + '">' + name + '<span class="n">' + groups.get(g).length + '</span></h2>'
          + groups.get(g).map((t) => rowHTML(t, now)).join('')).join('');
      } else html = S.tasks.map((t) => rowHTML(t, now)).join('');
      const list = $('#list');
      const active = document.activeElement;
      const row = active && list.contains(active) ? active.closest('.row') : null;
      const rowIndex = row ? $$('.row', list).indexOf(row) : null;
      renderInto(list, html, {
        scroller: $('#list-scroll'),
        fallback: (snap) => { snap.rowIndex = rowIndex; return listFallback(snap); },
      });
      S.flashId = null;
      updateHeader();
    });
  }

  async function setTaskStatus(id, status, opts) {
    opts = opts || {};
    const t = taskById(id);
    const before = t ? t.status : null;
    if (t) { t.status = status; renderList(); }
    try {
      const task = await api('PATCH', '/api/tasks/' + id, { status });
      applyTask(task);
      if (opts.undo && before && before !== status) {
        toast((status === 'done' ? 'Done: ' : 'Reopened: ') + task.title, {
          action: 'Undo',
          onAction: () => setTaskStatus(id, before),
        });
      }
      if (E.task && E.task.id === id) renderEditor();
      scheduleRefresh();
      return task;
    } catch (e) {
      if (t) { t.status = before; renderList(); }
      errorToast('Could not change the task:', e);
      return null;
    }
  }

  function bindList() {
    const list = $('#list');
    list.addEventListener('click', (e) => {
      const el = e.target.closest('[data-act]');
      if (!el || !list.contains(el)) return;
      const row = el.closest('.row');
      const id = row ? Number(row.dataset.id) : null;
      const act = el.dataset.act;
      if (act === 'check') {
        const t = taskById(id);
        if (t) setTaskStatus(id, t.status === 'done' ? 'open' : 'done', { undo: true });
      } else if (act === 'open') {
        if (e.target.closest('a')) return;
        openEditor(id);
      } else if (TT.listAction) TT.listAction(act, el, id, e);
    });
    list.addEventListener('change', (e) => { if (TT.listChange) TT.listChange(e); });
    list.addEventListener('keydown', (e) => {
      const el = e.target;
      if (TT.listKeydown && TT.listKeydown(e)) return;
      if ((el.dataset.act === 'open' || el.dataset.act === 'check') && (e.key === 'ArrowDown' || e.key === 'ArrowUp')
        && !e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
        e.preventDefault();
        const rows = $$('.row', list);
        const i = rows.indexOf(el.closest('.row')) + (e.key === 'ArrowDown' ? 1 : -1);
        if (i >= 0 && i < rows.length) {
          const target = rows[i].querySelector('[data-act="' + el.dataset.act + '"]');
          if (target) target.focus();
        }
      }
    });
    $('#sort').addEventListener('change', (e) => {
      S.sort = e.target.value;
      save('tt-sort', S.sort);
      refresh();
    });
  }

  // ------------------------------------------------------------- editor

  const E = {
    task: null,
    pending: { id: null, fields: {} },   // unsaved fields and the task they belong to
    sending: [],                          // [{id, fields}] saves queued or on the wire, oldest first
    timer: 0,
    chain: Promise.resolve(),
    saving: 0,
    saveError: false,
    labelSug: { items: [], active: -1, arrowUsed: false },
    loadSeq: 0,
  };

  function editorValues(t) {
    const v = Object.assign({}, t);
    for (const rec of E.sending) if (rec.id === t.id) Object.assign(v, rec.fields);
    if (E.pending.id === t.id) Object.assign(v, E.pending.fields);
    if (v.labels && v.labels.length && typeof v.labels[0] === 'string') {
      v.labels = v.labels.map((name) => labelByName(name) || (t.labels.find((l) => l.name.toLowerCase() === name.toLowerCase()))
        || { id: null, name, color: '#808890' });
    }
    return v;
  }

  function optionsHTML(map, current) {
    return Object.entries(map).map(([value, name]) =>
      '<option value="' + value + '"' + (value === current ? ' selected' : '') + '>' + name + '</option>').join('');
  }

  function editorHTML(t) {
    const v = editorValues(t);
    const now = new Date();
    const due = v.due_at || '';
    const date = due.slice(0, 10);
    const time = due.length > 10 ? due.slice(11, 16) : '';
    const labelChips = v.labels.map((l) => '<span class="chip" style="--lc:' + esc(l.color) + '"><span class="chip-dot" aria-hidden="true"></span>'
      + '<span class="chip-text">' + esc(l.name) + '</span>'
      + '<button type="button" class="chip-x" data-act="label-x" data-name="' + esc(l.name) + '" data-k="ed:label-x:' + esc(l.name.toLowerCase()) + '"'
      + ' aria-label="Remove label ' + esc(l.name) + '" title="Remove">✕</button></span>').join('');
    const checklist = MD.findChecklist(v.description || '');
    const desc = v.description || '';
    const preview = MD.render(desc);
    return '<div class="ed-head">'
      + '<label class="sr-only" for="ed-title">Title</label>'
      + '<input id="ed-title" class="ed-title" type="text" data-k="ed:title" maxlength="500" autocomplete="off" value="' + esc(v.title) + '">'
      + '<button type="button" class="icon-btn" data-act="close" data-k="ed:close" aria-label="Close details (Esc)" title="Close (Esc)">✕</button>'
      + '</div>'
      + '<div class="ed-body">'
      + '<div class="ed-grid">'
      + '<label class="ed-label" for="ed-status">Status</label>'
      + '<div class="ed-row"><select id="ed-status" data-k="ed:status">' + optionsHTML(STATUS_NAMES, v.status) + '</select>'
      + '<label class="ed-label" for="ed-priority" style="margin-left:10px">Priority</label>'
      + '<select id="ed-priority" data-k="ed:priority">' + optionsHTML(PRIORITY_NAMES, v.priority) + '</select></div>'
      + '<label class="ed-label" for="ed-date">Target</label>'
      + '<div class="ed-row">'
      + '<input id="ed-date" type="date" data-k="ed:date" value="' + esc(date) + '">'
      + '<input id="ed-time" type="time" data-k="ed:time" value="' + esc(time) + '" aria-label="Target time (optional)" title="Time (optional)">'
      + (due ? '<button type="button" class="icon-btn" data-act="nodate" data-k="ed:nodate" aria-label="No target date" title="No target date">✕</button>'
        + dueBadgeHTML(due, now) : '<span class="view-sub">No target date</span>')
      + '</div>'
      + '<span class="ed-label" id="ed-labels-lbl">Labels</span>'
      + '<div class="ed-labels" data-act="focus-labels">' + labelChips
      + '<input class="label-input" type="text" data-k="ed:label-input" maxlength="41" autocomplete="off" role="combobox"'
      + ' aria-labelledby="ed-labels-lbl" aria-autocomplete="list" aria-controls="ed-label-suggest" aria-expanded="false"'
      + ' placeholder="' + (v.labels.length ? '' : 'Add a label…') + '">'
      + '<ul id="ed-label-suggest" class="suggest" role="listbox" aria-label="Label suggestions" hidden></ul>'
      + '</div>'
      + '</div>'
      + (TT.editorSubtasksHTML ? TT.editorSubtasksHTML(t) : '')
      + '<div class="ed-desc">'
      + '<div class="ed-section-title"><label for="ed-desc">Description</label><span class="grow"></span>'
      + '<span style="text-transform:none;letter-spacing:0;font-weight:400">Markdown · paste or drop images</span></div>'
      + '<textarea id="ed-desc" data-k="ed:desc" rows="7" spellcheck="true">\n' + esc(desc) + '</textarea>'
      + '<div class="md-tools">'
      + (checklist.length && TT.convertChecklist
        ? '<button type="button" class="link-btn" data-act="convert" data-k="ed:convert">Turn ' + checklist.length
          + (checklist.length === 1 ? ' checklist line' : ' checklist lines') + ' into subtasks</button>' : '')
      + '</div>'
      + '<div class="ed-section-title preview-title" id="ed-preview-lbl">Preview</div>'
      + '<div class="md md-preview' + (preview ? '' : ' empty-preview') + '" data-k="ed:preview" aria-labelledby="ed-preview-lbl">'
      + (preview || 'Nothing written yet.') + '</div>'
      + '</div>'
      + '<div class="ed-foot">'
      + '<span title="' + esc(t.created_at.replace('T', ' ')) + '">Created ' + esc(P.stampLabel(t.created_at, now)) + '</span>'
      + '<span data-k="ed:edited" title="' + esc(t.updated_at.replace('T', ' ')) + '">Edited ' + esc(P.stampLabel(t.updated_at, now)) + '</span>'
      + (t.completed_at ? '<span title="' + esc(t.completed_at.replace('T', ' ')) + '">Done ' + esc(P.stampLabel(t.completed_at, now)) + '</span>' : '')
      + '<span class="grow"></span>'
      + '<span class="save-state" role="status" aria-live="polite" data-k="ed:save"></span>'
      + '<button type="button" class="btn danger" data-act="delete" data-k="ed:delete">Delete task</button>'
      + '</div>'
      + '</div>';
  }

  function renderEditor() {
    schedule('editor', () => {
      const panel = $('#editor');
      if (!S.openId) { panel.hidden = true; panel.innerHTML = ''; return; }
      panel.hidden = false;
      if (!E.task || E.task.id !== S.openId) {
        renderInto(panel, '<div class="ed-head"><span class="view-sub">Loading…</span>'
          + '<button type="button" class="icon-btn" data-act="close" data-k="ed:close" aria-label="Close details">✕</button></div>');
        return;
      }
      renderInto(panel, editorHTML(E.task), { scroller: panel });
      updateSaveState();
      const li = $(kq('ed:label-input'), panel);
      if (li && document.activeElement === li) showLabelSuggest();
    });
  }

  function updateSaveState() {
    const el = $('#editor .save-state');
    if (!el) return;
    const pending = E.pending.id !== null && Object.keys(E.pending.fields).length > 0;
    const busy = pending || E.saving > 0 || (TT.subtaskBusy && TT.subtaskBusy());
    el.className = 'save-state' + (E.saveError ? ' error' : busy ? ' saving' : '');
    el.textContent = E.saveError ? 'Not saved' : busy ? 'Saving…' : (E.task ? 'Saved' : '');
  }

  async function openEditor(id, initial, opts) {
    opts = opts || {};
    const focusKey = () => {
      const a = document.activeElement;
      return a && a !== document.body ? (a.dataset && a.dataset.k) || a.id || a.tagName : null;
    };
    const startFocus = focusKey();
    if (S.openId !== id) {
      flushFields();
      if (TT.beforeEditorSwitch) TT.beforeEditorSwitch();
    }
    if (opts.details != null && S.subDetails) S.subDetails.add(opts.details);
    S.openId = id;
    E.saveError = false;
    if (initial && initial.description !== undefined) E.task = initial;
    else if (!E.task || E.task.id !== id) E.task = null;
    renderEditor();
    renderList();
    const seq = ++E.loadSeq;
    try {
      const task = await api('GET', '/api/tasks/' + id);
      if (seq !== E.loadSeq || S.openId !== id) return;
      E.task = mergeEditorLocal(task);
      renderEditor();
    } catch (e) {
      if (S.openId !== id) return;
      if (e.status === 404) { closeEditor(); toast('That task no longer exists.'); return; }
      errorToast('Could not open the task:', e);
      return;
    }
    // Move focus into the editor unless the user has moved on meanwhile.
    if (opts.focus !== 'none') {
      const target = $(kq(opts.focusK || 'ed:title'));
      const now = focusKey();
      if (target && (now === startFocus || now === null)) target.focus({ preventScroll: true });
    }
    if (TT.afterEditorOpen) TT.afterEditorOpen(opts);
  }

  function mergeEditorLocal(task) { return TT.mergeEditorLocal ? TT.mergeEditorLocal(task) : task; }

  async function reloadEditor() {
    if (!S.openId) return;
    const id = S.openId;
    const seq = ++E.loadSeq;
    try {
      const task = await api('GET', '/api/tasks/' + id);
      if (seq !== E.loadSeq || S.openId !== id) return;
      E.task = mergeEditorLocal(task);
      renderEditor();
    } catch (e) {
      if (e.status === 404 && S.openId === id) {
        closeEditor({ keepFocus: true });
        toast('This task was deleted (in another window).');
      }
    }
  }

  function closeEditor(opts) {
    opts = opts || {};
    if (!S.openId) return;
    const id = S.openId;
    flushFields();
    if (TT.beforeEditorSwitch) TT.beforeEditorSwitch();
    S.openId = null;
    E.task = null;
    E.loadSeq++;
    const panel = $('#editor');
    const hadFocus = panel.contains(document.activeElement);
    panel.hidden = true;
    panel.innerHTML = '';
    renderList();
    if (hadFocus && !opts.keepFocus) {
      const target = S.page === 'list' ? $(kq('t' + id + ':open')) : (TT.matrixFocusAfterClose && TT.matrixFocusAfterClose(id));
      if (target) target.focus({ preventScroll: true });
      else if (S.page === 'list') $('#list').focus({ preventScroll: true });
    }
  }

  function queueField(id, field, value, delay) {
    if (E.pending.id !== null && E.pending.id !== id) flushFields();
    E.pending.id = id;
    E.pending.fields[field] = value;
    E.saveError = false;
    clearTimeout(E.timer);
    if (delay) E.timer = setTimeout(flushFields, delay);
    else flushFields();
    updateSaveState();
  }

  /** Send the pending fields. Saves are chained, so an older save can
   *  never land after a newer one. */
  function flushFields() {
    clearTimeout(E.timer);
    const id = E.pending.id;
    const fields = E.pending.fields;
    if (id === null || !Object.keys(fields).length) return E.chain;
    E.pending = { id: null, fields: {} };
    const rec = { id, fields };
    E.sending.push(rec);    // recorded now, so redraws keep showing these values
    E.saving++;
    updateSaveState();
    E.chain = E.chain.then(async () => {
      try {
        const task = await api('PATCH', '/api/tasks/' + id, fields);
        applyTask(task);
        afterFieldSave(task, fields);
      } catch (e) {
        E.saveError = true;
        errorToast('Could not save:', e);
        if (e.status === 404 && S.openId === id) closeEditor({ keepFocus: true });
        else if (S.openId === id) reloadEditor();
      } finally {
        E.sending.splice(E.sending.indexOf(rec), 1);
        E.saving--;
        updateSaveState();
      }
    });
    E.chain.then(() => scheduleRefresh(250));
    return E.chain;
  }

  function afterFieldSave(task, fields) {
    if (S.openId !== task.id) return;
    // Text fields: the inputs already show the text, so only the footer
    // changes. Everything else (selects, dates, labels) redraws.
    const onlyText = Object.keys(fields).every((f) => f === 'title' || f === 'description');
    if (onlyText) {
      const edited = $(kq('ed:edited'));
      if (edited) {
        edited.textContent = 'Edited ' + P.stampLabel(task.updated_at, new Date());
        edited.title = task.updated_at.replace('T', ' ');
      }
      const title = $(kq('ed:title'));
      if (title && document.activeElement !== title && title.value !== E.task.title && !(E.pending.id === task.id && 'title' in E.pending.fields)) {
        title.value = E.task.title;
        title.defaultValue = E.task.title;
      } else if (title) title.defaultValue = title.value;
      const desc = $(kq('ed:desc'));
      if (desc) desc.defaultValue = desc.value;
    } else renderEditor();
  }

  function currentLabelNames() {
    const v = editorValues(E.task);
    return v.labels.map((l) => l.name);
  }

  function setEditorLabels(names) {
    queueField(E.task.id, 'labels', names, 0);
    renderEditor();
  }

  function addEditorLabel(raw) {
    const name = String(raw || '').trim().replace(/^#/, '');
    if (!name) return true;
    if (name.length > 40 || /[\s,#]/.test(name)) {
      toast('Label names have at most 40 characters and no spaces, commas or #.', { kind: 'error' });
      return false;
    }
    const existing = labelByName(name);
    const canon = existing ? existing.name : name;
    const names = currentLabelNames();
    if (!names.some((n) => n.toLowerCase() === canon.toLowerCase())) names.push(canon);
    const input = $(kq('ed:label-input'));
    if (input) { input.value = ''; input.defaultValue = ''; }
    hideLabelSuggest();
    setEditorLabels(names);
    return true;
  }

  function showLabelSuggest() {
    const input = $(kq('ed:label-input'));
    const list = $('#ed-label-suggest');
    if (!input || !list || !E.task) return;
    const prefix = input.value.trim().replace(/^#/, '');
    const items = P.suggestLabels(prefix, labelNames(), currentLabelNames(), 8);
    const sug = E.labelSug;
    if (!items.length) { hideLabelSuggest(); return; }
    sug.items = items;
    if (sug.active >= items.length) sug.active = -1;
    list.innerHTML = items.map((name, i) => {
      const l = labelByName(name);
      return '<li role="option" id="ed-opt-' + i + '" data-i="' + i + '" aria-selected="' + (i === sug.active) + '">'
        + '<span class="chip" style="--lc:' + esc(l ? l.color : '#888888') + '"><span class="chip-dot" aria-hidden="true"></span>'
        + '<span class="chip-text">' + esc(name) + '</span></span></li>';
    }).join('');
    list.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    if (sug.active >= 0) input.setAttribute('aria-activedescendant', 'ed-opt-' + sug.active);
    else input.removeAttribute('aria-activedescendant');
  }

  function hideLabelSuggest() {
    const list = $('#ed-label-suggest');
    if (list) { list.hidden = true; list.innerHTML = ''; }
    const input = $(kq('ed:label-input'));
    if (input) { input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant'); }
    E.labelSug.items = [];
    E.labelSug.active = -1;
    E.labelSug.arrowUsed = false;
  }

  /** A year typed digit by digit passes through 0002, 0020, 0203: not a real date yet. */
  function incompleteDate(date) {
    return !!date && Number(date.slice(0, 4)) < 1000;
  }

  function dueFromInputs() {
    const date = $(kq('ed:date')).value;
    let time = $(kq('ed:time')).value;
    if (!date && !time) return null;
    const d = date || P.ymd(new Date());
    time = time ? time.slice(0, 5) : '';
    return time ? d + 'T' + time : d;
  }

  async function deleteTask() {
    const t = E.task;
    if (!t) return;
    if (!window.confirm('Delete “' + t.title + '”? This cannot be undone.')) return;
    try {
      await api('DELETE', '/api/tasks/' + t.id);
      if (E.pending.id === t.id) E.pending = { id: null, fields: {} };
      closeEditor({ keepFocus: true });
      S.tasks = S.tasks.filter((x) => x.id !== t.id);
      renderList();
      toast('Deleted “' + t.title + '”.');
      refresh();
      const first = $('#list .row-main');
      if (first) first.focus({ preventScroll: true }); else $('#qa').focus({ preventScroll: true });
    } catch (e) { errorToast('Could not delete:', e); }
  }

  function insertAtCaret(ta, text) {
    ta.focus({ preventScroll: true });
    // execCommand keeps the textarea's undo history and fires "input".
    if (!document.execCommand('insertText', false, text)) {
      ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, 'end');
      ta.dispatchEvent(new Event('input', { bubbles: true }));
    }
  }

  async function uploadImages(ta, files) {
    const id = E.task && E.task.id;
    const pos = [ta.selectionStart, ta.selectionEnd];
    const parts = [];
    const el = $('#editor .save-state');
    if (el) { el.className = 'save-state saving'; el.textContent = 'Uploading image…'; }
    for (const f of files) {
      if (f.size > 25 * 1024 * 1024) { toast('“' + f.name + '” is larger than 25 MB.', { kind: 'error' }); continue; }
      try {
        const res = await api('POST', '/api/images', f);
        const alt = (f.name || 'image').replace(/\.[a-z0-9]+$/i, '').replace(/[[\]]/g, '') || 'image';
        parts.push('![' + alt + '](' + res.url + ')');
      } catch (e) { errorToast('Image upload failed:', e); }
    }
    updateSaveState();
    if (!parts.length || !E.task || E.task.id !== id) return;
    const current = $(kq('ed:desc'));
    if (!current) return;
    if (document.activeElement !== current) current.setSelectionRange(Math.min(pos[0], current.value.length), Math.min(pos[1], current.value.length));
    const before = current.value.slice(0, current.selectionStart);
    const sep = before && !before.endsWith('\n') ? '\n' : '';
    insertAtCaret(current, sep + parts.join('\n') + '\n');
  }

  function imageFiles(list) {
    return Array.from(list || []).filter((f) => f.type && f.type.startsWith('image/'));
  }

  function bindEditor() {
    const panel = $('#editor');
    panel.addEventListener('click', (e) => {
      const img = e.target.closest('.md-preview img');
      if (img) { openLightbox(img.getAttribute('src'), img.getAttribute('alt')); return; }
      const el = e.target.closest('[data-act]');
      if (!el || !panel.contains(el)) return;
      const act = el.dataset.act;
      if (act === 'close') closeEditor();
      else if (act === 'nodate') {
        queueField(E.task.id, 'due_at', null, 0);
        renderEditor();
        const d = $(kq('ed:date'));
        if (d) d.focus({ preventScroll: true });
      } else if (act === 'label-x') {
        const name = el.dataset.name.toLowerCase();
        setEditorLabels(currentLabelNames().filter((n) => n.toLowerCase() !== name));
        const input = $(kq('ed:label-input'));
        if (input) input.focus({ preventScroll: true });
      } else if (act === 'focus-labels') {
        if (e.target === el) $(kq('ed:label-input')).focus();
      } else if (act === 'delete') deleteTask();
      else if (act === 'convert' && TT.convertChecklist) TT.convertChecklist();
      else if (TT.editorAction) TT.editorAction(act, el, e);
    });
    panel.addEventListener('input', (e) => {
      const el = e.target;
      const k = el.dataset.k;
      if (!E.task) return;
      if (k === 'ed:title') {
        const ok = el.value.trim().length > 0;
        el.classList.toggle('invalid', !ok);
        if (ok) queueField(E.task.id, 'title', el.value, 600);
      } else if (k === 'ed:desc') {
        queueField(E.task.id, 'description', el.value, 600);
        const prev = $(kq('ed:preview'));
        const html = MD.render(el.value);
        prev.innerHTML = html || 'Nothing written yet.';
        prev.classList.toggle('empty-preview', !html);
        const tools = $('#editor .md-tools');
        const n = TT.convertChecklist ? MD.findChecklist(el.value).length : 0;
        const want = n ? '<button type="button" class="link-btn" data-act="convert" data-k="ed:convert">Turn ' + n
          + (n === 1 ? ' checklist line' : ' checklist lines') + ' into subtasks</button>' : '';
        if (tools && tools.innerHTML !== want) tools.innerHTML = want;
      } else if (k === 'ed:label-input') {
        E.labelSug.arrowUsed = false;
        E.labelSug.active = -1;
        if (/[,\s]$/.test(el.value)) { addEditorLabel(el.value.replace(/[,\s]+$/, '')); return; }
        showLabelSuggest();
      } else if (TT.editorInput) TT.editorInput(e);
    });
    panel.addEventListener('change', (e) => {
      const el = e.target;
      const k = el.dataset.k;
      if (!E.task) return;
      if (k === 'ed:status') queueField(E.task.id, 'status', el.value, 0);
      else if (k === 'ed:priority') queueField(E.task.id, 'priority', el.value, 0);
      else if (k === 'ed:date' || k === 'ed:time') {
        if (!incompleteDate($(kq('ed:date')).value)) queueField(E.task.id, 'due_at', dueFromInputs(), 0);
      }
      else if (TT.editorChange) TT.editorChange(e);
    });
    panel.addEventListener('keydown', (e) => {
      const el = e.target;
      const k = el.dataset.k;
      if (k === 'ed:label-input') {
        const sug = E.labelSug;
        const open = sug.items.length > 0;
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
          if (!open) showLabelSuggest();
          if (!sug.items.length) return;
          e.preventDefault();
          const n = sug.items.length;
          sug.active = e.key === 'ArrowDown' ? (sug.active + 1) % n : (sug.active <= 0 ? n - 1 : sug.active - 1);
          sug.arrowUsed = true;
          showLabelSuggest();
        } else if (e.key === 'Enter') {
          e.preventDefault();
          if (open && sug.arrowUsed && sug.active >= 0) addEditorLabel(sug.items[sug.active]);
          else if (el.value.trim()) addEditorLabel(el.value);
        } else if (e.key === 'Tab' && !e.shiftKey && open && el.value.trim()) {
          e.preventDefault();
          addEditorLabel(sug.items[sug.active >= 0 ? sug.active : 0]);
        } else if (e.key === 'Backspace' && !el.value && el.selectionStart === 0) {
          const names = currentLabelNames();
          if (names.length) { e.preventDefault(); names.pop(); setEditorLabels(names); }
        } else if (e.key === 'Escape') {
          if (open) { e.preventDefault(); e.stopPropagation(); hideLabelSuggest(); }
          else if (el.value) { e.preventDefault(); e.stopPropagation(); el.value = ''; }
        }
        return;
      }
      if (k === 'ed:title' && e.key === 'Enter') {
        e.preventDefault();
        flushFields();
        const next = $(kq('ed:desc'));
        if (next) next.focus();
        return;
      }
      if (TT.editorKeydown) TT.editorKeydown(e);
    });
    panel.addEventListener('focusin', (e) => {
      if (e.target.dataset.k === 'ed:label-input') showLabelSuggest();
    });
    panel.addEventListener('focusout', (e) => {
      const el = e.target;
      if (el.dataset.k === 'ed:label-input') {
        setTimeout(() => {
          if (document.activeElement === el) return;
          hideLabelSuggest();
          if (el.isConnected && el.value.trim()) addEditorLabel(el.value);
        }, 0);
      } else if (el.dataset.k === 'ed:title') {
        if (!el.value.trim() && E.task) { el.value = E.task.title; el.classList.remove('invalid'); }
        flushFields();
      } else if (el.dataset.k === 'ed:desc') flushFields();
      else if (TT.editorFocusout) TT.editorFocusout(e);
    });
    panel.addEventListener('mousedown', (e) => {
      const li = e.target.closest('#ed-label-suggest li[data-i]');
      if (!li) return;
      e.preventDefault();
      addEditorLabel(E.labelSug.items[Number(li.dataset.i)]);
    });
    panel.addEventListener('paste', (e) => {
      if (e.target.dataset.k !== 'ed:desc') return;
      const files = imageFiles(e.clipboardData && e.clipboardData.files);
      if (!files.length) return;
      e.preventDefault();
      uploadImages(e.target, files);
    });
    panel.addEventListener('dragover', (e) => {
      if (e.target.dataset && e.target.dataset.k === 'ed:desc' && e.dataTransfer && Array.from(e.dataTransfer.types).includes('Files')) {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'copy';
      }
    });
    panel.addEventListener('drop', (e) => {
      if (!e.target.dataset || e.target.dataset.k !== 'ed:desc') return;
      const files = imageFiles(e.dataTransfer && e.dataTransfer.files);
      if (!files.length) return;
      e.preventDefault();
      uploadImages(e.target, files);
    });
  }

  // ------------------------------------------------------------- lightbox

  function openLightbox(src, alt) {
    const box = $('#lightbox');
    const img = $('img', box);
    box.dataset.returnK = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.k || '' : '';
    img.src = src;
    img.alt = alt || '';
    box.hidden = false;
    $('.lightbox-close', box).focus();
  }

  function closeLightbox() {
    const box = $('#lightbox');
    if (box.hidden) return false;
    box.hidden = true;
    $('img', box).removeAttribute('src');
    const back = box.dataset.returnK ? $(kq(box.dataset.returnK)) : null;
    if (back) back.focus({ preventScroll: true });
    return true;
  }

  function bindLightbox() {
    const box = $('#lightbox');
    box.addEventListener('click', (e) => { if (e.target === box || e.target.closest('.lightbox-close') || e.target.tagName === 'IMG') closeLightbox(); });
  }

  // ------------------------------------------------------------- polling

  let pollBusy = false;
  let lastHotkey = null;

  async function pollState() {
    if (pollBusy) return;
    pollBusy = true;
    try {
      const st = await api('GET', '/api/state');
      net.failures = 0;
      setOffline(false);
      if (st.build !== BOOT.build || st.api !== BOOT.api) {
        await saveEverything();
        location.reload();
        return;
      }
      if (lastHotkey !== null && st.hotkey !== lastHotkey) focusQuickAdd();
      lastHotkey = st.hotkey;
      checkExternalChanges(st);
    } catch (e) {
      net.failures++;
      if (net.failures >= 2) setOffline(true);
    } finally {
      pollBusy = false;
    }
  }

  function checkExternalChanges(st) {
    if (net.epoch !== st.epoch || net.lastChanges === null) {
      const first = net.epoch === null;
      net.epoch = st.epoch;
      net.lastChanges = st.changes;
      net.own.clear();
      if (!first) { refresh(); reloadEditor(); }
      return;
    }
    if (st.changes === net.lastChanges) return;
    // Own writes still on the wire: their numbers are not known yet.
    if (net.inflight > 0 || (TT.subtaskBusy && TT.subtaskBusy()) || (TT.matrixBusy && TT.matrixBusy())) return;
    let external = false;
    for (let n = net.lastChanges + 1; n <= st.changes; n++) {
      if (!net.own.has(n)) { external = true; break; }
    }
    for (const n of Array.from(net.own)) if (n <= st.changes) net.own.delete(n);
    net.lastChanges = st.changes;
    if (external) {
      refresh();
      reloadEditor();
    }
  }

  function setOffline(off) {
    const banner = $('#banner');
    if (off) {
      banner.textContent = 'TodoTracker is not running. Changes cannot be saved until it is started again.';
      banner.className = 'banner';
      banner.hidden = false;
      banner.dataset.kind = 'offline';
    } else if (banner.dataset.kind === 'offline') {
      banner.hidden = true;
      banner.dataset.kind = '';
      checkVersionBanner();
    }
  }

  function checkVersionBanner() {
    if (BOOT.api >= CLIENT_API) return;
    const banner = $('#banner');
    banner.textContent = 'TodoTracker was updated, but the old version is still running. '
      + 'Start TodoTracker again (Start menu) to switch to the new version; some features are hidden until then.';
    banner.className = 'banner info';
    banner.hidden = false;
    banner.dataset.kind = 'version';
  }

  // ------------------------------------------- leaving the page

  /** Edits that have not reached the server yet. */
  function pendingRequests() {
    const reqs = [];
    // Saves already queued may not finish once the page goes away; PATCH is
    // idempotent, so they are sent again (merged per task, in order).
    const records = E.sending.slice();
    if (E.pending.id !== null && Object.keys(E.pending.fields).length) records.push(E.pending);
    for (const rec of records) {
      const last = reqs[reqs.length - 1];
      if (last && last.url === '/api/tasks/' + rec.id) Object.assign(last.body, rec.fields);
      else reqs.push({ method: 'PATCH', url: '/api/tasks/' + rec.id, body: Object.assign({}, rec.fields) });
    }
    if (TT.pendingSubtaskRequests) reqs.push(...TT.pendingSubtaskRequests());
    return reqs;
  }

  function bodySize(r) { return new Blob([JSON.stringify(r.body)]).size; }

  async function saveEverything() {
    flushFields();
    if (TT.flushSubtasks) TT.flushSubtasks();
    await E.chain;
    if (TT.subtaskChain) await TT.subtaskChain();
  }

  function bindUnload() {
    window.addEventListener('beforeunload', (e) => {
      const reqs = pendingRequests();
      if (!reqs.length) return;
      const size = reqs.reduce((n, r) => n + bodySize(r), 0);
      if (size > KEEPALIVE_BUDGET) {
        // Too big for a keepalive request: save the normal way and ask the
        // user to wait for it.
        saveEverything();
        e.preventDefault();
        e.returnValue = '';
      }
    });
    window.addEventListener('pagehide', () => {
      const reqs = pendingRequests();
      let budget = KEEPALIVE_BUDGET;
      for (const r of reqs) {
        const body = JSON.stringify(r.body);
        const size = new Blob([body]).size;
        if (size > budget) continue;
        budget -= size;
        try {
          fetch(r.url, { method: r.method, keepalive: true, body,
            headers: { 'Content-Type': 'application/json', 'X-Todo': '1' } });
        } catch (e) { /* nothing more we can do */ }
      }
      E.pending = { id: null, fields: {} };
      if (TT.clearPendingSubtasks) TT.clearPendingSubtasks();
    });
  }

  // ------------------------------------------------------------- keys

  function bindGlobalKeys() {
    document.addEventListener('keydown', (e) => {
      const ctrl = e.ctrlKey || e.metaKey;
      if (ctrl && !e.altKey && !e.shiftKey && (e.key === 'f' || e.key === 'F')) {
        e.preventDefault();
        const s = $('#search');
        s.focus();
        s.select();
      } else if (ctrl && !e.altKey && !e.shiftKey && (e.key === 'n' || e.key === 'N')) {
        e.preventDefault();
        focusQuickAdd();
      } else if (e.key === 'Escape' && !e.defaultPrevented) {
        if (closeLightbox()) { e.preventDefault(); return; }
        if (TT.escape && !$('#editor').contains(document.activeElement) && TT.escape(e)) return;
        if (S.openId) { e.preventDefault(); closeEditor(); return; }
        if ($('#search').value) { e.preventDefault(); clearSearch(); return; }
        if ($('#sidebar').classList.contains('open')) toggleMenu(false);
      }
    });
    $('#menu-btn').addEventListener('click', () => toggleMenu());
  }

  function toggleMenu(force) {
    const side = $('#sidebar');
    const open = force == null ? !side.classList.contains('open') : force;
    side.classList.toggle('open', open);
    $('#menu-btn').setAttribute('aria-expanded', String(open));
  }

  // ------------------------------------------------------------- subtasks

  // All subtask writes go through one serialized queue. Changes show at once
  // (optimistically) in the list and the editor; server answers are drawn
  // only when no further subtask write is queued, and refreshes keep the
  // local subtasks of tasks with writes pending. New subtasks get a negative
  // temporary id until the server's id is known.
  const Q = {
    chain: Promise.resolve(),
    pending: 0,
    byTask: new Map(),
    real: new Map(),
    nextTemp: -1,
    failed: false,
    entries: [],          // queued requests (for sending them when the window closes)
    drafts: new Map(),    // subtask id -> {taskId, title} typed but not saved yet
  };
  let allDoneToast = null;

  function realId(id) { return id < 0 ? (Q.real.get(id) || null) : id; }

  function nowStamp() {
    const d = new Date();
    return P.ymd(d) + 'T' + P.pad(d.getHours()) + ':' + P.pad(d.getMinutes()) + ':' + P.pad(d.getSeconds());
  }

  /** Every local copy of a task (list, editor, matrix). */
  function taskCopies(taskId) {
    const out = [];
    const t = taskById(taskId);
    if (t) out.push(t);
    if (E.task && E.task.id === taskId && !out.includes(E.task)) out.push(E.task);
    if (TT.matrixCopies) for (const m of TT.matrixCopies(taskId)) if (!out.includes(m)) out.push(m);
    return out;
  }

  function currentTask(taskId) {
    return (E.task && E.task.id === taskId ? E.task : null) || taskById(taskId)
      || (TT.matrixCopies ? TT.matrixCopies(taskId)[0] : null) || null;
  }

  function recount(t) {
    t.progress = [t.subtasks.filter((s) => s.done).length, t.subtasks.length];
  }

  function mutate(taskId, fn) {
    for (const t of taskCopies(taskId)) {
      t.subtasks = t.subtasks || [];
      fn(t);
      t.subtasks.forEach((s, i) => { s.position = i; });
      recount(t);
    }
  }

  function findSub(taskId, sid) {
    const t = currentTask(taskId);
    return t ? (t.subtasks || []).find((s) => s.id === sid) || null : null;
  }

  function renderSubtaskViews(taskId) {
    renderList();
    if (E.task && E.task.id === taskId) renderEditor();
    if (TT.matrix) TT.matrix.render();
  }

  function subtaskBusy() { return Q.pending > 0 || N.pending.size > 0; }

  function enqueue(taskId, build, opts) {
    opts = opts || {};
    const entry = { build, sent: false, kind: opts.kind || 'write' };
    Q.entries.push(entry);
    Q.pending++;
    Q.byTask.set(taskId, (Q.byTask.get(taskId) || 0) + 1);
    updateSaveState();
    Q.chain = Q.chain.then(async () => {
      entry.sent = true;
      let req = null;
      try { req = build(); } catch (e) { req = null; }
      try {
        if (req) {
          const task = await api(req.method, req.url, req.body);
          if (opts.onSaved) opts.onSaved(task);
          // Draw the server's answer only if no other subtask write follows.
          if (Q.pending === 1) applyServerTask(task);
        }
      } catch (e) {
        Q.failed = true;
        errorToast('Could not save the subtask change:', e);
        if (opts.onFailed) opts.onFailed(e);
      } finally {
        Q.entries.splice(Q.entries.indexOf(entry), 1);
        Q.pending--;
        const n = (Q.byTask.get(taskId) || 1) - 1;
        if (n) Q.byTask.set(taskId, n); else Q.byTask.delete(taskId);
        updateSaveState();
        if (Q.pending === 0) {
          if (Q.failed) {
            Q.failed = false;
            refresh();
            reloadEditor();
          } else scheduleRefresh(300);
        }
      }
    });
    return Q.chain;
  }

  function applyServerTask(task) {
    applyTask(task);
    if (TT.matrix) TT.matrix.applyTask(task);
    renderSubtaskViews(task.id);
  }

  function mapTemps(taskId, temps, ids) {
    temps.forEach((temp, i) => {
      const real = ids[i];
      if (!real) return;
      Q.real.set(temp, real);
      for (const t of taskCopies(taskId)) {
        for (const s of t.subtasks || []) if (s.id === temp) s.id = real;
      }
      if (Q.drafts.has(temp)) { Q.drafts.set(real, Q.drafts.get(temp)); Q.drafts.delete(temp); }
      if (S.subDetails.has(temp)) { S.subDetails.delete(temp); S.subDetails.add(real); }
      if (N.pending.has(temp)) { N.pending.set(real, N.pending.get(temp)); N.pending.delete(temp); }
      // Rename the live elements, so focus and typed text follow the subtask.
      for (const el of $$('[data-k^="s' + temp + ':"], [data-k^="ls' + temp + ':"]')) {
        el.dataset.k = el.dataset.k.replace(/^(l?s)-\d+:/, '$1' + real + ':');
      }
      for (const el of $$('[data-sid="' + temp + '"]')) el.dataset.sid = String(real);
    });
  }

  function subtaskItem(it) {
    const out = { title: it.title };
    for (const key of ['done', 'position', 'created_at', 'completed_at']) {
      if (it[key] !== undefined && it[key] !== null) out[key] = it[key];
    }
    if (TT.subtaskItemExtra) TT.subtaskItemExtra(it, out);
    return out;
  }

  function addSubtasks(taskId, items, opts) {
    opts = opts || {};
    const temps = items.map(() => Q.nextTemp--);
    const stamp = nowStamp();
    mutate(taskId, (t) => {
      items.forEach((it, i) => {
        const sub = Object.assign({ task_id: taskId, done: false, created_at: stamp, completed_at: null, labels: [], notes: '' },
          localSubtaskFields(taskId, it), { id: temps[i] });
        if (sub.done && !sub.completed_at) sub.completed_at = stamp;
        if (it.position != null && it.position < t.subtasks.length) t.subtasks.splice(it.position, 0, sub);
        else t.subtasks.push(sub);
      });
    });
    renderSubtaskViews(taskId);
    return enqueue(taskId, () => ({
      method: 'POST', url: '/api/tasks/' + taskId + '/subtasks', body: { items: items.map(subtaskItem) },
    }), {
      kind: 'add',
      onSaved: (task) => {
        mapTemps(taskId, temps, task.created_ids || []);
        if (opts.onSaved) opts.onSaved(task);
      },
      onFailed: (e) => { if (opts.onFailed) opts.onFailed(e); },
    });
  }

  function updateSubtask(taskId, sid, fields) {
    const stamp = nowStamp();
    mutate(taskId, (t) => {
      const s = t.subtasks.find((x) => x.id === sid);
      if (!s) return;
      if ('done' in fields && fields.done !== s.done) s.completed_at = fields.done ? stamp : null;
      Object.assign(s, localSubtaskFields(taskId, fields));
    });
    renderSubtaskViews(taskId);
    return enqueue(taskId, () => {
      const id = realId(sid);
      return id ? { method: 'PATCH', url: '/api/subtasks/' + id, body: fields } : null;
    });
  }

  function removeSubtask(taskId, sid, opts) {
    opts = opts || {};
    let removed = null;
    let index = -1;
    mutate(taskId, (t) => {
      const i = t.subtasks.findIndex((x) => x.id === sid);
      if (i < 0) return;
      if (!removed || t === E.task) { removed = Object.assign({}, t.subtasks[i]); index = i; }
      t.subtasks.splice(i, 1);
    });
    if (!removed) return;
    Q.drafts.delete(sid);
    if (allDoneToast && allDoneToast.taskId === taskId) checkAllDone(taskId, false);
    renderSubtaskViews(taskId);
    enqueue(taskId, () => {
      const id = realId(sid);
      return id ? { method: 'DELETE', url: '/api/subtasks/' + id } : null;
    });
    if (opts.undo !== false) {
      toast('Removed subtask “' + removed.title + '”.', {
        action: 'Undo',
        onAction: () => {
          const item = Object.assign({}, removed, { position: index });
          delete item.id;
          addSubtasks(taskId, [item]);
        },
      });
    }
  }

  function reorderSubtasks(taskId, ids) {
    mutate(taskId, (t) => {
      const byId = new Map(t.subtasks.map((s) => [s.id, s]));
      const ordered = ids.map((id) => byId.get(id)).filter(Boolean);
      for (const s of t.subtasks) if (!ordered.includes(s)) ordered.push(s);
      t.subtasks = ordered;
    });
    renderSubtaskViews(taskId);
    return enqueue(taskId, () => ({
      method: 'POST', url: '/api/tasks/' + taskId + '/subtasks/order',
      body: { ids: ids.map(realId).filter(Boolean) },
    }));
  }

  function setSubtaskDone(taskId, sid, done) {
    updateSubtask(taskId, sid, { done });
    checkAllDone(taskId, done);
  }

  /** After the last subtask is ticked, offer to complete the task. */
  function checkAllDone(taskId, ticked) {
    const t = currentTask(taskId);
    const subs = t ? t.subtasks || [] : [];
    const all = subs.length > 0 && subs.every((s) => s.done);
    const showing = allDoneToast && allDoneToast.taskId === taskId && allDoneToast.toast.el.isConnected;
    if (all && ticked && t.status !== 'done' && !showing) {
      if (allDoneToast) allDoneToast.toast.dismiss();
      const handle = toast('All subtasks of “' + t.title + '” are done.', {
        action: 'Mark task done',
        timeout: 12000,
        onAction: () => {
          allDoneToast = null;
          const cur = currentTask(taskId);
          const subsNow = cur ? cur.subtasks || [] : [];
          if (cur && cur.status !== 'done' && subsNow.length && subsNow.every((s) => s.done)) {
            setTaskStatus(taskId, 'done', { undo: true });
          }
        },
      });
      allDoneToast = { taskId, toast: handle };
    } else if (!all && showing) {
      allDoneToast.toast.dismiss();
      allDoneToast = null;
    }
  }

  // -- subtasks in the list

  function rowExtraBadges(t, now) {
    const [done, total] = t.progress || [0, 0];
    if (!total || !supports('subtasks')) return '';
    let early = '';
    const sub = supports('subtaskDetails') ? earlierSubtaskDue(t) : null;
    if (sub) early = dueBadgeHTML(sub, now, '↳ ⏱').replace('title="Target: ', 'title="A subtask is due ');
    const open = S.unfolded.has(t.id);
    const pct = Math.round((done / total) * 100);
    return '<button type="button" class="badge sub-badge" data-act="unfold" data-k="t' + t.id + ':unfold" aria-expanded="' + open + '"'
      + ' aria-label="Subtasks ' + done + ' of ' + total + ' done; ' + (open ? 'hide' : 'show') + ' them"'
      + ' title="' + (open ? 'Hide' : 'Show') + ' subtasks">'
      + '<span class="mini-bar" aria-hidden="true"><span style="width:' + pct + '%"></span></span>'
      + done + '/' + total + '<span aria-hidden="true">' + (open ? '▲' : '▼') + '</span></button>' + early;
  }

  function miniSubHTML(t, s, now) {
    const k = 'ls' + s.id;
    return '<li class="mini-sub' + (s.done ? ' done' : '') + '" data-sid="' + s.id + '">'
      + '<input type="checkbox" id="mini-' + s.id + '" data-act="mini-check" data-sid="' + s.id + '" data-k="' + k + ':check"' + (s.done ? ' checked' : '') + '>'
      + '<label for="mini-' + s.id + '">' + esc(s.title) + '</label>'
      + (TT.subtaskMetaHTML ? TT.subtaskMetaHTML(t, s, now, 'list') : '')
      + '</li>';
  }

  function rowExtra(t, now) {
    const subs = t.subtasks || [];
    if (!subs.length || !supports('subtasks')) return '';
    let shown;
    if (S.unfolded.has(t.id)) shown = subs;
    else if (t.match_subs && t.match_subs.length) shown = subs.filter((s) => t.match_subs.includes(s.id));
    else return '';
    if (!shown.length) return '';
    return '<ul class="row-subs" aria-label="Subtasks of ' + esc(t.title) + '">'
      + shown.map((s) => miniSubHTML(t, s, now)).join('') + '</ul>';
  }

  function listAction(act, el, id) {
    if (act === 'unfold') {
      if (S.unfolded.has(id)) S.unfolded.delete(id); else S.unfolded.add(id);
      renderList();
    } else if (TT.listActionExtra) TT.listActionExtra(act, el, id);
  }

  function listChange(e) {
    const el = e.target;
    if (el.dataset.act !== 'mini-check') return;
    const taskId = Number(el.closest('.row').dataset.id);
    setSubtaskDone(taskId, Number(el.dataset.sid), el.checked);
  }

  function listKeydown(e) {
    const el = e.target;
    if (el.dataset.act === 'mini-check' && e.key === 'Enter') {
      e.preventDefault();
      el.click();
      return true;
    }
    return false;
  }

  // -- subtasks in the editor

  function editorSubtasksHTML(t) {
    if (!supports('subtasks')) return '';
    const subs = t.subtasks || [];
    const [done, total] = t.progress || [0, 0];
    const now = new Date();
    const pct = total ? Math.round((done / total) * 100) : 0;
    return '<section class="ed-subtasks" aria-labelledby="ed-subs-title">'
      + '<div class="ed-section-title"><span id="ed-subs-title">Subtasks</span>'
      + (total ? '<span class="sub-count">' + done + ' of ' + total + ' done</span>' : '') + '</div>'
      + (total ? '<div class="progress" role="progressbar" aria-label="Subtasks done" aria-valuemin="0" aria-valuemax="' + total
        + '" aria-valuenow="' + done + '"><span style="width:' + pct + '%"></span></div>' : '')
      + '<ul class="subtasks" data-task="' + t.id + '">' + subs.map((s) => subtaskRowHTML(t, s, now)).join('') + '</ul>'
      + '<input type="text" class="sub-add" data-k="sub:add" maxlength="500" autocomplete="off"'
      + ' placeholder="Add a subtask (paste several lines to add several)" aria-label="Add a subtask">'
      + '</section>';
  }

  function subtaskRowHTML(t, s, now) {
    const k = 's' + s.id;
    return '<li class="sub' + (s.done ? ' done' : '') + '" data-sid="' + s.id + '">'
      + '<div class="sub-line">'
      + '<span class="grip" data-act="sub-grip" title="Drag to reorder (or Alt+↑/↓)" aria-hidden="true">⋮⋮</span>'
      + '<input type="checkbox" class="sub-check" data-act="sub-check" data-k="' + k + ':check"' + (s.done ? ' checked' : '')
      + ' aria-label="Done: ' + esc(s.title) + '">'
      + '<input type="text" class="sub-title" data-k="' + k + ':title" value="' + esc(s.title) + '" maxlength="500"'
      + ' autocomplete="off" aria-label="Subtask title">'
      + (TT.subtaskButtonsHTML ? TT.subtaskButtonsHTML(t, s) : '')
      + '<button type="button" class="icon-btn sub-del" data-act="sub-del" data-k="' + k + ':del"'
      + ' aria-label="Remove subtask ' + esc(s.title) + '" title="Remove (Undo is offered)">✕</button>'
      + '</div>'
      + (TT.subtaskMetaHTML ? TT.subtaskMetaHTML(t, s, now, 'editor') : '')
      + (TT.subtaskDetailsHTML ? TT.subtaskDetailsHTML(t, s) : '')
      + '</li>';
  }

  function subIdFromKey(k) {
    const m = /^s(-?\d+):/.exec(k || '');
    return m ? Number(m[1]) : null;
  }

  function titleInputs() { return $$('#editor .sub-title'); }

  function focusTitle(el, atEnd) {
    if (!el) return;
    el.focus({ preventScroll: false });
    if (atEnd !== false) {
      const n = el.value.length;
      try { el.setSelectionRange(n, n); } catch (e) { /* ignore */ }
    }
  }

  function commitTitle(el) {
    if (!E.task) return;
    const sid = subIdFromKey(el.dataset.k);
    const s = findSub(E.task.id, sid);
    if (!s) return;
    Q.drafts.delete(sid);
    const parsed = TT.parseSubtaskInput ? TT.parseSubtaskInput(el.value, E.task) : { title: el.value.replace(/\s+/g, ' ').trim() };
    if (!parsed.title) {
      el.value = s.title;
      return;
    }
    const fields = {};
    if (parsed.title !== s.title) fields.title = parsed.title;
    if (TT.subtaskTokenFields) Object.assign(fields, TT.subtaskTokenFields(parsed, s));
    if (parsed.title !== el.value) el.value = parsed.title;
    el.defaultValue = parsed.title;
    if (Object.keys(fields).length) updateSubtask(E.task.id, sid, fields);
  }

  function addFromBox(text) {
    const t = E.task;
    if (!t) return;
    const parsed = TT.parseSubtaskInput ? TT.parseSubtaskInput(text, t) : { title: text.replace(/\s+/g, ' ').trim() };
    if (!parsed.title) return;
    const item = { title: parsed.title };
    if (TT.subtaskTokenFields) Object.assign(item, TT.subtaskTokenFields(parsed, null));
    addSubtasks(t.id, [item]);
  }

  function moveSubtask(sid, delta) {
    const t = E.task;
    const ids = t.subtasks.map((s) => s.id);
    const i = ids.indexOf(sid);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= ids.length) return;
    ids.splice(i, 1);
    ids.splice(j, 0, sid);
    reorderSubtasks(t.id, ids);
  }

  function editorKeydown(e) {
    const el = e.target;
    const k = el.dataset.k || '';
    const plain = !e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey;
    if (k === 'sub:add') {
      if (e.key === 'Enter' && !e.isComposing && !e.altKey) {
        e.preventDefault();
        if (el.value.trim()) {
          addFromBox(el.value);
          el.value = '';
          el.defaultValue = '';
        }
      } else if (e.key === 'Escape' && el.value) {
        // First Esc clears the draft; the next one closes the panel.
        e.preventDefault();
        e.stopPropagation();
        el.value = '';
      } else if (e.key === 'ArrowUp' && plain) {
        const titles = titleInputs();
        if (titles.length) { e.preventDefault(); focusTitle(titles[titles.length - 1]); }
      }
      return;
    }
    const sid = subIdFromKey(k);
    if (sid === null || !E.task) return;
    if (k.endsWith(':title')) {
      const titles = titleInputs();
      const i = titles.indexOf(el);
      if (e.key === 'Enter' && plain && !e.isComposing) {
        e.preventDefault();
        commitTitle(el);
        const next = titleInputs()[i + 1];
        if (next) focusTitle(next); else $(kq('sub:add')).focus();
      } else if (e.key === 'Enter' && e.altKey && TT.openSubtaskDetails) {
        e.preventDefault();
        commitTitle(el);
        TT.openSubtaskDetails(sid);
      } else if (e.key === 'Escape') {
        const s = findSub(E.task.id, sid);
        if (s && el.value !== s.title) {
          e.preventDefault();
          e.stopPropagation();
          el.value = s.title;
          el.defaultValue = s.title;
          Q.drafts.delete(sid);
        }
      } else if (e.key === 'Backspace' && plain && !el.value && !e.repeat) {
        // Deleting with Backspace on an empty title, but never on key repeat
        // (holding Backspace must not eat the subtasks above).
        e.preventDefault();
        const prev = titles[i - 1];
        const next = titles[i + 1];
        removeSubtask(E.task.id, sid);
        const target = prev ? $(kq(prev.dataset.k)) : next ? $(kq(next.dataset.k)) : $(kq('sub:add'));
        focusTitle(target);
      } else if ((e.key === 'ArrowUp' || e.key === 'ArrowDown') && plain) {
        e.preventDefault();
        const target = e.key === 'ArrowUp' ? titles[i - 1] : (titles[i + 1] || $(kq('sub:add')));
        if (target) focusTitle(target);
      } else if ((e.key === 'ArrowUp' || e.key === 'ArrowDown') && e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
        e.preventDefault();
        commitTitle(el);
        moveSubtask(sid, e.key === 'ArrowUp' ? -1 : 1);
      }
      return;
    }
    if (k.endsWith(':check') && e.key === 'Enter') {
      e.preventDefault();
      el.click();
    } else if (TT.subtaskKeydownExtra) TT.subtaskKeydownExtra(e, sid);
  }

  function editorAction(act, el, e) {
    const li = el.closest('li.sub');
    const sid = li ? Number(li.dataset.sid) : null;
    if (act === 'sub-del' && sid !== null) {
      const titles = titleInputs();
      const i = titles.findIndex((x) => subIdFromKey(x.dataset.k) === sid);
      removeSubtask(E.task.id, sid);
      const left = titleInputs();
      const target = left[Math.min(i, left.length - 1)];
      if (target) focusTitle(target); else $(kq('sub:add')).focus({ preventScroll: true });
    } else if (TT.subtaskActionExtra) TT.subtaskActionExtra(act, el, sid, e);
  }

  function editorChange(e) {
    const el = e.target;
    if (el.dataset.act === 'sub-check') {
      setSubtaskDone(E.task.id, subIdFromKey(el.dataset.k), el.checked);
    } else if (TT.subtaskChangeExtra) TT.subtaskChangeExtra(e);
  }

  function editorInput(e) {
    const el = e.target;
    const k = el.dataset.k || '';
    if (k.endsWith(':title') && k.startsWith('s')) {
      Q.drafts.set(subIdFromKey(k), { taskId: E.task.id, title: el.value });
    } else if (TT.subtaskInputExtra) TT.subtaskInputExtra(e);
  }

  function editorFocusout(e) {
    const el = e.target;
    const k = el.dataset.k || '';
    if (k.startsWith('s') && k.endsWith(':title') && el.isConnected) commitTitle(el);
    else if (TT.subtaskFocusoutExtra) TT.subtaskFocusoutExtra(e);
  }

  function editorPaste(e) {
    const el = e.target;
    if (el.dataset.k !== 'sub:add' || !E.task) return;
    const text = e.clipboardData ? e.clipboardData.getData('text/plain') : '';
    if (!/[\r\n\u2028\u2029]/.test(text)) return;
    const before = el.value.slice(0, el.selectionStart);
    const after = el.value.slice(el.selectionEnd);
    const items = P.splitPastedLines(before + text + after);
    if (items.length < 2) return;
    e.preventDefault();
    const t = E.task;
    addSubtasks(t.id, items.map((it) => {
      const parsed = TT.parseSubtaskInput ? TT.parseSubtaskInput(it.title, t) : { title: it.title };
      const item = { title: parsed.title || it.title, done: it.done };
      if (TT.subtaskTokenFields) Object.assign(item, TT.subtaskTokenFields(parsed, null));
      return item;
    }));
    el.value = '';
  }

  function startSubtaskDrag(e, grip) {
    if (e.button !== 0) return;
    const li = grip.closest('li.sub');
    const ul = li.parentNode;
    const taskId = Number(ul.dataset.task);
    const start = Array.from(ul.children).map((x) => Number(x.dataset.sid));
    e.preventDefault();
    beginDrag();
    li.classList.add('dragging');
    ul.classList.add('reordering');
    // Capture on the list: moving the row would detach the grip and drop the capture.
    try { ul.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
    const onMove = (ev) => {
      let before = null;
      for (const sib of Array.from(ul.children)) {
        if (sib === li) continue;
        const r = sib.getBoundingClientRect();
        if (ev.clientY < r.top + r.height / 2) { before = sib; break; }
      }
      if (before !== li.nextSibling && before !== li) ul.insertBefore(li, before);
    };
    let finished = false;
    const finish = (commit) => {
      if (finished) return;
      finished = true;
      window.removeEventListener('pointermove', onMove, true);
      window.removeEventListener('pointerup', onUp, true);
      window.removeEventListener('pointercancel', onCancel, true);
      window.removeEventListener('blur', onCancel);
      document.removeEventListener('keydown', onKey, true);
      try { ul.releasePointerCapture(e.pointerId); } catch (err) { /* ignore */ }
      li.classList.remove('dragging');
      ul.classList.remove('reordering');
      const order = Array.from(ul.children).map((x) => Number(x.dataset.sid));
      endDrag();
      if (commit && order.join() !== start.join()) reorderSubtasks(taskId, order);
      else renderEditor();
    };
    const onUp = () => finish(true);
    const onCancel = () => finish(false);
    const onKey = (ev) => {
      if (ev.key !== 'Escape') return;
      ev.preventDefault();
      ev.stopPropagation();
      for (const id of start) {
        const node = ul.querySelector('[data-sid="' + id + '"]');
        if (node) ul.appendChild(node);
      }
      finish(false);
    };
    window.addEventListener('pointermove', onMove, true);
    window.addEventListener('pointerup', onUp, true);
    window.addEventListener('pointercancel', onCancel, true);
    window.addEventListener('blur', onCancel);
    document.addEventListener('keydown', onKey, true);
  }

  function bindSubtasks() {
    const panel = $('#editor');
    panel.addEventListener('pointerdown', (e) => {
      const grip = e.target.closest('.grip');
      if (grip && panel.contains(grip)) startSubtaskDrag(e, grip);
    });
    panel.addEventListener('paste', editorPaste);
  }

  // -- checklist conversion

  async function convertChecklist() {
    const t = E.task;
    if (!t) return;
    const ta = $(kq('ed:desc'));
    const desc = ta ? ta.value : (t.description || '');
    const items = MD.findChecklist(desc);
    if (!items.length) return;
    flushFields();
    // Create the subtasks first; remove exactly those lines only after that worked.
    addSubtasks(t.id, items.map((i) => ({ title: i.title, done: i.done })), {
      onSaved: async () => {
        let base;
        if (E.task && E.task.id === t.id) {
          const cur = $(kq('ed:desc'));
          base = cur ? cur.value : editorValues(E.task).description;
        } else {
          try { base = (await api('GET', '/api/tasks/' + t.id)).description; } catch (e) { return; }
        }
        const next = MD.removeLines(base, items);
        if (next !== base) {
          queueField(t.id, 'description', next, 0);
          if (E.task && E.task.id === t.id) {
            const cur = $(kq('ed:desc'));
            if (cur) { cur.value = next; cur.defaultValue = next; }
            renderEditor();
          }
        }
        toast('Turned ' + items.length + (items.length === 1 ? ' checklist line' : ' checklist lines') + ' into subtasks.');
      },
      onFailed: () => toast('The checklist was left in the description.', { kind: 'error' }),
    });
  }

  // -- keeping local state, leaving the page

  function mergeLocalSubtasks(tasks) {
    for (const t of tasks) {
      if (!Q.byTask.has(t.id)) continue;
      const local = currentTask(t.id);
      if (local && local.subtasks) {
        t.subtasks = local.subtasks;
        recount(t);
      }
    }
    return tasks;
  }

  function mergeEditorLocal(task) {
    if (Q.byTask.has(task.id) && E.task && E.task.id === task.id && E.task.subtasks) {
      task.subtasks = E.task.subtasks;
      recount(task);
    }
    return task;
  }

  function pendingSubtaskRequests() {
    const reqs = [];
    for (const entry of Q.entries) {
      if (entry.sent && entry.kind === 'add') continue;   // a second add would duplicate
      let req = null;
      try { req = entry.build(); } catch (e) { req = null; }
      if (req) reqs.push({ method: req.method, url: req.url, body: req.body || {} });
    }
    for (const [sid, draft] of Q.drafts) {
      const s = findSub(draft.taskId, sid);
      const title = draft.title.replace(/\s+/g, ' ').trim();
      const id = realId(sid);
      if (id && s && title && title !== s.title) reqs.push({ method: 'PATCH', url: '/api/subtasks/' + id, body: { title } });
    }
    if (TT.pendingSubtaskExtra) reqs.push(...TT.pendingSubtaskExtra());
    return reqs;
  }

  function clearPendingSubtasks() {
    for (const entry of Q.entries) entry.sent = true;
    Q.entries.length = 0;
    Q.drafts.clear();
    if (TT.clearSubtaskExtra) TT.clearSubtaskExtra();
  }

  function flushSubtasks() {
    const el = document.activeElement;
    if (el && el.dataset && /^s-?\d+:title$/.test(el.dataset.k || '')) commitTitle(el);
    if (TT.flushSubtaskExtra) TT.flushSubtaskExtra();
  }

  function beforeEditorSwitch() {
    flushSubtasks();
    S.subDetails.clear();
  }

  // ------------------------------------------------- subtask details

  // Each subtask can have a target date (with optional time), a subset of
  // its task's labels and free-text notes. Notes autosave after a 700 ms
  // pause, when the box is left, on Esc, when the editor switches or closes
  // and when the window closes.
  const NOTES_DELAY = 700;
  const N = { pending: new Map() };   // subtask id -> {taskId, value, timer}
  S.subDetails = new Set();

  function firstLine(text) {
    for (const line of String(text || '').split('\n')) {
      const s = line.trim();
      if (s) return s.length > 120 ? s.slice(0, 119) + '…' : s;
    }
    return '';
  }

  function localSubtaskFields(taskId, fields) {
    const out = Object.assign({}, fields);
    if (fields.labels) {
      const t = currentTask(taskId);
      const byName = new Map(((t && t.labels) || []).map((l) => [l.name.toLowerCase(), l]));
      out.labels = fields.labels.map((n) => (typeof n === 'string' ? byName.get(n.toLowerCase()) : n)).filter(Boolean);
    }
    if ('notes' in fields) {
      out.note1 = firstLine(fields.notes);
      out.has_notes = !!String(fields.notes || '').trim();
    }
    return out;
  }

  function subtaskItemExtra(it, out) {
    if (it.due_at) out.due_at = it.due_at;
    if (it.labels && it.labels.length) out.labels = it.labels.map((l) => (typeof l === 'string' ? l : l.name));
    if (it.notes) out.notes = it.notes;
  }

  function parseSubtaskInput(text, task) {
    return P.parseSubtaskTitle(text, ((task && task.labels) || []).map((l) => l.name), new Date());
  }

  function subtaskTokenFields(parsed, s) {
    const fields = {};
    if (!supports('subtaskDetails')) return fields;
    if (parsed.labels && parsed.labels.length) {
      const names = ((s && s.labels) || []).map((l) => l.name);
      for (const n of parsed.labels) if (!names.some((x) => x.toLowerCase() === n.toLowerCase())) names.push(n);
      fields.labels = names;
    }
    if (parsed.due) fields.due_at = parsed.due;
    return fields;
  }

  function subMetaHTML(t, s, now, where) {
    if (!supports('subtaskDetails')) return '';
    const parts = [];
    const act = where === 'list' ? 'mini-meta' : 'sub-meta';
    const k = (where === 'list' ? 'ls' : 's') + s.id;
    if (s.due_at) {
      const st = s.done ? 'future' : P.dueState(s.due_at, now);
      parts.push('<button type="button" class="badge due ' + st + '" data-act="' + act + '" data-part="date" data-sid="' + s.id + '" data-k="' + k + ':meta-date"'
        + ' title="Target ' + esc(P.dueLong(s.due_at)) + (st === 'overdue' ? ' (overdue)' : '') + '">⏱ ' + esc(P.dueLabel(s.due_at, now)) + '</button>');
    }
    if (s.labels && s.labels.length) {
      parts.push('<button type="button" class="meta-labels" data-act="' + act + '" data-part="labels" data-sid="' + s.id + '" data-k="' + k + ':meta-labels"'
        + ' aria-label="Labels: ' + esc(s.labels.map((l) => l.name).join(', ')) + '">'
        + s.labels.map((l) => chipHTML(l)).join('') + '</button>');
    }
    const note = s.note1 !== undefined ? s.note1 : firstLine(s.notes);
    if (note) {
      parts.push('<button type="button" class="note1" data-act="' + act + '" data-part="notes" data-sid="' + s.id + '" data-k="' + k + ':meta-notes"'
        + ' title="Notes">📝 ' + esc(note) + '</button>');
    }
    return parts.length ? '<div class="sub-meta">' + parts.join('') + '</div>' : '';
  }

  function subButtonsHTML(t, s) {
    if (!supports('subtaskDetails')) return '';
    const open = S.subDetails.has(s.id);
    return '<button type="button" class="icon-btn sub-more" data-act="sub-more" data-k="s' + s.id + ':more" aria-expanded="' + open + '"'
      + ' aria-label="Details of ' + esc(s.title) + ' (Alt+Enter)" title="Date, labels, notes (Alt+Enter)">⋯</button>';
  }

  function subDetailsHTML(t, s) {
    if (!supports('subtaskDetails') || !S.subDetails.has(s.id)) return '';
    const k = 's' + s.id;
    const due = s.due_at || '';
    const notes = s.notes !== undefined ? s.notes : '';
    const pending = N.pending.get(s.id);
    const labels = (t.labels || []).length
      ? t.labels.map((l) => {
        const on = (s.labels || []).some((x) => x.id === l.id || x.name.toLowerCase() === l.name.toLowerCase());
        return '<button type="button" class="chip toggle' + (on ? ' on' : '') + '" style="--lc:' + esc(l.color) + '" data-act="sub-label"'
          + ' data-name="' + esc(l.name) + '" data-k="' + k + ':lbl:' + esc(l.name.toLowerCase()) + '" aria-pressed="' + on + '">'
          + '<span class="chip-dot" aria-hidden="true"></span><span class="chip-text">' + esc(l.name) + '</span></button>';
      }).join('')
      : '<span class="hint">The task has no labels yet; a subtask can only use its task’s labels.</span>';
    return '<div class="sub-details" role="group" aria-label="Details of ' + esc(s.title) + '">'
      + '<div class="sd-row"><label class="ed-label" for="sd-date-' + s.id + '">Target</label>'
      + '<input type="date" id="sd-date-' + s.id + '" data-k="' + k + ':date" value="' + esc(due.slice(0, 10)) + '">'
      + '<input type="time" data-k="' + k + ':time" value="' + esc(due.length > 10 ? due.slice(11, 16) : '') + '" aria-label="Target time (optional)">'
      + (due ? '<button type="button" class="icon-btn" data-act="sub-nodate" data-k="' + k + ':nodate" aria-label="No target date" title="No target date">✕</button>' : '')
      + '</div>'
      + '<div class="sd-row"><span class="ed-label" id="sd-lbl-' + s.id + '">Labels</span>'
      + '<div class="sd-labels" role="group" aria-labelledby="sd-lbl-' + s.id + '">' + labels + '</div></div>'
      + '<label class="ed-label" for="sd-notes-' + s.id + '">Notes</label>'
      + '<textarea id="sd-notes-' + s.id + '" class="sd-notes" data-k="' + k + ':notes" rows="4" maxlength="20000" spellcheck="true">\n'
      + esc(pending ? pending.value : notes) + '</textarea>'
      + '</div>';
  }

  function openSubtaskDetails(sid, part) {
    if (!E.task || !supports('subtaskDetails')) return;
    S.subDetails.add(sid);
    renderEditor();
    const k = 's' + sid + ':' + (part === 'labels' ? 'lbl:' : part === 'notes' ? 'notes' : 'date');
    const target = part === 'labels' ? $('#editor [data-k^="' + k + '"]') || $(kq('s' + sid + ':date')) : $(kq(k));
    if (target) {
      target.focus({ preventScroll: false });
      target.scrollIntoView({ block: 'nearest' });
    }
  }

  function closeSubtaskDetails(sid) {
    flushNotes(sid);
    S.subDetails.delete(sid);
    renderEditor();
    const title = $(kq('s' + sid + ':title'));
    if (title) title.focus({ preventScroll: true });
  }

  function subDueFromInputs(sid) {
    const date = $(kq('s' + sid + ':date')).value;
    const time = $(kq('s' + sid + ':time')).value;
    if (!date && !time) return null;
    const d = date || P.ymd(new Date());
    return time ? d + 'T' + time.slice(0, 5) : d;
  }

  function queueNotes(taskId, sid, value) {
    const prev = N.pending.get(sid);
    if (prev) clearTimeout(prev.timer);
    N.pending.set(sid, { taskId, value, timer: setTimeout(() => flushNotes(sid), NOTES_DELAY) });
    updateSaveState();
  }

  function flushNotes(sid) {
    const p = N.pending.get(sid);
    if (!p) return;
    clearTimeout(p.timer);
    N.pending.delete(sid);
    const s = findSub(p.taskId, sid);
    const value = p.value.replace(/\s+$/, '');
    if (s && value !== (s.notes || '')) updateSubtask(p.taskId, sid, { notes: value });
    else updateSaveState();
  }

  function flushAllNotes() {
    for (const sid of Array.from(N.pending.keys())) flushNotes(sid);
  }

  function subtaskActionExtra(act, el, sid) {
    if (!E.task) return;
    if (act === 'sub-more') {
      if (S.subDetails.has(sid)) closeSubtaskDetails(sid);
      else openSubtaskDetails(sid, 'date');
    } else if (act === 'sub-meta') openSubtaskDetails(Number(el.dataset.sid), el.dataset.part);
    else if (act === 'sub-nodate') {
      updateSubtask(E.task.id, sid, { due_at: null });
      const d = $(kq('s' + sid + ':date'));
      if (d) d.focus({ preventScroll: true });
    } else if (act === 'sub-label') {
      const s = findSub(E.task.id, sid);
      if (!s) return;
      const name = el.dataset.name;
      const names = (s.labels || []).map((l) => l.name);
      const has = names.some((n) => n.toLowerCase() === name.toLowerCase());
      updateSubtask(E.task.id, sid, { labels: has ? names.filter((n) => n.toLowerCase() !== name.toLowerCase()) : names.concat([name]) });
    }
  }

  function subtaskChangeExtra(e) {
    const k = e.target.dataset.k || '';
    const m = /^s(-?\d+):(date|time)$/.exec(k);
    if (m && E.task && !incompleteDate($(kq('s' + m[1] + ':date')).value)) {
      updateSubtask(E.task.id, Number(m[1]), { due_at: subDueFromInputs(Number(m[1])) });
    }
  }

  function subtaskInputExtra(e) {
    const m = /^s(-?\d+):notes$/.exec(e.target.dataset.k || '');
    if (m && E.task) queueNotes(E.task.id, Number(m[1]), e.target.value);
  }

  function subtaskFocusoutExtra(e) {
    const m = /^s(-?\d+):notes$/.exec(e.target.dataset.k || '');
    if (m) flushNotes(Number(m[1]));
  }

  function subtaskKeydownExtra(e, sid) {
    const k = e.target.dataset.k || '';
    if (e.key === 'Escape' && /^s-?\d+:(notes|date|time|nodate|lbl:.*)$/.test(k)) {
      // Esc saves the notes and closes the details, back to the title.
      e.preventDefault();
      e.stopPropagation();
      closeSubtaskDetails(sid);
    } else if (e.key === 'Enter' && /:lbl:/.test(k)) {
      // Buttons toggle natively on Enter; nothing else to do.
    }
  }

  function listActionExtra(act, el, id) {
    if (act !== 'mini-meta') return;
    const sid = Number(el.dataset.sid);
    const part = el.dataset.part;
    openEditor(id, null, { details: sid, focusK: 's' + sid + ':' + (part === 'notes' ? 'notes' : 'date') });
  }

  function pendingNotesRequests() {
    const reqs = [];
    for (const [sid, p] of N.pending) {
      const id = realId(sid);
      if (id) reqs.push({ method: 'PATCH', url: '/api/subtasks/' + id, body: { notes: p.value.replace(/\s+$/, '') } });
    }
    return reqs;
  }

  function clearNotes() {
    for (const p of N.pending.values()) clearTimeout(p.timer);
    N.pending.clear();
  }

  /** Earliest open subtask date, when it comes before the task's own. */
  function earlierSubtaskDue(t) {
    if (t.status === 'done') return null;
    let best = null;
    for (const s of t.subtasks || []) {
      if (!s.done && s.due_at && (!best || s.due_at < best)) best = s.due_at;
    }
    return best && (!t.due_at || best < t.due_at) ? best : null;
  }

  // ------------------------------------------------------------- boot

  const TT = {
    // shared with matrix.js and the subtask code
    $, $$, esc, kq, api, toast, errorToast, schedule, renderInto, beginDrag, endDrag, held,
    S, E, P, MD, net, BOOT, FEATURE_API, supports, labelById, labelByName, labelNames, taskById,
    refresh, scheduleRefresh, applyTask, renderList, renderSidebar, renderEditor, openEditor,
    closeEditor, reloadEditor, queueField, flushFields, updateSaveState, setTaskStatus, chipHTML,
    dueBadgeHTML, updateHeader, focusQuickAdd, insertAtCaret, on, emit, load, save, editorValues,
    // subtasks
    Q, realId, taskCopies, currentTask, mutate, findSub, addSubtasks, updateSubtask, removeSubtask,
    reorderSubtasks, setSubtaskDone, renderSubtaskViews, subtaskBusy, nowStamp,
    rowExtraBadges, rowExtra, listAction, listChange, listKeydown, editorSubtasksHTML, editorKeydown,
    editorAction, editorChange, editorInput, editorFocusout, convertChecklist, beforeEditorSwitch,
    mergeLocal: mergeLocalSubtasks, mergeEditorLocal, pendingSubtaskRequests, clearPendingSubtasks,
    flushSubtasks, subtaskChain: () => Q.chain,
    // subtask details
    subtaskMetaHTML: subMetaHTML, subtaskButtonsHTML: subButtonsHTML, subtaskDetailsHTML: subDetailsHTML,
    openSubtaskDetails, parseSubtaskInput, subtaskTokenFields, subtaskItemExtra, subtaskActionExtra,
    subtaskChangeExtra, subtaskInputExtra, subtaskFocusoutExtra, subtaskKeydownExtra, listActionExtra,
    pendingSubtaskExtra: pendingNotesRequests, clearSubtaskExtra: clearNotes, flushSubtaskExtra: flushAllNotes,
    earlierSubtaskDue, firstLine,
    showPage(page) {
      S.page = page === 'matrix' && TT.matrix ? 'matrix' : 'list';
      $('#list-page').hidden = S.page !== 'list';
      $('#matrix-page').hidden = S.page !== 'matrix';
      ROOT.dataset.page = S.page;
      if (location.hash !== (S.page === 'matrix' ? '#matrix' : '')) {
        history.replaceState(null, '', S.page === 'matrix' ? '#matrix' : location.pathname);
      }
      toggleMenu(false);
      if (TT.matrix) TT.matrix.shown(S.page === 'matrix');
      renderSidebar();
      updateHeader();
      refresh();
    },
  };
  window.TT = TT;

  function boot() {
    bindPointerHold();
    bindSidebar();
    bindQuickAdd();
    bindSearch();
    bindList();
    bindEditor();
    bindLightbox();
    bindSubtasks();
    bindGlobalKeys();
    bindUnload();
    $('#sort').value = S.sort;
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { applyTheme(); renderSidebar(); });
    window.addEventListener('storage', (e) => {
      if (e.key === 'tt-theme') { applyTheme(); renderSidebar(); }
    });
    window.addEventListener('focus', pollState);
    window.addEventListener('hashchange', () => TT.showPage(location.hash === '#matrix' ? 'matrix' : 'list'));
    checkVersionBanner();
    renderSidebar();
    renderList();
    TT.showPage(location.hash === '#matrix' ? 'matrix' : 'list');
    setInterval(pollState, 1500);
    pollState();
    focusQuickAdd();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => setTimeout(boot, 0));
  else setTimeout(boot, 0);
})();
