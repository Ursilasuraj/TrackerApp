"""Backend tests: storage, migrations, search, reminders, HTTP API, single instance.

Run:  python -m unittest tests.test_backend -v   (from the app folder)
Every test uses scratch data and a test port; the real app is never touched.
"""

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import backup  # noqa: E402
import db  # noqa: E402
import platforms  # noqa: E402
import reminders  # noqa: E402
from helpers import APP_DIR, AppServer, copy_app, free_port, request, scratch_dir  # noqa: E402

NOW = datetime(2026, 10, 6, 15, 0, 0)   # a Tuesday
logging.getLogger('todotracker').addHandler(logging.NullHandler())   # expected warnings stay quiet


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = scratch_dir()
        self.path = os.path.join(self.dir, 'todo.db')
        self._now = db.now_dt
        db.now_dt = lambda: NOW
        self.store = db.Store(self.path)
        self.store.open()

    def tearDown(self):
        self.store.close()
        db.now_dt = self._now
        os.environ.pop('TODOTRACKER_NO_FTS', None)
        shutil.rmtree(self.dir, ignore_errors=True)

    def reopen(self, no_fts=False):
        self.store.close()
        if no_fts:
            os.environ['TODOTRACKER_NO_FTS'] = '1'
        else:
            os.environ.pop('TODOTRACKER_NO_FTS', None)
        self.store = db.Store(self.path)
        self.store.open()
        return self.store

    def titles(self, **kw):
        return [t['title'] for t in self.store.list_tasks(**kw)]


class TaskTests(StoreCase):
    def test_create_records_time_and_defaults(self):
        t = self.store.create_task('  Buy   milk ', labels=['home'])
        self.assertEqual(t['title'], 'Buy milk')
        self.assertEqual(t['created_at'], '2026-10-06T15:00:00')
        self.assertEqual((t['status'], t['priority'], t['due_at']), ('open', 'medium', None))
        self.assertEqual([l['name'] for l in t['labels']], ['home'])

    def test_validation(self):
        bad = [
            dict(title=''), dict(title='   '), dict(title='x' * 501), dict(title=5),
            dict(title='a', status='later'), dict(title='a', priority='urgent'),
            dict(title='a', due_at='2026-02-30'), dict(title='a', due_at='2026-10-01T24:00'),
            dict(title='a', due_at='tomorrow'), dict(title='a', due_at=20261001),
            dict(title='a', labels='home'), dict(title='a', labels=['two words']),
            dict(title='a', labels=['x' * 41]), dict(title='a', labels=['a,b']),
            dict(title='a', description='x' * 200_001),
        ]
        for kw in bad:
            with self.subTest(kw=str(kw)[:60]):
                with self.assertRaises(db.ValidationError):
                    self.store.create_task(**kw)
        self.assertEqual(self.store.counts()['tasks'], 0)

    def test_status_sets_and_clears_completion_time(self):
        t = self.store.create_task('a')
        t = self.store.update_task(t['id'], {'status': 'done'})
        self.assertEqual(t['completed_at'], '2026-10-06T15:00:00')
        t = self.store.update_task(t['id'], {'status': 'in_progress'})
        self.assertIsNone(t['completed_at'])
        with self.assertRaises(db.NotFound):
            self.store.update_task(999, {'title': 'x'})

    def test_delete_cascades_labels(self):
        t = self.store.create_task('a', labels=['x', 'y'])
        self.store.delete_task(t['id'])
        with self.store.read() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM task_labels').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT count(*) FROM labels').fetchone()[0], 2)
        with self.assertRaises(db.NotFound):
            self.store.delete_task(t['id'])

    def test_snippet_and_image_count(self):
        t = self.store.create_task('a', description='\n# Head **line**\n![x](/images/abc.png) ![y](https://a.b/c.png)\n')
        self.assertEqual(t['snippet'], 'Head line')
        self.assertEqual(t['images'], 2)


class LabelTests(StoreCase):
    def test_palette_then_golden_angle(self):
        names = [f'l{i}' for i in range(15)]
        t = self.store.create_task('a', labels=names)
        colors = [l['color'] for l in t['labels']]
        self.assertEqual(colors[:12], db.PALETTE)
        self.assertEqual(len(set(colors)), 15)
        for c in colors[12:]:
            lum = db._hex_luminance(c)
            self.assertTrue(0.12 < lum <= 0.2, (c, lum))

    def test_first_unused_palette_colour(self):
        self.store.create_task('a', labels=['a', 'b', 'c'])
        lid = self.store.list_labels()[1]['id']   # 'b' has the 2nd colour
        self.store.delete_label(lid)
        t = self.store.create_task('b', labels=['new'])
        self.assertEqual(t['labels'][0]['color'], db.PALETTE[1])

    def test_case_insensitive_unique_and_rename_merge(self):
        t1 = self.store.create_task('one', labels=['Work'])
        t2 = self.store.create_task('two', labels=['work', 'home'])
        self.assertEqual(len(self.store.list_labels()), 2)
        self.assertEqual(t2['labels'][0]['name'], 'Work')
        work = next(l for l in self.store.list_labels() if l['name'] == 'Work')
        home = next(l for l in self.store.list_labels() if l['name'] == 'home')
        res = self.store.update_label(home['id'], name='WORK')     # merge onto Work
        self.assertTrue(res['merged'])
        self.assertEqual(res['id'], work['id'])
        self.assertEqual([l['name'] for l in self.store.list_labels()], ['Work'])
        self.assertEqual([l['name'] for l in self.store.get_task(t2['id'])['labels']], ['Work'])
        self.assertEqual([l['name'] for l in self.store.get_task(t1['id'])['labels']], ['Work'])
        res = self.store.update_label(work['id'], name='Job', color='#123ABC')
        self.assertEqual((res['name'], res['color']), ('Job', '#123abc'))
        for bad in ({'name': ''}, {'name': 'a b'}, {'name': 'x' * 41}, {'color': 'red'}, {'color': '#12345'}):
            with self.assertRaises(db.ValidationError):
                self.store.update_label(work['id'], **bad)

    def test_delete_label_keeps_tasks(self):
        t = self.store.create_task('a', labels=['x'])
        self.store.delete_label(t['labels'][0]['id'])
        self.assertEqual(self.store.get_task(t['id'])['labels'], [])
        self.assertEqual(self.store.counts()['tasks'], 1)

    def test_label_filter_any_all(self):
        self.store.create_task('ab', labels=['a', 'b'])
        self.store.create_task('a', labels=['a'])
        self.store.create_task('none')
        ids = {l['name']: l['id'] for l in self.store.list_labels()}
        self.assertEqual(sorted(self.titles(label_ids=[ids['a'], ids['b']], match='any')), ['a', 'ab'])
        self.assertEqual(self.titles(label_ids=[ids['a'], ids['b']], match='all'), ['ab'])
        meta = self.store.meta()
        self.assertEqual({l['name']: l['open'] for l in meta['labels']}, {'a': 2, 'b': 1})


class ViewTests(StoreCase):
    def test_views_counts_and_sorting(self):
        s = self.store
        s.create_task('overdue', due_at='2026-10-05')
        s.create_task('late today', due_at='2026-10-06T14:00')
        s.create_task('today', due_at='2026-10-06')
        s.create_task('in a week', due_at='2026-10-12')
        s.create_task('later', due_at='2026-10-13', priority='high')
        s.create_task('no date', priority='low')
        d = s.create_task('done')
        s.update_task(d['id'], {'status': 'done'})
        self.assertEqual(sorted(self.titles(view='overdue')), ['late today', 'overdue'])
        self.assertEqual(sorted(self.titles(view='today')), ['late today', 'today'])
        self.assertEqual(sorted(self.titles(view='week')), ['in a week', 'late today', 'today'])
        self.assertEqual(self.titles(view='nodate'), ['no date'])
        self.assertEqual(self.titles(view='done'), ['done'])
        self.assertEqual(len(self.titles(view='all')), 7)
        self.assertEqual(self.titles(view='open', sort='due'),
                         ['overdue', 'today', 'late today', 'in a week', 'later', 'no date'])
        self.assertEqual(self.titles(view='open', sort='priority')[0], 'later')
        self.assertEqual(self.titles(view='open', sort='priority')[-1], 'no date')
        counts = s.meta()['counts']
        self.assertEqual(counts, {'open': 6, 'overdue': 2, 'today': 2, 'week': 3, 'nodate': 1, 'done': 1, 'all': 7, 'do': 0})
        for bad in (dict(view='soon'), dict(sort='random'), dict(match='some')):
            with self.assertRaises(db.ValidationError):
                s.list_tasks(**bad)


