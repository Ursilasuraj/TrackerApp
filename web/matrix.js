/* TodoTracker: the Eisenhower matrix page.
 *
 * Tasks and subtasks are notes, placed freely by (urgency, importance), each
 * 0..1, of their centre; no position means "unsorted" (the tray). Notes can
 * be linked ("A before B"); clicking a note traces its chain. Moving a task
 * note across the middle line changes its priority (top half High, bottom
 * half Low). Everything works with the keyboard too.
 */
(function () {
  'use strict';

  const TT = window.TT;
  if (!TT) return;
  const { $, $$, esc, kq, api, toast, errorToast, schedule, renderInto, beginDrag, endDrag, S, P } = TT;

  const QUADS = {
    schedule: { name: 'Schedule', sub: 'Important, not urgent' },
    do: { name: 'Do', sub: 'Important and urgent' },
    eliminate: { name: 'Eliminate', sub: 'Neither urgent nor important' },
    delegate: { name: 'Delegate', sub: 'Urgent, not important' },
  };
  const ORDER = ['schedule', 'do', 'eliminate', 'delegate'];   // grid order: TL, TR, BL, BR
  const BAND = 48;            // px kept free for the quadrant titles (top and bottom)
  const STEP = 0.02;          // arrow-key move, Shift: 0.1
  const DRAG_START = 4;       // px before a press becomes a drag

  const M = {
    deps: [],
    shown: false,
    filters: {
      tasks: TT.load('tt-mx-tasks', true) !== false,
      subs: TT.load('tt-mx-subs', true) !== false,
      done: TT.load('tt-mx-done', false) === true,
    },
    trace: null,          // key whose chain is highlighted
    selectedDep: null,    // id of the selected link
    highlight: null,      // {keys:Set, label} from a warning
    chain: Promise.resolve(),
    pending: 0,
    items: new Map(),     // key -> item (current render)
    depsSeq: 0,
  };

  // ------------------------------------------------------------- data

  function labelsMatch(labels) {
    if (!S.labelIds.length) return true;
    const ids = new Set(labels.map((l) => l.id));
    return S.match === 'all' ? S.labelIds.every((id) => ids.has(id)) : S.labelIds.some((id) => ids.has(id));
  }

  /** Notes and tray entries from the loaded tasks, filters applied. */
  function buildItems() {
    const items = new Map();
    const searching = !!S.q.trim();
    for (const t of S.tasks) {
      const tDone = t.status === 'done';
      items.set('t' + t.id, {
        key: 't' + t.id, kind: 'task', id: t.id, task: t, title: t.title, done: tDone,
        pos: t.matrix, due: t.due_at, priority: t.priority, labels: t.labels,
        progress: t.progress, visible: M.filters.tasks && (M.filters.done || !tDone),
      });
      for (const s of t.subtasks || []) {
        const done = s.done || tDone;
        let visible = M.filters.subs && (M.filters.done || !done);
        // Search: the subtask itself must match. Labels: a subtask without
        // labels of its own counts with its task's labels.
        if (visible && searching) visible = (t.text_subs || []).includes(s.id);
        if (visible && S.labelIds.length) visible = labelsMatch(s.labels && s.labels.length ? s.labels : t.labels);
        items.set('s' + s.id, {
          key: 's' + s.id, kind: 'sub', id: s.id, task: t, sub: s, title: s.title, done,
          pos: s.matrix, due: s.due_at || t.due_at, ownDue: s.due_at, priority: t.priority,
          labels: s.labels || [], hasNotes: s.has_notes || !!s.notes, visible,
        });
      }
    }
    return items;
  }

  function unfinished(key) {
    const it = M.items.get(key);
    if (it) return !it.done;
    const d = M.deps.find((x) => x.before === key);
    return d ? !d.before_done : false;
  }

  function waitsFor(key) {
    return M.deps.filter((d) => d.after === key && unfinished(d.before));
  }

  function chainOf(key) {
    const up = new Set();
    const down = new Set();
    const walk = (start, from, to, out) => {
      const stack = [start];
      while (stack.length) {
        const k = stack.pop();
        for (const d of M.deps) {
          if (d[from] === k && !out.has(d[to])) { out.add(d[to]); stack.push(d[to]); }
        }
      }
    };
    walk(key, 'after', 'before', up);
    walk(key, 'before', 'after', down);
    return new Set([key, ...up, ...down]);
  }

  function quadOf(it) { return P.quadrantOf(it.pos); }

  function today() { return new Date(); }

  function isDueSoon(it, days) {
    if (!it.due) return false;
    return P.dayDiff(today(), P.parseYMD(it.due)) <= days;
  }

  function sortForToday(a, b) {
    const now = today();
    const ao = P.isOverdue(a.due, now) ? 0 : 1;
    const bo = P.isOverdue(b.due, now) ? 0 : 1;
    if (ao !== bo) return ao - bo;
    if ((a.due || '~') !== (b.due || '~')) return (a.due || '~') < (b.due || '~') ? -1 : 1;
    return b.pos[0] - a.pos[0];
  }

  // ------------------------------------------------------------- rendering

  function viewForList() { return M.filters.done ? 'all' : 'open'; }

  function sidebarEntry() {
    if (!TT.supports('matrix')) return '';
    const n = (S.meta.counts && S.meta.counts.do) || 0;
    const current = S.page === 'matrix' ? ' aria-current="page"' : '';
    return '<button type="button" class="nav-item matrix-entry" data-act="matrix" data-k="nav:matrix"' + current + '>'
      + '<span class="nav-icon" aria-hidden="true">▦</span><span>Eisenhower matrix</span>'
      + '<span class="count" aria-label="' + n + ' open in Do" title="Open items in Do">' + n + '</span></button>';
  }

  function noteHTML(it, now) {
    const quad = quadOf(it);
    const q = QUADS[quad];
    const waits = waitsFor(it.key);
    const meta = [];
    if (it.ownDue || (it.kind === 'task' && it.due)) {
      const due = it.kind === 'task' ? it.due : it.ownDue;
      const st = it.done ? 'future' : P.dueState(due, now);
      meta.push('<span class="badge due ' + st + '" title="Target ' + esc(P.dueLong(due)) + '">⏱ ' + esc(P.dueLabel(due, now)) + '</span>');
    }
    for (const l of it.labels) meta.push(TT.chipHTML(l));
    if (it.kind === 'sub') meta.push('<span class="note-parent" title="Subtask of ' + esc(it.task.title) + '">↳ ' + esc(it.task.title) + '</span>');
    else if (it.progress && it.progress[1]) meta.push('<span class="badge">' + it.progress[0] + '/' + it.progress[1] + '</span>');
    if (it.kind === 'sub' && it.hasNotes) meta.push('<span class="note-notes" title="Has notes">📝</span>');
    if (waits.length) {
      meta.push('<span class="badge waits" title="Waits for: ' + esc(waits.map((d) => d.before_title).join(', ')) + '">⛓ waits for ' + waits.length + '</span>');
    }
    meta.push('<span class="qtag q-' + quad + '">' + q.name + '</span>');
    const cls = ['note', it.kind === 'task' ? 'is-task' : 'is-sub', 'q-' + quad];
    if (it.done) cls.push('done');
    if (M.trace) cls.push(M.traceSet.has(it.key) ? (it.key === M.trace ? 'traced origin' : 'traced') : 'dim');
    else if (M.highlight) cls.push(M.highlight.keys.has(it.key) ? 'traced' : 'dim');
    const label = (it.kind === 'sub' ? 'Subtask ' : 'Task ') + it.title + ', ' + q.name
      + (it.due ? ', due ' + P.dueLong(it.due) : '') + (waits.length ? ', waits for ' + waits.length : '')
      + (it.done ? ', done' : '');
    return '<div class="' + cls.join(' ') + '" data-key="' + it.key + '" data-k="mx:' + it.key + '" tabindex="0"'
      + ' data-x="' + it.pos[0] + '" data-y="' + it.pos[1] + '" role="button" aria-label="' + esc(label) + '"'
      + ' aria-describedby="mx-note-help">'
      + '<div class="note-top">'
      + '<input type="checkbox" class="note-check" data-act="note-check" data-k="mx:' + it.key + ':check"' + (it.done ? ' checked' : '')
      + ' aria-label="Done: ' + esc(it.title) + '" tabindex="-1">'
      + '<span class="note-title">' + esc(it.title) + '</span>'
      + '</div>'
      + '<div class="note-meta">' + meta.join('') + '</div>'
      + '<span class="note-link" data-act="link" title="Drag onto a note that must wait for this one" aria-hidden="true">⇢</span>'
      + '</div>';
  }

  function trayItemHTML(it, header) {
    if (header) {
      return '<div class="tray-head" data-key="' + it.key + '">' + esc(it.title)
        + ' <span class="tray-placed">(on the board)</span></div>';
    }
    const sug = P.suggestPlacement({ key: it.key, due: it.due, priority: it.priority }, today());
    const q = QUADS[sug.quad];
    return '<div class="tray-item ' + (it.kind === 'sub' ? 'is-sub' : 'is-task') + '" data-key="' + it.key + '" data-k="tray:' + it.key + '"'
      + ' tabindex="0" aria-label="' + esc((it.kind === 'sub' ? 'Subtask ' : 'Task ') + it.title + '; Enter places it in ' + q.name) + '">'
      + '<span class="tray-title">' + (it.kind === 'sub' ? '↳ ' : '') + esc(it.title) + '</span>'
      + (it.due ? '<span class="badge due ' + P.dueState(it.due, today()) + '">⏱ ' + esc(P.dueLabel(it.due, today())) + '</span>' : '')
      + '<button type="button" class="suggest-chip q-' + sug.quad + '" data-act="suggest" data-key="' + it.key + '" data-k="tray:' + it.key + ':suggest"'
      + ' tabindex="-1" title="Place in ' + q.name + '">→ ' + q.name + '</button>'
      + '</div>';
  }

  function warningsFor(placed, tray) {
    const out = [];
    const now = today();
    const late = placed.filter((it) => !it.done && ['schedule', 'eliminate'].includes(quadOf(it)) && isDueSoon(it, 0));
    if (late.length) out.push({ id: 'late', keys: late.map((i) => i.key), text: late.length + (late.length === 1 ? ' item is' : ' items are') + ' due by today but sits in Schedule or Eliminate' });
    const inDo = placed.filter((it) => !it.done && quadOf(it) === 'do');
    if (inDo.length > 8) out.push({ id: 'crowded', keys: inDo.map((i) => i.key), text: inDo.length + ' items in Do — more than 8 is hard to finish' });
    const soon = tray.filter((it) => isDueSoon(it, 2));
    if (soon.length) out.push({ id: 'unsorted', keys: soon.map((i) => i.key), text: soon.length + (soon.length === 1 ? ' unsorted item is' : ' unsorted items are') + ' due within 2 days' });
    const blocked = inDo.filter((it) => waitsFor(it.key).some((d) => {
      const b = M.items.get(d.before);
      return !b || !b.pos || quadOf(b) !== 'do';
    }));
    if (blocked.length) out.push({ id: 'blocked', keys: blocked.map((i) => i.key), text: blocked.length + (blocked.length === 1 ? ' item in Do waits' : ' items in Do wait') + ' for something that is not in Do' });
    void now;
    return out;
  }

  function pageHTML() {
    const now = today();
    M.items = buildItems();
    const visible = Array.from(M.items.values()).filter((it) => it.visible);
    const placed = visible.filter((it) => it.pos);
    const tray = visible.filter((it) => !it.pos && !it.done);
    M.traceSet = M.trace ? chainOf(M.trace) : null;
    const counts = { do: 0, schedule: 0, delegate: 0, eliminate: 0 };
    for (const it of placed) if (!it.done) counts[quadOf(it)]++;

    const todayItems = placed.filter((it) => !it.done && quadOf(it) === 'do').sort(sortForToday);
    const strip = todayItems.slice(0, 6).map((it) => '<button type="button" class="today-item" data-act="select" data-key="' + it.key + '" data-k="today:' + it.key + '">'
      + (it.due ? '<span class="badge due ' + P.dueState(it.due, now) + '">' + esc(P.dueLabel(it.due, now)) + '</span>' : '')
      + '<span class="today-title">' + esc(it.title) + '</span></button>').join('')
      + (todayItems.length > 6 ? '<span class="today-more">+' + (todayItems.length - 6) + ' more</span>' : '');

    const warnings = warningsFor(placed, tray);
    const warnHTML = warnings.map((w) => '<button type="button" class="warning' + (M.highlight && M.highlight.id === w.id ? ' active' : '') + '"'
      + ' data-act="warning" data-warning="' + w.id + '" data-k="warn:' + w.id + '" aria-pressed="' + !!(M.highlight && M.highlight.id === w.id) + '">'
      + '⚠ ' + esc(w.text) + '</button>').join('');
    M.warnings = warnings;

    const quads = ORDER.map((q) => '<div class="mx-q q-' + q + '"><div class="mx-q-title"><strong>' + QUADS[q].name + '</strong>'
      + ' <span class="mx-q-count" aria-label="' + counts[q] + ' open">' + counts[q] + '</span>'
      + '<span class="mx-q-sub">' + QUADS[q].sub + '</span></div></div>').join('');

    // Tray: grouped by task; a placed task with unplaced subtasks becomes a header.
    const groups = [];
    for (const t of S.tasks) {
      const tItem = M.items.get('t' + t.id);
      const subs = (t.subtasks || []).map((s) => M.items.get('s' + s.id)).filter((it) => it && it.visible && !it.pos && !it.done);
      const taskInTray = tItem.visible && !tItem.pos && !tItem.done;
      if (!taskInTray && !subs.length) continue;
      groups.push('<div class="tray-group">' + (taskInTray ? trayItemHTML(tItem) : trayItemHTML(tItem, true))
        + subs.map((it) => trayItemHTML(it)).join('') + '</div>');
    }
    const filter = (key, label) => '<button type="button" class="seg-btn" data-act="filter" data-filter="' + key + '" data-k="mxf:' + key + '"'
      + ' aria-pressed="' + M.filters[key] + '">' + label + '</button>';
    const dep = M.selectedDep != null ? M.deps.find((d) => d.id === M.selectedDep) : null;

    return '<div class="mx">'
      + '<div class="mx-main">'
      + '<div class="mx-head"><h1 class="view-title" id="mx-title">Eisenhower matrix</h1>'
      + '<div class="mx-filters seg" role="group" aria-label="Show">' + filter('tasks', 'Tasks') + filter('subs', 'Subtasks') + filter('done', 'Show done') + '</div>'
      + (S.q.trim() || S.labelIds.length ? '<span class="view-sub">Filtered by the sidebar and search</span>' : '')
      + '</div>'
      + '<div class="mx-today" role="group" aria-label="Today: open items in Do"><span class="mx-today-label">Today</span>'
      + (strip || '<span class="view-sub">Nothing in Do.</span>') + '</div>'
      + (warnHTML ? '<div class="mx-warnings" role="group" aria-label="Warnings">' + warnHTML + '</div>' : '')
      + (dep ? '<div class="mx-linkbar" role="group" aria-label="Selected link"><span>Link: <strong>' + esc(dep.before_title) + '</strong> before <strong>'
        + esc(dep.after_title) + '</strong></span><button type="button" class="btn danger" data-act="unlink" data-k="mx:unlink">Remove link</button>'
        + '<span class="view-sub">(or press Delete)</span></div>' : '')
      + '<div class="mx-board-wrap">'
      + '<div class="mx-axis-y" aria-hidden="true">Low → Importance → High</div>'
      + '<div class="mx-board" id="mx-board" role="application" aria-labelledby="mx-title">'
      + quads
      + '<svg class="mx-links" aria-hidden="false"></svg>'
      + '<div class="mx-notes">' + placed.map((it) => noteHTML(it, now)).join('') + '</div>'
      + '</div>'
      + '<div class="mx-axis-x" aria-hidden="true">Low → Urgency → High</div>'
      + '</div>'
      + '<p id="mx-note-help" class="sr-only">Arrow keys move the note, Shift moves further, Enter opens it, Space shows its chain, Delete puts it back in the tray.</p>'
      + '</div>'
      + '<aside class="mx-tray" aria-label="Unsorted">'
      + '<div class="mx-tray-head"><h2>Unsorted <span class="count">' + tray.length + '</span></h2>'
      + (tray.length ? '<button type="button" class="btn" data-act="place-all" data-k="mx:place-all">Place all by suggestion</button>' : '')
      + '</div>'
      + '<div class="mx-tray-list">' + (groups.join('') || '<p class="view-sub">Everything is on the board.</p>') + '</div>'
      + '</aside>'
      + '</div>';
  }

  function render() {
    if (!M.shown) return;
    schedule('matrix', () => {
      if (!M.shown) return;
      const page = $('#matrix-page');
      renderInto(page, pageHTML(), {
        scroller: $('.mx-tray-list', page),
        fallback: (snap) => (snap.k && snap.k.startsWith('mx:') ? $('#mx-board') : null),
      });
      layout();
    });
  }

  // ------------------------------------------------------------- layout

  function boardBox() {
    const board = $('#mx-board');
    return board ? { board, w: board.clientWidth, h: board.clientHeight } : null;
  }

  /** Pixel rectangle for a note centred at pos (kept clear of the title bands). */
  function placeRect(pos, w, h, box) {
    const cx = pos[0] * box.w;
    const cy = (1 - pos[1]) * box.h;
    const left = Math.min(Math.max(cx - w / 2, 4), box.w - w - 4);
    const top = Math.min(Math.max(cy - h / 2, BAND), box.h - BAND - h);
    return { left, top, w, h };
  }

  function posFromRect(r, box) {
    const x = (r.left + r.w / 2) / box.w;
    const y = 1 - (r.top + r.h / 2) / box.h;
    return [Math.round(Math.min(1, Math.max(0, x)) * 1000) / 1000, Math.round(Math.min(1, Math.max(0, y)) * 1000) / 1000];
  }

  function layout() {
    const box = boardBox();
    if (!box) return;
    for (const note of $$('.note', box.board)) {
      const r = placeRect([Number(note.dataset.x), Number(note.dataset.y)], note.offsetWidth, note.offsetHeight, box);
      note.style.left = r.left + 'px';
      note.style.top = r.top + 'px';
    }
    drawLinks();
  }

  function noteRect(key) {
    const el = $('#mx-board .note[data-key="' + key + '"]');
    if (!el) return null;
    return { x: el.offsetLeft, y: el.offsetTop, w: el.offsetWidth, h: el.offsetHeight };
  }

  /** Where the segment from the centre of r towards (tx, ty) leaves r. */
  function edgePoint(r, tx, ty) {
    const cx = r.x + r.w / 2;
    const cy = r.y + r.h / 2;
    const dx = tx - cx;
    const dy = ty - cy;
    if (!dx && !dy) return [cx, cy];
    const sx = dx ? (r.w / 2) / Math.abs(dx) : Infinity;
    const sy = dy ? (r.h / 2) / Math.abs(dy) : Infinity;
    const s = Math.min(sx, sy);
    return [cx + dx * s, cy + dy * s];
  }

  function drawLinks(extra) {
    const svg = $('#mx-board .mx-links');
    if (!svg) return;
    const parts = ['<defs>'
      + '<marker id="mx-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
      + '<path d="M0,0 L10,5 L0,10 z" class="arrow-head"/></marker>'
      + '<marker id="mx-arrow-sel" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
      + '<path d="M0,0 L10,5 L0,10 z" class="arrow-head selected"/></marker></defs>'];
    for (const d of M.deps) {
      const a = noteRect(d.before);
      const b = noteRect(d.after);
      if (!a || !b) continue;
      const [x1, y1] = edgePoint(a, b.x + b.w / 2, b.y + b.h / 2);
      const [x2, y2] = edgePoint(b, a.x + a.w / 2, a.y + a.h / 2);
      const sel = M.selectedDep === d.id;
      const dim = M.traceSet && !(M.traceSet.has(d.before) && M.traceSet.has(d.after));
      const cls = 'link' + (unfinished(d.before) ? '' : ' done') + (sel ? ' selected' : '') + (dim ? ' dim' : '');
      const path = 'M' + x1.toFixed(1) + ',' + y1.toFixed(1) + ' L' + x2.toFixed(1) + ',' + y2.toFixed(1);
      parts.push('<g class="' + cls + '" data-dep="' + d.id + '">'
        + '<path d="' + path + '" class="link-line" marker-end="url(#' + (sel ? 'mx-arrow-sel' : 'mx-arrow') + ')"/>'
        + '<path d="' + path + '" class="link-hit" data-act="dep" data-dep="' + d.id + '" data-k="dep:' + d.id + '" tabindex="0" role="button"'
        + ' aria-label="Link: ' + esc(d.before_title) + ' before ' + esc(d.after_title) + '"/></g>');
    }
    if (extra) parts.push(extra);
    svg.innerHTML = parts.join('');
  }

  // ------------------------------------------------------------- writes

  function findItemObjects(key) {
    // The objects to update for an item: the task or subtask in S.tasks.
    const kind = key[0];
    const id = Number(key.slice(1));
    for (const t of S.tasks) {
      if (kind === 't' && t.id === id) return { task: t };
      if (kind === 's') {
        const s = (t.subtasks || []).find((x) => x.id === id);
        if (s) return { task: t, sub: s };
      }
    }
    return null;
  }

  /** Move items (optimistically) and save; priority follows manual moves. */
  function moveItems(moves, opts) {
    opts = opts || {};
    const items = [];
    const notices = [];
    for (const mv of moves) {
      const found = findItemObjects(mv.key);
      if (!found) continue;
      const target = found.sub || found.task;
      const from = target.matrix || null;
      let prio = null;
      if (opts.manual && !found.sub) prio = P.priorityForMove('task', found.task.priority, from, mv.pos);
      target.matrix = mv.pos;
      const item = { key: mv.key, matrix: mv.pos };
      if (prio) {
        found.task.priority = prio;
        item.priority = prio;
        notices.push('“' + found.task.title + '” is now ' + (prio === 'high' ? 'High' : 'Low') + ' priority (' + (prio === 'high' ? 'top' : 'bottom') + ' half).');
      }
      items.push(item);
    }
    if (!items.length) return;
    render();
    for (const n of notices) toast(n, { timeout: 3500 });
    M.pending++;
    M.chain = M.chain.then(async () => {
      try {
        const body = items.map((it) => {
          const key = it.key[0] === 's' && Number(it.key.slice(1)) < 0 ? 's' + TT.realId(Number(it.key.slice(1))) : it.key;
          return Object.assign({}, it, { key });
        }).filter((it) => !/null|NaN/.test(it.key));
        const res = await api('POST', '/api/matrix', { items: body });
        if (M.pending === 1) for (const t of res.tasks) TT.applyTask(t);
      } catch (e) {
        errorToast('Could not move the note:', e);
        TT.refresh();
      } finally {
        M.pending--;
        if (!M.pending) TT.scheduleRefresh(300);
        render();
      }
    });
  }

  async function addLink(before, after) {
    try {
      const res = await api('POST', '/api/deps', { before, after });
      M.deps = res.deps;
      if (!res.created) toast('These notes are already linked.');
      render();
    } catch (e) {
      toast(e.message, { kind: 'error' });
    }
  }

  async function removeLink(id) {
    try {
      const res = await api('DELETE', '/api/deps/' + id);
      M.deps = res.deps;
    } catch (e) { errorToast('Could not remove the link:', e); }
    M.selectedDep = null;
    render();
    const board = $('#mx-board');
    if (board) board.focus({ preventScroll: true });
  }

  async function loadDeps() {
    const seq = ++M.depsSeq;
    try {
      const res = await api('GET', '/api/deps');
      if (seq !== M.depsSeq) return;
      M.deps = res.deps;
      if (M.selectedDep != null && !M.deps.some((d) => d.id === M.selectedDep)) M.selectedDep = null;
    } catch (e) { /* the list refresh shows errors */ }
    render();
  }

  function completeItem(it, done) {
    if (it.kind === 'task') TT.setTaskStatus(it.id, done ? 'done' : 'open', { undo: true });
    else TT.setSubtaskDone(it.task.id, it.id, done);
  }

  function placeBySuggestion(keys) {
    const moves = keys.map((key) => {
      const it = M.items.get(key);
      return { key, pos: P.suggestPlacement({ key, due: it.due, priority: it.priority }, today()).pos };
    });
    moveItems(moves);   // suggestions never change priority
  }

  function openItem(it) {
    if (it.kind === 'task') TT.openEditor(it.id);
    else TT.openEditor(it.task.id, null, { focusK: 's' + it.id + ':title' });
  }

  function setTrace(key) {
    M.trace = key && M.trace !== key ? key : null;
    M.highlight = null;
    M.selectedDep = null;
    render();
  }

  // ------------------------------------------------------------- pointer

  function trayRect() {
    const tray = $('.mx-tray');
    return tray ? tray.getBoundingClientRect() : null;
  }

  function inside(r, x, y) {
    return r && x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
  }

  /** Drag a note around the board (or onto the tray to unplace it). */
  function startNoteDrag(e, note) {
    const box = boardBox();
    const key = note.dataset.key;
    const it = M.items.get(key);
    if (!box || !it) return;
    const startX = e.clientX;
    const startY = e.clientY;
    const left0 = note.offsetLeft;
    const top0 = note.offsetTop;
    let dragging = false;
    let finished = false;
    const onMove = (ev) => {
      const dx = ev.clientX - startX;
      const dy = ev.clientY - startY;
      if (!dragging) {
        if (Math.abs(dx) + Math.abs(dy) < DRAG_START) return;
        dragging = true;
        beginDrag();
        note.classList.add('dragging');
      }
      note.style.left = (left0 + dx) + 'px';
      note.style.top = (top0 + dy) + 'px';
      $('.mx-tray').classList.toggle('drop-target', inside(trayRect(), ev.clientX, ev.clientY));
      drawLinks();
    };
    const finish = (ev, cancel) => {
      if (finished) return;
      finished = true;
      window.removeEventListener('pointermove', onMove, true);
      window.removeEventListener('pointerup', onUp, true);
      window.removeEventListener('pointercancel', onCancel, true);
      window.removeEventListener('blur', onCancel);
      document.removeEventListener('keydown', onKey, true);
      const tray = $('.mx-tray');
      if (tray) tray.classList.remove('drop-target');
      if (!dragging) return;
      note.classList.remove('dragging');
      endDrag();
      if (cancel) { render(); return; }
      if (inside(trayRect(), ev.clientX, ev.clientY)) { moveItems([{ key, pos: null }], { manual: true }); return; }
      const b = box.board.getBoundingClientRect();
      if (!inside(b, ev.clientX, ev.clientY)) { render(); return; }
      const r = placeRect(posFromRect({ left: note.offsetLeft, top: note.offsetTop, w: note.offsetWidth, h: note.offsetHeight }, box),
        note.offsetWidth, note.offsetHeight, box);
      moveItems([{ key, pos: posFromRect(r, box) }], { manual: true });
    };
    const onUp = (ev) => finish(ev, false);
    const onCancel = (ev) => finish(ev, true);
    const onKey = (ev) => {
      if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); finish(ev, true); }
    };
    window.addEventListener('pointermove', onMove, true);
    window.addEventListener('pointerup', onUp, true);
    window.addEventListener('pointercancel', onCancel, true);
    window.addEventListener('blur', onCancel);
    document.addEventListener('keydown', onKey, true);
  }

  /** Drag a tray item onto the board. */
  function startTrayDrag(e, el) {
    const key = el.dataset.key;
    const it = M.items.get(key);
    if (!it) return;
    const startX = e.clientX;
    const startY = e.clientY;
    let ghost = null;
    let finished = false;
    const onMove = (ev) => {
      if (!ghost) {
        if (Math.abs(ev.clientX - startX) + Math.abs(ev.clientY - startY) < DRAG_START) return;
        beginDrag();
        ghost = document.createElement('div');
        ghost.className = 'note ghost ' + (it.kind === 'task' ? 'is-task' : 'is-sub');
        ghost.innerHTML = '<div class="note-top"><span class="note-title">' + esc(it.title) + '</span></div>';
        document.body.append(ghost);
      }
      ghost.style.left = (ev.clientX - ghost.offsetWidth / 2) + 'px';
      ghost.style.top = (ev.clientY - ghost.offsetHeight / 2) + 'px';
      const board = $('#mx-board');
      if (board) board.classList.toggle('drop-target', inside(board.getBoundingClientRect(), ev.clientX, ev.clientY));
    };
    const finish = (ev, cancel) => {
      if (finished) return;
      finished = true;
      window.removeEventListener('pointermove', onMove, true);
      window.removeEventListener('pointerup', onUp, true);
      window.removeEventListener('pointercancel', onCancel, true);
      window.removeEventListener('blur', onCancel);
      document.removeEventListener('keydown', onKey, true);
      if (!ghost) return;
      const gw = ghost.offsetWidth;
      const gh = ghost.offsetHeight;
      ghost.remove();
      const box = boardBox();
      if (box) box.board.classList.remove('drop-target');
      endDrag();
      if (cancel || !box) { render(); return; }
      const b = box.board.getBoundingClientRect();
      if (!inside(b, ev.clientX, ev.clientY)) { render(); return; }
      const r = placeRect(posFromRect({ left: ev.clientX - b.left - gw / 2, top: ev.clientY - b.top - gh / 2, w: gw, h: gh }, box), gw, gh, box);
      moveItems([{ key, pos: posFromRect(r, box) }], { manual: true });
    };
    const onUp = (ev) => finish(ev, false);
    const onCancel = (ev) => finish(ev, true);
    const onKey = (ev) => {
      if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); finish(ev, true); }
    };
    window.addEventListener('pointermove', onMove, true);
    window.addEventListener('pointerup', onUp, true);
    window.addEventListener('pointercancel', onCancel, true);
    window.addEventListener('blur', onCancel);
    document.addEventListener('keydown', onKey, true);
  }

  /** Drag from a note's ⇢ handle onto the note that must wait for it. */
  function startLinkDrag(e, note) {
    const box = boardBox();
    if (!box) return;
    const from = note.dataset.key;
    const b = box.board.getBoundingClientRect();
    const r = noteRect(from);
    const x0 = r.x + r.w;
    const y0 = r.y + r.h / 2;
    let finished = false;
    beginDrag();
    note.classList.add('linking');
    const noteAt = (x, y) => {
      const el = document.elementFromPoint(x, y);
      return el && el.closest ? el.closest('#mx-board .note') : null;
    };
    const targetAt = (x, y) => {
      const n = noteAt(x, y);
      return n && n.dataset.key !== from ? n : null;
    };
    const onMove = (ev) => {
      const x = ev.clientX - b.left;
      const y = ev.clientY - b.top;
      for (const n of $$('#mx-board .note.link-target')) n.classList.remove('link-target');
      const t = targetAt(ev.clientX, ev.clientY);
      if (t) t.classList.add('link-target');
      drawLinks('<path class="link-line drafting" d="M' + x0 + ',' + y0 + ' L' + x + ',' + y + '" marker-end="url(#mx-arrow)"/>');
    };
    const finish = (ev, cancel) => {
      if (finished) return;
      finished = true;
      window.removeEventListener('pointermove', onMove, true);
      window.removeEventListener('pointerup', onUp, true);
      window.removeEventListener('pointercancel', onCancel, true);
      window.removeEventListener('blur', onCancel);
      document.removeEventListener('keydown', onKey, true);
      note.classList.remove('linking');
      const over = cancel ? null : noteAt(ev.clientX, ev.clientY);
      endDrag();
      drawLinks();
      for (const n of $$('#mx-board .note.link-target')) n.classList.remove('link-target');
      if (over && over.dataset.key === from) toast('A note cannot wait for itself.', { kind: 'error' });
      else if (over) addLink(from, over.dataset.key);
    };
    const onUp = (ev) => finish(ev, false);
    const onCancel = (ev) => finish(ev, true);
    const onKey = (ev) => {
      if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); finish(ev, true); }
    };
    window.addEventListener('pointermove', onMove, true);
    window.addEventListener('pointerup', onUp, true);
    window.addEventListener('pointercancel', onCancel, true);
    window.addEventListener('blur', onCancel);
    document.addEventListener('keydown', onKey, true);
  }

  // ------------------------------------------------------------- events

  function bind() {
    const page = $('#matrix-page');
    page.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      const link = e.target.closest('.note-link');
      const note = e.target.closest('#mx-board .note');
      if (link && note) { e.preventDefault(); startLinkDrag(e, note); return; }
      if (note && !e.target.closest('.note-check')) { startNoteDrag(e, note); return; }
      const trayItem = e.target.closest('.tray-item');
      if (trayItem && !e.target.closest('.suggest-chip')) startTrayDrag(e, trayItem);
    });
    page.addEventListener('click', (e) => {
      const el = e.target.closest('[data-act]');
      const note = e.target.closest('#mx-board .note');
      if (el && page.contains(el)) {
        const act = el.dataset.act;
        if (act === 'note-check') return;
        if (act === 'filter') {
          const f = el.dataset.filter;
          M.filters[f] = !M.filters[f];
          TT.save('tt-mx-' + f, M.filters[f]);
          if (f === 'done') TT.refresh(); else render();
          return;
        }
        if (act === 'suggest') { placeBySuggestion([el.dataset.key]); return; }
        if (act === 'place-all') {
          const keys = $$('.mx-tray .tray-item', page).map((x) => x.dataset.key);
          placeBySuggestion(keys);
          return;
        }
        if (act === 'select') {
          const key = el.dataset.key;
          M.trace = key;
          M.highlight = null;
          render();
          const n = $(kq('mx:' + key));
          if (n) { n.focus({ preventScroll: false }); n.scrollIntoView({ block: 'nearest' }); }
          return;
        }
        if (act === 'warning') {
          const w = (M.warnings || []).find((x) => x.id === el.dataset.warning);
          M.highlight = w && !(M.highlight && M.highlight.id === w.id) ? { id: w.id, keys: new Set(w.keys) } : null;
          M.trace = null;
          render();
          return;
        }
        if (act === 'dep') {
          M.selectedDep = Number(el.dataset.dep);
          render();
          return;
        }
        if (act === 'unlink' && M.selectedDep != null) { removeLink(M.selectedDep); return; }
      }
      if (note && !e.target.closest('.note-link')) {
        // A double click opens the note. The board may have been rebuilt
        // between the two clicks (the first one traces), and then the
        // browser sends no dblclick; the click's detail still says 2.
        if (e.detail >= 2) {
          const it = M.items.get(note.dataset.key);
          if (it && e.detail === 2) openItem(it);
          return;
        }
        setTrace(note.dataset.key);
        note.focus({ preventScroll: true });
      } else if (e.target.closest('#mx-board') && !note) {
        if (M.trace || M.selectedDep != null || M.highlight) {
          M.trace = null;
          M.selectedDep = null;
          M.highlight = null;
          render();
        }
      }
    });
    page.addEventListener('change', (e) => {
      if (e.target.dataset.act !== 'note-check') return;
      const it = M.items.get(e.target.closest('.note').dataset.key);
      if (it) completeItem(it, e.target.checked);
    });
    page.addEventListener('keydown', (e) => {
      const el = e.target;
      const note = el.classList && el.classList.contains('note') ? el : null;
      if (note) { noteKeydown(e, note); return; }
      if (el.classList && el.classList.contains('tray-item') && e.key === 'Enter') {
        e.preventDefault();
        const items = $$('.mx-tray .tray-item');
        const i = items.indexOf(el);
        placeBySuggestion([el.dataset.key]);
        const next = $$('.mx-tray .tray-item').filter((x) => x.dataset.key !== el.dataset.key)[Math.min(i, items.length - 2)];
        if (next) next.focus({ preventScroll: true });
        return;
      }
      if (el.dataset && el.dataset.act === 'dep' && (e.key === 'Delete' || e.key === 'Backspace')) {
        e.preventDefault();
        removeLink(Number(el.dataset.dep));
        return;
      }
      if (el.dataset && el.dataset.act === 'dep' && (e.key === 'Enter' || e.key === ' ')) {
        e.preventDefault();
        M.selectedDep = Number(el.dataset.dep);
        render();
      }
    });
    document.addEventListener('keydown', (e) => {
      if (!M.shown || S.page !== 'matrix') return;
      if ((e.key === 'Delete' || e.key === 'Backspace') && M.selectedDep != null && !isTyping(e.target)) {
        e.preventDefault();
        removeLink(M.selectedDep);
      }
    });
    if (window.ResizeObserver) {
      new ResizeObserver(() => { if (M.shown && !TT.held()) layout(); }).observe(page);
    }
  }

  function isTyping(el) {
    return el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable);
  }

  function noteKeydown(e, note) {
    const key = note.dataset.key;
    const it = M.items.get(key);
    if (!it) return;
    const arrows = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] };
    if (arrows[e.key] && !e.altKey && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      const step = e.shiftKey ? 0.1 : STEP;
      const [dx, dy] = arrows[e.key];
      const pos = [Math.min(1, Math.max(0, it.pos[0] + dx * step)), Math.min(1, Math.max(0, it.pos[1] + dy * step))];
      moveItems([{ key, pos: [Math.round(pos[0] * 1000) / 1000, Math.round(pos[1] * 1000) / 1000] }], { manual: true });
    } else if (e.key === 'Enter') {
      e.preventDefault();
      openItem(it);
    } else if (e.key === ' ') {
      e.preventDefault();
      setTrace(key);
    } else if (e.key === 'Delete' || e.key === 'Backspace') {
      e.preventDefault();
      const notes = $$('#mx-board .note');
      const i = notes.indexOf(note);
      moveItems([{ key, pos: null }], { manual: true });
      const rest = $$('#mx-board .note');
      const next = rest[Math.min(i, rest.length - 1)];
      if (next) next.focus({ preventScroll: true });
      else { const tray = $(kq('tray:' + key)); if (tray) tray.focus({ preventScroll: true }); }
    } else if (e.key === 'Escape' && (M.trace || M.highlight || M.selectedDep != null)) {
      e.preventDefault();
      e.stopPropagation();
      M.trace = null;
      M.highlight = null;
      M.selectedDep = null;
      render();
    }
  }

  // ------------------------------------------------------------- hooks

  function shown(on) {
    M.shown = on;
    const page = $('#matrix-page');
    if (!on) { page.innerHTML = ''; return; }
    loadDeps();
  }

  function escape(e) {
    if (S.page !== 'matrix') return false;
    if (M.trace || M.highlight || M.selectedDep != null) {
      e.preventDefault();
      M.trace = null;
      M.highlight = null;
      M.selectedDep = null;
      render();
      return true;
    }
    return false;
  }

  function mergeMatrixLocal(tasks) {
    if (!M.pending) return tasks;
    // Keep positions and priorities that are still being saved.
    for (const t of tasks) {
      const local = S.tasks.find((x) => x.id === t.id);
      if (!local) continue;
      t.matrix = local.matrix;
      t.priority = local.priority;
      for (const s of t.subtasks || []) {
        const ls = (local.subtasks || []).find((x) => x.id === s.id);
        if (ls) s.matrix = ls.matrix;
      }
    }
    return tasks;
  }

  TT.FEATURE_API.matrix = 4;
  TT.matrix = { shown, render, applyTask() { render(); } };
  TT.matrixView = viewForList;
  TT.sidebarMatrixEntry = sidebarEntry;
  TT.matrixBusy = () => M.pending > 0;
  TT.matrixFocusAfterClose = (taskId) => $(kq('mx:t' + taskId));
  TT.escape = escape;
  TT.mergeMatrixLocal = mergeMatrixLocal;
  TT.on('refresh', () => { if (M.shown) loadDeps(); });
  TT.on('task', () => { if (M.shown) render(); });
  bind();
})();
