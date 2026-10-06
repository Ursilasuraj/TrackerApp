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
  const CLIENT_API = 1;            // the server API this page is written for
  const FEATURE_API = {};          // feature -> minimum server API
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

  function isTextField(el) {
    return !!el && (el.tagName === 'TEXTAREA' || (el.tagName === 'INPUT' && TEXT_INPUT.test(el.type)));
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
    if (snap && snap.k && isTextField(snap.el)) kept = rebuildAround(container, html, snap.el);
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
  function mergeLocal(tasks) { return TT.mergeLocal ? TT.mergeLocal(tasks) : tasks; }

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
      + '<div class="row-main" data-act="open" data-k="' + k + ':open" tabindex="0" role="button"'
      + ' aria-label="' + esc(t.title) + (t.status === 'in_progress' ? ', in progress' : '') + (t.due_at ? ', due ' + esc(P.dueLong(t.due_at)) : '') + '">'
      + '<div class="row-title">' + esc(t.title) + '</div>'
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
      const target = rows[idx].querySelector('[data-k$=":' + part + '"]') || rows[idx].querySelector('.row-main');
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
    list.addEventListener('keydown', (e) => {
      const el = e.target;
      if (TT.listKeydown && TT.listKeydown(e)) return;
      if (el.dataset.act === 'open' && (e.key === 'Enter' || e.key === ' ')) {
        e.preventDefault();
        openEditor(Number(el.closest('.row').dataset.id));
      } else if ((el.dataset.act === 'open' || el.dataset.act === 'check') && (e.key === 'ArrowDown' || e.key === 'ArrowUp')
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
    inflight: null,                       // {id, fields} of the save on the wire
    timer: 0,
    chain: Promise.resolve(),
    saving: 0,
    saveError: false,
    labelSug: { items: [], active: -1, arrowUsed: false },
    loadSeq: 0,
  };

  function editorValues(t) {
    const v = Object.assign({}, t);
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
    const startFocus = document.activeElement;
    if (S.openId !== id) {
      flushFields();
      if (TT.beforeEditorSwitch) TT.beforeEditorSwitch();
    }
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
      const now = document.activeElement;
      if (target && (now === startFocus || now === document.body || !now)) target.focus({ preventScroll: true });
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
    E.saving++;
    updateSaveState();
    E.chain = E.chain.then(async () => {
      E.inflight = { id, fields };
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
        E.inflight = null;
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
      else if (k === 'ed:date' || k === 'ed:time') queueField(E.task.id, 'due_at', dueFromInputs(), 0);
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
    if (net.inflight > 0 || (TT.subtaskBusy && TT.subtaskBusy())) return;
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
    const fields = {};
    let id = null;
    if (E.inflight) { id = E.inflight.id; Object.assign(fields, E.inflight.fields); }
    if (E.pending.id !== null && Object.keys(E.pending.fields).length) {
      if (id !== null && id !== E.pending.id) {
        reqs.push({ method: 'PATCH', url: '/api/tasks/' + id, body: Object.assign({}, fields) });
        for (const key of Object.keys(fields)) delete fields[key];
      }
      id = E.pending.id;
      Object.assign(fields, E.pending.fields);
    }
    if (id !== null && Object.keys(fields).length) reqs.push({ method: 'PATCH', url: '/api/tasks/' + id, body: fields });
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
        if (TT.escape && TT.escape(e)) return;
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

  // ------------------------------------------------------------- boot

  const TT = {
    // shared with matrix.js and the subtask code
    $, $$, esc, kq, api, toast, errorToast, schedule, renderInto, beginDrag, endDrag, held,
    S, E, P, MD, net, BOOT, FEATURE_API, supports, labelById, labelByName, labelNames, taskById,
    refresh, scheduleRefresh, applyTask, renderList, renderSidebar, renderEditor, openEditor,
    closeEditor, reloadEditor, queueField, flushFields, updateSaveState, setTaskStatus, chipHTML,
    dueBadgeHTML, updateHeader, focusQuickAdd, insertAtCaret, on, emit, load, save, editorValues,
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