class SearchTests(StoreCase):
    def fill(self):
        self.store.create_task('Buy groceries', description='milk, eggs and bread')
        self.store.create_task('Café visit', description='meet Zoë')
        self.store.create_task('Write report', description='quarterly numbers')

    def test_fts_prefix_per_word_all_words(self):
        self.fill()
        self.assertTrue(self.store.fts)
        self.assertEqual(self.titles(q='gro'), ['Buy groceries'])
        self.assertEqual(self.titles(q='buy mil'), ['Buy groceries'])
        self.assertEqual(self.titles(q='buy report'), [])
        self.assertEqual(self.titles(q='cafe zoe'), ['Café visit'])
        self.assertEqual(self.titles(q='"quarter'), ['Write report'])
        self.assertEqual(self.titles(q='*** ((('), self.titles(), 'no words means no filter')

    def test_like_fallback(self):
        self.fill()
        self.reopen(no_fts=True)
        self.assertFalse(self.store.fts)
        self.assertEqual(self.titles(q='gro'), ['Buy groceries'])
        self.assertEqual(self.titles(q='buy mil'), ['Buy groceries'])
        self.assertEqual(self.titles(q='100%'), [])
        self.assertEqual(self.store.meta()['search'], 'like')

    def test_dirty_flag_rebuilds_index(self):
        self.fill()
        self.reopen(no_fts=True)
        self.store.create_task('Added without index')
        with self.store.read() as c:
            self.assertEqual(c.execute("SELECT value FROM meta WHERE key='fts_dirty'").fetchone()[0], '1')
        self.reopen()
        self.assertTrue(self.store.fts)
        self.assertEqual(self.titles(q='without'), ['Added without index'])
        with self.store.read() as c:
            self.assertIsNone(c.execute("SELECT value FROM meta WHERE key='fts_dirty'").fetchone())

    def test_count_mismatch_and_wrong_columns_rebuild(self):
        self.fill()
        self.store.close()
        c = sqlite3.connect(self.path)
        c.execute('DELETE FROM tasks_fts WHERE rowid = 1')
        c.commit()
        c.close()
        self.reopen()
        self.assertEqual(self.titles(q='groceries'), ['Buy groceries'])
        self.store.close()
        c = sqlite3.connect(self.path)
        c.execute('DROP TABLE tasks_fts')
        c.execute('CREATE VIRTUAL TABLE tasks_fts USING fts5(title, description)')
        c.commit()
        c.close()
        self.reopen()
        with self.store.read() as c:
            cols = [r[1] for r in c.execute('PRAGMA table_info(tasks_fts)')]
        self.assertEqual(cols, db.FTS_COLUMNS)
        self.assertEqual(self.titles(q='report'), ['Write report'])

    def test_updates_and_deletes_keep_index(self):
        t = self.store.create_task('alpha')
        self.store.update_task(t['id'], {'title': 'beta', 'description': 'gamma'})
        self.assertEqual(self.titles(q='alpha'), [])
        self.assertEqual(self.titles(q='gam'), ['beta'])
        self.store.delete_task(t['id'])
        self.assertEqual(self.titles(q='beta'), [])


class ReminderTests(StoreCase):
    def test_claims_due_items_once(self):
        s = self.store
        db.now_dt = lambda: datetime(2026, 10, 6, 8, 0)
        a = s.create_task('date only today', due_at='2026-10-06')
        b = s.create_task('at 8:30', due_at='2026-10-06T08:30')
        s.create_task('tomorrow', due_at='2026-10-07')
        d = s.create_task('done', due_at='2026-10-06T08:00')
        s.update_task(d['id'], {'status': 'done'})
        self.assertEqual(s.due_reminders(datetime(2026, 10, 6, 8, 29)), [])
        got = s.due_reminders(datetime(2026, 10, 6, 8, 30))
        self.assertEqual([i['title'] for i in got], ['at 8:30'])
        got = s.due_reminders(datetime(2026, 10, 6, 9, 0))
        self.assertEqual([i['title'] for i in got], ['date only today'])
        self.assertEqual(s.due_reminders(datetime(2026, 10, 6, 23, 0)), [])
        # Changing the target date re-arms the reminder.
        s.update_task(a['id'], {'due_at': '2026-10-06T10:00'})
        got = s.due_reminders(datetime(2026, 10, 6, 10, 0))
        self.assertEqual([i['id'] for i in got], [a['id']])
        self.assertTrue(b['id'])

    def test_target_already_past_when_set_is_not_reminded(self):
        t = self.store.create_task('added late', due_at='2026-10-06')   # NOW is 15:00
        self.assertEqual(self.store.due_reminders(NOW), [])
        self.store.update_task(t['id'], {'due_at': '2026-10-06T16:00'})
        self.assertEqual(len(self.store.due_reminders(datetime(2026, 10, 6, 16, 0))), 1)

    def test_compose_summary_and_texts(self):
        items = [{'kind': 'task', 'title': f't{i}', 'due_at': '2026-10-06T09:00', 'id': i} for i in range(4)]
        toasts = reminders.compose(items, NOW)
        self.assertEqual(len(toasts), 1)
        self.assertIn('4 TodoTracker items', toasts[0][0])
        toasts = reminders.compose(items[:1], NOW)
        self.assertEqual(toasts, [('t0', 'Due today 09:00')])
        sub = [{'kind': 'subtask', 'title': 'Call', 'task_title': 'Party', 'due_at': '2026-10-05', 'id': 1}]
        self.assertEqual(reminders.compose(sub, NOW), [('Call', 'Subtask of “Party” · due yesterday')])
        shown = []
        self.store.create_task('x', due_at='2026-10-06T16:00')
        reminders.check_once(self.store, lambda t, b: shown.append((t, b)), now=datetime(2026, 10, 6, 16, 1))
        self.assertEqual(shown, [('x', 'Due today 16:00')])


class ExportBackupTests(StoreCase):
    def test_export(self):
        self.store.create_task('a', labels=['x'], description='d', due_at='2026-10-07')
        data = self.store.export()
        self.assertEqual(data['app'], 'TodoTracker')
        self.assertEqual(data['tasks'][0]['labels'], ['x'])
        self.assertEqual(data['labels'][0]['name'], 'x')
        json.dumps(data)

    def test_backup_daily_and_keep_14(self):
        self.store.create_task('a')
        bdir = os.path.join(self.dir, 'backups')
        from datetime import date, timedelta
        for i in range(16):
            backup.run_backup(self.path, bdir, day=date(2026, 9, 1) + timedelta(days=i))
        files = sorted(os.listdir(bdir))
        self.assertEqual(len(files), 14)
        self.assertEqual(files[0], 'todo-2026-09-03.db')
        p = backup.run_backup(self.path, bdir, day=date(2026, 9, 16))
        self.assertTrue(os.path.exists(p))
        c = sqlite3.connect(p)
        self.assertEqual(c.execute('SELECT title FROM tasks').fetchone()[0], 'a')
        self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        c.close()


