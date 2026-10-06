"""Daily database backups using SQLite's online backup API.

At start and then hourly, data/backups/todo-YYYY-MM-DD.db is written if it
does not exist yet; the newest 14 files are kept. "Back up now" forces one.
"""

import logging
import os
import re
import sqlite3
import threading
from datetime import date

log = logging.getLogger('todotracker.backup')

KEEP = 14
INTERVAL = 3600
NAME_RE = re.compile(r'^todo-\d{4}-\d{2}-\d{2}\.db$')


def backup_path(backups_dir, day=None):
    return os.path.join(backups_dir, f'todo-{(day or date.today()).isoformat()}.db')


def copy_database(src_path, dst_path):
    """Consistent copy of a live database (works while it is being written)."""
    tmp = dst_path + '.part'
    if os.path.exists(tmp):
        os.remove(tmp)
    src = sqlite3.connect(src_path, timeout=15)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.replace(tmp, dst_path)


def run_backup(db_path, backups_dir, force=False, day=None, keep=KEEP):
    """Write today's backup if missing (or always, with force). Returns its path."""
    os.makedirs(backups_dir, exist_ok=True)
    target = backup_path(backups_dir, day)
    if os.path.exists(target) and not force:
        return target
    copy_database(db_path, target)
    log.info('backup written: %s', os.path.basename(target))
    prune(backups_dir, keep)
    return target


def prune(backups_dir, keep=KEEP):
    names = sorted((n for n in os.listdir(backups_dir) if NAME_RE.match(n)), reverse=True)
    for name in names[keep:]:
        try:
            os.remove(os.path.join(backups_dir, name))
            log.info('old backup removed: %s', name)
        except OSError as e:
            log.warning('could not remove old backup %s: %s', name, e)


class BackupThread(threading.Thread):
    def __init__(self, db_path, backups_dir, stop_event, interval=INTERVAL):
        super().__init__(name='backup', daemon=True)
        self.db_path = db_path
        self.backups_dir = backups_dir
        self.stop_event = stop_event
        self.interval = interval

    def run(self):
        while not self.stop_event.is_set():
            try:
                run_backup(self.db_path, self.backups_dir)
            except Exception:
                log.exception('backup failed')
            if self.stop_event.wait(self.interval):
                return
