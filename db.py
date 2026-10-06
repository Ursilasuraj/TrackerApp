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
MAX_NOTES = 20_000
REMIND_DATE_ONLY_AT = '09:00'

DATE_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')
DATETIME_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$')
COLOR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')
KEY_RE = re.compile(r'^([ts])([1-9]\d{0,11})$')
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
        reminded_at TEXT,
        mx REAL,
        my REAL
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
        completed_at TEXT,
        due_at TEXT,
        reminded_at TEXT,
        notes TEXT NOT NULL DEFAULT '',
        mx REAL,
        my REAL
    )'''),
    ('subtask_labels', '''CREATE TABLE IF NOT EXISTS subtask_labels (
        subtask_id INTEGER NOT NULL REFERENCES subtasks(id) ON DELETE CASCADE,
        label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
        PRIMARY KEY (subtask_id, label_id)
    )'''),
    # "A before B": each end is a task or a subtask; links live only while
    # both items exist.
    ('dependencies', '''CREATE TABLE IF NOT EXISTS dependencies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        before_task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
        before_subtask_id INTEGER REFERENCES subtasks(id) ON DELETE CASCADE,
        after_task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
        after_subtask_id INTEGER REFERENCES subtasks(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        CHECK ((before_task_id IS NULL) != (before_subtask_id IS NULL)),
        CHECK ((after_task_id IS NULL) != (after_subtask_id IS NULL))
    )'''),
]

ADDED_COLUMNS = [
    # (table, column, definition)
    ('tasks', 'reminded_at', 'TEXT'),
    ('subtasks', 'due_at', 'TEXT'),
    ('subtasks', 'reminded_at', 'TEXT'),
    ('subtasks', 'notes', "TEXT NOT NULL DEFAULT ''"),
    ('tasks', 'mx', 'REAL'),
    ('tasks', 'my', 'REAL'),
    ('subtasks', 'mx', 'REAL'),
    ('subtasks', 'my', 'REAL'),
]

INDEXES = [
    ('idx_tasks_status', 'CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)'),
    ('idx_task_labels_label', 'CREATE INDEX IF NOT EXISTS idx_task_labels_label ON task_labels(label_id)'),
    ('idx_subtasks_task', 'CREATE INDEX IF NOT EXISTS idx_subtasks_task ON subtasks(task_id, position)'),
    ('idx_subtask_labels_label', 'CREATE INDEX IF NOT EXISTS idx_subtask_labels_label ON subtask_labels(label_id)'),
    ('idx_deps_unique', 'CREATE UNIQUE INDEX IF NOT EXISTS idx_deps_unique ON dependencies('
                        'ifnull(before_task_id, 0), ifnull(before_subtask_id, 0), '
                        'ifnull(after_task_id, 0), ifnull(after_subtask_id, 0))'),
    ('idx_deps_before_sub', 'CREATE INDEX IF NOT EXISTS idx_deps_before_sub ON dependencies(before_subtask_id)'),
    ('idx_deps_after_task', 'CREATE INDEX IF NOT EXISTS idx_deps_after_task ON dependencies(after_task_id)'),
    ('idx_deps_after_sub', 'CREATE INDEX IF NOT EXISTS idx_deps_after_sub ON dependencies(after_subtask_id)'),
]

# A subtask's labels are always a subset of its task's labels. The database
# enforces it: inserting another label fails, and taking a label off a task
# takes it off all of the task's subtasks.
_SUBSET_CHECK = '''NOT EXISTS (SELECT 1 FROM subtasks s JOIN task_labels tl ON tl.task_id = s.task_id
                   WHERE s.id = NEW.subtask_id AND tl.label_id = NEW.label_id)'''
TRIGGERS = [
    ('trg_subtask_labels_subset_insert',
     f'''CREATE TRIGGER IF NOT EXISTS trg_subtask_labels_subset_insert BEFORE INSERT ON subtask_labels
         WHEN {_SUBSET_CHECK}
         BEGIN SELECT RAISE(ABORT, 'A subtask can only have labels of its task'); END'''),
    ('trg_subtask_labels_subset_update',
     f'''CREATE TRIGGER IF NOT EXISTS trg_subtask_labels_subset_update BEFORE UPDATE ON subtask_labels
         WHEN {_SUBSET_CHECK}
         BEGIN SELECT RAISE(ABORT, 'A subtask can only have labels of its task'); END'''),
    ('trg_task_labels_cascade_subtasks',
     '''CREATE TRIGGER IF NOT EXISTS trg_task_labels_cascade_subtasks AFTER DELETE ON task_labels
        BEGIN
          DELETE FROM subtask_labels WHERE label_id = OLD.label_id
             AND subtask_id IN (SELECT id FROM subtasks WHERE task_id = OLD.task_id);
        END'''),
]

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


def clean_notes(value):
    if not isinstance(value, str):
        raise ValidationError('Notes must be text.')
    value = value.replace('\r\n', '\n').replace('\r', '\n').rstrip()
    if len(value) > MAX_NOTES:
        raise ValidationError(f'Notes are too long (at most {MAX_NOTES} characters).')
    return value


def first_line(text, limit=120):
    for line in (text or '').split('\n'):
        line = line.strip()
        if line:
            return line if len(line) <= limit else line[:limit - 1].rstrip() + '…'
    return ''


def parse_key(key):
    """'t12' -> ('t', 12) for a task, 's34' -> ('s', 34) for a subtask."""
    m = KEY_RE.match(key) if isinstance(key, str) else None
    if not m:
        raise ValidationError(f'Invalid item key {str(key)[:20]!r} (use t<id> or s<id>).')
    return m.group(1), int(m.group(2))


def clean_matrix(value):
    """None, or [urgency, importance] with both in 0..1."""
    if value is None:
        return None
    if (not isinstance(value, list) or len(value) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value)):
        raise ValidationError('A matrix position is [urgency, importance] with numbers from 0 to 1.')
    x, y = float(value[0]), float(value[1])
    if not (0 <= x <= 1 and 0 <= y <= 1):
        raise ValidationError('Matrix positions must lie between 0 and 1.')
    return [round(x, 4), round(y, 4)]


def in_do(matrix):
    return bool(matrix) and matrix[0] >= 0.5 and matrix[1] >= 0.5


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
# Import helpers (lenient: older exports may differ in details)
# ---------------------------------------------------------------------------

def _import_stamp(value):
    if not isinstance(value, str):
        return None
    v = value.strip().replace(' ', 'T', 1)
    m = re.match(r'^(\d{4}-\d{2}-\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2}))?)?', v)
    if not m:
        return None
    try:
        dt = datetime.fromisoformat(f'{m.group(1)}T{m.group(2) or "00"}:{m.group(3) or "00"}:{m.group(4) or "00"}')
    except ValueError:
        return None
    return dt.isoformat()


def _import_due(value):
    if not isinstance(value, str):
        return None
    v = value.strip().replace(' ', 'T', 1)
    for candidate in (v, v[:16], v[:10]):
        try:
            return clean_due(candidate)
        except ValidationError:
            continue
    return None


def _import_status(value, done=None):
    v = str(value or '').strip().lower().replace('-', '_').replace(' ', '_')
    if v in ('done', 'completed', 'complete', 'closed', 'finished') or done is True:
        return 'done'
    if v in ('in_progress', 'inprogress', 'progress', 'doing', 'started', 'active', 'wip'):
        return 'in_progress'
    return 'open'


def _import_priority(value):
    v = str(value or '').strip().lower()
    if v in ('high', 'h', '1', 'urgent', 'important'):
        return 'high'
    if v in ('low', 'l', '3'):
        return 'low'
    return 'medium'


def _import_label_name(value):
    if not isinstance(value, str):
        return None
    name = re.sub(r'[\s,#]+', '-', value.strip().lstrip('#')).strip('-')[:MAX_LABEL]
    return name or None


def _import_matrix(item):
    m = item.get('matrix')
    if m is None and item.get('mx') is not None and item.get('my') is not None:
        m = [item.get('mx'), item.get('my')]
    try:
        m = clean_matrix(m)
    except ValidationError:
        m = None
    return (m[0], m[1]) if m else (None, None)


def _import_dep_keys(d):
    if not isinstance(d, dict):
        return None, None
    if isinstance(d.get('before'), str) and isinstance(d.get('after'), str):
        return d['before'], d['after']
    before = f"t{d['before_task_id']}" if d.get('before_task_id') is not None else (
        f"s{d['before_subtask_id']}" if d.get('before_subtask_id') is not None else None)
    after = f"t{d['after_task_id']}" if d.get('after_task_id') is not None else (
        f"s{d['after_subtask_id']}" if d.get('after_subtask_id') is not None else None)
    return before, after


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

    def _subtask_label_ids(self, c, task_id, names):
        """Label ids for a subtask: only labels its task has. A subtask never
        creates labels."""
        if not isinstance(names, list):
            raise ValidationError('Labels must be a list of names.')
        task_labels = {r[1].lower(): r[0] for r in c.execute(
            'SELECT l.id, l.name FROM task_labels tl JOIN labels l ON l.id = tl.label_id WHERE tl.task_id = ?',
            (task_id,))}
        ids = []
        for name in names:
            if not isinstance(name, str):
                raise ValidationError('Labels must be a list of names.')
            key = name.strip().lstrip('#').lower()
            if key not in {k for k in task_labels}:
                raise ValidationError(f'Label "{name.strip()[:40]}" is not on the task; a subtask can only use its task\'s labels.')
            if task_labels[key] not in ids:
                ids.append(task_labels[key])
        return ids

    def _set_subtask_labels(self, c, sub_id, task_id, names):
        wanted = self._subtask_label_ids(c, task_id, names)
        current = [r[0] for r in c.execute(
            'SELECT label_id FROM subtask_labels WHERE subtask_id = ? ORDER BY rowid', (sub_id,))]
        if current == wanted:
            return False
        c.execute('DELETE FROM subtask_labels WHERE subtask_id = ?', (sub_id,))
        for lid in wanted:
            c.execute('INSERT INTO subtask_labels(subtask_id, label_id) VALUES (?, ?)', (sub_id, lid))
        return True

    def _labels_by_subtask(self, c, sub_ids):
        out = {}
        if not sub_ids:
            return out
        if len(sub_ids) > 500:
            rows = c.execute('SELECT sl.subtask_id, l.id, l.name, l.color FROM subtask_labels sl '
                             'JOIN labels l ON l.id = sl.label_id ORDER BY sl.rowid').fetchall()
            wanted = set(sub_ids)
            rows = [r for r in rows if r[0] in wanted]
        else:
            rows = c.execute('SELECT sl.subtask_id, l.id, l.name, l.color FROM subtask_labels sl '
                             f'JOIN labels l ON l.id = sl.label_id WHERE sl.subtask_id IN ({",".join("?" * len(sub_ids))}) '
                             'ORDER BY sl.rowid', tuple(sub_ids)).fetchall()
        for r in rows:
            out.setdefault(r[0], []).append({'id': r[1], 'name': r[2], 'color': r[3]})
        return out

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
        c.execute('INSERT OR IGNORE INTO subtask_labels(subtask_id, label_id) '
                  'SELECT subtask_id, ? FROM subtask_labels WHERE label_id = ?', (target_id, source_id))

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

    SUBTASK_ITEM_KEYS = ('title', 'done', 'position', 'created_at', 'completed_at',
                         'due_at', 'labels', 'notes', 'matrix')

    def _clean_subtask_item_extra(self, item, out):
        out['due_at'] = clean_due(item.get('due_at'))
        out['notes'] = clean_notes(item.get('notes') or '')
        out['matrix'] = clean_matrix(item.get('matrix'))
        labels = item.get('labels') or []
        if not isinstance(labels, list):
            raise ValidationError('Labels must be a list of names.')
        out['labels'] = labels

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
        if item.get('due_at') or item.get('notes'):
            c.execute('UPDATE subtasks SET due_at = ?, reminded_at = ?, notes = ? WHERE id = ?',
                      (item.get('due_at'), rearm_value(item.get('due_at'), now), item.get('notes') or '', sid))
        if item.get('labels'):
            self._set_subtask_labels(c, sid, task_id, item['labels'])
        if item.get('matrix'):
            c.execute('UPDATE subtasks SET mx = ?, my = ? WHERE id = ?', (*item['matrix'], sid))

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
        changed = False
        if 'due_at' in fields:
            due = clean_due(fields['due_at'])
            if due != row['due_at']:
                sets['due_at'] = due
                sets['reminded_at'] = rearm_value(due, now)
        if 'notes' in fields:
            notes = clean_notes(fields['notes'])
            if notes != row['notes']:
                sets['notes'] = notes
        if 'labels' in fields:
            changed = self._set_subtask_labels(c, row['id'], row['task_id'], fields['labels'])
        return changed

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

    # -- matrix ------------------------------------------------------------

    def set_matrix(self, items):
        """Place items on the matrix (or back in the tray with None). A task
        item may also carry the priority that follows from the placement.
        Returns the affected tasks."""
        if not isinstance(items, list) or not items:
            raise ValidationError('No items given.')
        if len(items) > 5000:
            raise ValidationError('Too many items at once.')
        clean = []
        for it in items:
            if not isinstance(it, dict):
                raise ValidationError('Each item must be an object.')
            for key in it:
                if key not in ('key', 'matrix', 'priority'):
                    raise ValidationError(f'Unknown field "{key}".')
            if 'matrix' not in it:
                raise ValidationError('Missing field "matrix".')
            kind, item_id = parse_key(it.get('key'))
            prio = it.get('priority')
            if prio is not None:
                if kind != 't':
                    raise ValidationError('Subtasks have no priority.')
                prio = clean_priority(prio)
            clean.append((kind, item_id, clean_matrix(it['matrix']), prio))
        stamp = now_iso()
        with self.write() as c:
            touched = []
            for kind, item_id, pos, prio in clean:
                table = 'tasks' if kind == 't' else 'subtasks'
                x, y = pos if pos else (None, None)
                cur = c.execute(f'UPDATE {table} SET mx = ?, my = ? WHERE id = ?', (x, y, item_id))
                if cur.rowcount == 0:
                    raise NotFound(f'{"Task" if kind == "t" else "Subtask"} {item_id} not found.')
                task_id = item_id if kind == 't' else c.execute(
                    'SELECT task_id FROM subtasks WHERE id = ?', (item_id,)).fetchone()[0]
                if prio:
                    c.execute('UPDATE tasks SET priority = ?, updated_at = ? WHERE id = ? AND priority != ?',
                              (prio, stamp, task_id, prio))
                if task_id not in touched:
                    touched.append(task_id)
            return [self._task(c, tid, full=False) for tid in touched]

    # -- dependencies ------------------------------------------------------

    @staticmethod
    def _deps(c):
        """All links, with the title and done state of both ends (a subtask
        counts as done when it or its task is done)."""
        out = []
        sql = '''
            SELECT d.id, d.created_at,
                   d.before_task_id, d.before_subtask_id, d.after_task_id, d.after_subtask_id,
                   coalesce(bt.title, bs.title) AS before_title,
                   CASE WHEN bt.id IS NOT NULL THEN bt.status = 'done'
                        ELSE (bs.done = 1 OR bst.status = 'done') END AS before_done,
                   coalesce(at.title, asb.title) AS after_title,
                   CASE WHEN at.id IS NOT NULL THEN at.status = 'done'
                        ELSE (asb.done = 1 OR ast.status = 'done') END AS after_done
              FROM dependencies d
              LEFT JOIN tasks bt ON bt.id = d.before_task_id
              LEFT JOIN subtasks bs ON bs.id = d.before_subtask_id
              LEFT JOIN tasks bst ON bst.id = bs.task_id
              LEFT JOIN tasks at ON at.id = d.after_task_id
              LEFT JOIN subtasks asb ON asb.id = d.after_subtask_id
              LEFT JOIN tasks ast ON ast.id = asb.task_id
             ORDER BY d.id'''
        for r in c.execute(sql):
            before = f"t{r['before_task_id']}" if r['before_task_id'] is not None else f"s{r['before_subtask_id']}"
            after = f"t{r['after_task_id']}" if r['after_task_id'] is not None else f"s{r['after_subtask_id']}"
            out.append({'id': r['id'], 'before': before, 'after': after, 'created_at': r['created_at'],
                        'before_title': r['before_title'], 'after_title': r['after_title'],
                        'before_done': bool(r['before_done']), 'after_done': bool(r['after_done'])})
        return out

    def list_deps(self):
        with self.read() as c:
            return self._deps(c)

    @staticmethod
    def _item_title(c, key):
        kind, item_id = parse_key(key)
        table = 'tasks' if kind == 't' else 'subtasks'
        row = c.execute(f'SELECT title FROM {table} WHERE id = ?', (item_id,)).fetchone()
        if not row:
            raise NotFound(f'{"Task" if kind == "t" else "Subtask"} {item_id} not found.')
        return row[0]

    def add_dep(self, before, after):
        """'before' must be finished before 'after'. Self-links and loops are
        refused; a duplicate link is a no-op. Returns (id, created, all deps)."""
        bkind, bid = parse_key(before)
        akind, aid = parse_key(after)
        if before == after:
            raise ValidationError('An item cannot wait for itself.')
        with self.write() as c:
            before_title = self._item_title(c, before)
            after_title = self._item_title(c, after)
            deps = self._deps(c)
            for d in deps:
                if d['before'] == before and d['after'] == after:
                    return d['id'], False, deps
            following = {}
            for d in deps:
                following.setdefault(d['before'], []).append(d['after'])
            stack, seen = [after], set()
            while stack:
                node = stack.pop()
                if node == before:
                    raise ValidationError(f'“{after_title}” already comes before “{before_title}”, '
                                          'so this link would make a loop.')
                if node in seen:
                    continue
                seen.add(node)
                stack.extend(following.get(node, ()))
            cur = c.execute(
                'INSERT INTO dependencies(before_task_id, before_subtask_id, after_task_id, after_subtask_id, created_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (bid if bkind == 't' else None, bid if bkind == 's' else None,
                 aid if akind == 't' else None, aid if akind == 's' else None, now_iso()))
            return cur.lastrowid, True, self._deps(c)

    def delete_dep(self, dep_id):
        with self.write() as c:
            if c.execute('DELETE FROM dependencies WHERE id = ?', (dep_id,)).rowcount == 0:
                raise NotFound('Link not found.')
            return self._deps(c)

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
        labels = self._labels_by_subtask(c, [r['id'] for r in rows])
        out = {}
        for r in rows:
            d = dict(r)
            d['labels'] = labels.get(r['id'], [])
            out.setdefault(r['task_id'], []).append(d)
        return out

    @staticmethod
    def _subtask_json(r, full=False):
        d = {
            'id': r['id'],
            'task_id': r['task_id'],
            'title': r['title'],
            'done': bool(r['done']),
            'position': r['position'],
            'created_at': r['created_at'],
            'completed_at': r['completed_at'],
            'due_at': r['due_at'],
            'labels': r['labels'],
            'note1': first_line(r['notes']),
            'has_notes': bool(r['notes']),
            'matrix': [r['mx'], r['my']] if r['mx'] is not None and r['my'] is not None else None,
        }
        if full:
            d['notes'] = r['notes']
        return d

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
            'matrix': [row['mx'], row['my']] if row['mx'] is not None and row['my'] is not None else None,
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
                "SELECT 1 FROM subtasks s WHERE s.task_id = t.id AND (s.title LIKE ? ESCAPE '\\'"
                " OR s.notes LIKE ? ESCAPE '\\')))")

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
        """Which subtasks match the search and/or the label filter (by their
        own labels), for showing them under a folded task."""
        if not words and not label_ids:
            return
        wanted = set(label_ids or ())
        if words:
            t['text_subs'] = [r['id'] for r in rows
                              if text_matches_any(words, r['title'] + ' ' + (r['notes'] or ''))]
        hits = []
        for r in rows:
            if words and not text_matches_any(words, r['title'] + ' ' + (r['notes'] or '')):
                continue
            if wanted:
                own = {l['id'] for l in r['labels']}
                if not ((wanted <= own) if match == 'all' else (wanted & own)):
                    continue
            hits.append(r['id'])
        t['match_subs'] = hits

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
            counts['do'] = 0
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
        if in_do(t['matrix']):
            counts['do'] += 1
        for s in t['subtasks']:
            if not s['done'] and in_do(s['matrix']):
                counts['do'] += 1

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
        moment = "CASE WHEN length(s.due_at) = 10 THEN s.due_at || 'T{}' ELSE s.due_at END".format(REMIND_DATE_ONLY_AT)
        return [{'kind': 'subtask', 'id': r[0], 'title': r[1], 'due_at': r[2], 'task_id': r[3], 'task_title': r[4]}
                for r in c.execute(
                    f"SELECT s.id, s.title, s.due_at, t.id, t.title FROM subtasks s JOIN tasks t ON t.id = s.task_id "
                    f"WHERE s.done = 0 AND t.status != 'done' AND s.due_at IS NOT NULL AND s.reminded_at IS NULL "
                    f"AND {moment} <= ? ORDER BY {moment}, s.id", (current,))]

    # -- import ------------------------------------------------------------

    def import_data(self, data):
        """Add the contents of an Export JSON file. Old ids are mapped to new
        ones; a task whose title and created time already exist is skipped
        (so importing the same file twice adds nothing). Lenient about the
        exact shape, since older versions may have written it differently."""
        if not isinstance(data, dict) or not isinstance(data.get('tasks'), list):
            raise ValidationError('This file is not a TodoTracker export (no task list found).')
        now = now_dt()
        stamp = now_iso(now)
        summary = {'tasks': 0, 'skipped': 0, 'invalid': 0, 'subtasks': 0, 'labels': 0,
                   'dependencies': 0, 'dependencies_skipped': 0}
        key_map = {}
        with self.write() as c:
            colors = {}
            for l in data.get('labels') or []:
                if isinstance(l, dict) and isinstance(l.get('name'), str):
                    name = _import_label_name(l['name'])
                    color = l.get('color')
                    if name and isinstance(color, str) and COLOR_RE.match(color):
                        colors[name.lower()] = color.lower()
            label_ids = {}

            def label_id(raw):
                name = _import_label_name(raw.get('name') if isinstance(raw, dict) else raw)
                if not name:
                    return None
                if name.lower() in label_ids:
                    return label_ids[name.lower()]
                row = c.execute('SELECT id FROM labels WHERE name = ?', (name,)).fetchone()
                if row:
                    lid = row[0]
                else:
                    color = colors.get(name.lower())
                    used = {r[0].lower() for r in c.execute('SELECT color FROM labels')}
                    if not color or color in used:
                        color = self._next_color(c)
                    lid = c.execute('INSERT INTO labels(name, color, created_at) VALUES (?, ?, ?)',
                                    (name, color, stamp)).lastrowid
                    summary['labels'] += 1
                label_ids[name.lower()] = lid
                return lid

            for t in data['tasks']:
                if not isinstance(t, dict):
                    summary['invalid'] += 1
                    continue
                try:
                    title = clean_title(str(t.get('title') or ''))
                except ValidationError:
                    summary['invalid'] += 1
                    continue
                created = _import_stamp(t.get('created_at')) or stamp
                old_id = t.get('id')
                subs_in = [x for x in (t.get('subtasks') or []) if isinstance(x, dict)]
                existing = c.execute('SELECT id FROM tasks WHERE title = ? AND created_at = ?',
                                     (title, created)).fetchone()
                if existing:
                    summary['skipped'] += 1
                    if old_id is not None:
                        key_map[f't{old_id}'] = f't{existing[0]}'
                    for sub in subs_in:
                        row = c.execute('SELECT id FROM subtasks WHERE task_id = ? AND title = ? AND created_at = ?',
                                        (existing[0], str(sub.get('title') or '').strip(),
                                         _import_stamp(sub.get('created_at')) or '')).fetchone()
                        if row and sub.get('id') is not None:
                            key_map[f"s{sub['id']}"] = f's{row[0]}'
                    continue
                status = _import_status(t.get('status'), t.get('done'))
                due = _import_due(t.get('due_at') or t.get('due'))
                completed = _import_stamp(t.get('completed_at')) or (stamp if status == 'done' else None)
                mx, my = _import_matrix(t)
                reminded = _import_stamp(t.get('reminded_at')) or rearm_value(due, now)
                desc = t.get('description') if isinstance(t.get('description'), str) else ''
                try:
                    desc = clean_description(desc)
                except ValidationError:
                    desc = desc[:MAX_DESCRIPTION]
                task_id = c.execute(
                    'INSERT INTO tasks(title, description, status, priority, created_at, updated_at, due_at, '
                    'completed_at, reminded_at, mx, my) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (title, desc, status, _import_priority(t.get('priority')), created,
                     _import_stamp(t.get('updated_at')) or created, due,
                     completed if status == 'done' else None, reminded, mx, my)).lastrowid
                summary['tasks'] += 1
                if old_id is not None:
                    key_map[f't{old_id}'] = f't{task_id}'
                task_labels = []
                for raw in (t.get('labels') or [])[:MAX_LABELS_PER_TASK]:
                    lid = label_id(raw)
                    if lid and lid not in task_labels:
                        task_labels.append(lid)
                        c.execute('INSERT INTO task_labels(task_id, label_id) VALUES (?, ?)', (task_id, lid))
                subs_in.sort(key=lambda x: x.get('position') if isinstance(x.get('position'), int) else 10**9)
                for pos, sub in enumerate(subs_in):
                    try:
                        sub_title = clean_title(str(sub.get('title') or ''), 'Subtask title')
                    except ValidationError:
                        summary['invalid'] += 1
                        continue
                    done = bool(sub.get('done'))
                    s_created = _import_stamp(sub.get('created_at')) or created
                    s_due = _import_due(sub.get('due_at') or sub.get('due'))
                    notes = sub.get('notes') if isinstance(sub.get('notes'), str) else ''
                    notes = notes.replace('\r\n', '\n').replace('\r', '\n').rstrip()[:MAX_NOTES]
                    smx, smy = _import_matrix(sub)
                    sid = c.execute(
                        'INSERT INTO subtasks(task_id, title, done, position, created_at, completed_at, due_at, '
                        'reminded_at, notes, mx, my) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        (task_id, sub_title, int(done), pos, s_created,
                         (_import_stamp(sub.get('completed_at')) or stamp) if done else None, s_due,
                         _import_stamp(sub.get('reminded_at')) or rearm_value(s_due, now), notes, smx, smy)).lastrowid
                    summary['subtasks'] += 1
                    if sub.get('id') is not None:
                        key_map[f"s{sub['id']}"] = f's{sid}'
                    for raw in sub.get('labels') or []:
                        lid = label_id(raw)
                        if lid in task_labels:      # only the task's own labels
                            c.execute('INSERT OR IGNORE INTO subtask_labels(subtask_id, label_id) VALUES (?, ?)',
                                      (sid, lid))
                self._fts_sync(c, task_id)

            existing_deps = {(d['before'], d['after']) for d in self._deps(c)}
            following = {}
            for b, a in existing_deps:
                following.setdefault(b, []).append(a)
            for d in data.get('dependencies') or data.get('deps') or []:
                before, after = _import_dep_keys(d)
                before, after = key_map.get(before), key_map.get(after)
                if not before or not after or before == after or (before, after) in existing_deps:
                    summary['dependencies_skipped'] += 1
                    continue
                stack, seen, loop = [after], set(), False
                while stack:
                    node = stack.pop()
                    if node == before:
                        loop = True
                        break
                    if node not in seen:
                        seen.add(node)
                        stack.extend(following.get(node, ()))
                if loop:
                    summary['dependencies_skipped'] += 1
                    continue
                bk, bid = parse_key(before)
                ak, aid = parse_key(after)
                c.execute('INSERT INTO dependencies(before_task_id, before_subtask_id, after_task_id, '
                          'after_subtask_id, created_at) VALUES (?, ?, ?, ?, ?)',
                          (bid if bk == 't' else None, bid if bk == 's' else None,
                           aid if ak == 't' else None, aid if ak == 's' else None,
                           _import_stamp(d.get('created_at') if isinstance(d, dict) else None) or stamp))
                existing_deps.add((before, after))
                following.setdefault(before, []).append(after)
                summary['dependencies'] += 1
        return summary

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
                t['matrix'] = [r['mx'], r['my']] if r['mx'] is not None and r['my'] is not None else None
                tasks.append(t)
            data = {'app': 'TodoTracker', 'format': 1, 'exported_at': now_iso(),
                    'labels': labels, 'tasks': tasks}
            self._export_extra(c, data)
            return data

    def _export_extra(self, c, data):
        children = self._task_children(c, [t['id'] for t in data['tasks']])
        for t in data['tasks']:
            t['subtasks'] = [self._subtask_export(r) for r in children.get(t['id'], ())]
        data['dependencies'] = [{k: d[k] for k in ('id', 'before', 'after', 'created_at')} for d in self._deps(c)]

    def _subtask_export(self, r):
        return {k: r[k] for k in ('id', 'title', 'position', 'created_at', 'completed_at', 'due_at',
                                  'reminded_at', 'notes')} | {
            'done': bool(r['done']), 'labels': [l['name'] for l in r['labels']],
            'matrix': [r['mx'], r['my']] if r['mx'] is not None and r['my'] is not None else None}

    def counts(self):
        with self.read() as c:
            return {table: c.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                    for table in ('tasks', 'labels', 'subtasks', 'dependencies')}