class SubtaskTests(StoreCase):
    def test_add_update_delete_and_positions(self):
        s = self.store
        t = s.create_task('Party')
        t, ids = s.add_subtasks(t['id'], ['cake', {'title': 'music', 'done': True}, 'balloons'])
        self.assertEqual([(x['title'], x['position'], x['done']) for x in t['subtasks']],
                         [('cake', 0, False), ('music', 1, True), ('balloons', 2, False)])
        self.assertEqual(t['progress'], [1, 3])
        self.assertEqual(t['subtasks'][1]['completed_at'], '2026-10-06T15:00:00')
        t = s.update_subtask(ids[0], {'done': True, 'title': '  big   cake '})
        self.assertEqual((t['subtasks'][0]['title'], t['subtasks'][0]['done']), ('big cake', True))
        t = s.update_subtask(ids[0], {'done': False})
        self.assertIsNone(t['subtasks'][0]['completed_at'])
        t = s.delete_subtask(ids[1])
        self.assertEqual([(x['title'], x['position']) for x in t['subtasks']], [('big cake', 0), ('balloons', 1)])
        with self.assertRaises(db.NotFound):
            s.delete_subtask(ids[1])
        with self.assertRaises(db.NotFound):
            s.add_subtasks(999, ['x'])

    def test_restore_at_position_with_timestamps(self):
        t = self.store.create_task('T')
        t, ids = self.store.add_subtasks(t['id'], ['a', 'b', 'c'])
        t, new = self.store.add_subtasks(t['id'], [{'title': 'restored', 'position': 1, 'done': True,
                                                     'created_at': '2025-05-05T05:05:05',
                                                     'completed_at': '2025-06-06T06:06:06'}])
        sub = t['subtasks'][1]
        self.assertEqual((sub['title'], sub['created_at'], sub['completed_at']),
                         ('restored', '2025-05-05T05:05:05', '2025-06-06T06:06:06'))
        self.assertEqual([x['title'] for x in t['subtasks']], ['a', 'restored', 'b', 'c'])

    def test_validation(self):
        t = self.store.create_task('T')
        bad = [[], [''], ['x' * 501], [5], [{'title': 'a', 'done': 'yes'}], [{'title': 'a', 'position': -1}],
               [{'title': 'a', 'created_at': '2026-10-06'}], [{'title': 'a', 'bogus': 1}],
               ['x'] * (db.MAX_SUBTASKS_PER_REQUEST + 1)]
        for items in bad:
            with self.subTest(items=str(items)[:50]):
                with self.assertRaises(db.ValidationError):
                    self.store.add_subtasks(t['id'], items)
        t, ids = self.store.add_subtasks(t['id'], ['ok'])
        for fields in ({'title': ''}, {'done': 1}, {'title': None}):
            with self.assertRaises(db.ValidationError):
                self.store.update_subtask(ids[0], fields)

    def test_reorder(self):
        t = self.store.create_task('T')
        t, ids = self.store.add_subtasks(t['id'], ['a', 'b', 'c', 'd'])
        t = self.store.reorder_subtasks(t['id'], [ids[3], ids[1]])
        self.assertEqual([x['title'] for x in t['subtasks']], ['d', 'b', 'a', 'c'])
        other = self.store.create_task('U')
        other, oids = self.store.add_subtasks(other['id'], ['z'])
        for bad in ([oids[0]], [ids[0], ids[0]], ['x'], 'nope'):
            with self.assertRaises(db.ValidationError):
                self.store.reorder_subtasks(t['id'], bad)

    def test_concurrent_adds_get_distinct_positions(self):
        t = self.store.create_task('T')
        errors = []

        def worker(n):
            try:
                store = db.Store(self.path)
                for i in range(10):
                    store.add_subtasks(t['id'], [f'{n}-{i}'])
                store.close()
            except Exception as e:   # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(errors, [])
        subs = self.store.get_task(t['id'])['subtasks']
        self.assertEqual(sorted(x['position'] for x in subs), list(range(40)))

    def test_search_and_cascade_and_export(self):
        t = self.store.create_task('Party')
        self.store.add_subtasks(t['id'], ['Buy balloons', 'Music'])
        self.assertEqual(self.titles(q='ballo'), ['Party'])
        hit = self.store.list_tasks(q='party ball')[0]
        self.assertEqual([x['title'] for x in hit['subtasks'] if x['id'] in hit['match_subs']], ['Buy balloons'])
        self.reopen(no_fts=True)
        self.assertEqual(self.titles(q='music'), ['Party'])
        data = self.store.export()
        self.assertEqual([x['title'] for x in data['tasks'][0]['subtasks']], ['Buy balloons', 'Music'])
        self.store.delete_task(t['id'])
        with self.store.read() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM subtasks').fetchone()[0], 0)


class SubtaskDetailTests(StoreCase):
    def party(self):
        t = self.store.create_task('Party', labels=['home', 'fun'])
        t, ids = self.store.add_subtasks(t['id'], ['Cake', 'Music'])
        return t, ids

    def test_dates_labels_notes_and_validation(self):
        t, ids = self.party()
        before = len(self.store.list_labels())
        t = self.store.update_subtask(ids[0], {'due_at': '2026-10-08T09:30', 'labels': ['FUN'],
                                               'notes': 'line one\r\nline two  \n\n'})
        sub = t['subtasks'][0]
        self.assertEqual((sub['due_at'], [l['name'] for l in sub['labels']]), ('2026-10-08T09:30', ['fun']))
        self.assertEqual((sub['notes'], sub['note1'], sub['has_notes']), ('line one\nline two', 'line one', True))
        for fields in ({'due_at': '2026-02-30'}, {'labels': ['work']}, {'labels': 'fun'},
                       {'notes': 'x' * (db.MAX_NOTES + 1)}, {'notes': 5}):
            with self.subTest(fields=str(fields)[:40]):
                with self.assertRaises(db.ValidationError):
                    self.store.update_subtask(ids[1], fields)
        self.assertEqual(len(self.store.list_labels()), before, 'a subtask never creates labels')
        listed = self.store.list_tasks()[0]['subtasks'][0]
        self.assertNotIn('notes', listed)
        self.assertEqual(listed['note1'], 'line one')

    def test_subset_rule_is_enforced_by_the_database(self):
        t, ids = self.party()
        self.store.create_task('Other', labels=['work'])
        work = next(l['id'] for l in self.store.list_labels() if l['name'] == 'work')
        with self.store.write() as c:
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute('INSERT INTO subtask_labels(subtask_id, label_id) VALUES (?, ?)', (ids[0], work))

    def test_label_changes_reach_subtasks(self):
        t, ids = self.party()
        self.store.update_subtask(ids[0], {'labels': ['home', 'fun']})
        t = self.store.update_task(t['id'], {'labels': ['home']})
        self.assertEqual([l['name'] for l in t['subtasks'][0]['labels']], ['home'])
        # Merging a label carries the subtask label along.
        self.store.create_task('x', labels=['house'])
        home = next(l['id'] for l in self.store.list_labels() if l['name'] == 'home')
        self.store.update_label(home, name='House')
        t = self.store.get_task(t['id'])
        self.assertEqual([l['name'] for l in t['subtasks'][0]['labels']], ['house'])
        house = next(l['id'] for l in self.store.list_labels() if l['name'] == 'house')
        self.store.delete_label(house)
        self.assertEqual(self.store.get_task(t['id'])['subtasks'][0]['labels'], [])

    def test_subtask_dates_count_for_their_task(self):
        s = self.store
        a = s.create_task('sub due today')
        s.add_subtasks(a['id'], [{'title': 'x', 'due_at': '2026-10-06'}])
        b = s.create_task('sub overdue', due_at='2026-12-01')
        s.add_subtasks(b['id'], [{'title': 'x', 'due_at': '2026-10-01'}])
        c = s.create_task('sub this week')
        s.add_subtasks(c['id'], [{'title': 'x', 'due_at': '2026-10-10'}])
        d = s.create_task('only done sub has a date')
        d, ids = s.add_subtasks(d['id'], [{'title': 'x', 'due_at': '2026-10-01', 'done': True}])
        self.assertEqual(self.titles(view='today'), ['sub due today'])
        self.assertEqual(self.titles(view='overdue'), ['sub overdue'])
        self.assertEqual(sorted(self.titles(view='week')), ['sub due today', 'sub this week'])
        self.assertEqual(self.titles(view='nodate'), ['only done sub has a date'])
        self.assertEqual(self.titles(sort='due'), ['sub overdue', 'sub due today', 'sub this week',
                                                   'only done sub has a date'])
        self.assertEqual(s.list_tasks(view='overdue')[0]['eff_due'], '2026-10-01')
        self.assertEqual(s.meta()['counts']['overdue'], 1)

    def test_subtask_reminders(self):
        s = self.store
        t = s.create_task('Party')
        t, ids = s.add_subtasks(t['id'], [{'title': 'call', 'due_at': '2026-10-06T16:00'},
                                          {'title': 'done one', 'due_at': '2026-10-06T16:00', 'done': True},
                                          {'title': 'date only', 'due_at': '2026-10-07'}])
        other = s.create_task('Finished')
        other, oids = s.add_subtasks(other['id'], [{'title': 'of a done task', 'due_at': '2026-10-06T16:00'}])
        s.update_task(other['id'], {'status': 'done'})
        got = s.due_reminders(datetime(2026, 10, 6, 16, 0))
        self.assertEqual([(i['kind'], i['title'], i['task_title']) for i in got], [('subtask', 'call', 'Party')])
        self.assertEqual(s.due_reminders(datetime(2026, 10, 6, 17, 0)), [])
        got = s.due_reminders(datetime(2026, 10, 7, 9, 0))
        self.assertEqual([i['title'] for i in got], ['date only'])
        s.update_subtask(ids[0], {'due_at': '2026-10-08T10:00'})
        self.assertEqual([i['title'] for i in s.due_reminders(datetime(2026, 10, 8, 10, 0))], ['call'])

    def test_label_filter_marks_matching_subtasks(self):
        t, ids = self.party()
        self.store.update_subtask(ids[1], {'labels': ['fun']})
        fun = next(l['id'] for l in self.store.list_labels() if l['name'] == 'fun')
        hit = self.store.list_tasks(label_ids=[fun])[0]
        self.assertEqual(hit['match_subs'], [ids[1]])
        hit = self.store.list_tasks(label_ids=[fun], q='cake')[0]
        self.assertEqual(hit['match_subs'], [])

    def test_restore_and_export_keep_details(self):
        t, ids = self.party()
        t, new = self.store.add_subtasks(t['id'], [{'title': 'Back', 'position': 0, 'due_at': '2026-10-09',
                                                    'labels': ['home'], 'notes': 'kept'}])
        sub = t['subtasks'][0]
        self.assertEqual((sub['title'], sub['due_at'], sub['notes'], [l['name'] for l in sub['labels']]),
                         ('Back', '2026-10-09', 'kept', ['home']))
        exported = self.store.export()['tasks'][0]['subtasks'][0]
        self.assertEqual((exported['due_at'], exported['labels'], exported['notes']), ('2026-10-09', ['home'], 'kept'))
        with self.assertRaises(db.ValidationError):
            self.store.add_subtasks(t['id'], [{'title': 'bad', 'labels': ['nope']}])


class MatrixTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.a = self.store.create_task('A', priority='low')
        self.b = self.store.create_task('B')
        self.c = self.store.create_task('C')
        _, self.subs = self.store.add_subtasks(self.b['id'], ['b1', 'b2'])
        self.ka, self.kb, self.kc = f"t{self.a['id']}", f"t{self.b['id']}", f"t{self.c['id']}"
        self.ks = f"s{self.subs[0]}"

    def test_positions_priority_and_do_count(self):
        tasks = self.store.set_matrix([{'key': self.ka, 'matrix': [0.9, 0.8], 'priority': 'high'},
                                       {'key': self.ks, 'matrix': [0.6, 0.55]}])
        self.assertEqual(sorted(t['title'] for t in tasks), ['A', 'B'])
        a = self.store.get_task(self.a['id'])
        self.assertEqual((a['matrix'], a['priority']), ([0.9, 0.8], 'high'))
        self.assertEqual(self.store.get_task(self.b['id'])['subtasks'][0]['matrix'], [0.6, 0.55])
        self.assertEqual(self.store.meta()['counts']['do'], 2)
        self.store.set_matrix([{'key': self.ka, 'matrix': None}])
        self.assertIsNone(self.store.get_task(self.a['id'])['matrix'])
        self.assertEqual(self.store.meta()['counts']['do'], 1)
        for bad in ([], [{'key': self.ks, 'matrix': [0.5, 0.5], 'priority': 'high'}],
                    [{'key': 'q1', 'matrix': None}], [{'key': self.ka, 'matrix': [0.5]}],
                    [{'key': self.ka, 'matrix': [-0.1, 0.5]}], [{'key': self.ka, 'matrix': ['a', 0.5]}],
                    [{'key': self.ka, 'matrix': [True, 0.5]}], [{'key': self.ka}],
                    [{'key': self.ka, 'matrix': None, 'extra': 1}], [{'key': self.ka, 'matrix': None, 'priority': 'max'}]):
            with self.subTest(bad=str(bad)[:60]):
                with self.assertRaises(db.ValidationError):
                    self.store.set_matrix(bad)
        with self.assertRaises(db.NotFound):
            self.store.set_matrix([{'key': 't99999', 'matrix': None}])

    def test_dependencies_self_loops_duplicates(self):
        s = self.store
        dep_id, created, deps = s.add_dep(self.ka, self.ks)
        self.assertTrue(created)
        self.assertEqual((deps[0]['before'], deps[0]['after'], deps[0]['before_title']), (self.ka, self.ks, 'A'))
        self.assertEqual(s.add_dep(self.ka, self.ks)[:2], (dep_id, False), 'a duplicate is a no-op')
        s.add_dep(self.ks, self.kc)
        with self.assertRaises(db.ValidationError) as cm:
            s.add_dep(self.kc, self.ka)
        self.assertIn('loop', str(cm.exception))
        with self.assertRaises(db.ValidationError):
            s.add_dep(self.ka, self.ka)
        with self.assertRaises(db.NotFound):
            s.add_dep(self.ka, 't99999')
        with self.assertRaises(db.ValidationError):
            s.add_dep(self.ka, 'x1')
        self.assertEqual(len(s.list_deps()), 2)
        s.update_task(self.a['id'], {'status': 'done'})
        self.assertTrue(next(d for d in s.list_deps() if d['before'] == self.ka)['before_done'])
        s.update_task(self.b['id'], {'status': 'done'})
        self.assertTrue(next(d for d in s.list_deps() if d['before'] == self.ks)['before_done'],
                        'a subtask of a done task counts as done')

    def test_links_live_only_while_both_items_exist(self):
        s = self.store
        s.add_dep(self.ka, self.ks)
        s.add_dep(self.kb, self.kc)
        s.delete_subtask(self.subs[0])
        self.assertEqual([(d['before'], d['after']) for d in s.list_deps()], [(self.kb, self.kc)])
        s.delete_task(self.c['id'])
        self.assertEqual(s.list_deps(), [])
        with s.write() as c:
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute('INSERT INTO dependencies(before_task_id, before_subtask_id, after_task_id, created_at) '
                          'VALUES (?, ?, ?, ?)', (self.a['id'], self.subs[1], self.b['id'], 'x'))
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute('INSERT INTO dependencies(before_task_id, after_task_id, created_at) VALUES (?, ?, ?)',
                          (99999, self.b['id'], 'x'))
        did, _, _ = s.add_dep(self.ka, self.kb)
        self.assertEqual(s.delete_dep(did), [])
        with self.assertRaises(db.NotFound):
            s.delete_dep(did)

    def test_export_and_restore_keep_positions(self):
        self.store.set_matrix([{'key': self.ka, 'matrix': [0.1, 0.2]}, {'key': self.ks, 'matrix': [0.3, 0.4]}])
        self.store.add_dep(self.ka, self.ks)
        data = self.store.export()
        a = next(t for t in data['tasks'] if t['title'] == 'A')
        b = next(t for t in data['tasks'] if t['title'] == 'B')
        self.assertEqual((a['matrix'], b['subtasks'][0]['matrix']), ([0.1, 0.2], [0.3, 0.4]))
        self.assertEqual(data['dependencies'][0]['before'], self.ka)
        self.assertNotIn('before_title', data['dependencies'][0])
        t, new = self.store.add_subtasks(self.b['id'], [{'title': 'restored', 'matrix': [0.7, 0.7]}])
        self.assertEqual(t['subtasks'][-1]['matrix'], [0.7, 0.7])


