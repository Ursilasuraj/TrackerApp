"""Reminders: a Windows toast when a task's (or subtask's) target time arrives.

Every 60 seconds (first check 20 s after start) the not-done items whose
target time has arrived and that were not reminded yet are claimed and shown.
Date-only targets remind at 09:00. More than three at once become a single
summary toast.
"""

import base64
import logging
import os
import subprocess
import threading
from datetime import date, timedelta

import db as dbmod

log = logging.getLogger('todotracker.reminders')

FIRST_CHECK_DELAY = 20
INTERVAL = 60
MAX_SINGLE = 3
APP_ID = r'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
CREATE_NO_WINDOW = 0x08000000
WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

# Texts arrive through environment variables, so nothing typed into a task
# title can change the script.
TOAST_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $xml.GetElementsByTagName('text')
$texts.Item(0).AppendChild($xml.CreateTextNode($env:TT_TOAST_TITLE)) | Out-Null
$texts.Item(1).AppendChild($xml.CreateTextNode($env:TT_TOAST_BODY)) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:TT_TOAST_APPID).Show($toast)
'''


def show_toast(title, body):
    """Show a Windows toast through a hidden powershell.exe (WinRT API)."""
    title, body = title[:200], body[:400]
    if os.name != 'nt':
        log.info('toast: %s | %s', title, body)
        return True
    exe = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                       'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
    env = dict(os.environ, TT_TOAST_TITLE=title, TT_TOAST_BODY=body, TT_TOAST_APPID=APP_ID)
    encoded = base64.b64encode(TOAST_SCRIPT.encode('utf-16-le')).decode('ascii')
    try:
        result = subprocess.run(
            [exe, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
             '-WindowStyle', 'Hidden', '-EncodedCommand', encoded],
            env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
            creationflags=CREATE_NO_WINDOW)
    except Exception:
        log.exception('toast could not be shown')
        return False
    if result.returncode != 0:
        log.warning('toast failed (%s): %s', result.returncode,
                    result.stderr.decode('utf-8', 'replace')[:600])
        return False
    return True


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
