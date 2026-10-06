"""SQLite storage for TodoTracker: schema, migrations, search index and queries.

All timestamps are local ISO strings (YYYY-MM-DDTHH:MM:SS). Target dates are
YYYY-MM-DD or YYYY-MM-DDTHH:MM. Every public method opens its own short
transaction, so the store can be shared by the HTTP threads, the reminder
thread and the backup thread.
"""

import colorsys
import logging
import os
import queue
import re
import sqlite3
import threading
import time
import unicodedata
from contextlib import contextmanager
from datetime import date, datetime, timedelta

log = logging.getLogger('todotracker.db')

STATUSES = ('open', 'in_progress', 'done')
PRIORITIES = ('high', 'medium', 'low')
PRIORITY_RANK = {'high': 0, 'medium': 1, 'low': 2}
VIEWS = ('open', 'overdue', 'today', 'week', 'nodate', 'done', 'all')
SORTS = ('newest', 'due', 'priority')

PALETTE = ['#e11d48', '#2563eb', '#16a34a', '#d97706', '#7c3aed', '#0891b2',
           '#db2777', '#65a30d', '#ea580c', '#4f46e5', '#0d9488', '#9333ea']

MAX_TITLE = 500
MAX_DESCRIPTION = 200_000
MAX_LABEL = 40
MAX_LABELS_PER_TASK = 50
MAX_SUBTASKS_PER_REQUEST = 500
REMIND_DATE_ONLY_AT = '09:00'

DATE_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')
DATETIME_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$')
COLOR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')
STAMP_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$')
IMG_RE = re.compile(r'!\[[^\]\n]*\]\(\s*(?:/images/[^\s)]+|https?://[^\s)]+)[^)\n]*\)')


class ValidationError(ValueError):
    """Bad input; the message is shown to the user (HTTP 400)."""


class NotFound(LookupError):
    """A referenced item does not exist (HTTP 404)."""


def now_dt():
    """Current local time. Tests replace this to pin the clock."""
    return datetime.now()


def now_iso(dt=None):
    return (dt or now_dt()).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# Each table is created with its current column set. Columns added after a
# table first shipped are also listed in ADDED_COLUMNS so that older
# databases get them through ALTER TABLE.
TABLES = [
    ('tasks', '''CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'in_progress', 'done')),
        priority TEXT NOT NULL DEFAULT 'medium' CHECK (priority IN ('high', 'medium', 'low')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        due_at TEXT,
        completed_at TEXT,
        reminded_at TEXT
    )'''),
    ('labels', '''CREATE TABLE IF NOT EXISTS labels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        color TEXT NOT NULL,
        created_at TEXT NOT NULL
    )'''),
    ('task_labels', '''CREATE TABLE IF NOT EXISTS task_labels (
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
        PRIMARY KEY (task_id, label_id)
    )'''),
    ('meta', '''CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    )'''),
    ('subtasks', '''CREATE TABLE IF NOT EXISTS subtasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        done INTEGER NOT NULL DEFAULT 0,
        position INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        completed_at TEXT
    )'''),
]

ADDED_COLUMNS = [
    # (table, column, definition)
    ('tasks', 'reminded_at', 'TEXT'),
]

INDEXES = [
    ('idx_tasks_status', 'CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)'),
    ('idx_task_labels_label', 'CREATE INDEX IF NOT EXISTS idx_task_labels_label ON task_labels(label_id)'),
    ('idx_subtasks_task', 'CREATE INDEX IF NOT EXISTS idx_subtasks_task ON subtasks(task_id, position)'),
]

TRIGGERS = []

FTS_COLUMNS = ['title', 'description', 'subtasks']
FTS_CREATE = ("CREATE VIRTUAL TABLE tasks_fts USING fts5("
              "title, description, subtasks, tokenize = 'unicode61 remove_diacritics 2')")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _valid_ymd(y, m, d):
    try:
        date(int(y), int(m), int(d))
        return True
    except ValueError:
        return False


def clean_due(value):
    """Validate a target date: None, 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM'."""
    if value is None or value == '':
        return None
    if not isinstance(value, str):
        raise ValidationError('Target date must be a string like 2026-10-01 or 2026-10-01T14:30.')
    m = DATE_RE.match(value)
    if m and _valid_ymd(*m.groups()):
        return value
    m = DATETIME_RE.match(value)
    if m and _valid_ymd(*m.groups()[:3]) and int(m.group(4)) < 24 and int(m.group(5)) < 60:
        return value
    raise ValidationError(f'Invalid target date "{value[:40]}". Use YYYY-MM-DD or YYYY-MM-DDTHH:MM.')


def clean_title(value, what='Title'):
    if not isinstance(value, str):
        raise ValidationError(f'{what} must be text.')
    value = ' '.join(value.split())
    if not value:
        raise ValidationError(f'{what} cannot be empty.')
    if len(value) > MAX_TITLE:
        raise ValidationError(f'{what} is too long (at most {MAX_TITLE} characters).')
    return value


def clean_description(value):
    if not isinstance(value, str):
        raise ValidationError('Description must be text.')
    if len(value) > MAX_DESCRIPTION:
        raise ValidationError(f'Description is too long (at most {MAX_DESCRIPTION} characters).')
    return value.replace('\r\n', '\n').replace('\r', '\n')


