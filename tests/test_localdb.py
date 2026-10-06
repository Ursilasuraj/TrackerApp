"""Conformance: web/localdb.js (the phone's on-device data) answers like the server.

Run:  python -m unittest tests.test_localdb -v      (needs Node 18+)
Every scenario sends the same requests, with the same pinned clock, to the
Python server (in this process, scratch database) and to localdb.js (in
Node) and compares status and JSON of every answer. A random walk adds a
few thousand mixed valid and invalid requests.
"""

import base64
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import unittest
from datetime import datetime, timedelta
from unittest import mock
from urllib.parse import quote_plus

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import db  # noqa: E402
import server  # noqa: E402
from helpers import free_port, request, scratch_dir  # noqa: E402

NODE = shutil.which('node')
START = datetime(2026, 10, 6, 15, 0, 0)     # a Tuesday
PNG = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89'
       b'\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x8b\x8a\x86\x00\x00\x00\x00IEND\xaeB`\x82')


class PythonSide:
    def __init__(self, clock):
        self.dir = scratch_dir('tt-conf-')
        self.store = db.Store(os.path.join(self.dir, 'todo.db'))
        self.store.open()
        self.port = free_port()
        self.app = server.App(store=self.store, app_dir=os.path.dirname(HERE), data_dir=self.dir,
                              build='test', port=self.port)
        self.httpd = server.Server(self.port, self.app)
        self.app.httpd = self.httpd
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        self.thread.start()
        self.clock = clock

    def request(self, method, path, body):
        if isinstance(body, dict) and '$bytes' in body:
            st, data, _ = request(self.port, method, path, raw=base64.b64decode(body['$bytes']),
                                  headers={'Content-Type': 'application/octet-stream'})
        else:
            st, data, _ = request(self.port, method, path, body)
        return {'status': st, 'data': data}

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)


class NodeSide:
    def __init__(self):
        self.proc = subprocess.Popen([NODE, os.path.join(HERE, 'localdb_runner.js')], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True, encoding='utf-8', bufsize=1)

    def send(self, cmd):
        self.proc.stdin.write(json.dumps(cmd, ensure_ascii=False) + '\n')
        self.proc.stdin.flush()
        out = json.loads(self.proc.stdout.readline())
        if 'crash' in out:
            raise AssertionError('localdb.js crashed:\n' + out['crash'])
        return out

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(10)


def normalize(method, path, resp):
    data = resp['data']
    if path.startswith('/api/meta') and isinstance(data, dict):
        data = dict(data)
        data.pop('search', None)
    if path == '/api/images' and resp['status'] == 201:
        data = {'url': data['url'][:8] + '<name>.' + data['url'].rsplit('.', 1)[1], 'size': data['size']}
    return {'status': resp['status'], 'data': data}