class ImportTests(StoreCase):
    def source(self):
        src_path = os.path.join(self.dir, 'source.db')
        src = db.Store(src_path)
        src.open()
        a = src.create_task('Write report', description='see ![x](/images/0123456789abcdef0123456789abcdef.png)',
                            labels=['work', 'home'], due_at='2026-10-01', priority='high')
        a, sids = src.add_subtasks(a['id'], [{'title': 'Draft', 'labels': ['work'], 'notes': 'n1', 'due_at': '2026-10-02'},
                                             {'title': 'Review', 'done': True}])
        b = src.create_task('Second')
        src.update_task(b['id'], {'status': 'in_progress'})
        src.set_matrix([{'key': f"t{a['id']}", 'matrix': [0.7, 0.8]}, {'key': f's{sids[0]}', 'matrix': [0.6, 0.6]}])
        src.add_dep(f's{sids[0]}', f"t{b['id']}")
        src.update_label(next(l['id'] for l in src.list_labels() if l['name'] == 'home'), color='#123456')
        data = json.loads(json.dumps(src.export()))
        src.close()
        return data

    def test_round_trip_keeps_everything_and_maps_ids(self):
        data = self.source()
        self.store.create_task('Already here')            # shifts the ids
        summary = self.store.import_data(data)
        self.assertEqual(summary, {'tasks': 2, 'skipped': 0, 'invalid': 0, 'subtasks': 2, 'labels': 2,
                                   'dependencies': 1, 'dependencies_skipped': 0})
        t = next(x for x in self.store.list_tasks(view='all') if x['title'] == 'Write report')
        full = self.store.get_task(t['id'])
        src = next(x for x in data['tasks'] if x['title'] == 'Write report')
        for key in ('created_at', 'updated_at', 'due_at', 'priority', 'status', 'description', 'matrix'):
            self.assertEqual(full[key], src[key], key)
        self.assertEqual([l['name'] for l in full['labels']], ['work', 'home'])
        self.assertEqual(next(l['color'] for l in self.store.list_labels() if l['name'] == 'home'), '#123456')
        subs = [(x['title'], x['done'], x['due_at'], x['notes'], [l['name'] for l in x['labels']], x['matrix'], x['created_at'])
                for x in full['subtasks']]
        self.assertEqual(subs[0][:6], ('Draft', False, '2026-10-02', 'n1', ['work'], [0.6, 0.6]))
        self.assertEqual(subs[1][:2], ('Review', True))
        self.assertEqual(subs[0][6], src['subtasks'][0]['created_at'])
        deps = self.store.list_deps()
        self.assertEqual(len(deps), 1)
        self.assertEqual((deps[0]['before_title'], deps[0]['after_title']), ('Draft', 'Second'))
        self.assertEqual(self.store.due_reminders(NOW), [], 'old targets do not fire a storm of reminders')
        self.assertEqual([x['title'] for x in self.store.list_tasks(q='review')], ['Write report'])

    def test_importing_twice_adds_nothing(self):
        data = self.source()
        self.store.import_data(data)
        before = self.store.counts()
        summary = self.store.import_data(data)
        self.assertEqual((summary['tasks'], summary['skipped'], summary['dependencies']), (0, 2, 0))
        self.assertEqual(self.store.counts(), before)

    def test_lenient_formats_and_bad_entries(self):
        summary = self.store.import_data({'tasks': [
            {'id': 'a', 'title': 'Variant', 'status': 'In Progress', 'priority': 'H', 'labels': [{'name': 'two words'}],
             'mx': 0.2, 'my': 0.3, 'created_at': '2025-01-01 10:00:00', 'due': '2025-02-01 09:30:00',
             'subtasks': [{'id': 'x', 'title': 'child', 'labels': ['two-words', 'stranger'], 'position': 1},
                          {'id': 'y', 'title': 'first', 'position': 0}, {'title': ''}]},
            {'title': ''}, 'junk', {'title': 'Done one', 'done': True}],
            'deps': [{'before_task_id': 'a', 'after_subtask_id': 'x'}, {'before': 'tnope', 'after': 'ta'}]})
        self.assertEqual((summary['tasks'], summary['subtasks'], summary['invalid'], summary['dependencies'],
                          summary['dependencies_skipped']), (2, 2, 3, 1, 1))
        v = next(x for x in self.store.list_tasks(view='all') if x['title'] == 'Variant')
        self.assertEqual((v['status'], v['priority'], v['matrix'], v['created_at'], v['due_at']),
                         ('in_progress', 'high', [0.2, 0.3], '2025-01-01T10:00:00', '2025-02-01T09:30'))
        self.assertEqual([x['title'] for x in v['subtasks']], ['first', 'child'])
        self.assertEqual([l['name'] for l in v['subtasks'][1]['labels']], ['two-words'], 'only the task\'s labels')
        self.assertEqual(next(x for x in self.store.list_tasks(view='all') if x['title'] == 'Done one')['status'], 'done')
        for bad in ([], {'tasks': 'x'}, 'text', {'labels': []}):
            with self.assertRaises(db.ValidationError):
                self.store.import_data(bad)


PHASE2_SCHEMA = """
CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open', priority TEXT NOT NULL DEFAULT 'medium', created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, due_at TEXT, completed_at TEXT, reminded_at TEXT);
CREATE TABLE labels (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    color TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE task_labels (task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE, PRIMARY KEY (task_id, label_id));
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE subtasks (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    title TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, completed_at TEXT);
INSERT INTO tasks(title, created_at, updated_at) VALUES ('phase two', '2026-01-01T00:00:00', '2026-01-01T00:00:00');
INSERT INTO subtasks(task_id, title, position, created_at) VALUES (1, 'old subtask', 0, '2026-01-01T00:00:00');
"""


class SubtaskMigrationTests(unittest.TestCase):
    def test_upgrade_from_subtasks_without_details(self):
        d = scratch_dir()
        try:
            path = os.path.join(d, 'todo.db')
            c = sqlite3.connect(path)
            c.executescript(PHASE2_SCHEMA)
            c.close()
            s = db.Store(path)
            s.open()
            t = s.get_task(1)
            self.assertEqual(t['subtasks'][0]['title'], 'old subtask')
            self.assertEqual((t['subtasks'][0]['due_at'], t['subtasks'][0]['notes']), (None, ''))
            with s.read() as c:
                names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
                cols = {r[1] for r in c.execute('PRAGMA table_info(subtasks)')}
                tcols = {r[1] for r in c.execute('PRAGMA table_info(tasks)')}
                self.assertTrue(c.execute("SELECT 1 FROM sqlite_master WHERE name = 'dependencies'").fetchone())
            self.assertEqual(names, {n for n, _ in db.TRIGGERS})
            self.assertTrue({'mx', 'my', 'notes', 'due_at'} <= cols and {'mx', 'my'} <= tcols)
            t = s.update_subtask(t['subtasks'][0]['id'], {'notes': 'now possible'})
            self.assertEqual(t['subtasks'][0]['notes'], 'now possible')
            s.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)