def clean_stamp(value, what='Timestamp'):
    """A local timestamp YYYY-MM-DDTHH:MM:SS (used when restoring/importing)."""
    if not isinstance(value, str) or not STAMP_RE.match(value):
        raise ValidationError(f'{what} must look like 2026-10-06T14:30:00.')
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise ValidationError(f'{what} is not a valid date and time.')
    return value


def clean_status(value):
    if value not in STATUSES:
        raise ValidationError('Status must be one of: open, in_progress, done.')
    return value


def clean_priority(value):
    if value not in PRIORITIES:
        raise ValidationError('Priority must be one of: high, medium, low.')
    return value


def clean_label_name(value):
    if not isinstance(value, str):
        raise ValidationError('Label names must be text.')
    name = value.strip()
    if name.startswith('#'):
        name = name[1:]
    if not name:
        raise ValidationError('Label name cannot be empty.')
    if len(name) > MAX_LABEL:
        raise ValidationError(f'Label "{name[:40]}…" is too long (at most {MAX_LABEL} characters).')
    if any(ch.isspace() for ch in name) or ',' in name or '#' in name:
        raise ValidationError(f'Label "{name}" cannot contain spaces, commas or #.')
    return name


def clean_color(value):
    if not isinstance(value, str) or not COLOR_RE.match(value):
        raise ValidationError('Colour must look like #1a2b3c.')
    return value.lower()


def due_moment(due):
    """The minute a target date 'arrives' (date-only targets remind at 09:00)."""
    if due is None:
        return None
    return due if len(due) > 10 else f'{due}T{REMIND_DATE_ONLY_AT}'


def rearm_value(due, now):
    """reminded_at for a freshly set target date.

    A target that has already arrived when it is set needs no reminder (the
    user is looking at it), so it counts as reminded. Otherwise the reminder
    is armed again (NULL).
    """
    moment = due_moment(due)
    if moment is None:
        return None
    current = now.strftime('%Y-%m-%dT%H:%M')
    return now_iso(now) if moment <= current else None


def is_overdue(due, now):
    if due is None:
        return False
    if len(due) == 10:
        return due < now.strftime('%Y-%m-%d')
    return due < now.strftime('%Y-%m-%dT%H:%M')


def snippet(desc, limit=160):
    """First line of readable text in a Markdown description."""
    in_fence = False
    for line in desc.split('\n'):
        s = line.strip()
        if s.startswith('```') or s.startswith('~~~'):
            in_fence = not in_fence
            continue
        if in_fence or not s or re.fullmatch(r'(?:[-*_]\s*){3,}', s):
            continue
        s = re.sub(r'^(?:#{1,6}\s+|(?:>\s*)+|(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?)', '', s)
        s = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', s)
        s = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', s)
        s = re.sub(r'\*\*|__|~~|`', '', s)
        s = ' '.join(s.split())
        if s:
            return s if len(s) <= limit else s[:limit - 1].rstrip() + '…'
    return ''


def image_count(desc):
    return len(IMG_RE.findall(desc or ''))


def search_words(q):
    """Words of a search query (letters and digits, like FTS5's unicode61)."""
    return [w for w in re.findall(r'[^\W_]+', q or '') if w][:12]


def _fold(text):
    text = unicodedata.normalize('NFKD', text or '')
    return ''.join(ch for ch in text if not unicodedata.combining(ch)).lower()


def text_matches_any(words, text):
    """True if any query word is a prefix of a word in text."""
    if not words:
        return False
    tokens = re.findall(r'[^\W_]+', _fold(text))
    folded = [_fold(w) for w in words]
    return any(t.startswith(w) for w in folded for t in tokens)


def text_matches_all(words, text):
    tokens = re.findall(r'[^\W_]+', _fold(text))
    return all(any(t.startswith(_fold(w)) for t in tokens) for w in words)


