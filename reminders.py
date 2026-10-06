"""Reminders: a desktop notification when a task's (or subtask's) target time arrives.

Every 60 seconds (first check 20 s after start) the not-done items whose
target time has arrived and that were not reminded yet are claimed and shown.
Date-only targets remind at 09:00. More than three at once become a single
summary notification. How a notification is shown depends on the OS
(platforms.notify).
"""

import logging
import threading
from datetime import date, timedelta

import db as dbmod
import platforms

log = logging.getLogger('todotracker.reminders')

FIRST_CHECK_DELAY = 20
INTERVAL = 60
MAX_SINGLE = 3
WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def show_toast(title, body):
    return platforms.notify(title, body)


def describe_due(due, now):
    day = date.fromisoformat(due[:10])
    today = now.date()
    if day == today:
        text = 'today'
    elif day == today - timedelta(days=1):
        text = 'yesterday'
    elif day == today + timedelta(days=1):
        text = 'tomorrow'
    else:
        text = f'{WEEKDAYS[day.weekday()]} {day.day} {MONTHS[day.month - 1]}'
        if day.year != today.year:
            text += f' {day.year}'
    if len(due) > 10:
        text += f' {due[11:16]}'
    return text


def compose(items, now):
    """(title, body) pairs for the toasts to show."""
    if len(items) > MAX_SINGLE:
        names = [it['title'] for it in items[:5]]
        body = ' · '.join(names) + (f' … and {len(items) - 5} more' if len(items) > 5 else '')
        return [(f'{len(items)} TodoTracker items are due', body)]
    toasts = []
    for it in items:
        when = describe_due(it['due_at'], now)
        if it['kind'] == 'subtask':
            toasts.append((it['title'], f'Subtask of “{it["task_title"]}” · due {when}'))
        else:
            toasts.append((it['title'], f'Due {when}'))
    return toasts


def check_once(store, notify=show_toast, now=None):
    now = now or dbmod.now_dt()
    items = store.due_reminders(now)
    if items:
        log.info('reminding %d item(s)', len(items))
        for title, body in compose(items, now):
            notify(title, body)
    return items


class ReminderThread(threading.Thread):
    def __init__(self, store, stop_event, notify=show_toast,
                 delay=FIRST_CHECK_DELAY, interval=INTERVAL):
        super().__init__(name='reminders', daemon=True)
        self.store = store
        self.stop_event = stop_event
        self.notify = notify
        self.delay = delay
        self.interval = interval

    def run(self):
        if self.stop_event.wait(self.delay):
            return
        while True:
            try:
                check_once(self.store, self.notify)
            except Exception:
                log.exception('reminder check failed')
            if self.stop_event.wait(self.interval):
                return