@unittest.skipUnless(NODE, 'Node is needed to run localdb.js')
class Conformance(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.now = START
        patcher = mock.patch.object(db, 'now_dt', lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.py = PythonSide(lambda: self.now)
        self.addCleanup(self.py.close)
        self.js = NodeSide()
        self.addCleanup(self.js.close)
        self.steps = []

    def stamp(self):
        return self.now.strftime('%Y-%m-%dT%H:%M:%S')

    def req(self, method, path, body=None, tick=1):
        """Send to both, compare, return the (Python) data."""
        self.now += timedelta(seconds=tick)
        self.steps.append((method, path, body))
        a = normalize(method, path, self.py.request(method, path, body))
        b = normalize(method, path, self.js.send({'op': 'request', 'method': method, 'path': path, 'body': body,
                                                   'now': self.stamp()}))
        if a != b:
            self.fail(f'step {len(self.steps)}: {method} {path} {json.dumps(body, ensure_ascii=False)[:300]}\n'
                      f'python: {json.dumps(a, ensure_ascii=False)[:3000]}\n'
                      f'local:  {json.dumps(b, ensure_ascii=False)[:3000]}')
        return a['data']

    def everything(self):
        """Compare all reads (lists in every view and sort, meta, deps, export)."""
        for view in db.VIEWS:
            for sort in db.SORTS:
                self.req('GET', f'/api/tasks?view={view}&sort={sort}', tick=0)
        self.req('GET', '/api/meta', tick=0)
        self.req('GET', '/api/deps', tick=0)
        data = self.req('GET', '/api/export', tick=0)
        for t in data['tasks']:
            self.req('GET', f'/api/tasks/{t["id"]}', tick=0)

    def reminders(self, now):
        self.now = now
        a = self.py.store.due_reminders(now)
        b = self.js.send({'op': 'reminders', 'now': now.strftime('%Y-%m-%dT%H:%M:%S')})['items']
        self.assertEqual(a, b, f'reminders at {now}')
        return a

    # ------------------------------------------------------------- scenarios

    def test_tasks_views_sorts_and_validation(self):
        today = START.strftime('%Y-%m-%d')
        tasks = [
            {'title': '  Pay   the rent ', 'priority': 'high', 'due_at': '2026-10-01', 'labels': ['home', '#Money']},
            {'title': 'Call mum', 'due_at': today, 'labels': ['family', 'HOME']},
            {'title': 'Plan trip', 'due_at': f'{today}T18:30', 'description': '# Ideas\n- **Rome** ![x](/images/ab.png)\n'},
            {'title': 'Someday', 'priority': 'low'},
            {'title': 'Next week', 'due_at': '2026-10-12', 'status': 'in_progress'},
            {'title': 'Done thing', 'status': 'done', 'due_at': '2026-10-02'},
            {'title': 'Café crème', 'description': 'Über naïve résumé'},
        ]
        for t in tasks:
            self.req('POST', '/api/tasks', t)
        bad = [
            {}, {'title': ''}, {'title': '   '}, {'title': 'x' * 501}, {'title': 'x' * 500 + '😀'}, {'title': 5},
            {'title': 'a', 'status': 'later'}, {'title': 'a', 'priority': 'urgent'}, {'title': 'a', 'due_at': '2026-02-30'},
            {'title': 'a', 'due_at': '2026-10-01T24:00'}, {'title': 'a', 'due_at': 'tomorrow'}, {'title': 'a', 'due_at': 20261001},
            {'title': 'a', 'due_at': '2026-10-06\n'}, {'title': 'a', 'labels': 'home'}, {'title': 'a', 'labels': ['two words']},
            {'title': 'a', 'labels': ['x' * 41]}, {'title': 'a', 'labels': ['a,b']}, {'title': 'a', 'labels': ['#']},
            {'title': 'a', 'labels': [5]}, {'title': 'a', 'bogus': 1}, {'title': True}, {'title': 'a', 'description': None},
            {'title': 'a', 'labels': ['l%d' % i for i in range(51)]},
        ]
        for body in bad:
            self.req('POST', '/api/tasks', body)
        self.req('POST', '/api/tasks', ['not', 'an', 'object'])
        self.everything()
        # Updates: status round trip, due re-arms, labels re-ordered, no-op.
        self.req('PATCH', '/api/tasks/1', {'status': 'done'})
        self.req('PATCH', '/api/tasks/1', {'status': 'open'})
        self.req('PATCH', '/api/tasks/2', {'due_at': None, 'labels': ['HOME', 'extra', 'family']})
        self.req('PATCH', '/api/tasks/2', {'due_at': '2026-10-06T10:00'})
        self.req('PATCH', '/api/tasks/3', {'title': 'Plan trip', 'description': 'x\r\ny\rz'})
        self.req('PATCH', '/api/tasks/3', {'priority': 'low', 'bogus': 1})
        self.req('PATCH', '/api/tasks/99', {'title': 'x'})
        self.req('DELETE', '/api/tasks/4')
        self.req('DELETE', '/api/tasks/4')
        # Search, label filters and both together.
        for q in ('rent', 'RENT', 'ren', 'cafe', 'uber', 'naive resume', 'rome', 'ideas', 'nothing', 'pay rent',
                  'a', '"', '%', '_', 'trip!!', 'x' * 201):
            self.req('GET', '/api/tasks?view=all&q=' + quote_plus(q))
        labels = {l['name']: l['id'] for l in self.req('GET', '/api/meta')['labels']}
        for match in ('any', 'all'):
            ids = f'{labels["home"]},{labels["family"]}'
            self.req('GET', f'/api/tasks?view=all&labels={ids}&match={match}')
            self.req('GET', f'/api/tasks?view=open&labels={ids}&match={match}&q=call')
        for path in ('/api/tasks?view=nope', '/api/tasks?sort=nope', '/api/tasks?match=some', '/api/tasks?labels=1,x',
                     '/api/tasks?labels=%C2%B2', '/api/tasks?view=', '/api/tasks/abc', '/api/nothing', '/api/tasks/1/nothing'):
            self.req('GET', path)
        self.req('PUT', '/api/tasks', {'title': 'x'})
        self.req('DELETE', '/api/tasks')
        # Time moves on: overdue, today and week views change.
        self.now = datetime(2026, 10, 13, 9, 30)
        self.everything()

    def test_labels_rename_merge_colours_delete(self):
        for i in range(16):
            self.req('POST', '/api/tasks', {'title': f'task {i}', 'labels': [f'l{i}', 'shared']})
        self.req('POST', '/api/tasks/1/subtasks', {'items': [{'title': 'sub', 'labels': ['l0', 'shared']}]})
        self.everything()
        meta = self.req('GET', '/api/meta')
        ids = {l['name']: l['id'] for l in meta['labels']}
        self.req('PATCH', f'/api/labels/{ids["l1"]}', {'color': '#ABCDEF'})
        self.req('PATCH', f'/api/labels/{ids["l1"]}', {'color': 'red'})
        self.req('PATCH', f'/api/labels/{ids["l1"]}', {'name': 'L1-renamed'})
        self.req('PATCH', f'/api/labels/{ids["l1"]}', {'name': 'bad name'})
        self.req('PATCH', f'/api/labels/{ids["l0"]}', {'name': 'SHARED', 'color': '#000000'})   # merges
        self.req('PATCH', f'/api/labels/{ids["l2"]}', {})
        self.req('PATCH', '/api/labels/9999', {'name': 'x'})
        self.req('DELETE', f'/api/labels/{ids["l3"]}')
        self.req('DELETE', f'/api/labels/{ids["l3"]}')
        for i in range(5):     # new labels after deletions reuse free palette colours first
            self.req('POST', '/api/tasks', {'title': f'more {i}', 'labels': [f'new{i}']})
        self.everything()
        helper = self.js.send({'op': 'helper', 'name': 'goldenColor', 'calls': [[n] for n in range(300)]})['result']
        self.assertEqual(helper, [db.golden_color(n) for n in range(300)])

    def test_subtasks(self):
        self.req('POST', '/api/tasks', {'title': 'Party', 'labels': ['home', 'food'], 'due_at': '2026-10-20'})
        self.req('POST', '/api/tasks/1/subtasks', {'title': 'Invite people'})
        self.req('POST', '/api/tasks/1/subtasks', {'items': [
            'Buy drinks', {'title': 'Cake', 'position': 0, 'labels': ['FOOD'], 'due_at': '2026-10-19T10:00', 'notes': 'Chocolate\r\nbig  '},
            {'title': 'Old one', 'done': True, 'created_at': '2025-01-01T10:00:00', 'completed_at': '2025-01-02T10:00:00'},
            {'title': 'Placed', 'matrix': [0.7, 0.8], 'position': 99}]})
        bad = [
            {}, {'title': 'a', 'items': []}, {'items': []}, {'items': 'x'}, {'items': [5]}, {'items': [{'title': ''}]},
            {'items': [{'title': 'a', 'done': 'yes'}]}, {'items': [{'title': 'a', 'position': -1}]},
            {'items': [{'title': 'a', 'position': True}]}, {'items': [{'title': 'a', 'labels': ['nope']}]},
            {'items': [{'title': 'a', 'bogus': 1}]}, {'items': [{'title': 'a', 'created_at': '2026-13-01T00:00:00'}]},
            {'items': [{'title': 'a', 'notes': 5}]}, {'items': [{'title': 'a', 'matrix': [2, 0]}]},
            {'items': [{'title': 'a', 'due_at': 'soon'}]}, {'items': ['x'] * 501},
        ]
        for body in bad:
            self.req('POST', '/api/tasks/1/subtasks', body)
        self.req('POST', '/api/tasks/9/subtasks', {'title': 'x'})
        task = self.req('GET', '/api/tasks/1')
        sids = [s['id'] for s in task['subtasks']]
        self.req('PATCH', f'/api/subtasks/{sids[1]}', {'done': True})
        self.req('PATCH', f'/api/subtasks/{sids[1]}', {'done': False, 'title': ' Cake  2 '})
        self.req('PATCH', f'/api/subtasks/{sids[2]}', {'labels': ['home', '#food', 'home']})
        self.req('PATCH', f'/api/subtasks/{sids[2]}', {'labels': ['party']})
        self.req('PATCH', f'/api/subtasks/{sids[2]}', {'due_at': '2026-10-06', 'notes': '\n\nfirst line\nsecond'})
        self.req('PATCH', f'/api/subtasks/{sids[2]}', {})
        self.req('PATCH', f'/api/subtasks/{sids[2]}', {'done': 1})
        self.req('PATCH', '/api/subtasks/999', {'done': True})
        self.req('POST', '/api/tasks/1/subtasks/order', {'ids': [sids[3], sids[0]]})
        self.req('POST', '/api/tasks/1/subtasks/order', {'ids': [sids[0], sids[0]]})
        self.req('POST', '/api/tasks/1/subtasks/order', {'ids': [12345]})
        self.req('POST', '/api/tasks/1/subtasks/order', {'ids': ['1']})
        self.req('POST', '/api/tasks/1/subtasks/order', {})
        # Taking a label off the task takes it off its subtasks.
        self.req('PATCH', '/api/tasks/1', {'labels': ['home']})
        self.req('DELETE', f'/api/subtasks/{sids[0]}')
        self.req('DELETE', f'/api/subtasks/{sids[0]}')
        self.req('GET', '/api/tasks?view=all&q=chocolate')
        self.req('GET', '/api/tasks?view=all&q=first')
        self.everything()

    def test_matrix_and_links(self):
        for t in ('A', 'B', 'C', 'D'):
            self.req('POST', '/api/tasks', {'title': t, 'priority': 'medium'})
        self.req('POST', '/api/tasks/1/subtasks', {'items': ['A1', 'A2']})
        self.req('POST', '/api/matrix', {'items': [{'key': 't1', 'matrix': [0.8, 0.9], 'priority': 'high'},
                                                   {'key': 's1', 'matrix': [0.6, 0.55]},
                                                   {'key': 't2', 'matrix': [0.123456, 0.987654]}]})
        for body in ({'items': []}, {'items': [{'key': 's1', 'matrix': None, 'priority': 'high'}]},
                     {'items': [{'key': 'x1', 'matrix': None}]}, {'items': [{'key': 't1'}]},
                     {'items': [{'key': 't99', 'matrix': None}]}, {'items': [{'key': 't1', 'matrix': [1.5, 0]}]},
                     {'items': [{'key': 't1', 'matrix': [True, 0]}]}, {'items': [{'key': 't1', 'matrix': None, 'x': 1}]},
                     {'items': [5]}, {}, {'items': [{'key': 't1\n', 'matrix': None}]}):
            self.req('POST', '/api/matrix', body)
        self.req('POST', '/api/deps', {'before': 't1', 'after': 't2'})
        self.req('POST', '/api/deps', {'before': 't1', 'after': 't2'})       # duplicate
        self.req('POST', '/api/deps', {'before': 't2', 'after': 's2'})
        self.req('POST', '/api/deps', {'before': 's2', 'after': 't1'})       # loop
        self.req('POST', '/api/deps', {'before': 't3', 'after': 't3'})
        self.req('POST', '/api/deps', {'before': 't3', 'after': 't99'})
        self.req('POST', '/api/deps', {'before': 'q3', 'after': 't1'})
        self.req('POST', '/api/deps', {'before': 's1', 'after': 't4'})
        self.req('PATCH', '/api/subtasks/1', {'done': True})
        self.req('PATCH', '/api/tasks/2', {'status': 'done'})
        self.everything()
        self.req('DELETE', '/api/deps/1')
        self.req('DELETE', '/api/deps/1')
        self.req('DELETE', '/api/tasks/1')        # takes its subtasks' links along
        self.everything()

    def test_reminders(self):
        self.req('POST', '/api/tasks', {'title': 'Morning', 'due_at': '2026-10-07'})
        self.req('POST', '/api/tasks', {'title': 'Exact', 'due_at': '2026-10-06T16:00'})
        self.req('POST', '/api/tasks', {'title': 'Past when set', 'due_at': '2026-10-06T10:00'})
        self.req('POST', '/api/tasks/1/subtasks', {'items': [{'title': 'sub', 'due_at': '2026-10-06T16:00'}]})
        upcoming = self.js.send({'op': 'upcoming', 'now': self.stamp()})['items']
        self.assertEqual([u['key'] for u in upcoming], ['s1', 't2', 't1'])   # same minute: by key
        self.assertEqual(self.reminders(datetime(2026, 10, 6, 15, 59)), [])
        self.assertEqual(len(self.reminders(datetime(2026, 10, 6, 16, 0))), 2)
        self.assertEqual(self.reminders(datetime(2026, 10, 6, 16, 1)), [])
        self.assertEqual([r['title'] for r in self.reminders(datetime(2026, 10, 7, 9, 0))], ['Morning'])
        self.req('PATCH', '/api/tasks/1', {'due_at': '2026-10-08'})        # re-armed
        self.assertEqual([r['title'] for r in self.reminders(datetime(2026, 10, 8, 9, 5))], ['Morning'])
        self.everything()

    def test_export_import_round_trip_and_lenient_import(self):
        self.req('POST', '/api/tasks', {'title': 'Exported', 'labels': ['keep'], 'description': 'body',
                                        'due_at': '2026-11-01T08:00', 'priority': 'low'})
        self.req('POST', '/api/tasks/1/subtasks', {'items': [{'title': 'one', 'labels': ['keep'], 'notes': 'n'},
                                                             {'title': 'two', 'done': True}]})
        self.req('POST', '/api/tasks', {'title': 'Second', 'status': 'done'})
        self.req('POST', '/api/matrix', {'items': [{'key': 't1', 'matrix': [0.2, 0.3]}, {'key': 's2', 'matrix': [0.9, 0.9]}]})
        self.req('POST', '/api/deps', {'before': 's1', 'after': 't2'})
        export = self.req('GET', '/api/export')
        self.req('POST', '/api/import', export)       # everything is already here
        weird = {
            'labels': [{'name': ' #Big Label, x ', 'color': '#123ABC'}, {'name': 'keep', 'color': '#e11d48'}, 'junk', {'name': 5}],
            'tasks': [
                {'id': 70, 'title': 'From elsewhere', 'created_at': '2025-03-04 05:06', 'status': 'Completed',
                 'priority': 'URGENT', 'due': '2025-03-10 14:30:00', 'labels': ['Big Label, x', {'name': 'keep'}, '', None],
                 'mx': 0.25, 'my': 0.75, 'description': 'line\r\nline',
                 'subtasks': [{'id': 9, 'title': ' later ', 'position': 5, 'done': 'yes', 'labels': ['keep', 'new-one']},
                              {'id': 8, 'title': 'first', 'position': 0, 'due_at': '2025-03-09', 'notes': 'x\r\n  '},
                              {'title': '', 'position': 1}, 'not a dict', {'title': 'no position'}]},
                {'id': 71, 'title': 7, 'created_at': 'yesterday', 'status': 'doing', 'priority': 3, 'done': True},
                {'title': '   '}, 'junk', {'id': 72, 'title': 'Matrix as list', 'matrix': [0.5, 2], 'priority': 'h'},
                {'id': 73, 'title': 'Bad dates', 'created_at': '2026-02-30T10:00:00', 'due_at': '2026-13-01',
                 'completed_at': 5, 'status': 'wip', 'updated_at': '2026-01-01T25:00'},
            ],
            'dependencies': [{'before': 't70', 'after': 't71'}, {'before_task_id': 71, 'after_subtask_id': 9},
                             {'before': 's9', 'after': 't70'}, {'before': 't70', 'after': 't70'}, {'before': 't1', 'after': 't70'},
                             {'before_subtask_id': 8, 'after_task_id': 72, 'created_at': '2025-01-01T00:00:00'}, 'junk'],
        }
        self.req('POST', '/api/import', weird)
        self.req('POST', '/api/import', weird)
        self.req('POST', '/api/import', {'tasks': 'nope'})
        self.req('POST', '/api/import', {'deps': [], 'tasks': [{'title': 'deps key', 'id': 1}]})
        self.req('POST', '/api/import', [1, 2])
        self.req('POST', '/api/import', {'tasks': [{'title': 'pic', 'description': '![a](/images/' + 'a' * 32 + '.png)'}]})
        self.everything()

    def test_images(self):
        png = base64.b64encode(PNG).decode()
        self.req('POST', '/api/images', {'$bytes': png})
        self.req('POST', '/api/images', {'$bytes': base64.b64encode(b'GIF89a' + b'\0' * 20).decode()})
        self.req('POST', '/api/images', {'$bytes': base64.b64encode(b'not an image at all').decode()})
        self.req('POST', '/api/images', {'$bytes': ''})

    def test_snippets_and_search_words(self):
        samples = ['', '\n\n', '# Head **line**\n![x](/images/abc.png) ![y](https://a.b/c.png)\n', '```\ncode\n```\nafter',
                   '---\n***\n- [ ] todo item', '> quoted > twice', '1. first\n2) second', '١. arabic digit', '[link](http://x) text',
                   'x' * 200, ('word ' * 50), '    nbsp spaces  ', '~~gone~~ `code` __u__', '* * *', '\t# not heading']
        a = [db.snippet(s) for s in samples]
        b = self.js.send({'op': 'helper', 'name': 'snippet', 'calls': [[s] for s in samples]})['result']
        self.assertEqual(a, b)
        a = [db.image_count(s) for s in samples]
        b = self.js.send({'op': 'helper', 'name': 'imageCount', 'calls': [[s] for s in samples]})['result']
        self.assertEqual(a, b)
        queries = ['café crème', 'naïve_x', "can't stop", '数字 test', 'Ünïcödé', 'a-b-c', 'x' * 30 + ' y', '', '½ ²']
        a = [db.search_words(q) for q in queries]
        b = self.js.send({'op': 'helper', 'name': 'searchWords', 'calls': [[q] for q in queries]})['result']
        self.assertEqual(a, b)

    def test_reminder_texts(self):
        import reminders
        now = datetime(2026, 10, 6, 15, 0)
        cases = []
        for due in ('2026-10-06', '2026-10-05T08:00', '2026-10-07T23:59', '2026-10-12', '2027-01-03T10:00', '2025-12-31'):
            cases.append([{'kind': 'task', 'id': 1, 'title': 'T', 'due_at': due}])
            cases.append([{'kind': 'subtask', 'id': 2, 'title': 'S', 'due_at': due, 'task_id': 1, 'task_title': 'Parent'}])
        many = [{'kind': 'task', 'id': i, 'title': f'Item {i}', 'due_at': '2026-10-06'} for i in range(7)]
        cases += [many[:3], many[:4], many[:5], many[:6], many]
        a = [[list(p) for p in reminders.compose(items, now)] for items in cases]
        b = self.js.send({'op': 'helper', 'name': 'compose', 'calls': [[items, '2026-10-06T15:00:00'] for items in cases]})['result']
        self.assertEqual(a, b)

    def test_random_walk(self):
        """Thousands of mixed requests from a fixed seed; every answer must agree."""
        rng = random.Random(20261006)
        words = ['rent', 'Café', 'naïve', 'home', 'x', '😀', 'two words', '#tag', 'a,b', '', '  ', 'Ünï', 'Zz']
        label_names = ['home', 'Home', 'work', 'Ärger', 'ärger', 'x' * 40, 'l1', 'l2', '#l3', 'bad name', '']

        def text():
            return ' '.join(rng.choice(words) for _ in range(rng.randint(0, 4)))

        def due():
            return rng.choice([None, '', '2026-10-05', '2026-10-06', '2026-10-06T15:30', '2026-10-09', '2026-11-30T23:59',
                               '2026-02-30', 'soon'])

        def labels():
            return [rng.choice(label_names) for _ in range(rng.randint(0, 3))]

        def ids(sql):
            with self.py.store.read() as c:
                return [r[0] for r in c.execute(sql)]

        def pick(sql, spread):
            have = ids(sql)
            return rng.choice(have) if have and rng.random() < 0.85 else rng.randint(1, spread)

        def key():
            if rng.random() < 0.5:
                return 't' + str(pick('SELECT id FROM tasks', 30))
            return 's' + str(pick('SELECT id FROM subtasks', 40))

        def sub_labels(sid):
            # Mostly labels the subtask's task has (anything else is refused).
            with self.py.store.read() as c:
                own = [r[0] for r in c.execute('SELECT l.name FROM subtasks s JOIN task_labels tl ON tl.task_id = s.task_id '
                                               'JOIN labels l ON l.id = tl.label_id WHERE s.id = ?', (sid,))]
            if own and rng.random() < 0.8:
                return rng.sample(own, rng.randint(0, len(own)))
            return labels()

        for step in range(2500):
            r = rng.random()
            tid = pick('SELECT id FROM tasks', 30)
            sid = pick('SELECT id FROM subtasks', 40)
            if r < 0.16:
                body = {'title': text() or 'task'}
                for k, gen in (('due_at', due), ('labels', labels), ('priority', lambda: rng.choice(db.PRIORITIES + ('x',))),
                               ('status', lambda: rng.choice(db.STATUSES)), ('description', text)):
                    if rng.random() < 0.4:
                        body[k] = gen()
                self.req('POST', '/api/tasks', body)
            elif r < 0.30:
                body = {}
                for k, gen in (('title', text), ('due_at', due), ('labels', labels), ('status', lambda: rng.choice(db.STATUSES)),
                               ('priority', lambda: rng.choice(db.PRIORITIES)), ('description', text)):
                    if rng.random() < 0.35:
                        body[k] = gen()
                self.req('PATCH', f'/api/tasks/{tid}', body)
            elif r < 0.42:
                items = []
                for _ in range(rng.randint(1, 3)):
                    item = {'title': text() or 'sub'}
                    if rng.random() < 0.3:
                        with self.py.store.read() as c:
                            own = [r[0] for r in c.execute('SELECT l.name FROM task_labels tl JOIN labels l ON l.id = tl.label_id '
                                                           'WHERE tl.task_id = ?', (tid,))]
                        item['labels'] = rng.sample(own, rng.randint(0, len(own))) if own and rng.random() < 0.8 else labels()
                    if rng.random() < 0.3:
                        item['due_at'] = due()
                    if rng.random() < 0.2:
                        item['position'] = rng.randint(0, 5)
                    if rng.random() < 0.2:
                        item['done'] = rng.random() < 0.5
                    items.append(item)
                self.req('POST', f'/api/tasks/{tid}/subtasks', {'items': items})
            elif r < 0.54:
                body = {}
                for k, gen in (('title', text), ('done', lambda: rng.random() < 0.5), ('due_at', due),
                               ('labels', lambda: sub_labels(sid)), ('notes', text)):
                    if rng.random() < 0.35:
                        body[k] = gen()
                self.req('PATCH', f'/api/subtasks/{sid}', body)
            elif r < 0.60:
                own = ids(f'SELECT id FROM subtasks WHERE task_id = {int(tid)}')
                pool = own if own and rng.random() < 0.85 else list(range(1, 41))
                self.req('POST', f'/api/tasks/{tid}/subtasks/order', {'ids': rng.sample(pool, rng.randint(0, min(3, len(pool))))})
            elif r < 0.66:
                items = [{'key': key(), 'matrix': rng.choice([None, [round(rng.random(), 3), round(rng.random(), 3)]])}
                         for _ in range(rng.randint(1, 3))]
                if rng.random() < 0.3:
                    items[0]['priority'] = rng.choice(db.PRIORITIES)
                self.req('POST', '/api/matrix', {'items': items})
            elif r < 0.73:
                self.req('POST', '/api/deps', {'before': key(), 'after': key()})
            elif r < 0.75:
                self.req('DELETE', f'/api/deps/{pick("SELECT id FROM dependencies", 20)}')
            elif r < 0.78:
                self.req('DELETE', f'/api/subtasks/{sid}')
            elif r < 0.80:
                self.req('DELETE', f'/api/tasks/{tid}')
            elif r < 0.84:
                lid = pick('SELECT id FROM labels', 12)
                body = rng.choice([{'name': rng.choice(label_names)}, {'color': rng.choice(['#00ff00', '#ABCDEF', 'red'])},
                                   {'name': rng.choice(label_names), 'color': '#123456'}])
                self.req('PATCH', f'/api/labels/{lid}', body)
            elif r < 0.85:
                self.req('DELETE', f'/api/labels/{pick("SELECT id FROM labels", 12)}')
            elif r < 0.97:
                q = rng.choice(['', 'rent', 'caf', 'NAÏVE', 'ü', 'two', 'x', 'zz'])
                view = rng.choice(db.VIEWS)
                sort = rng.choice(db.SORTS)
                lab = ','.join(str(rng.randint(1, 8)) for _ in range(rng.randint(0, 2)))
                match = rng.choice(['any', 'all'])
                self.req('GET', f'/api/tasks?view={view}&sort={sort}&labels={lab}&match={match}&q={quote_plus(q)}', tick=0)
            else:
                self.now += timedelta(hours=rng.choice([1, 6, 30]))
                self.req('GET', '/api/meta', tick=0)
                self.reminders(self.now)
        self.everything()

    def test_persistence_round_trip(self):
        self.req('POST', '/api/tasks', {'title': 'kept', 'labels': ['a', 'b']})
        self.req('POST', '/api/tasks/1/subtasks', {'items': [{'title': 's', 'labels': ['b']}]})
        self.req('POST', '/api/deps', {'before': 's1', 'after': 't1'})
        snap = self.js.send({'op': 'snapshot'})['snap']
        self.js.send({'op': 'restore', 'snap': snap})
        self.everything()
        self.req('POST', '/api/tasks', {'title': 'after restore', 'labels': ['c']})   # ids keep counting
        self.everything()


if __name__ == '__main__':
    unittest.main()