def _hex_luminance(hex_color):
    def chan(c):
        c = c / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def golden_color(n):
    """The n-th label colour after the palette: golden-angle hues at a mid-tone
    luminance, so the colour keeps 3:1 against both light and dark surfaces."""
    hue = ((n * 137.508) + 20) % 360 / 360
    for lightness in range(62, 20, -1):
        r, g, b = colorsys.hls_to_rgb(hue, lightness / 100, 0.72)
        col = '#%02x%02x%02x' % (round(r * 255), round(g * 255), round(b * 255))
        if _hex_luminance(col) <= 0.2:
            return col
    return col


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class Store:
    def __init__(self, path):
        self.path = path
        self._pool = queue.LifoQueue()
        self._all = []
        self._pool_lock = threading.Lock()
        self.fts = False            # FTS5 index usable in this run
        self.fts_error = None       # why not (logged)
        self.on_change = None       # callback after every committed write

    # -- connections -------------------------------------------------------

    def _connect(self):
        c = sqlite3.connect(self.path, timeout=15, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys = ON')
        c.execute('PRAGMA busy_timeout = 15000')
        return c

    @contextmanager
    def _conn(self):
        try:
            c = self._pool.get_nowait()
        except queue.Empty:
            c = self._connect()
            with self._pool_lock:
                self._all.append(c)
        try:
            yield c
        finally:
            if c.in_transaction:
                try:
                    c.execute('ROLLBACK')
                except sqlite3.Error:
                    pass
            self._pool.put(c)

    @contextmanager
    def read(self):
        """A read transaction: one consistent snapshot for several queries."""
        with self._conn() as c:
            c.execute('BEGIN')
            try:
                yield c
            finally:
                if c.in_transaction:
                    c.execute('COMMIT')

    @contextmanager
    def write(self):
        """A write transaction (takes the write lock up front)."""
        with self._conn() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                yield c
            except BaseException:
                if c.in_transaction:
                    c.execute('ROLLBACK')
                raise
            c.execute('COMMIT')
        if self.on_change:
            self.on_change()

    def close(self):
        with self._pool_lock:
            for c in self._all:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
            self._all.clear()
        while not self._pool.empty():
            self._pool.get_nowait()

    # -- startup -----------------------------------------------------------

    def open(self):
        """Prepare the database: WAL mode, migrations, search index."""
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        c = self._connect()
        try:
            self._ensure_wal(c)
            self._migrate(c)
            self._prepare_fts(c)
        finally:
            c.close()

    @staticmethod
    def _ensure_wal(c):
        mode = c.execute('PRAGMA journal_mode').fetchone()[0]
        if str(mode).lower() == 'wal':
            return
        # The switch needs an exclusive lock and SQLite does not reliably wait
        # for it, so retry briefly (short busy timeout per attempt, ~10 s total).
        c.execute('PRAGMA busy_timeout = 100')
        try:
            for _ in range(50):
                try:
                    mode = c.execute('PRAGMA journal_mode = WAL').fetchone()[0]
                    if str(mode).lower() == 'wal':
                        log.info('database switched to WAL mode')
                        return
                except sqlite3.OperationalError as e:
                    if 'locked' not in str(e) and 'busy' not in str(e):
                        raise
                time.sleep(0.1)
            log.warning('could not switch the database to WAL mode (still %s)', mode)
        finally:
            c.execute('PRAGMA busy_timeout = 15000')

    @staticmethod
    def _schema_gaps(c):
        """What the database lacks compared to the current schema."""
        names = {(r[0], r[1]) for r in c.execute("SELECT type, name FROM sqlite_master")}
        gaps = []
        for table, _ in TABLES:
            if ('table', table) not in names:
                gaps.append(('table', table))
        for table, column, _ in ADDED_COLUMNS:
            if ('table', table) in names:
                cols = {r[1] for r in c.execute(f'PRAGMA table_info({table})')}
                if column not in cols:
                    gaps.append(('column', f'{table}.{column}'))
        for name, _ in INDEXES:
            if ('index', name) not in names:
                gaps.append(('index', name))
        for name, _ in TRIGGERS:
            if ('trigger', name) not in names:
                gaps.append(('trigger', name))
        return gaps

    def _migrate(self, c):
        # Normal starts only read the schema; the write lock is taken only
        # when something is missing, and the check is repeated under the lock
        # because another process may have migrated in the meantime.
        if not self._schema_gaps(c):
            return
        c.execute('BEGIN IMMEDIATE')
        try:
            gaps = self._schema_gaps(c)
            if gaps:
                log.info('migrating database: %s', ', '.join(f'{k} {n}' for k, n in gaps))
                for _, ddl in TABLES:
                    c.execute(ddl)
                for table, column, definition in ADDED_COLUMNS:
                    cols = {r[1] for r in c.execute(f'PRAGMA table_info({table})')}
                    if column not in cols:
                        c.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
                for _, ddl in INDEXES:
                    c.execute(ddl)
                for _, ddl in TRIGGERS:
                    c.execute(ddl)
            c.execute('COMMIT')
        except BaseException:
            if c.in_transaction:
                c.execute('ROLLBACK')
            raise

    def _prepare_fts(self, c):
        self.fts = False
        if os.environ.get('TODOTRACKER_NO_FTS') == '1':
            self.fts_error = 'disabled by TODOTRACKER_NO_FTS'
            log.warning('full-text search disabled (TODOTRACKER_NO_FTS); using LIKE search')
            return
        try:
            c.execute('CREATE VIRTUAL TABLE temp.fts_probe USING fts5(x)')
            c.execute('DROP TABLE temp.fts_probe')
        except sqlite3.Error as e:
            self.fts_error = f'FTS5 unavailable: {e}'
            log.warning('SQLite FTS5 is not available (%s); using LIKE search', e)
            return
        try:
            reason = self._fts_stale(c)
            if reason:
                c.execute('BEGIN IMMEDIATE')
                try:
                    reason = self._fts_stale(c)
                    if reason:
                        log.info('rebuilding search index (%s)', reason)
                        self._fts_rebuild(c, recreate=(reason == 'columns'))
                    c.execute('COMMIT')
                except BaseException:
                    if c.in_transaction:
                        c.execute('ROLLBACK')
                    raise
            self.fts = True
            self.fts_error = None
        except sqlite3.Error as e:
            self.fts_error = f'search index error: {e}'
            log.exception('search index could not be prepared; using LIKE search')

    def _fts_stale(self, c):
        exists = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks_fts'").fetchone()
        if not exists:
            return 'columns'
        cols = [r[1] for r in c.execute('PRAGMA table_info(tasks_fts)')]
        if cols != FTS_COLUMNS:
            return 'columns'
        dirty = c.execute("SELECT value FROM meta WHERE key = 'fts_dirty'").fetchone()
        if dirty and dirty[0] == '1':
            return 'dirty'
        n_fts = c.execute('SELECT count(*) FROM tasks_fts').fetchone()[0]
        n_tasks = c.execute('SELECT count(*) FROM tasks').fetchone()[0]
        if n_fts != n_tasks:
            return 'count'
        return None

    def _fts_rebuild(self, c, recreate=False):
        if recreate:
            c.execute('DROP TABLE IF EXISTS tasks_fts')
            c.execute(FTS_CREATE)
        else:
            c.execute('DELETE FROM tasks_fts')
        for row in c.execute('SELECT id FROM tasks').fetchall():
            self._fts_insert(c, row[0])
        c.execute("DELETE FROM meta WHERE key = 'fts_dirty'")

    def _subtask_text(self, c, task_id):
        cols = 'title, notes' if self._has_notes(c) else "title, '' AS notes"
        return ' '.join(f'{r[0]} {r[1]}'.strip() for r in c.execute(
            f'SELECT {cols} FROM subtasks WHERE task_id = ? ORDER BY position, id', (task_id,)))

    @staticmethod
    def _has_notes(c):
        return any(r[1] == 'notes' for r in c.execute('PRAGMA table_info(subtasks)'))

    def _fts_insert(self, c, task_id):
        row = c.execute('SELECT title, description FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if row:
            c.execute('INSERT INTO tasks_fts(rowid, title, description, subtasks) VALUES (?, ?, ?, ?)',
                      (task_id, row['title'], row['description'], self._subtask_text(c, task_id)))

    def _fts_sync(self, c, task_id):
        """Keep the search index in step with a task (inside the write transaction)."""
        if not self.fts:
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('fts_dirty', '1')")
            return
        try:
            c.execute('DELETE FROM tasks_fts WHERE rowid = ?', (task_id,))
            self._fts_insert(c, task_id)
        except sqlite3.Error:
            log.exception('search index update failed; falling back to LIKE search until restart')
            self.fts = False
            self.fts_error = 'index update failed'
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('fts_dirty', '1')")

    # -- labels ------------------------------------------------------------

    @staticmethod
    def _next_color(c):
        used = {r[0].lower() for r in c.execute('SELECT color FROM labels')}
        for col in PALETTE:
            if col not in used:
                return col
        for n in range(2000):
            col = golden_color(n)
            if col not in used:
                return col
        return golden_color(len(used))

    def _label_id(self, c, name, create=True):
        name = clean_label_name(name)
        row = c.execute('SELECT id FROM labels WHERE name = ?', (name,)).fetchone()
        if row:
            return row[0]
        if not create:
            return None
        cur = c.execute('INSERT INTO labels(name, color, created_at) VALUES (?, ?, ?)',
                        (name, self._next_color(c), now_iso()))
        return cur.lastrowid

    def _set_task_labels(self, c, task_id, names):
        if not isinstance(names, list):
            raise ValidationError('Labels must be a list of names.')
        if len(names) > MAX_LABELS_PER_TASK:
            raise ValidationError(f'A task can have at most {MAX_LABELS_PER_TASK} labels.')
        wanted = []
        for name in names:
            lid = self._label_id(c, name)
            if lid not in wanted:
                wanted.append(lid)
        current = [r[0] for r in c.execute(
            'SELECT label_id FROM task_labels WHERE task_id = ? ORDER BY rowid', (task_id,))]
        if current == wanted:
            return False
        for lid in current:
            if lid not in wanted:
                c.execute('DELETE FROM task_labels WHERE task_id = ? AND label_id = ?', (task_id, lid))
        for lid in wanted:
            if lid not in current:
                c.execute('INSERT INTO task_labels(task_id, label_id) VALUES (?, ?)', (task_id, lid))
        return True

    def _labels_by_task(self, c, task_ids=None):
        sql = ('SELECT tl.task_id, l.id, l.name, l.color FROM task_labels tl '
               'JOIN labels l ON l.id = tl.label_id')
        params = ()
        if task_ids is not None:
            if not task_ids:
                return {}
            sql += f' WHERE tl.task_id IN ({",".join("?" * len(task_ids))})'
            params = tuple(task_ids)
        out = {}
        for r in c.execute(sql + ' ORDER BY tl.rowid', params):
            out.setdefault(r[0], []).append({'id': r[1], 'name': r[2], 'color': r[3]})
        return out

    def list_labels(self):
        with self.read() as c:
            return self._labels_with_counts(c)

    @staticmethod
    def _labels_with_counts(c):
        rows = c.execute('''
            SELECT l.id, l.name, l.color,
                   (SELECT count(*) FROM task_labels tl JOIN tasks t ON t.id = tl.task_id
                     WHERE tl.label_id = l.id AND t.status != 'done') AS open_count
              FROM labels l ORDER BY l.name COLLATE NOCASE''').fetchall()
        return [{'id': r[0], 'name': r[1], 'color': r[2], 'open': r[3]} for r in rows]

    def update_label(self, label_id, name=None, color=None):
        """Rename and/or recolour a label. Renaming onto an existing name merges."""
        with self.write() as c:
            row = c.execute('SELECT * FROM labels WHERE id = ?', (label_id,)).fetchone()
            if not row:
                raise NotFound('Label not found.')
            result_id = label_id
            if color is not None:
                c.execute('UPDATE labels SET color = ? WHERE id = ?', (clean_color(color), label_id))
            if name is not None:
                name = clean_label_name(name)
                other = c.execute('SELECT id FROM labels WHERE name = ? AND id != ?',
                                  (name, label_id)).fetchone()
                if other:
                    result_id = self._merge_labels(c, label_id, other[0])
                elif name != row['name']:
                    c.execute('UPDATE labels SET name = ? WHERE id = ?', (name, label_id))
            r = c.execute('SELECT id, name, color FROM labels WHERE id = ?', (result_id,)).fetchone()
            return {'id': r[0], 'name': r[1], 'color': r[2], 'merged': result_id != label_id}

    def _merge_labels(self, c, source_id, target_id):
        """Move everything from label source_id onto target_id and delete source."""
        c.execute('INSERT OR IGNORE INTO task_labels(task_id, label_id) '
                  'SELECT task_id, ? FROM task_labels WHERE label_id = ?', (target_id, source_id))
        self._merge_subtask_labels(c, source_id, target_id)
        c.execute('DELETE FROM labels WHERE id = ?', (source_id,))
        return target_id

    def _merge_subtask_labels(self, c, source_id, target_id):
        pass

    def delete_label(self, label_id):
        with self.write() as c:
            cur = c.execute('DELETE FROM labels WHERE id = ?', (label_id,))
            if cur.rowcount == 0:
                raise NotFound('Label not found.')

    # -- tasks -------------------------------------------------------------

    def create_task(self, title, description='', status='open', priority='medium',
                    due_at=None, labels=None):
        title = clean_title(title)
        description = clean_description(description or '')
        status = clean_status(status)
        priority = clean_priority(priority)
        due_at = clean_due(due_at)
        now = now_dt()
        stamp = now_iso(now)
        with self.write() as c:
            cur = c.execute(
                'INSERT INTO tasks(title, description, status, priority, created_at, updated_at, '
                'due_at, completed_at, reminded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (title, description, status, priority, stamp, stamp, due_at,
                 stamp if status == 'done' else None, rearm_value(due_at, now)))
            task_id = cur.lastrowid
            if labels:
                self._set_task_labels(c, task_id, labels)
            self._fts_sync(c, task_id)
            return self._task(c, task_id)

    def get_task(self, task_id):
        with self.read() as c:
            return self._task(c, task_id)

    def update_task(self, task_id, fields):
        now = now_dt()
        stamp = now_iso(now)
        with self.write() as c:
            row = c.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
            if not row:
                raise NotFound('Task not found.')
            sets = {}
            if 'title' in fields:
                title = clean_title(fields['title'])
                if title != row['title']:
                    sets['title'] = title
            if 'description' in fields:
                desc = clean_description(fields['description'])
                if desc != row['description']:
                    sets['description'] = desc
            if 'status' in fields:
                status = clean_status(fields['status'])
                if status != row['status']:
                    sets['status'] = status
                    if status == 'done':
                        sets['completed_at'] = stamp
                    elif row['status'] == 'done':
                        sets['completed_at'] = None
            if 'priority' in fields:
                prio = clean_priority(fields['priority'])
                if prio != row['priority']:
                    sets['priority'] = prio
            if 'due_at' in fields:
                due = clean_due(fields['due_at'])
                if due != row['due_at']:
                    sets['due_at'] = due
                    sets['reminded_at'] = rearm_value(due, now)
            changed = bool(sets)
            if 'labels' in fields:
                changed = self._set_task_labels(c, task_id, fields['labels']) or changed
            changed = self._update_task_extra(c, task_id, row, fields, sets, now) or changed
            if changed:
                sets['updated_at'] = stamp
                cols = ', '.join(f'{k} = ?' for k in sets)
                c.execute(f'UPDATE tasks SET {cols} WHERE id = ?', (*sets.values(), task_id))
                self._fts_sync(c, task_id)
            return self._task(c, task_id)

    def _update_task_extra(self, c, task_id, row, fields, sets, now):
        return False

    def delete_task(self, task_id):
        with self.write() as c:
            cur = c.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
            if cur.rowcount == 0:
                raise NotFound('Task not found.')
            if self.fts:
                try:
                    c.execute('DELETE FROM tasks_fts WHERE rowid = ?', (task_id,))
                except sqlite3.Error:
                    log.exception('search index delete failed')
                    self.fts = False
                    c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('fts_dirty', '1')")
            else:
                c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('fts_dirty', '1')")

    # -- subtasks ----------------------------------------------------------

    def _clean_subtask_item(self, item):
        """A new subtask: a title, or a dict (also used to restore a deleted one)."""
        if isinstance(item, str):
            item = {'title': item}
        if not isinstance(item, dict):
            raise ValidationError('Each subtask must be a title or an object.')
        allowed = self.SUBTASK_ITEM_KEYS
        for key in item:
            if key not in allowed:
                raise ValidationError(f'Unknown subtask field "{key}".')
        out = {'title': clean_title(item.get('title'), 'Subtask title')}
        done = item.get('done', False)
        if not isinstance(done, bool):
            raise ValidationError('Subtask "done" must be true or false.')
        out['done'] = done
        pos = item.get('position')
        if pos is not None and (not isinstance(pos, int) or isinstance(pos, bool) or pos < 0):
            raise ValidationError('Subtask position must be a whole number ≥ 0.')
        out['position'] = pos
        out['created_at'] = clean_stamp(item['created_at'], 'created_at') if item.get('created_at') else None
        out['completed_at'] = clean_stamp(item['completed_at'], 'completed_at') if item.get('completed_at') else None
        self._clean_subtask_item_extra(item, out)
        return out

    SUBTASK_ITEM_KEYS = ('title', 'done', 'position', 'created_at', 'completed_at')

    def _clean_subtask_item_extra(self, item, out):
        pass

    @staticmethod
    def _renumber(c, task_id):
        ids = [r[0] for r in c.execute(
            'SELECT id FROM subtasks WHERE task_id = ? ORDER BY position, id', (task_id,))]
        for pos, sid in enumerate(ids):
            c.execute('UPDATE subtasks SET position = ? WHERE id = ? AND position != ?', (pos, sid, pos))

    def add_subtasks(self, task_id, items):
        """Append subtasks (or insert them at their given positions).
        Returns (task, new ids)."""
        if not isinstance(items, list) or not items:
            raise ValidationError('No subtasks given.')
        if len(items) > MAX_SUBTASKS_PER_REQUEST:
            raise ValidationError(f'At most {MAX_SUBTASKS_PER_REQUEST} subtasks at once.')
        clean = [self._clean_subtask_item(it) for it in items]
        now = now_dt()
        stamp = now_iso(now)
        with self.write() as c:
            # Take the write lock with an UPDATE before reading positions, so
            # concurrent adds can never share a position (BEGIN IMMEDIATE
            # already holds it; the UPDATE also marks the task as edited).
            cur = c.execute('UPDATE tasks SET updated_at = ? WHERE id = ?', (stamp, task_id))
            if cur.rowcount == 0:
                raise NotFound('Task not found.')
            ids = []
            for it in clean:
                count = c.execute('SELECT count(*), coalesce(max(position) + 1, 0) FROM subtasks WHERE task_id = ?',
                                  (task_id,)).fetchone()
                end = max(count[0], count[1])
                pos = end if it['position'] is None else min(it['position'], end)
                if pos < end:
                    c.execute('UPDATE subtasks SET position = position + 1 WHERE task_id = ? AND position >= ?',
                              (task_id, pos))
                completed = (it['completed_at'] or stamp) if it['done'] else None
                cur = c.execute(
                    'INSERT INTO subtasks(task_id, title, done, position, created_at, completed_at) '
                    'VALUES (?, ?, ?, ?, ?, ?)',
                    (task_id, it['title'], int(it['done']), pos, it['created_at'] or stamp, completed))
                sid = cur.lastrowid
                self._add_subtask_extra(c, task_id, sid, it, now)
                ids.append(sid)
            self._renumber(c, task_id)
            self._fts_sync(c, task_id)
            return self._task(c, task_id), ids

    def _add_subtask_extra(self, c, task_id, sid, item, now):
        pass

    def _subtask_row(self, c, sub_id):
        row = c.execute('SELECT * FROM subtasks WHERE id = ?', (sub_id,)).fetchone()
        if not row:
            raise NotFound('Subtask not found.')
        return row

    def update_subtask(self, sub_id, fields):
        now = now_dt()
        stamp = now_iso(now)
        with self.write() as c:
            row = self._subtask_row(c, sub_id)
            sets = {}
            if 'title' in fields:
                title = clean_title(fields['title'], 'Subtask title')
                if title != row['title']:
                    sets['title'] = title
            if 'done' in fields:
                done = fields['done']
                if not isinstance(done, bool):
                    raise ValidationError('Subtask "done" must be true or false.')
                if done != bool(row['done']):
                    sets['done'] = int(done)
                    sets['completed_at'] = stamp if done else None
            changed = self._update_subtask_extra(c, row, fields, sets, now)
            if sets:
                cols = ', '.join(f'{k} = ?' for k in sets)
                c.execute(f'UPDATE subtasks SET {cols} WHERE id = ?', (*sets.values(), sub_id))
            if sets or changed:
                c.execute('UPDATE tasks SET updated_at = ? WHERE id = ?', (stamp, row['task_id']))
                self._fts_sync(c, row['task_id'])
            return self._task(c, row['task_id'])

    def _update_subtask_extra(self, c, row, fields, sets, now):
        return False

    def delete_subtask(self, sub_id):
        with self.write() as c:
            row = self._subtask_row(c, sub_id)
            c.execute('DELETE FROM subtasks WHERE id = ?', (sub_id,))
            self._renumber(c, row['task_id'])
            c.execute('UPDATE tasks SET updated_at = ? WHERE id = ?', (now_iso(), row['task_id']))
            self._fts_sync(c, row['task_id'])
            return self._task(c, row['task_id'])

    def reorder_subtasks(self, task_id, ids):
        """Put the listed subtasks in this order; unlisted ones (e.g. added
        meanwhile in another window) keep their order after them."""
        if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
            raise ValidationError('ids must be a list of subtask ids.')
        if len(set(ids)) != len(ids):
            raise ValidationError('A subtask is listed twice.')
        with self.write() as c:
            if not c.execute('SELECT 1 FROM tasks WHERE id = ?', (task_id,)).fetchone():
                raise NotFound('Task not found.')
            current = [r[0] for r in c.execute(
                'SELECT id FROM subtasks WHERE task_id = ? ORDER BY position, id', (task_id,))]
            foreign = [i for i in ids if i not in current]
            if foreign:
                raise ValidationError(f'Subtask {foreign[0]} does not belong to this task.')
            order = ids + [i for i in current if i not in ids]
            if order != current:
                for pos, sid in enumerate(order):
                    c.execute('UPDATE subtasks SET position = ? WHERE id = ?', (pos, sid))
                c.execute('UPDATE tasks SET updated_at = ? WHERE id = ?', (now_iso(), task_id))
                self._fts_sync(c, task_id)
            return self._task(c, task_id)

    # -- reading tasks -----------------------------------------------------

    def _task(self, c, task_id, full=True):
        row = c.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if not row:
            raise NotFound('Task not found.')
        labels = self._labels_by_task(c, [task_id]).get(task_id, [])
        extra = self._task_children(c, [task_id], full=full)
        return self._task_json(row, labels, extra.get(task_id), full=full, now=now_dt())

    def _task_children(self, c, task_ids, full=False):
        """Subtask rows per task id, in position order."""
        if not task_ids:
            return {}
        if len(task_ids) > 500:
            rows = c.execute('SELECT * FROM subtasks ORDER BY task_id, position, id').fetchall()
            wanted = set(task_ids)
            rows = [r for r in rows if r['task_id'] in wanted]
        else:
            rows = c.execute(f'SELECT * FROM subtasks WHERE task_id IN ({",".join("?" * len(task_ids))}) '
                             'ORDER BY task_id, position, id', tuple(task_ids)).fetchall()
        out = {}
        for r in rows:
            out.setdefault(r['task_id'], []).append(r)
        return out

    @staticmethod
    def _subtask_json(r, full=False):
        return {
            'id': r['id'],
            'task_id': r['task_id'],
            'title': r['title'],
            'done': bool(r['done']),
            'position': r['position'],
            'created_at': r['created_at'],
            'completed_at': r['completed_at'],
        }

    def _task_json(self, row, labels, children, full=False, now=None):
        desc = row['description'] or ''
        d = {
            'id': row['id'],
            'title': row['title'],
            'status': row['status'],
            'priority': row['priority'],
            'created_at': row['created_at'],
            'updated_at': row['updated_at'],
            'due_at': row['due_at'],
            'completed_at': row['completed_at'],
            'labels': labels,
            'snippet': snippet(desc),
            'images': image_count(desc),
        }
        if full:
            d['description'] = desc
        self._decorate(d, row, children, full)
        d['eff_due'] = self._effective_due(d)
        return d

    def _decorate(self, d, row, children, full):
        subs = [self._subtask_json(r, full) for r in children or ()]
        d['subtasks'] = subs
        d['progress'] = [sum(1 for s in subs if s['done']), len(subs)]

    @staticmethod
    def _relevant_dues(t):
        """Target dates that still matter for a task: its own (if not done)
        plus those of its open subtasks."""
        if t['status'] == 'done':
            return [t['due_at']] if t['due_at'] else []
        dues = [t['due_at']] if t['due_at'] else []
        for s in t.get('subtasks') or ():
            if not s.get('done') and s.get('due_at'):
                dues.append(s['due_at'])
        return dues

    def _effective_due(self, t):
        dues = self._relevant_dues(t)
        return min(dues) if dues else None

    def _in_view(self, view, t, now):
        if view == 'all':
            return True
        if view == 'done':
            return t['status'] == 'done'
        if t['status'] == 'done':
            return False
        if view == 'open':
            return True
        dues = self._relevant_dues(t)
        today = now.strftime('%Y-%m-%d')
        if view == 'overdue':
            return any(is_overdue(d, now) for d in dues)
        if view == 'today':
            return any(d[:10] == today for d in dues)
        if view == 'week':
            last = (now.date() + timedelta(days=6)).isoformat()
            return any(today <= d[:10] <= last for d in dues)
        if view == 'nodate':
            return not dues
        return True

    def _search_ids(self, c, words):
        if self.fts:
            expr = ' '.join('"' + w.replace('"', '""') + '"*' for w in words)
            try:
                return {r[0] for r in c.execute(
                    'SELECT rowid FROM tasks_fts WHERE tasks_fts MATCH ?', (expr,))}
            except sqlite3.Error:
                log.exception('full-text search failed; using LIKE search')
        return self._like_search_ids(c, words)

    def _like_search_ids(self, c, words):
        conds, params = [], []
        for w in words:
            pat = '%' + w.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            conds.append(self._like_condition())
            params.extend([pat] * self._like_condition().count('?'))
        sql = 'SELECT t.id FROM tasks t WHERE ' + ' AND '.join(conds)
        return {r[0] for r in c.execute(sql, params)}

    @staticmethod
    def _like_condition():
        return ("(t.title LIKE ? ESCAPE '\\' OR t.description LIKE ? ESCAPE '\\' OR EXISTS ("
                "SELECT 1 FROM subtasks s WHERE s.task_id = t.id AND s.title LIKE ? ESCAPE '\\'))")

    def list_tasks(self, view='open', label_ids=(), match='any', q='', sort='newest'):
        if view not in VIEWS:
            raise ValidationError(f'Unknown view "{view}".')
        if sort not in SORTS:
            raise ValidationError(f'Unknown sort "{sort}".')
        if match not in ('any', 'all'):
            raise ValidationError('match must be "any" or "all".')
        now = now_dt()
        words = search_words(q)
        with self.read() as c:
            if view == 'done':
                where = "WHERE status = 'done'"
            elif view == 'all':
                where = ''
            else:
                where = "WHERE status != 'done'"
            rows = c.execute(f'SELECT * FROM tasks {where}').fetchall()
            if words:
                hits = self._search_ids(c, words)
                rows = [r for r in rows if r['id'] in hits]
            labels = self._labels_by_task(c)
            if label_ids:
                wanted = set(label_ids)
                kept = []
                for r in rows:
                    have = {l['id'] for l in labels.get(r['id'], ())}
                    if (wanted <= have) if match == 'all' else (wanted & have):
                        kept.append(r)
                rows = kept
            ids = [r['id'] for r in rows]
            children = self._task_children(c, ids, full=False)
            tasks = []
            for r in rows:
                t = self._task_json(r, labels.get(r['id'], []), children.get(r['id']), now=now)
                if self._in_view(view, t, now):
                    self._mark_matches(t, children.get(r['id'], ()), words, label_ids, match)
                    tasks.append(t)
        self._sort(tasks, sort, view)
        return tasks

    def _mark_matches(self, t, rows, words, label_ids, match):
        """Which subtasks match a search, for showing them under a folded task."""
        if words:
            t['match_subs'] = [r['id'] for r in rows
                               if text_matches_any(words, r['title'] + ' ' + self._row_notes(r))]

    @staticmethod
    def _row_notes(r):
        return r['notes'] if 'notes' in r.keys() else ''

    @staticmethod
    def _sort(tasks, sort, view):
        def newest_key(t):
            stamp = t['completed_at'] if view == 'done' and t['completed_at'] else t['created_at']
            return (stamp, t['id'])

        if sort == 'newest':
            tasks.sort(key=newest_key, reverse=True)
            return
        tasks.sort(key=lambda t: (t['created_at'], t['id']), reverse=True)  # tie-break: newest first
        if sort == 'due':
            tasks.sort(key=lambda t: (t['eff_due'] is None, t['eff_due'] or '',
                                      PRIORITY_RANK.get(t['priority'], 1)))
        else:
            tasks.sort(key=lambda t: (PRIORITY_RANK.get(t['priority'], 1),
                                      t['eff_due'] is None, t['eff_due'] or ''))

    def meta(self):
        """Sidebar data: labels with open counts and the count of each view."""
        now = now_dt()
        with self.read() as c:
            rows = c.execute("SELECT * FROM tasks WHERE status != 'done'").fetchall()
            ids = [r['id'] for r in rows]
            children = self._task_children(c, ids, full=False)
            counts = {v: 0 for v in VIEWS}
            for r in rows:
                t = self._task_json(r, [], children.get(r['id']), now=now)
                for v in ('open', 'overdue', 'today', 'week', 'nodate'):
                    if self._in_view(v, t, now):
                        counts[v] += 1
                self._count_extra(counts, t, now)
            counts['done'] = c.execute("SELECT count(*) FROM tasks WHERE status = 'done'").fetchone()[0]
            counts['all'] = counts['open'] + counts['done']
            return {'labels': self._labels_with_counts(c), 'counts': counts,
                    'search': 'fts5' if self.fts else 'like'}

    def _count_extra(self, counts, t, now):
        pass

    # -- reminders ---------------------------------------------------------

    def due_reminders(self, now=None):
        """Claim the items whose target time has arrived and that have not been
        reminded yet. They are marked as reminded in the same transaction."""
        now = now or now_dt()
        current = now.strftime('%Y-%m-%dT%H:%M')
        stamp = now_iso(now)
        moment = "CASE WHEN length(due_at) = 10 THEN due_at || 'T{}' ELSE due_at END".format(
            REMIND_DATE_ONLY_AT)
        items = []
        with self.write() as c:
            for r in c.execute(
                    f"SELECT id, title, due_at FROM tasks WHERE status != 'done' AND due_at IS NOT NULL "
                    f"AND reminded_at IS NULL AND {moment} <= ? ORDER BY {moment}, id", (current,)):
                items.append({'kind': 'task', 'id': r['id'], 'title': r['title'], 'due_at': r['due_at']})
            items.extend(self._due_subtask_reminders(c, current))
            for it in items:
                table = 'tasks' if it['kind'] == 'task' else 'subtasks'
                c.execute(f'UPDATE {table} SET reminded_at = ? WHERE id = ?', (stamp, it['id']))
        return items

    def _due_subtask_reminders(self, c, current):
        return []

    # -- export ------------------------------------------------------------

    def export(self):
        with self.read() as c:
            labels = [dict(r) for r in c.execute(
                'SELECT id, name, color, created_at FROM labels ORDER BY id')]
            names = self._labels_by_task(c)
            tasks = []
            for r in c.execute('SELECT * FROM tasks ORDER BY id').fetchall():
                t = {k: r[k] for k in ('id', 'title', 'description', 'status', 'priority',
                                       'created_at', 'updated_at', 'due_at', 'completed_at',
                                       'reminded_at')}
                t['labels'] = [l['name'] for l in names.get(r['id'], [])]
                tasks.append(t)
            data = {'app': 'TodoTracker', 'format': 1, 'exported_at': now_iso(),
                    'labels': labels, 'tasks': tasks}
            self._export_extra(c, data)
            return data

    def _export_extra(self, c, data):
        children = self._task_children(c, [t['id'] for t in data['tasks']])
        for t in data['tasks']:
            t['subtasks'] = [self._subtask_export(r) for r in children.get(t['id'], ())]

    def _subtask_export(self, r):
        return {k: r[k] for k in ('id', 'title', 'position', 'created_at', 'completed_at')} | {
            'done': bool(r['done'])}

    def counts(self):
        with self.read() as c:
            return {
                'tasks': c.execute('SELECT count(*) FROM tasks').fetchone()[0],
                'labels': c.execute('SELECT count(*) FROM labels').fetchone()[0],
            }