OLD_SCHEMA = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open', priority TEXT NOT NULL DEFAULT 'medium',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, due_at TEXT, completed_at TEXT);
CREATE TABLE labels (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    color TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE task_labels (task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE, PRIMARY KEY (task_id, label_id));
INSERT INTO tasks(title, description, created_at, updated_at, due_at) VALUES
    ('old one', 'from the first version', '2025-01-02T03:04:05', '2025-01-02T03:04:05', '2025-02-01');
INSERT INTO labels(name, color, created_at) VALUES ('legacy', '#e11d48', '2025-01-02T03:04:05');
INSERT INTO task_labels VALUES (1, 1);
"""

MIGRATE_SCRIPT = """
import sys, time
sys.path.insert(0, sys.argv[1])
import db
time.sleep(max(0, float(sys.argv[3]) - time.time()))
s = db.Store(sys.argv[2]); s.open(); s.close()
print('ok')
"""


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.dir = scratch_dir()
        self.path = os.path.join(self.dir, 'todo.db')
        c = sqlite3.connect(self.path)
        c.executescript(OLD_SCHEMA)
        c.close()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def check_upgraded(self):
        s = db.Store(self.path)
        s.open()
        try:
            with s.read() as c:
                cols = {r[1] for r in c.execute('PRAGMA table_info(tasks)')}
                for col in ('reminded_at',):
                    self.assertIn(col, cols)
                self.assertTrue(c.execute("SELECT 1 FROM sqlite_master WHERE name = 'subtasks'").fetchone())
                for table, _ in db.TABLES:
                    self.assertTrue(c.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone(), table)
                self.assertEqual(c.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
            t = s.get_task(1)
            self.assertEqual((t['title'], t['created_at']), ('old one', '2025-01-02T03:04:05'))
            self.assertEqual([l['name'] for l in t['labels']], ['legacy'])
            self.assertEqual([x['title'] for x in s.list_tasks(q='first')], ['old one'])
        finally:
            s.close()

    def test_upgrade_old_schema(self):
        self.check_upgraded()

    def test_two_processes_migrate_at_once(self):
        script = os.path.join(self.dir, 'migrate.py')
        with open(script, 'w') as f:
            f.write(MIGRATE_SCRIPT)
        start = str(time.time() + 1.0)
        procs = [subprocess.Popen([sys.executable, script, APP_DIR, self.path, start],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
        for p in procs:
            out, err = p.communicate(30)
            self.assertEqual(p.returncode, 0, err.decode())
            self.assertEqual(out.decode().strip(), 'ok')
        self.check_upgraded()

    def test_normal_start_needs_no_write_lock(self):
        s = db.Store(self.path)
        s.open()
        s.close()
        blocker = sqlite3.connect(self.path, isolation_level=None)
        blocker.execute('BEGIN IMMEDIATE')
        try:
            t0 = time.time()
            s = db.Store(self.path)
            s.open()
            s.close()
            self.assertLess(time.time() - t0, 2)
        finally:
            blocker.execute('ROLLBACK')
            blocker.close()

    def test_wal_switch_waits_for_readers(self):
        reader = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        reader.execute('BEGIN')
        reader.execute('SELECT count(*) FROM tasks').fetchone()

        def release():
            time.sleep(0.6)
            reader.execute('COMMIT')
            reader.close()

        threading.Thread(target=release).start()
        s = db.Store(self.path)
        s.open()
        with s.read() as c:
            self.assertEqual(c.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        s.close()


PNG = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89'
       b'\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x8b\x8a\x86\x00\x00\x00\x00IEND\xaeB`\x82')


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = AppServer()

    @classmethod
    def tearDownClass(cls):
        cls.srv.cleanup()

    def req(self, *a, **kw):
        return request(self.srv.port, *a, **kw)

    def test_ping_and_state(self):
        st, body, _ = self.req('GET', '/api/ping')
        self.assertEqual(st, 200)
        self.assertEqual(body['app'], 'TodoTracker')
        self.assertEqual(os.path.realpath(body['app_dir']), os.path.realpath(APP_DIR))
        self.assertEqual(os.path.realpath(body['data_dir']), os.path.realpath(self.srv.data))
        self.assertEqual(len(body['build']), 40)
        st, s1, _ = self.req('GET', '/api/state')
        st, _, hdrs = self.req('POST', '/api/tasks', {'title': 'counted'})
        self.assertEqual(st, 201)
        st, s2, _ = self.req('GET', '/api/state')
        self.assertEqual(s2['changes'], s1['changes'] + 1)
        self.assertEqual(hdrs.get('X-Todo-Changes'), str(s2['changes']))

    def test_security_checks(self):
        st, body, _ = self.req('GET', '/api/ping', headers={'Host': 'evil.example:8765'})
        self.assertEqual(st, 403)
        st, body, _ = self.req('GET', '/', headers={'Host': 'attacker.test'})
        self.assertEqual(st, 403)
        st, body, _ = self.req('POST', '/api/tasks', {'title': 'x'}, headers={'X-Todo': None})
        self.assertEqual(st, 403)
        st, body, _ = self.req('GET', '/api/ping', headers={'Host': 'localhost:1234'})
        self.assertEqual(st, 200)
        st, body, hdrs = self.req('GET', '/')
        self.assertEqual(st, 200)
        self.assertIn("script-src 'self' 'nonce-", hdrs['Content-Security-Policy'])
        self.assertNotIn(b'__BUILD__', body)
        for path in ('/../todo.pyw', '/db.py', '/images/../todo.db', '/web/app.js', '/%2e%2e/db.py'):
            st, _, _ = self.req('GET', path)
            self.assertEqual(st, 404, path)

    def test_validation_errors(self):
        cases = [
            ('POST', '/api/tasks', {'title': 'x', 'bogus': 1}, 400, 'Unknown field'),
            ('POST', '/api/tasks', {}, 400, 'Missing field'),
            ('POST', '/api/tasks', {'title': 7}, 400, 'wrong type'),
            ('POST', '/api/tasks', {'title': 'x', 'labels': 'a'}, 400, 'wrong type'),
            ('POST', '/api/tasks', {'title': 'x', 'due_at': '31.12.'}, 400, 'Invalid target date'),
            ('PATCH', '/api/tasks/999999', {'title': 'x'}, 404, 'not found'),
            ('DELETE', '/api/tasks/999999', None, 404, 'not found'),
            ('PATCH', '/api/labels/999999', {'name': 'x'}, 404, 'not found'),
            ('PATCH', '/api/labels/1', {}, 400, 'Nothing'),
            ('GET', '/api/tasks?view=nope', None, 400, 'Unknown view'),
            ('GET', '/api/tasks?labels=1,x', None, 400, 'Invalid labels'),
            ('GET', '/api/nothing', None, 404, 'Not found'),
            ('PUT', '/api/tasks', {'title': 'x'}, 405, 'not allowed'),
        ]
        for method, path, body, status, text in cases:
            with self.subTest(path=path, method=method):
                st, data, _ = self.req(method, path, body)
                self.assertEqual(st, status, data)
                self.assertIn(text.lower(), data['error'].lower())
        st, data, _ = self.req('POST', '/api/tasks', raw=b'{not json', headers={'Content-Type': 'application/json'})
        self.assertEqual(st, 400)
        st, data, _ = self.req('POST', '/api/tasks', raw=b'[1,2]', headers={'Content-Type': 'application/json'})
        self.assertEqual(st, 400)

    def test_subtask_api(self):
        st, t, _ = self.req('POST', '/api/tasks', {'title': 'With subtasks'})
        st, task, _ = self.req('POST', f'/api/tasks/{t["id"]}/subtasks', {'title': 'one'})
        self.assertEqual(st, 201)
        self.assertEqual([x['title'] for x in task['subtasks']], ['one'])
        self.assertEqual(len(task['created_ids']), 1)
        st, task, _ = self.req('POST', f'/api/tasks/{t["id"]}/subtasks', {'items': ['two', {'title': 'three', 'done': True}]})
        self.assertEqual(task['progress'], [1, 3])
        sid = task['subtasks'][0]['id']
        st, task, _ = self.req('PATCH', f'/api/subtasks/{sid}', {'done': True})
        self.assertEqual((st, task['progress']), (200, [2, 3]))
        ids = [x['id'] for x in task['subtasks']]
        st, task, _ = self.req('POST', f'/api/tasks/{t["id"]}/subtasks/order', {'ids': ids[::-1]})
        self.assertEqual([x['title'] for x in task['subtasks']], ['three', 'two', 'one'])
        st, task, _ = self.req('DELETE', f'/api/subtasks/{sid}')
        self.assertEqual((st, len(task['subtasks'])), (200, 2))
        for method, path, body, status in [
            ('POST', f'/api/tasks/{t["id"]}/subtasks', {'title': 'a', 'items': ['b']}, 400),
            ('POST', f'/api/tasks/{t["id"]}/subtasks', {}, 400),
            ('POST', f'/api/tasks/{t["id"]}/subtasks', {'title': ''}, 400),
            ('POST', '/api/tasks/999999/subtasks', {'title': 'x'}, 404),
            ('PATCH', f'/api/subtasks/{sid}', {'done': True}, 404),
            ('PATCH', f'/api/subtasks/{ids[1]}', {'done': 'yes'}, 400),
            ('PATCH', f'/api/subtasks/{ids[1]}', {'bogus': 1}, 400),
            ('PATCH', f'/api/subtasks/{ids[1]}', {}, 400),
            ('POST', f'/api/tasks/{t["id"]}/subtasks/order', {'ids': 'x'}, 400),
        ]:
            with self.subTest(path=path, body=body):
                st, data, _ = self.req(method, path, body)
                self.assertEqual(st, status, data)

    def test_subtask_detail_api(self):
        st, t, _ = self.req('POST', '/api/tasks', {'title': 'Details', 'labels': ['alpha']})
        st, task, _ = self.req('POST', f'/api/tasks/{t["id"]}/subtasks', {'title': 'sub'})
        sid = task['subtasks'][0]['id']
        st, task, _ = self.req('PATCH', f'/api/subtasks/{sid}', {'due_at': '2026-10-09', 'labels': ['alpha'], 'notes': 'n'})
        self.assertEqual(st, 200, task)
        sub = task['subtasks'][0]
        self.assertEqual((sub['due_at'], sub['notes'], sub['labels'][0]['name']), ('2026-10-09', 'n', 'alpha'))
        for body in ({'labels': ['beta']}, {'due_at': 'tomorrow'}, {'notes': None}, {'labels': [1]}):
            st, data, _ = self.req('PATCH', f'/api/subtasks/{sid}', body)
            self.assertEqual(st, 400, (body, data))

    def test_matrix_and_deps_api(self):
        st, a, _ = self.req('POST', '/api/tasks', {'title': 'first'})
        st, b, _ = self.req('POST', '/api/tasks', {'title': 'second'})
        st, res, _ = self.req('POST', '/api/matrix', {'items': [{'key': f't{a["id"]}', 'matrix': [0.8, 0.9]}]})
        self.assertEqual((st, res['tasks'][0]['matrix']), (200, [0.8, 0.9]))
        st, res, _ = self.req('POST', '/api/deps', {'before': f't{a["id"]}', 'after': f't{b["id"]}'})
        self.assertEqual((st, res['created']), (201, True))
        st, res, _ = self.req('POST', '/api/deps', {'before': f't{a["id"]}', 'after': f't{b["id"]}'})
        self.assertEqual((st, res['created']), (200, False))
        st, res, _ = self.req('POST', '/api/deps', {'before': f't{b["id"]}', 'after': f't{a["id"]}'})
        self.assertEqual(st, 400)
        self.assertIn('loop', res['error'])
        st, res, _ = self.req('GET', '/api/deps')
        dep = next(d for d in res['deps'] if d['before'] == f't{a["id"]}')
        st, res, _ = self.req('DELETE', f'/api/deps/{dep["id"]}')
        self.assertEqual(st, 200)
        for body in ({'before': 't1'}, {'before': 't1', 'after': 5}, {'before': 'nope', 'after': 't1'}):
            st, res, _ = self.req('POST', '/api/deps', body)
            self.assertEqual(st, 400, body)
        st, res, _ = self.req('POST', '/api/matrix', {'items': 'x'})
        self.assertEqual(st, 400)

    def test_import_api_reports_missing_images(self):
        export = {'tasks': [{'title': 'With picture', 'created_at': '2024-01-01T00:00:00',
                             'description': '![p](/images/0123456789abcdef0123456789abcdef.png)'}]}
        st, res, _ = self.req('POST', '/api/import', export)
        self.assertEqual(st, 200, res)
        self.assertEqual((res['summary']['tasks'], res['summary']['missing_images']), (1, 1))
        st, res, _ = self.req('POST', '/api/import', {'nothing': 1})
        self.assertEqual(st, 400)

    def test_images(self):
        st, data, _ = self.req('POST', '/api/images', raw=PNG, headers={'Content-Type': 'image/png'})
        self.assertEqual(st, 201, data)
        self.assertRegex(data['url'], r'^/images/[0-9a-f]{32}\.png$')
        st, body, hdrs = self.req('GET', data['url'])
        self.assertEqual((st, body, hdrs['Content-Type']), (200, PNG, 'image/png'))
        st, data, _ = self.req('POST', '/api/images', raw=b'<svg onload=alert(1)>', headers={'Content-Type': 'image/svg+xml'})
        self.assertEqual(st, 400)
        big = b'\x89PNG\r\n\x1a\n' + b'0' * (25 * 1024 * 1024)
        st, data, _ = self.req('POST', '/api/images', raw=big, headers={'Content-Type': 'image/png'}, timeout=60)
        self.assertEqual(st, 413)

    def test_export_and_backup(self):
        self.req('POST', '/api/tasks', {'title': 'exported', 'labels': ['e']})
        st, body, hdrs = self.req('GET', '/api/export')
        self.assertEqual(st, 200)
        self.assertIn('attachment; filename="todotracker-export-', hdrs['Content-Disposition'])
        self.assertIn('exported', [t['title'] for t in body['tasks']])
        st, body, _ = self.req('POST', '/api/backup', {})
        self.assertEqual(st, 200)
        self.assertTrue(os.path.exists(os.path.join(self.srv.data, 'backups', body['file'])))


class PlatformTests(unittest.TestCase):
    """OS helpers. Commands are argument lists, so task text never reaches a shell."""

    EVIL = 'Pay "rent" $(touch /tmp/x) `id` \'; & | \\ %PATH% end'

    def on(self, name):
        p = mock.patch.multiple(platforms, IS_WINDOWS=name == 'windows', IS_MAC=name == 'mac',
                                IS_LINUX=name == 'linux')
        p.start()
        self.addCleanup(p.stop)

    def which(self, *found):
        p = mock.patch.object(platforms.shutil, 'which', lambda name: '/usr/bin/' + name if name in found else None)
        p.start()
        self.addCleanup(p.stop)

    def test_windows_toast_gets_texts_through_the_environment(self):
        self.on('windows')
        cmd = platforms.notification_command(self.EVIL, 'body')
        self.assertIn('-EncodedCommand', cmd)
        self.assertFalse(any('rent' in part for part in cmd))

    def test_mac_notification_passes_texts_as_arguments(self):
        self.on('mac')
        cmd = platforms.notification_command(self.EVIL, 'Due today')
        self.assertEqual(cmd[0], 'osascript')
        self.assertEqual(cmd[-2:], [self.EVIL, 'Due today'])
        self.assertFalse(any('rent' in part for part in cmd[:-2]))

    def test_linux_notify_send(self):
        self.on('linux')
        self.which('notify-send', 'gdbus')
        cmd = platforms.notification_command(self.EVIL, 'Due today')
        self.assertEqual(cmd[0], 'notify-send')
        self.assertEqual(cmd[-3:], ['--', self.EVIL, 'Due today'])

    def test_linux_dbus_fallback_quotes_texts(self):
        self.on('linux')
        self.which('gdbus')
        cmd = platforms.notification_command("it's \\ fine", 'two\nlines')
        self.assertEqual(cmd[0], 'gdbus')
        self.assertIn("'it\\'s \\\\ fine'", cmd)
        self.assertIn("'two\\nlines'", cmd)
        self.which()
        self.assertIsNone(platforms.notification_command('a', 'b'))

    def test_notify_can_be_suppressed(self):
        with mock.patch.dict(os.environ, {'TODOTRACKER_NO_NOTIFY': '1'}), \
                mock.patch.object(platforms, '_run', side_effect=AssertionError('must not run')):
            self.assertTrue(platforms.notify('t', 'b'))

    def test_window_commands(self):
        url = 'http://127.0.0.1:8899/'
        self.assertEqual(platforms.window_command(url, ('exe', '/usr/bin/chromium')),
                         ['/usr/bin/chromium', '--app=' + url, '--window-size=1200,820'])
        self.assertEqual(platforms.window_command(url, ('mac-app', '/Applications/Google Chrome.app')),
                         ['open', '-n', '-a', '/Applications/Google Chrome.app', '--args',
                          '--app=' + url, '--window-size=1200,820'])

    def test_browser_choice(self):
        with mock.patch.dict(os.environ, {'TODOTRACKER_BROWSER': '/opt/my/browser'}):
            self.assertEqual(platforms.find_browser(), ('exe', '/opt/my/browser'))
        env = dict(os.environ)
        env.pop('TODOTRACKER_BROWSER', None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.on('linux')
            self.which('chromium-browser', 'brave-browser')
            self.assertEqual(platforms.find_browser(), ('exe', '/usr/bin/chromium-browser'))
            self.which()
            self.assertIsNone(platforms.find_browser())

    def test_no_window_mode_opens_and_activates_nothing(self):
        with mock.patch.dict(os.environ, {'TODOTRACKER_NO_WINDOW': '1'}), \
                mock.patch.object(platforms, '_spawn', side_effect=AssertionError('must not start')), \
                mock.patch.object(platforms, '_run', side_effect=AssertionError('must not run')):
            self.assertTrue(platforms.open_window('http://127.0.0.1:1/'))
            self.assertFalse(platforms.activate_window())

    @unittest.skipIf(os.name == 'nt', 'POSIX process helpers')
    def test_process_helpers(self):
        self.assertTrue(platforms.process_alive(os.getpid()))
        self.assertIn('python', platforms.process_cmdline(os.getpid()).lower())
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        child.wait(10)
        self.assertFalse(platforms.process_alive(child.pid))
        if not platforms.shutil.which('lsof'):
            self.skipTest('lsof is not installed')
        import socket
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            sock.listen(1)
            self.assertEqual(platforms.port_owner(sock.getsockname()[1]), os.getpid())
        self.assertIsNone(platforms.port_owner(free_port()))


class InstanceTests(unittest.TestCase):
    """Single instance, updates and races (each test uses its own port and data)."""

    def setUp(self):
        self.cleanups = []

    def tearDown(self):
        for fn in reversed(self.cleanups):
            try:
                fn()
            except Exception:
                pass

    def start(self, **kw):
        srv = AppServer(**kw)
        self.cleanups.append(srv.cleanup)
        return srv

    def run_app(self, app_dir, port, data, timeout=40):
        env = dict(os.environ, TODOTRACKER_DATA=data, TODOTRACKER_NO_WINDOW='1', TODOTRACKER_NO_NOTIFY='1',
                   PYTHONDONTWRITEBYTECODE='1')
        return subprocess.run([sys.executable, os.path.join(app_dir, 'todo.pyw'), '--port', str(port)],
                              env=env, capture_output=True, timeout=timeout)

    def ping(self, port):
        st, body, _ = request(port, 'GET', '/api/ping', timeout=3)
        return body

    def test_same_build_shows_window_and_exits(self):
        srv = self.start()
        t0 = time.time()
        r = self.run_app(APP_DIR, srv.port, srv.data)
        self.assertEqual(r.returncode, 0)
        self.assertLess(time.time() - t0, 10)
        self.assertEqual(self.ping(srv.port)['pid'], srv.proc.pid)
        self.assertIn('already runs', srv.log())
        self.assertIn('opening window', srv.log())
        # The running app was asked to show itself (the page then focuses quick add).
        self.assertEqual(srv.api('GET', '/api/state')['hotkey'], 1)

    @unittest.skipIf(os.name == 'nt', 'uses a shell script as the browser')
    def test_window_opens_in_the_chosen_browser(self):
        tmp = scratch_dir()
        self.cleanups.append(lambda: shutil.rmtree(tmp, ignore_errors=True))
        calls = os.path.join(tmp, 'calls.txt')
        fake = os.path.join(tmp, 'browser')
        with open(fake, 'w') as f:
            f.write('#!/bin/sh\necho "$@" >> "$TT_CALLS"\n')
        os.chmod(fake, 0o755)
        env = {'TODOTRACKER_NO_WINDOW': '0', 'TODOTRACKER_BROWSER': fake, 'TT_CALLS': calls,
               'PATH': '/usr/bin:/bin'}   # no wmctrl/xdotool from elsewhere

        def lines(n):
            deadline = time.time() + 15
            while time.time() < deadline:
                if os.path.exists(calls):
                    with open(calls) as f:
                        got = f.read().splitlines()
                    if len(got) >= n:
                        return got
                time.sleep(0.05)
            self.fail('the browser was not started')

        srv = self.start(env=env, background=False)
        url = f'http://127.0.0.1:{srv.port}/'
        self.assertEqual(lines(1), [f'--app={url} --window-size=1200,820'])
        r = subprocess.run([sys.executable, os.path.join(APP_DIR, 'todo.pyw'), '--port', str(srv.port)],
                           env=dict(os.environ, TODOTRACKER_DATA=srv.data, PYTHONDONTWRITEBYTECODE='1', **env),
                           capture_output=True, timeout=40)
        self.assertEqual(r.returncode, 0)
        # Nothing can raise the existing window here, so the running app opened another.
        self.assertEqual(len(lines(2)), 2)
        self.assertEqual(srv.api('GET', '/api/state')['hotkey'], 1)

    def test_new_build_replaces_old_and_keeps_data(self):
        app = copy_app()
        self.cleanups.append(lambda: shutil.rmtree(app, ignore_errors=True))
        srv = self.start(app_dir=app)
        srv.api('POST', '/api/tasks', {'title': 'survives'}, expect=201)
        old = self.ping(srv.port)
        with open(os.path.join(app, 'web', 'app.css'), 'a') as f:
            f.write('\n/* changed */\n')
        new = AppServer(app_dir=app, port=srv.port, data_dir=srv.data, wait=False)
        self.cleanups.append(new.stop)
        info = new.wait_ready(30)
        self.assertNotEqual(info['build'], old['build'])
        self.assertIsNotNone(srv.proc.wait(10))
        self.assertEqual([t['title'] for t in new.api('GET', '/api/tasks')['tasks']], ['survives'])
        self.assertIn('replacing the running instance', new.log())

    def test_lower_api_is_replaced(self):
        app = copy_app()
        self.cleanups.append(lambda: shutil.rmtree(app, ignore_errors=True))
        server_py = os.path.join(app, 'server.py')
        with open(server_py) as f:
            src = f.read()
        import server as server_mod
        with open(server_py, 'w') as f:
            f.write(src.replace(f'API = {server_mod.API}\n', f'API = {server_mod.API - 1}\n', 1))
        srv = self.start(app_dir=app)
        self.assertEqual(self.ping(srv.port)['api'], server_mod.API - 1)
        with open(server_py, 'w') as f:
            f.write(src)
        new = AppServer(app_dir=app, port=srv.port, data_dir=srv.data, wait=False)
        self.cleanups.append(new.stop)
        info = new.wait_ready(30)
        self.assertEqual(info['api'], server_mod.API)
        self.assertIsNotNone(srv.proc.wait(10))

    def test_copy_in_other_folder_never_replaces(self):
        srv = self.start()
        other = copy_app()
        self.cleanups.append(lambda: shutil.rmtree(other, ignore_errors=True))
        with open(os.path.join(other, 'web', 'app.css'), 'a') as f:
            f.write('\n/* other */\n')
        r = self.run_app(other, srv.port, srv.data)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.ping(srv.port)['pid'], srv.proc.pid)

    def test_racing_launches_leave_one_instance(self):
        port = free_port()
        data = scratch_dir()
        self.cleanups.append(lambda: shutil.rmtree(data, ignore_errors=True))
        env = dict(os.environ, TODOTRACKER_DATA=data, TODOTRACKER_NO_WINDOW='1', TODOTRACKER_NO_NOTIFY='1',
                   PYTHONDONTWRITEBYTECODE='1')
        procs = [subprocess.Popen([sys.executable, os.path.join(APP_DIR, 'todo.pyw'), '--background',
                                   '--port', str(port)], env=env, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL) for _ in range(3)]
        self.cleanups.append(lambda: [p.kill() for p in procs if p.poll() is None])
        deadline = time.time() + 30
        while time.time() < deadline and sum(p.poll() is None for p in procs) > 1:
            time.sleep(0.1)
        running = [p for p in procs if p.poll() is None]
        self.assertEqual(len(running), 1)
        self.assertTrue(all(p.returncode == 0 for p in procs if p.poll() is not None))
        self.assertEqual(self.ping(port)['pid'], running[0].pid)
        request(port, 'POST', '/api/shutdown', {})
        running[0].wait(10)

    def test_stuck_old_instance_is_stopped_only_if_it_is_todo_pyw(self):
        # A fake "old TodoTracker" that ignores /api/shutdown.
        port = free_port()
        data = scratch_dir()
        self.cleanups.append(lambda: shutil.rmtree(data, ignore_errors=True))
        fake_dir = scratch_dir()
        self.cleanups.append(lambda: shutil.rmtree(fake_dir, ignore_errors=True))
        fake_src = (
            'import json, os, sys\n'
            'from http.server import BaseHTTPRequestHandler, HTTPServer\n'
            'class H(BaseHTTPRequestHandler):\n'
            '    def log_message(self, *a): pass\n'
            '    def reply(self):\n'
            '        b = json.dumps({"app": "TodoTracker", "api": 0, "build": "old", "pid": os.getpid(),\n'
            '                        "app_dir": sys.argv[1], "data_dir": sys.argv[2]}).encode()\n'
            '        self.send_response(200); self.send_header("Content-Type", "application/json")\n'
            '        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)\n'
            '    do_GET = do_POST = reply\n'
            'HTTPServer(("127.0.0.1", int(sys.argv[3])), H).serve_forever()\n')
        for name in ('todo.pyw', 'stubborn.py'):
            with open(os.path.join(fake_dir, name), 'w') as f:
                f.write(fake_src)

        def fake(name):
            p = subprocess.Popen([sys.executable, os.path.join(fake_dir, name), APP_DIR, data, str(port)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.cleanups.append(lambda: p.kill())
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    if request(port, 'GET', '/api/ping', timeout=1)[0] == 200:
                        return p
                except OSError:
                    time.sleep(0.05)
            raise AssertionError('fake did not start')

        p = fake('stubborn.py')
        r = self.run_app(APP_DIR, port, data, timeout=60)
        self.assertEqual(r.returncode, 1, 'a process that is not todo.pyw must not be killed')
        self.assertIsNone(p.poll())
        p.kill()
        p.wait(5)
        p = fake('todo.pyw')
        env = dict(os.environ, TODOTRACKER_DATA=data, TODOTRACKER_NO_WINDOW='1', TODOTRACKER_NO_NOTIFY='1',
                   PYTHONDONTWRITEBYTECODE='1')
        new = subprocess.Popen([sys.executable, os.path.join(APP_DIR, 'todo.pyw'), '--background',
                                '--port', str(port)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.cleanups.append(lambda: new.kill())
        self.assertIsNotNone(p.wait(30), 'the old todo.pyw should have been stopped')
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if self.ping(port)['pid'] == new.pid:
                    break
            except OSError:
                pass
            time.sleep(0.1)
        self.assertEqual(self.ping(port)['pid'], new.pid)
        request(port, 'POST', '/api/shutdown', {})
        new.wait(10)


if __name__ == '__main__':
    unittest.main()
