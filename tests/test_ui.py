"""UI tests in headless Edge/Chromium with real mouse and keyboard input.

Run:  python -m unittest tests.test_ui -v     (TT_BROWSER selects the browser)
Every test starts its own TodoTracker on scratch data and a test port.
Synthetic element.click()/dispatchEvent would miss the bugs these tests are
about (redraws under a pressed mouse button, focus loss), so clicks are
Input.dispatchMouseEvent presses of ~100 ms and typing goes through
Input.insertText / Input.dispatchKeyEvent.
"""

import json
import os
import shutil
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from cdp import Browser  # noqa: E402
from helpers import AppServer, LocalPageServer, copy_app  # noqa: E402

PNG = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89'
       b'\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x8b\x8a\x86\x00\x00\x00\x00IEND\xaeB`\x82')


def js(value):
    return json.dumps(value, ensure_ascii=False)


class UICase(unittest.TestCase):
    browser = None

    @classmethod
    def setUpClass(cls):
        cls.browser = Browser()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()

    def setUp(self):
        self.srv = AppServer(**self.server_args())
        self.page = None
        self.expected_http_errors = 0

    def server_args(self):
        return {}

    def tearDown(self):
        errors = []
        if self.page:
            errors = [e for e in self.page.console_errors() if 'favicon' not in e]
            # A refused request (e.g. a loop) is logged by the browser as a failed resource.
            failed = [e for e in errors if 'Failed to load resource' in e and 'status of 400' in e]
            if failed and len(failed) <= self.expected_http_errors:
                errors = [e for e in errors if e not in failed]
            self.browser.close_page(self.page)
        self.srv.cleanup()
        self.assertEqual(errors, [], 'console errors')

    # -- helpers ---------------------------------------------------------

    def before_open(self):
        """Hook: the local-mode mix-in hands the data over to the new window."""

    def open(self, url=None, dark=False):
        self.before_open()
        self.page = self.browser.new_page()
        p = self.page
        p.send('Emulation.setDeviceMetricsOverride', {'width': 1200, 'height': 820, 'deviceScaleFactor': 1, 'mobile': False})
        p.send('Emulation.setEmulatedMedia', {'features': [{'name': 'prefers-color-scheme', 'value': 'dark' if dark else 'light'}]})
        p.navigate(url or self.srv.url)
        p.wait_for('!!(window.TT && TT.S.loaded)', timeout=8)
        return p

    def api(self, method, path, body=None, expect=None):
        return self.srv.api(method, path, body, expect)

    def task(self, title, **fields):
        return self.api('POST', '/api/tasks', dict(title=title, **fields), expect=201)

    def upload_image(self, data):
        from helpers import request
        st, img, _ = request(self.srv.port, 'POST', '/api/images', raw=data, headers={'Content-Type': 'image/png'})
        self.assertEqual(st, 201, img)
        return img

    def server_task(self, tid):
        return self.api('GET', f'/api/tasks/{tid}')

    def titles(self):
        return self.page.eval('Array.from(document.querySelectorAll("#list .row-title")).map(e => e.textContent)')

    def row_sel(self, title, part='.row-title'):
        # Aim only at a settled list: a refresh landing between finding the
        # row and pressing on it would move the row away (and the test would
        # click the empty list).
        self.page.wait_for('TT.idle()', timeout=8)
        idx = self.page.eval('Array.from(document.querySelectorAll("#list .row")).findIndex(r => '
                             'r.querySelector(".row-title").textContent === %s)' % js(title))
        self.assertGreaterEqual(idx, 0, f'row {title!r} not found in {self.titles()}')
        return f'#list .row:nth-of-type({idx + 1}) {part}'

    def wait_rows(self, titles, timeout=5):
        self.page.wait_for('JSON.stringify(Array.from(document.querySelectorAll("#list .row-title")).map(e => e.textContent)) === %s'
                           % js(json.dumps(titles, separators=(',', ':'), ensure_ascii=False)), timeout=timeout,
                           message=f'rows {titles}, have {self.titles() if self.page else None}')

    def quick_add(self, text, shift=False):
        p = self.page
        p.click('#qa')
        p.type(text)
        p.key('Enter', *(['shift'] if shift else []))

    def wait_saved(self):
        self.page.wait_for('(() => { const e = document.querySelector("#editor .save-state"); return e && e.textContent === "Saved"; })()',
                           timeout=5)

    def open_editor(self, title):
        self.page.click(self.row_sel(title))
        self.page.wait_for('!!document.querySelector("#editor:not([hidden]) .ed-title") && document.querySelector(".ed-title").value === %s' % js(title))


class LocalMode:
    """Mix-in: run a test class in the Android app's mode (no server; the data
    lives in the page, on IndexedDB). What a test sets up before opening the
    page goes through a short-lived tab of the same origin; later requests go
    through the open page (and look like changes made on another device)."""

    LOCAL = True

    def setUp(self):
        super().setUp()
        self.local = LocalPageServer()
        self.srv.url_override = self.local.url
        self._setup_page = None

    def tearDown(self):
        if self._setup_page:
            self.browser.close_page(self._setup_page)
            self._setup_page = None
        try:
            super().tearDown()
        finally:
            self.local.cleanup()

    def _runner(self):
        if self.page is not None:
            return self.page
        if self._setup_page is None:
            self._setup_page = self.browser.new_page()
            self._setup_page.navigate(self.local.url)
            self._setup_page.wait_for('!!(window.TT && TT.S.loaded)', timeout=8)
        return self._setup_page

    def before_open(self):
        if self._setup_page:
            self.browser.close_page(self._setup_page)     # hands the data over to the app's window
            self._setup_page = None

    def open(self, url=None, dark=False):
        p = super().open(url, dark)
        self.assertEqual(p.eval('TT.MODE'), 'local')
        return p

    def api(self, method, path, body=None, expect=None):
        res = self._runner().eval('TT.local.then(async (l) => { await l.sync(); return l.api.handle(%s, %s, %s); })'
                                  % (js(method), js(path), js(body)))
        if expect is not None:
            self.assertEqual(res['status'], expect, f'{method} {path} -> {res}')
        return res['data']

    def upload_image(self, data):
        import base64
        res = self._runner().eval('TT.local.then((l) => l.api.handle("POST", "/api/images",'
                                  ' Uint8Array.from(atob(%s), (c) => c.charCodeAt(0))))' % js(base64.b64encode(data).decode()))
        self.assertEqual(res['status'], 201, res)
        return res['data']


class QuickAddTests(UICase):
    def test_tokens_become_fields_and_chips(self):
        p = self.open()
        self.assertEqual(p.active(), 'qa', 'quick-add box is focused on load')
        p.type('Buy milk #home !h ^tom@9:30')
        chips = p.eval('document.querySelector("#qa-chips").textContent')
        self.assertIn('#home (new label)', chips)
        self.assertIn('High priority', chips)
        self.assertIn('09:30', chips)
        p.key('Enter')
        self.wait_rows(['Buy milk'])
        self.assertEqual(p.eval('document.querySelector("#qa").value'), '')
        t = self.api('GET', '/api/tasks')['tasks'][0]
        self.assertEqual(t['priority'], 'high')
        self.assertEqual([l['name'] for l in t['labels']], ['home'])
        self.assertTrue(t['due_at'].endswith('T09:30'))
        self.assertRegex(t['created_at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$')
        self.assertIn('Tomorrow 09:30', p.eval('document.querySelector("#list .row").textContent'))

    def test_label_suggestions_tab_arrows_enter(self):
        self.task('seed', labels=['household'])
        p = self.open()
        p.type('Clean #hou')
        p.wait_for('!document.querySelector("#qa-suggest").hidden')
        p.key('Tab')
        self.assertEqual(p.eval('document.querySelector("#qa").value'), 'Clean #household ')
        self.assertEqual(p.active(), 'qa')
        p.key('Enter')
        self.wait_rows(['Clean', 'seed'])
        # Enter without an arrow key adds the task as typed ("hou" is a new label).
        p.type('Call #hou')
        p.wait_for('!document.querySelector("#qa-suggest").hidden')
        p.key('Enter')
        p.wait_for('document.querySelectorAll("#list .row").length === 3')
        names = sorted(l['name'] for l in self.api('GET', '/api/meta')['labels'])
        self.assertEqual(names, ['hou', 'household'])
        # Arrow + Enter picks the suggestion instead of adding.
        p.type('Mail #hous')
        p.wait_for('!document.querySelector("#qa-suggest").hidden')
        p.key('ArrowDown')
        p.key('Enter')
        self.assertEqual(p.eval('document.querySelector("#qa").value'), 'Mail #household ')
        p.key('Enter')
        p.wait_for('document.querySelectorAll("#list .row").length === 4')
        mail = [t for t in self.api('GET', '/api/tasks')['tasks'] if t['title'] == 'Mail'][0]
        self.assertEqual([l['name'] for l in mail['labels']], ['household'])

    def test_shift_enter_opens_details(self):
        p = self.open()
        p.type('Plan the trip ^fri')
        p.key('Enter', 'shift')
        p.wait_for('!!document.querySelector(".ed-title") && document.querySelector(".ed-title").value === "Plan the trip"')
        p.wait_for('document.activeElement && document.activeElement.dataset.k === "ed:title"')

    def test_today_view_and_label_filter_defaults(self):
        self.task('seed', labels=['work'])
        p = self.open()
        p.click('[data-k="nav:today"]')
        p.wait_for('document.querySelector("#view-title").textContent === "Today"')
        self.quick_add('Due today automatically')
        self.wait_rows(['Due today automatically'])
        t = [t for t in self.api('GET', '/api/tasks')['tasks'] if t['title'] == 'Due today automatically'][0]
        today = p.eval('TT.P.ymd(new Date())')
        self.assertEqual(t['due_at'], today)
        p.click('[data-k="nav:open"]')
        p.click('[data-k^="lbl:"][data-k$=":name"]')
        self.wait_rows(['seed'])
        self.quick_add('Gets the filter label')
        self.wait_rows(['Gets the filter label', 'seed'])
        t = [t for t in self.api('GET', '/api/tasks')['tasks'] if t['title'] == 'Gets the filter label'][0]
        self.assertEqual([l['name'] for l in t['labels']], ['work'])


class ListTests(UICase):
    def test_done_with_undo(self):
        self.task('first')
        self.task('second')
        p = self.open()
        self.wait_rows(['second', 'first'])
        p.click(self.row_sel('first', '.check'))
        p.wait_for('Array.from(document.querySelectorAll(".toast")).some(t => t.textContent.includes("Done: first"))')
        self.wait_rows(['second'])
        p.click('.toast button:not(.toast-close)')
        self.wait_rows(['second', 'first'])
        self.assertEqual([t['status'] for t in self.api('GET', '/api/tasks')['tasks']], ['open', 'open'])

    def test_keyboard_done_keeps_focus_on_a_row(self):
        for t in ('a', 'b', 'c'):
            self.task(t)
        p = self.open()
        self.wait_rows(['c', 'b', 'a'])
        p.eval('document.querySelector(%s).focus()' % js(self.row_sel('b', '.check')))
        p.key(' ')
        self.wait_rows(['c', 'a'])
        time.sleep(0.3)
        active = p.active()
        self.assertNotEqual(active, 'BODY')
        self.assertTrue(active.startswith('t') and active.endswith(':check'), active)
        p.key('ArrowUp')
        self.assertTrue(p.active().endswith(':check'))

    def test_views_counts_and_sort_groups(self):
        today = time.strftime('%Y-%m-%d')
        self.task('overdue', due_at='2020-01-01')
        self.task('today', due_at=today)
        self.task('someday')
        p = self.open()
        p.wait_for('document.querySelector(\'[data-k="nav:overdue"] .count\').textContent === "1"')
        self.assertTrue(p.eval('document.querySelector(\'[data-k="nav:overdue"] .count\').classList.contains("alert")'))
        p.click('[data-k="nav:overdue"]')
        self.wait_rows(['overdue'])
        p.click('[data-k="nav:nodate"]')
        self.wait_rows(['someday'])
        p.click('[data-k="nav:open"]')
        p.eval('document.querySelector("#sort").focus()')
        p.key('ArrowDown')     # Newest -> Target date
        p.wait_for('Array.from(document.querySelectorAll(".group-head")).map(h => h.firstChild.textContent).join() === "Overdue,Today,No target date"')
        self.assertEqual(self.titles(), ['overdue', 'today', 'someday'])

    def test_search_ctrl_f_and_escape(self):
        self.task('Buy milk', description='semi-skimmed')
        self.task('Call Anna')
        p = self.open()
        p.key('f', 'ctrl')
        self.assertEqual(p.active(), 'search')
        p.type('semi')
        self.wait_rows(['Buy milk'])
        p.key('Escape')
        self.wait_rows(['Call Anna', 'Buy milk'])
        self.assertEqual(p.eval('document.querySelector("#search").value'), '')

    def test_ctrl_n_in_an_app_window(self):
        # Browsers reserve Ctrl+N in normal tabs; in an --app window (how
        # TodoTracker runs) the page gets it.
        app = Browser(app_url=self.srv.url)
        try:
            p = app.first_page()
            p.wait_for('!!(window.TT && TT.S.loaded)', timeout=8)
            p.eval('document.querySelector("#search").focus()')
            p.key('n', 'ctrl')
            p.wait_for('document.activeElement.id === "qa"')
            p.close()
        finally:
            app.close()

    def test_label_rename_merge_recolor_delete(self):
        self.task('a', labels=['alpha'])
        self.task('b', labels=['beta'])
        p = self.open()
        p.click('[data-k="lbl:1:edit"]')
        p.wait_for('document.activeElement.dataset.k === "lbl:1:rename"')
        p.key('a', 'ctrl')
        p.type('Gamma')
        p.key('Enter')
        p.wait_for('document.querySelector(\'[data-k="lbl:1:name"]\') && document.querySelector(\'[data-k="lbl:1:name"]\').textContent === "Gamma"')
        # Renaming onto an existing name merges the labels.
        p.click('[data-k="lbl:2:edit"]')
        p.key('a', 'ctrl')
        p.type('gamma')
        p.key('Enter')
        p.wait_for('Array.from(document.querySelectorAll(".toast")).some(t => t.textContent.includes("Merged"))')
        labels = self.api('GET', '/api/meta')['labels']
        self.assertEqual([(l['name'], l['open']) for l in labels], [('Gamma', 2)])
        # Recolour (the native colour picker itself cannot be driven headless).
        p.eval('(() => { const i = document.querySelector(\'.label-color input\'); i.value = "#00aa55";'
               ' i.dispatchEvent(new Event("change", {bubbles: true})); })()')
        p.wait_for('TT.S.meta.labels[0].color === "#00aa55"')
        p.send('Page.enable')
        p.click('[data-k="lbl:1:edit"]')
        p.wait_for('!!document.querySelector(\'[data-k="lbl:1:delete"]\')')
        dialog = p.click_dialog('[data-k="lbl:1:delete"]')
        self.assertIn('Delete the label', dialog['message'])
        p.wait_for('TT.S.meta.labels.length === 0')
        self.assertEqual(len(self.api('GET', '/api/tasks')['tasks']), 2, 'tasks stay')


class EditorTests(UICase):
    def test_fields_autosave(self):
        t = self.task('Original')
        p = self.open()
        self.open_editor('Original')
        p.click('.ed-title')
        p.key('End')
        p.type(' title')
        self.wait_saved()
        self.assertEqual(self.server_task(t['id'])['title'], 'Original title')
        self.wait_rows(['Original title'])
        p.eval('document.querySelector("#ed-status").focus()')
        p.key('ArrowDown')
        p.wait_for('TT.E.task.status === "in_progress"')
        p.eval('document.querySelector("#ed-priority").focus()')
        p.key('ArrowUp')
        p.wait_for('TT.E.task.priority === "high"')
        p.eval('document.querySelector("#ed-date").focus()')
        for ch in '10092030':
            p.key(ch)
        p.wait_for('TT.E.task.due_at === "2030-10-09"')
        p.click('[data-k="ed:nodate"]')
        p.wait_for('TT.E.task.due_at === null')
        st = self.server_task(t['id'])
        self.assertEqual((st['status'], st['priority'], st['due_at']), ('in_progress', 'high', None))

    def test_labels_editor(self):
        self.task('seed', labels=['urgent', 'errand'])
        t = self.task('Target')
        p = self.open()
        self.open_editor('Target')
        p.click('[data-k="ed:label-input"]')
        p.type('ur')
        p.wait_for('!document.querySelector("#ed-label-suggest").hidden')
        p.key('ArrowDown')
        p.key('Enter')
        p.wait_for('TT.E.task.labels.map(l => l.name).join() === "urgent"')
        p.type('fresh,')
        p.wait_for('TT.E.task.labels.map(l => l.name).join() === "urgent,fresh"')
        self.assertEqual(p.active(), 'ed:label-input')
        p.key('Backspace')
        p.wait_for('TT.E.task.labels.map(l => l.name).join() === "urgent"')
        self.assertEqual([l['name'] for l in self.server_task(t['id'])['labels']], ['urgent'])

    def test_markdown_preview_image_lightbox_and_links(self):
        self.task('Notes')
        p = self.open()
        self.open_editor('Notes')
        p.click('[data-k="ed:desc"]')
        p.type('# Hi\n**bold** and [a link](https://example.com/a_b_c)\n')
        p.wait_for('!!document.querySelector(".md-preview h1") && !!document.querySelector(".md-preview strong")')
        href = p.eval('document.querySelector(".md-preview a").getAttribute("href")')
        self.assertEqual(href, 'https://example.com/a_b_c')
        # Drop a real image file onto the description.
        img_path = os.path.join(self.srv.data, 'pixel.png')
        with open(img_path, 'wb') as f:
            f.write(PNG)
        x, y = p.center('[data-k="ed:desc"]')
        data = {'items': [], 'files': [img_path], 'dragOperationsMask': 1}
        for kind in ('dragEnter', 'dragOver', 'drop'):
            p.send('Input.dispatchDragEvent', {'type': kind, 'x': x, 'y': y, 'data': data})
        p.wait_for('!!document.querySelector(".md-preview img")', timeout=8)
        self.assertRegex(p.eval('document.querySelector("[data-k=\\"ed:desc\\"]").value'), r'!\[pixel\]\(/images/[0-9a-f]{32}\.png\)')
        p.click('.md-preview img')
        p.wait_for('!document.querySelector("#lightbox").hidden')
        p.key('Escape')
        p.wait_for('document.querySelector("#lightbox").hidden')
        self.assertTrue(p.eval('!!document.querySelector("#editor:not([hidden])")'), 'Esc closed only the lightbox')
        # A link opens outside the app; the app page stays where it is.
        before = len(self.browser.targets())
        p.click('.md-preview a')
        time.sleep(0.5)
        self.assertEqual(p.eval('location.pathname'), '/')
        self.assertGreater(len(self.browser.targets()), before)
        self.wait_saved()
        self.assertIn('/images/', self.api('GET', '/api/tasks/1')['description'])
        # Esc closes the editor.
        p.click('[data-k="ed:title"]')
        p.key('Escape')
        p.wait_for('document.querySelector("#editor").hidden')

    def test_delete_task(self):
        self.task('Doomed')
        self.task('Stays')
        p = self.open()
        self.open_editor('Doomed')
        dialog = p.click_dialog('[data-k="ed:delete"]')
        self.assertIn('Delete “Doomed”', dialog['message'])
        self.wait_rows(['Stays'])
        self.assertTrue(p.eval('document.querySelector("#editor").hidden'))


class RobustnessTests(UICase):
    def test_click_right_after_editing_a_title(self):
        self.task('Other')
        t = self.task('Edited')
        p = self.open()
        self.open_editor('Edited')
        p.click('.ed-title')
        p.key('End')
        p.type(' now')
        # Press on another row's check button: the title field blurs, its save
        # answers and redraws while the button is still held. The click must
        # still land.
        x, y = p.center(self.row_sel('Other', '.check'))
        p.move(x, y)
        p.mouse('mousePressed', x, y, buttons=1)
        time.sleep(0.6)
        p.mouse('mouseReleased', x, y)
        p.wait_for('Array.from(document.querySelectorAll(".toast")).some(t => t.textContent.includes("Done: Other"))')
        self.assertEqual(self.server_task(t['id'])['title'], 'Edited now')
        self.assertEqual([x['status'] for x in self.api('GET', '/api/tasks?view=all')['tasks'] if x['title'] == 'Other'], ['done'])

    def test_typing_while_redraws_happen(self):
        t = self.task('Busy')
        other = self.task('Background')
        p = self.open()
        self.open_editor('Busy')
        p.click('[data-k="ed:desc"]')
        p.eval('document.querySelector("[data-k=\\"ed:desc\\"]").__marker = 42')
        typed = ''
        for i in range(6):
            chunk = f'line {i} '
            p.type(chunk)
            typed += chunk
            # Another window changes things -> this window refreshes the list and reloads the editor.
            self.api('PATCH', f'/api/tasks/{other["id"]}', {'title': f'Background {i}'})
            self.api('PATCH', f'/api/tasks/{t["id"]}', {'priority': ['high', 'low'][i % 2]})
            time.sleep(0.45)
        p.wait_for('TT.E.task.priority === "low"', timeout=5)
        state = p.eval('(() => { const ta = document.querySelector("[data-k=\\"ed:desc\\"]");'
                       ' return {same: ta.__marker === 42, focused: document.activeElement === ta,'
                       ' value: ta.value, caret: ta.selectionStart}; })()')
        self.assertTrue(state['same'], 'the focused textarea node was kept')
        self.assertTrue(state['focused'])
        self.assertEqual(state['value'], typed)
        self.assertEqual(state['caret'], len(typed))
        self.wait_saved()
        self.assertEqual(self.server_task(t['id'])['description'], typed)

    def test_rename_from_another_window_shows_up(self):
        t = self.task('Old name')
        p = self.open()
        self.open_editor('Old name')
        p.click('[data-k="ed:desc"]')
        self.api('PATCH', f'/api/tasks/{t["id"]}', {'title': 'New name'})
        p.wait_for('document.querySelector(".ed-title").value === "New name"', timeout=5)
        self.wait_rows(['New name'])

    def test_reload_with_pending_edits_keeps_them(self):
        t = self.task('Pending')
        p = self.open()
        self.open_editor('Pending')
        p.click('[data-k="ed:desc"]')
        p.type('typed just before reload')
        p.reload()
        time.sleep(0.2)
        p.wait_for('!!(window.TT && TT.S.loaded)', timeout=8)
        deadline = time.time() + 5
        while time.time() < deadline and self.server_task(t['id'])['description'] != 'typed just before reload':
            time.sleep(0.1)
        self.assertEqual(self.server_task(t['id'])['description'], 'typed just before reload')

    def test_closing_window_with_pending_edits_keeps_them(self):
        t = self.task('Closing')
        p = self.open()
        self.open_editor('Closing')
        p.click('.ed-title')
        p.key('End')
        p.type(' edited')
        self.browser.close_page(p)
        self.page = None
        deadline = time.time() + 5
        while time.time() < deadline and self.server_task(t['id'])['title'] != 'Closing edited':
            time.sleep(0.1)
        self.assertEqual(self.server_task(t['id'])['title'], 'Closing edited')

    def test_big_pending_edit_asks_before_leaving(self):
        t = self.task('Big')
        p = self.open()
        self.open_editor('Big')
        big = 'x' * 70000
        p.eval('(() => { const ta = document.querySelector("[data-k=\\"ed:desc\\"]"); ta.focus(); })()')
        p.type(big)
        p.reload()
        time.sleep(0.4)
        dialogs = p.take_events('Page.javascriptDialogOpening')
        self.assertEqual(len(dialogs), 1, 'beforeunload asks the user to wait')
        self.assertEqual(dialogs[0]['params']['type'], 'beforeunload')
        p.send('Page.handleJavaScriptDialog', {'accept': False})
        deadline = time.time() + 5
        while time.time() < deadline and len(self.server_task(t['id'])['description']) != 70000:
            time.sleep(0.1)
        self.assertEqual(len(self.server_task(t['id'])['description']), 70000)


class SubtaskTests(UICase):
    def subs(self):
        return self.page.eval('TT.E.task ? TT.E.task.subtasks.map(s => s.title + (s.done ? " ✓" : "")) : null')

    def server_subs(self, tid):
        return [s['title'] + (' ✓' if s['done'] else '') for s in self.server_task(tid)['subtasks']]

    def wait_idle(self):
        self.page.wait_for('TT.Q.pending === 0 && TT.net.inflight === 0', timeout=8)

    def test_add_by_enter_and_paste_many_lines(self):
        t = self.task('Party')
        p = self.open()
        self.open_editor('Party')
        p.click('[data-k="sub:add"]')
        p.type('Cake')
        p.key('Enter')
        p.type('Music')
        p.key('Enter')
        self.assertEqual(p.active(), 'sub:add')
        p.paste('- milk\r\n- [x] eggs\n\n---\n3.5 kg flour\n-v flag\u2028* * *\n1. last', self.srv.url)
        self.wait_idle()
        want = ['Cake', 'Music', 'milk', 'eggs ✓', '3.5 kg flour', '-v flag', 'last']
        self.assertEqual(self.subs(), want)
        self.assertEqual(self.server_subs(t['id']), want)
        self.assertEqual(p.eval('document.querySelector("[data-k=\\"sub:add\\"]").value'), '')
        p.wait_for('document.querySelector(".sub-count").textContent === "1 of 7 done"')
        # Esc clears the draft first, and closes the panel only on the second Esc.
        p.type('draft')
        p.key('Escape')
        self.assertEqual(p.eval('document.querySelector("[data-k=\\"sub:add\\"]").value'), '')
        self.assertFalse(p.eval('document.querySelector("#editor").hidden'))
        p.key('Escape')
        p.wait_for('document.querySelector("#editor").hidden')

    def test_title_editing_keys(self):
        t = self.task('T')
        self.api('POST', f'/api/tasks/{t["id"]}/subtasks', {'items': ['one', 'two', 'three']}, expect=201)
        p = self.open()
        self.open_editor('T')
        first = p.eval('document.querySelector(".sub-title").dataset.k')
        p.click(f'[data-k="{first}"]')
        p.key('End')
        p.type(' edited')
        p.key('Enter')                      # saves and moves on
        self.assertTrue(p.active().endswith(':title'))
        self.assertNotEqual(p.active(), first)
        p.type(' changed')
        p.key('Escape')                     # reverts
        self.assertEqual(p.eval('document.activeElement.value'), 'two')
        p.key('ArrowDown')
        self.assertEqual(p.eval('document.activeElement.value'), 'three')
        p.key('ArrowUp')
        p.key('ArrowUp')
        self.assertEqual(p.eval('document.activeElement.value'), 'one edited')
        self.wait_idle()
        self.assertEqual(self.server_subs(t['id']), ['one edited', 'two', 'three'])
        # Backspace on an empty title deletes it, but not on key repeat.
        p.key('ArrowDown')
        p.key('a', 'ctrl')
        p.key('Backspace')
        self.assertEqual(p.eval('document.activeElement.value'), '')
        p.key('Backspace', repeat=True)
        self.assertEqual(self.subs(), ['one edited', 'two', 'three'])
        p.key('Backspace')
        self.wait_idle()
        self.assertEqual(self.server_subs(t['id']), ['one edited', 'three'])
        self.assertEqual(p.eval('document.activeElement.value'), 'one edited', 'focus moved to the previous title')

    def test_remove_with_undo_restores_position_and_times(self):
        t = self.task('T')
        task = self.api('POST', f'/api/tasks/{t["id"]}/subtasks', {'items': ['a', {'title': 'b', 'done': True}, 'c']}, expect=201)
        before = task['subtasks'][1]
        p = self.open()
        self.open_editor('T')
        p.click('.sub:nth-of-type(2) .sub-del')
        self.wait_idle()
        self.assertEqual(self.server_subs(t['id']), ['a', 'c'])
        p.click('.toast button:not(.toast-close)')
        self.wait_idle()
        after = self.server_task(t['id'])['subtasks']
        self.assertEqual([s['title'] for s in after], ['a', 'b', 'c'])
        self.assertEqual((after[1]['created_at'], after[1]['completed_at'], after[1]['done']),
                         (before['created_at'], before['completed_at'], True))

    def test_reorder_by_drag_alt_arrows_and_cancel(self):
        t = self.task('T')
        self.api('POST', f'/api/tasks/{t["id"]}/subtasks', {'items': ['a', 'b', 'c', 'd']}, expect=201)
        p = self.open()
        self.open_editor('T')
        # Drag the grip of "a" below "c".
        x0, y0 = p.center('.sub:nth-of-type(1) .grip')
        r = p.rect('.sub:nth-of-type(3)')
        p.drag(x0, y0, x0, r['y'] + r['h'] * 0.8, steps=10)
        self.wait_idle()
        self.assertEqual(self.server_subs(t['id']), ['b', 'c', 'a', 'd'])
        # Esc during a drag cancels it.
        x0, y0 = p.center('.sub:nth-of-type(1) .grip')
        r = p.rect('.sub:nth-of-type(4)')
        p.move(x0, y0)
        p.mouse('mousePressed', x0, y0, buttons=1)
        for i in range(1, 8):
            p.mouse('mouseMoved', x0, y0 + (r['y'] + r['h'] - y0) * i / 7, buttons=1)
            time.sleep(0.02)
        self.assertEqual(p.eval('Array.from(document.querySelectorAll(".sub-title")).map(e => e.value).join()'), 'c,a,d,b')
        p.key('Escape')
        p.mouse('mouseReleased', x0, r['y'] + r['h'])
        time.sleep(0.3)
        self.assertEqual(p.eval('Array.from(document.querySelectorAll(".sub-title")).map(e => e.value).join()'), 'b,c,a,d')
        self.assertFalse(p.eval('document.querySelector("#editor").hidden'), 'Esc only cancelled the drag')
        # Alt+Up/Down keeps focus on the moved subtask.
        p.click('.sub:nth-of-type(4) .sub-title')
        p.key('ArrowUp', 'alt')
        p.key('ArrowUp', 'alt')
        self.assertEqual(p.eval('document.activeElement.value'), 'd')
        self.wait_idle()
        self.assertEqual(self.server_subs(t['id']), ['b', 'd', 'c', 'a'])

    def test_last_tick_offers_mark_done_and_untick_withdraws(self):
        t = self.task('T')
        self.api('POST', f'/api/tasks/{t["id"]}/subtasks', {'items': ['a', 'b']}, expect=201)
        p = self.open()
        self.open_editor('T')
        p.click('.sub:nth-of-type(1) .sub-check')
        p.click('.sub:nth-of-type(2) .sub-check')
        p.wait_for('Array.from(document.querySelectorAll(".toast")).some(t => t.textContent.includes("Mark task done"))')
        p.click('.sub:nth-of-type(2) .sub-check')
        p.wait_for('!Array.from(document.querySelectorAll(".toast")).some(t => t.textContent.includes("Mark task done"))')
        p.click('.sub:nth-of-type(2) .sub-check')
        p.wait_for('Array.from(document.querySelectorAll(".toast")).some(t => t.textContent.includes("Mark task done"))')
        self.wait_idle()
        self.assertEqual(self.server_task(t['id'])['status'], 'open', 'nothing is completed automatically')
        p.eval('Array.from(document.querySelectorAll(".toast button")).find(b => b.textContent === "Mark task done").scrollIntoView()')
        x, y = p.eval('(() => { const b = Array.from(document.querySelectorAll(".toast button")).find(b => b.textContent === "Mark task done");'
                      ' const r = b.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()')
        p.click_at(x, y)
        p.wait_for('TT.E.task && TT.E.task.status === "done"')
        self.assertEqual(self.server_task(t['id'])['status'], 'done')

    def test_list_unfold_and_tick_with_keyboard(self):
        t = self.task('Folded')
        self.api('POST', f'/api/tasks/{t["id"]}/subtasks', {'items': ['first', 'second']}, expect=201)
        p = self.open()
        self.assertIn('0/2', p.eval('document.querySelector(".sub-badge").textContent'))
        self.assertEqual(p.eval('document.querySelectorAll(".row-subs").length'), 0)
        p.click('.sub-badge')
        p.wait_for('document.querySelectorAll(".mini-sub").length === 2')
        p.eval('document.querySelector(".sub-badge").focus()')
        p.key('Tab')
        self.assertTrue(p.active().endswith(':check') and p.active().startswith('ls'))
        p.key(' ')
        self.wait_idle()
        self.assertEqual(self.server_subs(t['id']), ['first ✓', 'second'])
        p.key('Tab')
        p.key('Enter')
        self.wait_idle()
        self.assertEqual(self.server_subs(t['id']), ['first ✓', 'second ✓'])
        self.assertTrue(p.active().startswith('ls'), 'focus stays on the subtask after the redraw')
        p.wait_for('document.querySelector(".sub-badge").textContent.includes("2/2")')

    def test_search_shows_matching_subtasks_of_folded_tasks(self):
        t = self.task('Party')
        self.api('POST', f'/api/tasks/{t["id"]}/subtasks', {'items': ['Buy balloons', 'Music']}, expect=201)
        p = self.open()
        p.click('#search')
        p.type('ballo')
        p.wait_for('document.querySelectorAll(".mini-sub").length === 1')
        self.assertEqual(p.eval('document.querySelector(".mini-sub label").textContent'), 'Buy balloons')

    def test_convert_checklist(self):
        desc = 'Intro\n\n- [ ] **cake**\n    1. [x] invite `a_b`\n```\n- [ ] not this\n```\n  indented keeps\n\n\nend'
        t = self.task('Party', description=desc)
        p = self.open()
        self.open_editor('Party')
        p.wait_for('!!document.querySelector("[data-k=\\"ed:convert\\"]")')
        self.assertIn('2 checklist lines', p.eval('document.querySelector("[data-k=\\"ed:convert\\"]").textContent'))
        p.click('[data-k="ed:convert"]')
        p.wait_for('Array.from(document.querySelectorAll(".toast")).some(t => t.textContent.includes("into subtasks"))', timeout=8)
        self.wait_idle()
        self.wait_saved()
        st = self.server_task(t['id'])
        self.assertEqual([s['title'] + (' ✓' if s['done'] else '') for s in st['subtasks']], ['cake', 'invite `a_b` ✓'])
        self.assertEqual(st['description'], 'Intro\n\n```\n- [ ] not this\n```\n  indented keeps\n\n\nend')
        self.assertFalse(p.eval('!!document.querySelector("[data-k=\\"ed:convert\\"]")'))

    def test_many_fast_writes_stay_consistent(self):
        t = self.task('Fast')
        p = self.open()
        self.open_editor('Fast')
        p.click('[data-k="sub:add"]')
        for i in range(6):
            p.type(f'item {i}')
            p.key('Enter')
        # Tick the new (maybe still temporary) subtasks right away.
        for i in (1, 3, 5):
            p.click(f'.sub:nth-of-type({i + 1}) .sub-check', hold=0.03)
        p.click('.sub:nth-of-type(1) .sub-del', hold=0.03)
        self.wait_idle()
        want = ['item 1 ✓', 'item 2', 'item 3 ✓', 'item 4', 'item 5 ✓']
        self.assertEqual(self.server_subs(t['id']), want)
        p.wait_for('JSON.stringify(TT.E.task.subtasks.map(s => s.title + (s.done ? " ✓" : ""))) === %s' % js(json.dumps(want, ensure_ascii=False, separators=(',', ':'))))
        self.assertTrue(p.eval('TT.E.task.subtasks.every(s => s.id > 0)'))

    def test_refresh_while_writes_are_pending_never_duplicates(self):
        t = self.task('Dups')
        p = self.open()
        self.open_editor('Dups')
        # Keep subtask writes pending for a while (slow answers), then refresh in between.
        p.eval('''(() => { const f = window.fetch; window.fetch = (u, o) => f(u, o).then(r =>
            String(u).includes('/subtasks') ? new Promise(res => setTimeout(() => res(r), 400)) : r); })()''')
        p.click('[data-k="sub:add"]')
        p.type('one')
        p.key('Enter')
        p.eval('TT.refresh()')
        time.sleep(0.2)
        p.type('two')
        p.key('Enter')
        p.eval('TT.refresh()')
        time.sleep(0.2)
        p.type('three')
        p.key('Enter')
        for _ in range(12):
            state = p.eval(f'''(() => {{ const t = TT.taskById({t["id"]}); const ids = TT.E.task.subtasks.map(s => s.id);
                return {{rows: document.querySelectorAll("#editor li.sub").length, subs: ids.length,
                         unique: new Set(ids).size, shared: !!t && t.subtasks === TT.E.task.subtasks}}; }})()''')
            self.assertEqual(state['rows'], state['subs'], state)
            self.assertEqual(state['unique'], state['subs'], state)
            self.assertFalse(state['shared'], 'list and editor must not share a subtask array')
            time.sleep(0.1)
        self.wait_idle()
        self.assertEqual(self.server_subs(t['id']), ['one', 'two', 'three'])
        self.assertEqual(self.subs(), ['one', 'two', 'three'])

    def test_click_right_after_editing_a_subtask_title(self):
        t = self.task('T')
        self.api('POST', f'/api/tasks/{t["id"]}/subtasks', {'items': ['a', 'b']}, expect=201)
        p = self.open()
        self.open_editor('T')
        p.click('.sub:nth-of-type(1) .sub-title')
        p.key('End')
        p.type(' renamed')
        x, y = p.center('.sub:nth-of-type(2) .sub-check')
        p.move(x, y)
        p.mouse('mousePressed', x, y, buttons=1)
        time.sleep(0.5)
        p.mouse('mouseReleased', x, y)
        self.wait_idle()
        self.assertEqual(self.server_subs(t['id']), ['a renamed', 'b ✓'])

    def test_unsaved_subtask_title_survives_closing(self):
        t = self.task('T')
        self.api('POST', f'/api/tasks/{t["id"]}/subtasks', {'items': ['draft']}, expect=201)
        p = self.open()
        self.open_editor('T')
        p.click('.sub-title')
        p.key('End')
        p.type(' kept')
        self.browser.close_page(p)
        self.page = None
        deadline = time.time() + 5
        while time.time() < deadline and self.server_subs(t['id']) != ['draft kept']:
            time.sleep(0.1)
        self.assertEqual(self.server_subs(t['id']), ['draft kept'])


class SubtaskDetailTests(UICase):
    def setup_party(self, **sub):
        t = self.task('Party', labels=['home', 'fun'], due_at='2030-01-20')
        task = self.api('POST', f'/api/tasks/{t["id"]}/subtasks', {'items': [dict({'title': 'Cake'}, **sub), 'Music']}, expect=201)
        return t, [s['id'] for s in task['subtasks']]

    def wait_idle(self):
        self.page.wait_for('TT.Q.pending === 0 && TT.net.inflight === 0 && TT.subtaskBusy() === false', timeout=8)

    def sub(self, tid, i=0):
        return self.server_task(tid)['subtasks'][i]

    def test_details_date_labels_and_notes(self):
        t, ids = self.setup_party()
        p = self.open()
        self.open_editor('Party')
        p.click(f'[data-k="s{ids[0]}:more"]')
        self.assertEqual(p.active(), f's{ids[0]}:date')
        for ch in '01102030':
            p.key(ch)
        self.wait_idle()
        self.assertEqual(self.sub(t['id'])['due_at'], '2030-01-10')
        p.click(f'[data-k="s{ids[0]}:lbl:fun"]')
        self.wait_idle()
        self.assertEqual([l['name'] for l in self.sub(t['id'])['labels']], ['fun'])
        p.click(f'[data-k="s{ids[0]}:notes"]')
        p.type('First line of notes\nsecond line')
        time.sleep(0.3)
        self.assertEqual(self.sub(t['id'])['notes'], '', 'not saved before the 700 ms pause')
        self.wait_idle()
        self.assertEqual(self.sub(t['id'])['notes'], 'First line of notes\nsecond line')
        meta = p.eval('document.querySelector(".sub .sub-meta").textContent')
        for part in ('fun', '📝 First line of notes'):
            self.assertIn(part, meta)
        # The list row shows that a subtask is due before the task itself.
        self.assertIn('↳ ⏱ 10 Jan', p.eval('document.querySelector("#list .row").textContent'))
        # Esc in the notes saves and closes the details, back on the title.
        p.type(' more')
        p.key('Escape')
        self.assertEqual(p.active(), f's{ids[0]}:title')
        self.assertFalse(p.eval('!!document.querySelector(".sub-details")'))
        self.wait_idle()
        self.assertEqual(self.sub(t['id'])['notes'], 'First line of notes\nsecond line more')
        # Clicking the 📝 under the title opens the details on the notes.
        p.click(f'[data-k="s{ids[0]}:meta-notes"]')
        self.assertEqual(p.active(), f's{ids[0]}:notes')

    def test_notes_save_when_leaving_switching_and_closing(self):
        t, ids = self.setup_party()
        other = self.task('Other')
        p = self.open()
        self.open_editor('Party')
        p.click(f'[data-k="s{ids[0]}:more"]')
        p.click(f'[data-k="s{ids[0]}:notes"]')
        p.type('saved on blur')
        p.click('[data-k="ed:title"]')
        self.wait_idle()
        self.assertEqual(self.sub(t['id'])['notes'], 'saved on blur')
        p.click(f'[data-k="s{ids[0]}:notes"]')
        p.key('End', 'ctrl')
        p.type(' + switch')
        p.click(self.row_sel('Other'))
        p.wait_for('TT.E.task && TT.E.task.title === "Other"')
        self.wait_idle()
        self.assertEqual(self.sub(t['id'])['notes'], 'saved on blur + switch')
        p.click(self.row_sel('Party'))
        p.wait_for('TT.E.task && TT.E.task.title === "Party"')
        p.click(f'[data-k="s{ids[0]}:more"]')
        p.click(f'[data-k="s{ids[0]}:notes"]')
        p.key('End', 'ctrl')
        p.type(' + close')
        self.browser.close_page(p)
        self.page = None
        deadline = time.time() + 5
        while time.time() < deadline and self.sub(t['id'])['notes'] != 'saved on blur + switch + close':
            time.sleep(0.1)
        self.assertEqual(self.sub(t['id'])['notes'], 'saved on blur + switch + close')
        self.assertTrue(other)

    def test_notes_box_survives_autosaves_and_redraws(self):
        t, ids = self.setup_party()
        p = self.open()
        self.open_editor('Party')
        p.click(f'[data-k="s{ids[0]}:more"]')
        p.click(f'[data-k="s{ids[0]}:notes"]')
        p.eval('(() => { const ta = document.activeElement; ta.__marker = 7; ta.style.height = "150px"; })()')
        typed = ''
        for i in range(4):
            chunk = f'part {i} '
            p.type(chunk)
            typed += chunk
            self.api('PATCH', f'/api/tasks/{t["id"]}', {'priority': ['high', 'low'][i % 2]})
            time.sleep(0.9)    # past the notes autosave
        self.wait_idle()
        state = p.eval('(() => { const ta = document.querySelector(\'[data-k="s%d:notes"]\'); return {same: ta.__marker === 7,'
                       ' focused: document.activeElement === ta, value: ta.value, height: ta.style.height, caret: ta.selectionStart}; })()' % ids[0])
        self.assertEqual(state, {'same': True, 'focused': True, 'value': typed, 'height': '150px', 'caret': len(typed)})
        self.assertEqual(self.sub(t['id'])['notes'], typed.rstrip())
        # Ctrl+Z history survived the autosaves.
        p.key('z', 'ctrl', commands=['undo'])
        self.assertNotEqual(p.eval('document.activeElement.value'), typed)

    def test_title_tokens_only_use_task_labels(self):
        t, ids = self.setup_party()
        p = self.open()
        self.open_editor('Party')
        p.click('[data-k="sub:add"]')
        p.type('Call Bob #fun #nope ^+2d@9:15')
        p.key('Enter')
        self.wait_idle()
        sub = self.sub(t['id'], 2)
        self.assertEqual(sub['title'], 'Call Bob #nope')
        self.assertEqual([l['name'] for l in sub['labels']], ['fun'])
        self.assertTrue(sub['due_at'].endswith('T09:15'))
        # Editing a title applies tokens too.
        p.click(f'[data-k="s{ids[1]}:title"]')
        p.key('End')
        p.type(' #home')
        p.key('Enter')
        self.wait_idle()
        sub = self.sub(t['id'], 1)
        self.assertEqual((sub['title'], [l['name'] for l in sub['labels']]), ('Music', ['home']))

    def test_removing_a_task_label_removes_it_from_subtasks(self):
        t, ids = self.setup_party(labels=['fun'])
        p = self.open()
        self.open_editor('Party')
        p.wait_for('document.querySelector(".sub .sub-meta").textContent.includes("fun")')
        p.click('[data-k="ed:label-x:fun"]')
        p.wait_for('!document.querySelector(".sub .sub-meta") || !document.querySelector(".sub .sub-meta").textContent.includes("fun")')
        self.wait_idle()
        self.assertEqual(self.sub(t['id'])['labels'], [])

    def test_list_meta_opens_details_and_views_use_subtask_dates(self):
        today = time.strftime('%Y-%m-%d')
        t, ids = self.setup_party(due_at=today, notes='bring candles')
        p = self.open()
        p.click('[data-k="nav:today"]')
        self.wait_rows(['Party'])
        self.assertIn('↳ ⏱ Today', p.eval('document.querySelector("#list .row").textContent'))
        p.click('.sub-badge')
        p.click(f'[data-k="ls{ids[0]}:meta-notes"]')
        p.wait_for(f'document.activeElement && document.activeElement.dataset.k === "s{ids[0]}:notes"')
        # Label filter: folded tasks show their matching subtasks.
        p.click('[data-k="nav:open"]')
        p.click('.sub-badge')     # fold again
        self.api('PATCH', f'/api/subtasks/{ids[1]}', {'labels': ['home']})
        home = next(l['id'] for l in self.api('GET', '/api/meta')['labels'] if l['name'] == 'home')
        p.click(f'[data-k="lbl:{home}:name"]')
        p.wait_for('document.querySelectorAll(".mini-sub").length === 1 && document.querySelector(".mini-sub label").textContent === "Music"')


class MatrixTests(UICase):
    def open_matrix(self):
        p = self.open(self.srv.url + '#matrix')
        p.wait_for('!!document.querySelector("#mx-board")', timeout=8)
        # Wait until the board shows every placed item the server knows about.
        p.wait_for('TT.S.tasks.reduce((n, t) => n + (t.matrix ? 1 : 0) + (t.subtasks || []).filter(s => s.matrix).length, 0)'
                   ' === document.querySelectorAll("#mx-board .note").length', timeout=8)
        return p

    def wait_mx(self):
        self.page.wait_for('TT.matrixBusy() === false && TT.net.inflight === 0 && TT.Q.pending === 0', timeout=8)
        time.sleep(0.2)

    def board_xy(self, x, y):
        r = self.page.rect('#mx-board')
        return r['x'] + x * r['w'], r['y'] + (1 - y) * r['h']

    def note(self, key):
        return f'#mx-board .note[data-key="{key}"]'

    def title_xy(self, key):
        return self.page.center(self.note(key) + ' .note-title')

    def place(self, items):
        self.api('POST', '/api/matrix', {'items': items}, expect=200)

    def pos(self, tid):
        return self.server_task(tid)['matrix']

    def test_drag_from_tray_and_priority_follows_placement(self):
        low = self.task('Low one', priority='low')
        med = self.task('Medium one')
        p = self.open_matrix()
        p.wait_for('document.querySelectorAll(".mx-tray .tray-item").length === 2')
        x0, y0 = p.center(f'.tray-item[data-key="t{low["id"]}"] .tray-title')
        x1, y1 = self.board_xy(0.75, 0.75)
        p.drag(x0, y0, x1, y1)
        self.wait_mx()
        st = self.server_task(low['id'])
        self.assertEqual(st['priority'], 'high', 'Low dropped in the top half becomes High')
        self.assertTrue(st['matrix'][0] > 0.6 and st['matrix'][1] > 0.6, st['matrix'])
        p.wait_for('Array.from(document.querySelectorAll(".toast")).some(t => t.textContent.includes("High priority"))')
        x0, y0 = p.center(f'.tray-item[data-key="t{med["id"]}"] .tray-title')
        x1, y1 = self.board_xy(0.3, 0.7)
        p.drag(x0, y0, x1, y1)
        self.wait_mx()
        self.assertEqual(self.server_task(med['id'])['priority'], 'medium', 'already important: unchanged')
        # Across the middle line (top -> bottom) sets Low; within a half nothing changes.
        x0, y0 = self.title_xy(f't{low["id"]}')
        x1, y1 = self.board_xy(0.75, 0.25)
        p.drag(x0, y0, x1, y1)
        self.wait_mx()
        self.assertEqual(self.server_task(low['id'])['priority'], 'low')
        x0, y0 = self.title_xy(f't{low["id"]}')
        x1, y1 = self.board_xy(0.25, 0.3)
        p.drag(x0, y0, x1, y1)
        self.wait_mx()
        st = self.server_task(low['id'])
        self.assertEqual(st['priority'], 'low')
        self.assertLess(st['matrix'][0], 0.5)
        # Onto the tray: back to unsorted.
        x0, y0 = self.title_xy(f't{low["id"]}')
        x1, y1 = p.center('.mx-tray-head h2')
        p.drag(x0, y0, x1, y1 + 60)
        self.wait_mx()
        self.assertIsNone(self.pos(low['id']))

    def test_suggestions_place_all_and_keyboard(self):
        today = time.strftime('%Y-%m-%d')
        a = self.task('Urgent important', due_at=today, priority='high')
        b = self.task('Someday', priority='low')
        c = self.task('Later important', due_at='2031-01-01')
        p = self.open_matrix()
        p.wait_for('document.querySelectorAll(".mx-tray .tray-item").length === 3')
        p.click(f'.tray-item[data-key="t{a["id"]}"] .suggest-chip')
        self.wait_mx()
        self.assertEqual(p.eval(f'TT.P.quadrantOf(TT.taskById({a["id"]}).matrix)'), 'do')
        p.click('[data-k="mx:place-all"]')
        self.wait_mx()
        quads = p.eval('TT.S.tasks.map(t => t.title + ":" + TT.P.quadrantOf(t.matrix) + ":" + t.priority).sort()')
        self.assertEqual(quads, ['Later important:schedule:medium', 'Someday:eliminate:low', 'Urgent important:do:high'])
        # Keyboard: arrows move by 2 % (Shift 10 %), Delete puts back, Enter in the tray takes the suggestion.
        before = self.pos(b['id'])
        p.eval(f'document.querySelector(\'{self.note("t" + str(b["id"]))}\').focus()')
        p.key('ArrowRight')
        self.wait_mx()
        after = self.pos(b['id'])
        self.assertAlmostEqual(after[0], round(before[0] + 0.02, 3), places=3)
        p.key('ArrowUp', 'shift')
        self.wait_mx()
        self.assertAlmostEqual(self.pos(b['id'])[1], round(before[1] + 0.1, 3), places=3)
        self.assertEqual(p.active(), f'mx:t{b["id"]}', 'focus stays on the moved note')
        p.key('Delete')
        self.wait_mx()
        self.assertIsNone(self.pos(b['id']))
        p.eval(f'document.querySelector(\'.tray-item[data-key="t{b["id"]}"]\').focus()')
        p.key('Enter')
        self.wait_mx()
        self.assertEqual(p.eval(f'TT.P.quadrantOf(TT.taskById({b["id"]}).matrix)'), 'eliminate')
        # Enter opens the editor, which floats over the board.
        p.eval(f'document.querySelector(\'{self.note("t" + str(c["id"]))}\').focus()')
        p.key('Enter')
        p.wait_for('!!document.querySelector("#editor:not([hidden]) .ed-title")')
        self.assertEqual(p.eval('getComputedStyle(document.querySelector("#editor")).position'), 'fixed')
        p.key('Escape')
        p.wait_for('document.querySelector("#editor").hidden')

    def test_links_trace_and_remove(self):
        a = self.task('A first')
        b = self.task('B second')
        c = self.task('C third')
        d = self.task('D alone')
        self.place([{'key': f't{a["id"]}', 'matrix': [0.15, 0.85]}, {'key': f't{b["id"]}', 'matrix': [0.45, 0.65]},
                    {'key': f't{c["id"]}', 'matrix': [0.8, 0.85]}, {'key': f't{d["id"]}', 'matrix': [0.8, 0.2]}])
        p = self.open_matrix()
        ka, kb, kc, kd = (f't{x["id"]}' for x in (a, b, c, d))
        for src, dst in ((ka, kb), (kb, kc)):
            x0, y0 = p.center(self.note(src) + ' .note-link')
            x1, y1 = self.title_xy(dst)
            p.drag(x0, y0, x1, y1)
            time.sleep(0.3)
        p.wait_for('document.querySelectorAll("#mx-board .link").length === 2')
        self.assertEqual(len(self.api('GET', '/api/deps')['deps']), 2)
        self.assertIn('waits for 1', p.eval(f'document.querySelector(\'{self.note(kc)}\').textContent'))
        # A loop is refused with a message; dropping on itself does nothing.
        self.expected_http_errors = 1
        x0, y0 = p.center(self.note(kc) + ' .note-link')
        x1, y1 = self.title_xy(ka)
        p.drag(x0, y0, x1, y1)
        p.wait_for('Array.from(document.querySelectorAll(".toast.error")).some(t => t.textContent.includes("loop"))')
        x0, y0 = p.center(self.note(kd) + ' .note-link')
        x1, y1 = self.title_xy(kd)
        p.drag(x0, y0, x1, y1)
        p.wait_for('Array.from(document.querySelectorAll(".toast.error")).some(t => t.textContent.includes("cannot wait for itself"))')
        self.assertEqual(len(self.api('GET', '/api/deps')['deps']), 2)
        # Clicking a note traces its chain; the rest dims.
        p.click(self.note(kb) + ' .note-title')
        p.wait_for('document.querySelectorAll("#mx-board .note.traced").length === 3')
        self.assertIn('dim', p.eval(f'document.querySelector(\'{self.note(kd)}\').className'))
        p.key('Escape')
        p.wait_for('document.querySelectorAll("#mx-board .note.dim").length === 0')
        # Done first item: with "Show done" its arrow turns dashed.
        p.click('[data-k="mxf:done"]')
        p.click(self.note(ka) + ' .note-check')
        p.wait_for('!!document.querySelector("#mx-board .link.done")', timeout=6)
        self.assertNotIn('waits for', p.eval(f'document.querySelector(\'{self.note(kb)}\').textContent'))
        # Select an arrow, remove it with the button; Delete removes a selected one too.
        mid = p.eval('(() => { const path = document.querySelector("#mx-board .link:not(.done) .link-hit"); const l = path.getTotalLength();'
                     ' const pt = path.getPointAtLength(l / 2); const r = document.querySelector("#mx-board").getBoundingClientRect();'
                     ' return [r.left + pt.x, r.top + pt.y]; })()')
        p.click_at(*mid)
        p.wait_for('!!document.querySelector("[data-k=\\"mx:unlink\\"]")')
        p.click('[data-k="mx:unlink"]')
        p.wait_for('document.querySelectorAll("#mx-board .link").length === 0 || document.querySelectorAll("#mx-board .link").length === 1')
        self.wait_mx()
        self.assertEqual(len(self.api('GET', '/api/deps')['deps']), 1)
        mid = p.eval('(() => { const path = document.querySelector("#mx-board .link-hit"); const l = path.getTotalLength();'
                     ' const pt = path.getPointAtLength(l / 2); const r = document.querySelector("#mx-board").getBoundingClientRect();'
                     ' return [r.left + pt.x, r.top + pt.y]; })()')
        p.click_at(*mid)
        p.wait_for('!!document.querySelector("[data-k=\\"mx:unlink\\"]")')
        p.key('Delete')
        p.wait_for('document.querySelectorAll("#mx-board .link").length === 0')
        self.assertEqual(self.api('GET', '/api/deps')['deps'], [])

    def test_today_strip_warnings_and_filters(self):
        today = time.strftime('%Y-%m-%d')
        keys = []
        for i in range(9):
            t = self.task(f'Do {i}', due_at=today if i < 3 else None)
            keys.append(t)
        late = self.task('Late in schedule', due_at='2020-01-01')
        parent = self.task('Parent', labels=['red'])
        task = self.api('POST', f'/api/tasks/{parent["id"]}/subtasks', {'items': ['Inherits red']}, expect=201)
        sid = task['subtasks'][0]['id']
        items = [{'key': f't{t["id"]}', 'matrix': [0.55 + i * 0.04, 0.6 + (i % 3) * 0.1]} for i, t in enumerate(keys)]
        items += [{'key': f't{late["id"]}', 'matrix': [0.2, 0.8]}, {'key': f's{sid}', 'matrix': [0.2, 0.3]}]
        self.place(items)
        p = self.open_matrix()
        p.wait_for('document.querySelectorAll(".today-item").length === 6')
        self.assertIn('+3 more', p.eval('document.querySelector(".mx-today").textContent'))
        warnings = p.eval('Array.from(document.querySelectorAll(".warning")).map(w => w.textContent)')
        self.assertTrue(any('more than 8' in w for w in warnings), warnings)
        self.assertTrue(any('due by today but sits in Schedule' in w for w in warnings), warnings)
        p.click('[data-k="warn:late"]')
        p.wait_for(f'document.querySelector(\'{self.note("t" + str(late["id"]))}\').classList.contains("traced")')
        self.assertTrue(p.eval(f'document.querySelector(\'{self.note("t" + str(keys[0]["id"]))}\').classList.contains("dim")'))
        # A Today item selects its note.
        p.click('.today-item')
        self.assertTrue(p.active().startswith('mx:t'))
        # Filters.
        p.click('[data-k="mxf:tasks"]')
        p.wait_for('document.querySelectorAll("#mx-board .note.is-task").length === 0 && document.querySelectorAll("#mx-board .note.is-sub").length === 1')
        p.click('[data-k="mxf:tasks"]')
        p.click('[data-k="mxf:subs"]')
        p.wait_for('document.querySelectorAll("#mx-board .note.is-sub").length === 0')
        p.click('[data-k="mxf:subs"]')
        # The label filter: a subtask without labels counts with its task's labels.
        red = next(l['id'] for l in self.api('GET', '/api/meta')['labels'] if l['name'] == 'red')
        p.click(f'[data-k="lbl:{red}:name"]')
        p.wait_for('document.querySelectorAll("#mx-board .note").length === 1 && !!document.querySelector("#mx-board .note.is-sub")')
        p.click('[data-k="side:clear"]')
        # Show done: done notes appear, muted.
        self.api('PATCH', f'/api/tasks/{keys[0]["id"]}', {'status': 'done'})
        p.wait_for(f'!document.querySelector(\'{self.note("t" + str(keys[0]["id"]))}\')', timeout=6)
        p.click('[data-k="mxf:done"]')
        p.wait_for(f'!!document.querySelector(\'{self.note("t" + str(keys[0]["id"]))}.done\')', timeout=6)

    def test_double_click_subtask_opens_its_task_on_that_subtask(self):
        t = self.task('Holder')
        task = self.api('POST', f'/api/tasks/{t["id"]}/subtasks', {'items': ['first', 'second']}, expect=201)
        sid = task['subtasks'][1]['id']
        self.place([{'key': f's{sid}', 'matrix': [0.7, 0.7]}])
        p = self.open_matrix()
        p.dblclick(self.note(f's{sid}') + ' .note-title')
        p.wait_for(f'document.activeElement && document.activeElement.dataset.k === "s{sid}:title"', timeout=6)
        self.assertEqual(p.eval('TT.E.task.title'), 'Holder')

    def test_drag_keeps_working_while_the_page_refreshes(self):
        a = self.task('Dragged')
        other = self.task('Background')
        self.place([{'key': f't{a["id"]}', 'matrix': [0.2, 0.8]}])
        p = self.open_matrix()
        x0, y0 = self.title_xy(f't{a["id"]}')
        x1, y1 = self.board_xy(0.3, 0.7)
        p.move(x0, y0)
        p.mouse('mousePressed', x0, y0, buttons=1)
        for i in range(1, 6):
            p.mouse('mouseMoved', x0 + (x1 - x0) * i / 10, y0 + (y1 - y0) * i / 10, buttons=1)
            time.sleep(0.03)
        self.api('PATCH', f'/api/tasks/{other["id"]}', {'title': 'Background changed'})
        time.sleep(2.0)   # a poll notices the change and refreshes meanwhile
        for i in range(6, 11):
            p.mouse('mouseMoved', x0 + (x1 - x0) * i / 10, y0 + (y1 - y0) * i / 10, buttons=1)
            time.sleep(0.03)
        p.mouse('mouseReleased', x1, y1)
        self.wait_mx()
        # The note moved with the pointer (it was grabbed at its title, not its centre).
        r = p.rect('#mx-board')
        pos = self.pos(a['id'])
        self.assertAlmostEqual(pos[0], 0.2 + (x1 - x0) / r['w'], delta=0.01)
        self.assertAlmostEqual(pos[1], 0.8 - (y1 - y0) / r['h'], delta=0.01)
        p.wait_for('document.querySelector(".mx-tray").textContent.includes("Background changed")', timeout=6)


class ImportTests(UICase):
    def test_import_json_from_the_sidebar(self):
        export = {'app': 'TodoTracker', 'labels': [{'name': 'moved', 'color': '#0d9488'}],
                  'tasks': [{'id': 7, 'title': 'From the old PC', 'created_at': '2025-03-04T05:06:07', 'labels': ['moved'],
                             'subtasks': [{'id': 9, 'title': 'old subtask', 'done': True}]}],
                  'dependencies': []}
        path = os.path.join(self.srv.data, 'export.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(export, f)
        p = self.open()
        p.send('Page.setInterceptFileChooserDialog', {'enabled': True})
        p.take_events()
        p.click('[data-k="side:import"]')
        deadline = time.time() + 5
        chooser = None
        while time.time() < deadline and not chooser:
            ev = p.take_events('Page.fileChooserOpened')
            chooser = ev[0]['params'] if ev else None
            time.sleep(0.05)
        self.assertIsNotNone(chooser, 'the file chooser opened')
        p.send('DOM.setFileInputFiles', {'files': [path], 'backendNodeId': chooser['backendNodeId']})
        p.wait_for('Array.from(document.querySelectorAll(".toast")).some(t => t.textContent.includes("Imported 1 task, 1 subtasks, 1 new labels"))', timeout=8)
        self.wait_rows(['From the old PC'])
        task = self.api('GET', '/api/tasks')['tasks'][0]
        self.assertEqual((task['created_at'], task['progress'], task['labels'][0]['color']), ('2025-03-04T05:06:07', [1, 1], '#0d9488'))


MARKDOWN_SAMPLE = ('# Heading one\n## Heading two\n### Heading three\nSome **bold**, *italic*, ~~gone~~, `code` and '
                   '[a link](https://example.com/a_b).\n\n> A quoted line\n\n- [ ] open item\n- [x] done item\n'
                   '1. first\n2. second\n\n---\n```\ncode block\n```\n')


class ThemeTests(UICase):
    def scan(self, where):
        p = self.page
        if not p.eval('typeof window.__ttContrastScan === "function"'):
            with open(os.path.join(HERE, 'contrast_scan.js'), encoding='utf-8') as f:
                p.eval(f.read())
        return [f'{where}: {x}' for x in p.eval('window.__ttContrastScan()')]

    def focus_scan(self, where, selector=None):
        p = self.page
        p.key('Tab')        # keyboard modality, so programmatic focus shows :focus-visible
        return [f'{where}: {x}' for x in p.eval('window.__ttFocusScan(%s)' % (js(selector) if selector else ''))]

    def set_theme(self, pref):
        p = self.page
        p.eval(f'localStorage.setItem("tt-theme", {js(pref)})')
        p.reload(wait=True)
        p.wait_for('!!(window.TT && TT.S.loaded)', timeout=8)

    def rich_data(self):
        today = time.strftime('%Y-%m-%d')
        labels = [f'label{i}' for i in range(15)]
        img = self.upload_image(PNG)
        self.image_url = img['url']
        a = self.task('Overdue high task', due_at='2020-02-02', priority='high', labels=labels[:5],
                      description='Snippet text here ![pic](%s)' % img['url'])
        b = self.task('In progress for today', due_at=today, labels=labels[5:10])
        self.api('PATCH', f'/api/tasks/{b["id"]}', {'status': 'in_progress'})
        c = self.task('Low and later', due_at='2031-05-05', priority='low', labels=labels[10:], description=MARKDOWN_SAMPLE)
        task = self.api('POST', f'/api/tasks/{c["id"]}/subtasks', {'items': [
            {'title': 'Subtask with everything', 'due_at': today, 'labels': ['label10'], 'notes': 'Notes first line'},
            {'title': 'Finished subtask', 'done': True}, {'title': 'Overdue subtask', 'due_at': '2020-01-01'}]}, expect=201)
        d = self.task('Already done')
        self.api('PATCH', f'/api/tasks/{d["id"]}', {'status': 'done'})
        e = self.task('Delegate me', due_at=today, priority='low')
        s1, s2 = task['subtasks'][0]['id'], task['subtasks'][2]['id']
        self.api('POST', '/api/matrix', {'items': [
            {'key': f't{a["id"]}', 'matrix': [0.8, 0.85]}, {'key': f't{b["id"]}', 'matrix': [0.2, 0.8]},
            {'key': f't{e["id"]}', 'matrix': [0.8, 0.2]}, {'key': f's{s1}', 'matrix': [0.2, 0.25]},
            {'key': f't{d["id"]}', 'matrix': [0.6, 0.6]}]}, expect=200)
        self.api('POST', '/api/deps', {'before': f't{d["id"]}', 'after': f't{a["id"]}'})
        self.api('POST', '/api/deps', {'before': f's{s1}', 'after': f't{e["id"]}'})
        self.api('POST', '/api/deps', {'before': f's{s2}', 'after': f't{b["id"]}'})
        return a, b, c, d, e

    def test_switch_persists_follows_os_and_other_windows(self):
        p = self.open()
        self.assertEqual(p.eval('document.documentElement.dataset.theme'), 'light')
        p.click('[data-k="theme:night"]')
        p.wait_for('document.documentElement.dataset.theme === "dark"')
        self.assertEqual(p.eval('localStorage.getItem("tt-theme")'), 'night')
        self.assertEqual(p.eval('document.querySelector("meta[name=theme-color]").content'), '#111317')
        self.assertEqual(p.eval('getComputedStyle(document.documentElement).colorScheme'), 'dark')
        p.reload(wait=True)
        p.wait_for('!!(window.TT && TT.S.loaded)', timeout=8)
        self.assertEqual(p.eval('document.documentElement.dataset.theme'), 'dark')
        # Auto follows the operating system, live.
        p.click('[data-k="theme:auto"]')
        p.wait_for('document.documentElement.dataset.theme === "light"')
        p.send('Emulation.setEmulatedMedia', {'features': [{'name': 'prefers-color-scheme', 'value': 'dark'}]})
        p.wait_for('document.documentElement.dataset.theme === "dark"')
        p.send('Emulation.setEmulatedMedia', {'features': [{'name': 'prefers-color-scheme', 'value': 'light'}]})
        p.wait_for('document.documentElement.dataset.theme === "light"')
        # Another window follows through the storage event.
        other = self.browser.new_page()
        try:
            other.navigate(self.srv.url)
            other.wait_for('!!(window.TT && TT.S.loaded)', timeout=8)
            p.click('[data-k="theme:night"]')
            other.wait_for('document.documentElement.dataset.theme === "dark"', timeout=5)
            self.assertEqual(other.eval('document.querySelector(\'[data-k="theme:night"]\').getAttribute("aria-checked")'), 'true')
        finally:
            self.browser.close_page(other)
        # An unknown stored value means Auto.
        p.eval('localStorage.setItem("tt-theme", "sepia")')
        p.reload(wait=True)
        p.wait_for('!!(window.TT && TT.S.loaded)', timeout=8)
        self.assertEqual(p.eval('document.documentElement.dataset.themePref'), 'auto')
        self.assertEqual(p.eval('document.documentElement.dataset.theme'), 'light')

    def test_no_flash_of_the_wrong_theme(self):
        p = self.open()
        p.eval('localStorage.setItem("tt-theme", "night")')
        p.send('Page.addScriptToEvaluateOnNewDocument', {'source': (
            'new MutationObserver((m, obs) => { if (document.body) { obs.disconnect();'
            ' window.__first = {theme: document.documentElement.dataset.theme,'
            ' bg: getComputedStyle(document.documentElement).getPropertyValue("--bg").trim(),'
            ' meta: document.querySelector("meta[name=theme-color]").content}; } })'
            '.observe(document, {childList: true, subtree: true});')})
        p.reload()
        p.wait_for('!!window.__first', timeout=8)
        self.assertEqual(p.eval('window.__first'), {'theme': 'dark', 'bg': '#111317', 'meta': '#111317'})

    def test_wcag_contrast_every_screen_in_both_themes(self):
        a, b, c, d, e = self.rich_data()
        p = self.open()
        failures = []
        for pref in ('day', 'night'):
            self.set_theme(pref)
            theme = p.eval('document.documentElement.dataset.theme')
            # List, sorted by date (group headings), one task unfolded, quick-add chips and suggestions.
            p.eval('document.querySelector("#sort").value = "due"; document.querySelector("#sort").dispatchEvent(new Event("change"))')
            p.wait_for('!!document.querySelector(".group-head")')
            p.click(self.row_sel('Low and later', '.sub-badge'))
            p.wait_for('!!document.querySelector(".mini-sub")')
            p.click('#qa')
            p.type('New thing #label1 !h ^nonsense #lab')
            p.wait_for('!document.querySelector("#qa-suggest").hidden')
            failures += self.scan(f'{theme}/list')
            failures += self.focus_scan(f'{theme}/list', '#side-content button, #side-content [tabindex], #list button, #list input, #qa, #search, #sort')
            p.key('a', 'ctrl')
            p.key('Backspace')
            p.click('[data-k="nav:done"]')
            self.wait_rows(['Already done'])
            failures += self.scan(f'{theme}/done')
            p.click('[data-k="nav:all"]')
            p.wait_for('document.querySelectorAll("#list .row").length === 5')
            # Editor with subtasks, details, preview and label suggestions.
            self.open_editor('Low and later')
            sid = p.eval('TT.E.task.subtasks[0].id')
            p.click(f'[data-k="s{sid}:more"]')
            p.click('[data-k="ed:label-input"]')
            p.type('lab')
            p.wait_for('!document.querySelector("#ed-label-suggest").hidden')
            failures += self.scan(f'{theme}/editor')
            failures += self.focus_scan(f'{theme}/editor', '#editor button, #editor input, #editor select, #editor textarea')
            p.key('Escape')
            p.key('Escape')
            p.key('Escape')
            # Toasts.
            p.eval('TT.toast("A normal message", {action: "Undo", onAction() {}}); TT.toast("An error message", {kind: "error"})')
            failures += self.scan(f'{theme}/toasts')
            p.eval('document.querySelectorAll(".toast").forEach(t => t.remove())')
            # Matrix: done notes, a selected link, a traced chain and a warning.
            p.click('[data-k="nav:matrix"]')
            p.wait_for('!!document.querySelector("#mx-board .note")', timeout=8)
            if p.eval('document.querySelector(\'[data-k="mxf:done"]\').getAttribute("aria-pressed")') != 'true':
                p.click('[data-k="mxf:done"]')
            p.wait_for('!!document.querySelector("#mx-board .note.done")', timeout=6)
            failures += self.scan(f'{theme}/matrix')
            p.click(f'#mx-board .note[data-key="t{a["id"]}"] .note-title')
            p.wait_for('!!document.querySelector("#mx-board .note.dim")')
            failures += self.scan(f'{theme}/matrix-traced')
            if p.eval('!!document.querySelector(".warning")'):
                p.click('.warning')
                failures += self.scan(f'{theme}/matrix-warning')
            failures += self.focus_scan(f'{theme}/matrix', '.mx button, .mx [tabindex]')
            p.eval('TT.matrix.render()')
            mid = p.eval('(() => { const path = document.querySelector("#mx-board .link-hit"); const l = path.getTotalLength();'
                         ' const pt = path.getPointAtLength(l / 2); const r = document.querySelector("#mx-board").getBoundingClientRect();'
                         ' return [r.left + pt.x, r.top + pt.y]; })()')
            p.click_at(*mid)
            p.wait_for('!!document.querySelector(".mx-linkbar")')
            failures += self.scan(f'{theme}/matrix-link')
            # Lightbox and banners.
            p.click('[data-k="nav:all"]')
            self.open_editor('Overdue high task')
            p.wait_for('!!document.querySelector(".md-preview img")')
            p.eval('TT.$("#lightbox img").src = document.querySelector(".md-preview img").src; TT.$("#lightbox").hidden = false')
            failures += self.scan(f'{theme}/lightbox')
            p.eval('TT.$("#lightbox").hidden = true')
            for kind in ('', 'info'):
                p.eval('(() => { const b = TT.$("#banner"); b.textContent = "Banner text for the contrast check"; b.className = "banner %s"; b.hidden = false; })()' % kind)
                failures += self.scan(f'{theme}/banner {kind or "offline"}')
            p.eval('TT.$("#banner").hidden = true')
            p.key('Escape')
        self.maxDiff = None
        self.assertEqual(failures, [], "\n".join(failures))


class PhoneTests(UICase):
    """A 390 x 844 touch screen (a typical phone): taps, swipes and holds."""

    W, H = 390, 844

    def open_phone(self, dark=False):
        self.before_open()
        self.page = self.browser.new_page()
        p = self.page
        p.send('Emulation.setDeviceMetricsOverride', {'width': self.W, 'height': self.H, 'deviceScaleFactor': 2, 'mobile': True})
        p.send('Emulation.setTouchEmulationEnabled', {'enabled': True, 'maxTouchPoints': 5})
        # Reduced motion: no sliding drawer, so a tap never lands mid-animation.
        p.send('Emulation.setEmulatedMedia', {'features': [{'name': 'prefers-color-scheme', 'value': 'dark' if dark else 'light'},
                                                           {'name': 'prefers-reduced-motion', 'value': 'reduce'}]})
        p.navigate(self.srv.url)
        p.wait_for('!!(window.TT && TT.S.loaded)', timeout=8)
        self.assertTrue(p.eval('matchMedia("(pointer: coarse)").matches && TT.NARROW.matches'))
        return p

    def fits(self, where):
        """Nothing scrolls sideways, and everything to tap is at least 24 x 24 px (WCAG 2.2)."""
        p = self.page
        wide = p.eval('Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) - innerWidth')
        small = p.eval(r"""(() => Array.from(document.querySelectorAll(
              'button, input, select, textarea, [role=button], a[href]'))
            .filter((el) => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden'
              && !el.matches('.sr-only, .skip, input[type=color], input[type=file]') && !el.closest('[hidden], [inert]'))
            .map((el) => { const r = el.getBoundingClientRect();
              return [el.dataset.k || el.id || String(el.className) || el.tagName, Math.round(r.width), Math.round(r.height)]; })
            .filter(([k, w, h]) => w < 24 || h < 24))()""")
        out = [f'{where}: page is {wide}px too wide'] if wide > 0 else []
        return out + [f'{where}: small target {k} {w}x{h}' for k, w, h in small]

    def scan(self, where):
        p = self.page
        if not p.eval('typeof window.__ttContrastScan === "function"'):
            with open(os.path.join(HERE, 'contrast_scan.js'), encoding='utf-8') as f:
                p.eval(f.read())
        return [f'{where}: {x}' for x in p.eval('window.__ttContrastScan()')] + self.fits(where)

    def drawer_open(self):
        return self.page.eval('document.querySelector("#sidebar").classList.contains("open")')

    def test_drawer_closes_on_backdrop_choice_and_back(self):
        self.task('Pay rent', labels=['home'], due_at=time.strftime('%Y-%m-%d'))
        self.task('Call mum', labels=['family'])
        p = self.open_phone()
        p.tap('#menu-btn')
        p.wait_for('document.querySelector("#sidebar").classList.contains("open")')
        self.assertTrue(p.eval('!document.querySelector("#drawer-backdrop").hidden && document.querySelector("#main").inert'))
        self.assertEqual(p.active(), 'nav:open')
        p.tap(x=self.W - 20, y=self.H / 2)            # the dimmed list beside the drawer
        p.wait_for('!document.querySelector("#sidebar").classList.contains("open")')
        self.assertEqual(p.eval('document.activeElement.id'), 'menu-btn')
        self.assertFalse(p.eval('document.querySelector("#main").inert'))
        # Picking a label keeps the drawer open (several can be picked); a view closes it.
        p.tap('#menu-btn')
        p.wait_for('document.querySelector("#sidebar").classList.contains("open")')
        label = p.eval('TT.S.meta.labels.find(l => l.name === "home").id')
        p.tap(f'[data-k="lbl:{label}:name"]')
        p.wait_for(f'TT.S.labelIds.includes({label})')
        self.assertTrue(self.drawer_open())
        p.tap('[data-k="nav:today"]')
        p.wait_for('!document.querySelector("#sidebar").classList.contains("open") && document.querySelector("#view-title").textContent === "Today"')
        self.wait_rows(['Pay rent'])
        # The close button, and the phone's Back button.
        p.tap('#menu-btn')
        p.wait_for('document.querySelector("#sidebar").classList.contains("open")')
        p.tap('#menu-close')
        p.wait_for('!document.querySelector("#sidebar").classList.contains("open")')
        p.tap('#menu-btn')
        p.wait_for('document.querySelector("#sidebar").classList.contains("open")')
        self.assertTrue(p.eval('TT.back()'))
        self.assertFalse(self.drawer_open())
        self.assertFalse(p.eval('TT.back()'), 'nothing left to close')

    def test_quick_add_button_keeps_the_keyboard_up(self):
        p = self.open_phone()
        p.tap('#qa')
        p.type('Buy milk #shop !high')
        p.tap('#qa-add')
        self.wait_rows(['Buy milk'])
        self.assertEqual(p.eval('[document.activeElement.id, document.querySelector("#qa").value]'), ['qa', ''])
        task = self.api('GET', '/api/tasks')['tasks'][0]
        self.assertEqual((task['priority'], [l['name'] for l in task['labels']]), ('high', ['shop']))

    def test_editor_fills_the_screen_and_back_closes_it(self):
        t = self.task('Plan the trip', description='Ideas')
        self.api('POST', f'/api/tasks/{t["id"]}/subtasks', {'items': [{'title': 'Book flights'}, {'title': 'Pack'}]}, expect=201)
        p = self.open_phone()
        p.tap(self.row_sel('Plan the trip'))
        p.wait_for('!!document.querySelector("#editor:not([hidden]) .ed-title")')
        r = p.rect('#editor', scroll=False)
        self.assertEqual((round(r['x']), round(r['w']), round(r['h'])), (0, self.W, self.H))
        # Controls that show on hover with a mouse are always there on a touch screen.
        sid = p.eval('TT.E.task.subtasks[0].id')
        self.assertEqual(p.eval(f'getComputedStyle(document.querySelector(\'[data-k="s{sid}:del"]\')).opacity'), '1')
        self.assertEqual(self.fits('editor'), [])
        # The header stays on screen while the details scroll.
        p.eval('document.querySelector("#editor").scrollTop = 600')
        time.sleep(0.2)
        self.assertLess(abs(p.rect('[data-k="ed:close"]', scroll=False)['y'] - p.rect('.ed-head', scroll=False)['y']), 30)
        self.assertGreaterEqual(p.rect('.ed-head', scroll=False)['y'], -1)
        p.tap('[data-k="ed:close"]')
        p.wait_for('document.querySelector("#editor").hidden')
        p.tap(self.row_sel('Plan the trip'))
        p.wait_for('!document.querySelector("#editor").hidden')
        self.assertTrue(p.eval('TT.back()'))
        p.wait_for('document.querySelector("#editor").hidden')

    def test_add_image_button(self):
        self.task('Receipt')
        path = os.path.join(self.srv.data, 'photo.png')
        with open(path, 'wb') as f:
            f.write(PNG)
        p = self.open_phone()
        p.tap(self.row_sel('Receipt'))
        p.wait_for('!!document.querySelector(\'[data-k="ed:add-image"]\')')
        p.send('Page.setInterceptFileChooserDialog', {'enabled': True})
        p.take_events()
        p.tap('[data-k="ed:add-image"]')
        deadline = time.time() + 5
        chooser = None
        while time.time() < deadline and not chooser:
            ev = p.take_events('Page.fileChooserOpened')
            chooser = ev[0]['params'] if ev else None
            time.sleep(0.05)
        self.assertIsNotNone(chooser, 'the file chooser opened')
        p.send('DOM.setFileInputFiles', {'files': [path], 'backendNodeId': chooser['backendNodeId']})
        p.wait_for(r'/!\[photo\]\(\/images\/[0-9a-f]{32}\.png\)/.test(document.querySelector("#ed-desc").value)', timeout=8)
        self.wait_saved()
        tid = p.eval('TT.E.task.id')
        self.assertRegex(self.server_task(tid)['description'], r'!\[photo\]\(/images/[0-9a-f]{32}\.png\)')
        p.wait_for('!!document.querySelector(".md-preview img")')

    def test_matrix_as_lists_move_and_link_by_tapping(self):
        today = time.strftime('%Y-%m-%d')
        a = self.task('Call the plumber', due_at=today, priority='high')
        b = self.task('Fix the sink')
        p = self.open_phone()
        p.tap('#menu-btn')
        p.wait_for('document.querySelector("#sidebar").classList.contains("open")')
        p.tap('[data-k="nav:matrix"]')
        p.wait_for('!!document.querySelector(".mx-lists") && !document.querySelector("#mx-board")', timeout=8)
        self.assertFalse(self.drawer_open())
        ka, kb = f't{a["id"]}', f't{b["id"]}'
        # Tap an unsorted item, then "Do".
        p.tap(f'.tray-item[data-key="{ka}"] .tray-title')
        p.wait_for('!!document.querySelector(".mx-actions")')
        p.tap('[data-k="mxa:do"]')
        p.wait_for(f'!!document.querySelector(\'.mx-list.q-do .mx-li[data-key="{ka}"]\')')
        p.wait_for('!TT.matrixBusy()')
        pos = self.server_task(a['id'])['matrix']
        self.assertTrue(pos[0] >= 0.5 and pos[1] >= 0.5, pos)
        self.assertEqual(p.eval('document.querySelector(\'[data-k="mxa:do"]\').getAttribute("aria-pressed")'), 'true')
        # "Must happen before…", then tap the item that waits.
        p.tap('[data-k="mxa:link"]')
        p.wait_for('!!document.querySelector(".mx-actions.linking")')
        p.tap(f'.tray-item[data-key="{kb}"] .tray-title')
        p.wait_for('TT.matrix && document.querySelectorAll(".mx-link-chip").length === 1')
        deps = self.api('GET', '/api/deps')['deps']
        self.assertEqual([(d['before'], d['after']) for d in deps], [(ka, kb)])
        dep = deps[0]['id']
        p.tap(f'[data-k="mxa:unlink:{dep}"]')
        p.wait_for('!document.querySelector(".mx-link-chip")')
        self.assertEqual(self.api('GET', '/api/deps')['deps'], [])
        # Back to the tray, then the Back button clears the selection and leaves the matrix.
        p.tap('[data-k="mxa:tray"]')
        p.wait_for(f'!!document.querySelector(\'.tray-item[data-key="{ka}"]\')')
        p.wait_for('!TT.matrixBusy()')
        self.assertIsNone(self.server_task(a['id'])['matrix'])
        self.assertTrue(p.eval('TT.back()'))
        p.wait_for('!document.querySelector(".mx-actions")')
        self.assertTrue(p.eval('TT.back()'))
        p.wait_for('!document.querySelector("#list-page").hidden')

    def test_board_swipe_scrolls_and_hold_drags(self):
        a = self.task('Water the plants')
        for i in range(8):
            self.task(f'Unsorted {i}')     # a tray long enough to scroll the page
        self.api('POST', '/api/matrix', {'items': [{'key': f't{a["id"]}', 'matrix': [0.2, 0.8]}]}, expect=200)
        p = self.open_phone()
        p.eval('TT.showPage("matrix")')
        p.wait_for('!!document.querySelector(".mx-lists")', timeout=8)
        p.tap('[data-k="mxv:board"]')
        p.wait_for('!!document.querySelector("#mx-board .note")', timeout=8)
        note = f'#mx-board .note[data-key="t{a["id"]}"]'
        x, y = p.center(note + ' .note-title')
        top = p.eval('document.querySelector(".matrix-page").scrollTop')
        p.touch_drag(x, y, x, y - 150)                 # a swipe that starts on the note
        p.wait_for(f'document.querySelector(".matrix-page").scrollTop > {top + 60}')
        time.sleep(0.3)
        self.assertEqual(self.server_task(a['id'])['matrix'], [0.2, 0.8], 'a swipe must not move the note')
        x, y = p.center(note + ' .note-title')
        top = p.eval('document.querySelector(".matrix-page").scrollTop')
        p.touch_drag(x, y, x + 100, y + 200, hold=0.6)   # hold, then drag down and right
        p.wait_for(f'!TT.matrixBusy() && TT.taskById({a["id"]}).matrix[1] < 0.5', timeout=5)
        self.assertEqual(p.eval('document.querySelector(".matrix-page").scrollTop'), top, 'the drag must not scroll the page')
        moved = self.server_task(a['id'])['matrix']
        self.assertGreater(moved[0], 0.3)
        self.assertLess(moved[1], 0.5)
        self.assertEqual(self.server_task(a['id'])['priority'], 'low', 'moved to the bottom half')

    def test_phone_screens_contrast_and_fit_in_both_themes(self):
        today = time.strftime('%Y-%m-%d')
        a = self.task('Overdue thing with a long title that wraps on a phone', due_at='2020-02-02', priority='high',
                      labels=['work', 'urgent'], description='- [ ] check me\n- [x] done')
        b = self.task('Today thing', due_at=today, labels=['home'])
        self.api('POST', f'/api/tasks/{b["id"]}/subtasks', {'items': [
            {'title': 'Sub with details', 'due_at': today, 'labels': ['home'], 'notes': 'A note'}, {'title': 'Second'}]}, expect=201)
        self.api('POST', '/api/matrix', {'items': [{'key': f't{a["id"]}', 'matrix': [0.8, 0.8]}]}, expect=200)
        self.api('POST', '/api/deps', {'before': f't{b["id"]}', 'after': f't{a["id"]}'})
        failures = []
        for dark in (False, True):
            theme = 'dark' if dark else 'light'
            p = self.open_phone(dark)
            p.tap(self.row_sel('Today thing', '.sub-badge'))
            p.wait_for('!!document.querySelector(".mini-sub")')
            failures += self.scan(f'{theme}/list')
            p.tap('#menu-btn')
            p.wait_for('document.querySelector("#sidebar").classList.contains("open")')
            time.sleep(0.3)
            failures += self.scan(f'{theme}/drawer')
            p.tap('#menu-close')
            p.wait_for('!document.querySelector("#sidebar").classList.contains("open")')
            time.sleep(0.3)
            p.tap(self.row_sel('Today thing'))
            p.wait_for('!!document.querySelector("#editor:not([hidden]) .ed-title")')
            sid = p.eval('TT.E.task.subtasks[0].id')
            p.tap(f'[data-k="s{sid}:more"]')
            p.wait_for('!!document.querySelector(".sub-details")')
            failures += self.scan(f'{theme}/editor')
            p.tap('[data-k="ed:close"]')
            p.wait_for('document.querySelector("#editor").hidden')
            p.eval('TT.showPage("matrix")')
            p.wait_for('!!document.querySelector(".mx-lists .mx-li")', timeout=8)
            p.tap(f'.mx-li[data-key="t{a["id"]}"] .mx-li-title')
            p.wait_for('!!document.querySelector(".mx-actions .mx-link-chip")')
            failures += self.scan(f'{theme}/matrix-lists')
            p.tap('[data-k="mxv:board"]')
            p.wait_for('!!document.querySelector("#mx-board .note")')
            failures += self.scan(f'{theme}/matrix-board')
            p.tap('[data-k="mxv:lists"]')
            p.wait_for('!!document.querySelector(".mx-lists")')
            self.browser.close_page(p)
            self.page = None
        self.maxDiff = None
        self.assertEqual(failures, [], '\n'.join(failures))


class LocalModeTests(UICase):
    """The Android app's mode: no server, the data lives in the page
    (localdb.js on IndexedDB). Served like the app serves its assets."""

    BRIDGE = ('window.__bridge = {notify: [], schedule: [], saveFile: []};'
              'window.TodoTrackerAndroid = {'
              ' showReminders(j) { __bridge.notify.push(JSON.parse(j)); },'
              ' scheduleReminders(j) { __bridge.schedule.push(JSON.parse(j)); },'
              ' saveFile(name, type, text) { __bridge.saveFile.push({name, type, text}); } };')

    def setUp(self):
        super().setUp()
        self.local = LocalPageServer()
        self.addCleanup(self.local.cleanup)

    def open_local(self, url=None, bridge=False, phone=False):
        self.page = self.browser.new_page()
        p = self.page
        if phone:
            p.send('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 2, 'mobile': True})
            p.send('Emulation.setTouchEmulationEnabled', {'enabled': True, 'maxTouchPoints': 5})
        else:
            p.send('Emulation.setDeviceMetricsOverride', {'width': 1200, 'height': 820, 'deviceScaleFactor': 1, 'mobile': False})
        p.send('Emulation.setEmulatedMedia', {'features': [{'name': 'prefers-reduced-motion', 'value': 'reduce'}]})
        if bridge:
            p.send('Page.addScriptToEvaluateOnNewDocument', {'source': self.BRIDGE})
        p.navigate(url or self.local.url)
        p.wait_for('!!(window.TT && TT.S.loaded)', timeout=8)
        self.assertEqual(p.eval('TT.MODE'), 'local')
        return p

    def local_api(self, method, path, body=None, page=None):
        res = (page or self.page).eval('TT.local.then((l) => l.api.handle(%s, %s, %s))' % (js(method), js(path), js(body)))
        self.assertLess(res['status'], 400, res)
        return res['data']

    def test_tasks_survive_a_reload_and_show_in_another_tab(self):
        p = self.open_local()
        self.assertFalse(p.eval('!!document.querySelector(\'[data-k="side:backup"]\')'), 'no "Back up now" without a server')
        self.quick_add('Water the plants #home !high ^tomorrow')
        self.wait_rows(['Water the plants'])
        self.quick_add('Call mum')
        self.wait_rows(['Call mum', 'Water the plants'])
        self.open_editor('Water the plants')
        p.click('[data-k="ed:desc"]')
        p.type('Balcony **first**')
        self.wait_saved()
        tid = p.eval('TT.E.task.id')
        p.click('[data-k="sub:add"]')
        p.type('Fill the can')
        p.key('Enter')
        p.wait_for('TT.E.task && TT.E.task.subtasks.length === 1 && TT.E.task.subtasks[0].id > 0')
        p.key('Escape')
        p.reload(wait=True)
        p.wait_for('!!(window.TT && TT.S.loaded)', timeout=8)
        self.wait_rows(['Call mum', 'Water the plants'])
        task = self.local_api('GET', f'/api/tasks/{tid}')
        self.assertEqual((task['description'], task['priority'], [l['name'] for l in task['labels']], task['subtasks'][0]['title']),
                         ('Balcony **first**', 'high', ['home'], 'Fill the can'))
        # A second window shows the same tasks but leaves the changes to the
        # first (two copies would overwrite each other's changes) ...
        other = self.browser.new_page()
        try:
            other.navigate(self.local.url)
            other.wait_for('!!(window.TT && TT.S.loaded)', timeout=8)
            other.wait_for('!document.querySelector("#banner").hidden && document.querySelector("#banner").textContent.includes("another window")', timeout=5)
            self.quick_add('From tab one')
            other.wait_for('Array.from(document.querySelectorAll("#list .row-title")).some(e => e.textContent === "From tab one")', timeout=6)
            other.click('#qa')
            other.type('From tab two')
            other.key('Enter')
            other.wait_for('Array.from(document.querySelectorAll(".toast")).some(t => t.textContent.includes("open in another window"))', timeout=6)
            self.assertEqual(self.local_api('GET', '/api/tasks?view=all&q=tab+two')['tasks'], [])
            # ... until the first one closes.
            self.browser.close_page(p)
            self.page = other
            other.wait_for('document.querySelector("#banner").hidden', timeout=6)
            other.click('#qa')
            other.key('a', 'ctrl')
            other.type('From tab two')
            other.key('Enter')
            self.wait_rows(['From tab two', 'From tab one', 'Call mum', 'Water the plants'], timeout=6)
        except Exception:
            self.browser.close_page(other)
            raise

    def test_pictures_are_kept_on_the_device(self):
        self.page = None
        p = self.open_local()
        self.local_api('POST', '/api/tasks', {'title': 'Receipt'})
        p.eval('TT.refresh()')
        self.wait_rows(['Receipt'])
        path = os.path.join(self.srv.data, 'photo.png')
        with open(path, 'wb') as f:
            f.write(PNG)
        self.open_editor('Receipt')
        p.send('Page.setInterceptFileChooserDialog', {'enabled': True})
        p.take_events()
        p.click('[data-k="ed:add-image"]')
        deadline = time.time() + 5
        chooser = None
        while time.time() < deadline and not chooser:
            ev = p.take_events('Page.fileChooserOpened')
            chooser = ev[0]['params'] if ev else None
            time.sleep(0.05)
        self.assertIsNotNone(chooser)
        p.send('DOM.setFileInputFiles', {'files': [path], 'backendNodeId': chooser['backendNodeId']})
        p.wait_for('!!document.querySelector(".md-preview img") && document.querySelector(".md-preview img").src.startsWith("blob:")', timeout=8)
        self.wait_saved()
        p.reload(wait=True)
        p.wait_for('!!(window.TT && TT.S.loaded)', timeout=8)
        self.open_editor('Receipt')
        p.wait_for('(() => { const i = document.querySelector(".md-preview img");'
                   ' return !!i && i.src.startsWith("blob:") && i.complete && i.naturalWidth === 1; })()', timeout=8)

    def test_export_import_and_reminders_through_the_android_bridge(self):
        p = self.open_local(bridge=True)
        today = time.strftime('%Y-%m-%d')
        # A date that has already arrived when it is set needs no reminder.
        self.local_api('POST', '/api/tasks', {'title': 'Already due', 'due_at': '2020-01-01'})
        self.local_api('POST', '/api/tasks', {'title': 'Set when due', 'due_at': f'{today}T00:00'})
        self.local_api('POST', '/api/tasks', {'title': 'Later', 'due_at': '2031-05-05T08:30'})
        self.local_api('POST', '/api/import', {'tasks': [{'title': 'Imported overdue', 'due_at': '2021-03-04T05:06',
                                                         'created_at': '2021-01-01T00:00:00'}]})
        p.eval('TT.checkLocalReminders()')
        time.sleep(0.5)
        self.assertEqual(p.eval('__bridge.notify.length'), 0)
        # One whose time arrives while the app is open (as if set an hour ago).
        p.eval('TT.local.then((l) => l.store.write((tx) => { const t = tx.all("tasks").find((x) => x.title === "Imported overdue");'
               ' tx.put("tasks", Object.assign({}, t, {reminded_at: null})); }))')
        p.eval('TT.checkLocalReminders()')
        p.wait_for('__bridge.schedule.length > 0 && __bridge.notify.length > 0', timeout=5)
        shown = p.eval('__bridge.notify.flat()')
        self.assertEqual([n['title'] for n in shown], ['Imported overdue'])
        self.assertEqual(shown[0]['keys'], ['t4@2021-03-04T05:06'])
        upcoming = p.eval('__bridge.schedule[__bridge.schedule.length - 1]')
        self.assertEqual([(u['key'], u['local'], u['title'], u['body']) for u in upcoming],
                         [('t3@2031-05-05T08:30', '2031-05-05T08:30', 'Later', 'Due today 08:30')])
        import datetime as _dt
        self.assertEqual(upcoming[0]['at'], int(_dt.datetime(2031, 5, 5, 8, 30).timestamp() * 1000))
        p.eval('TT.checkLocalReminders()')
        time.sleep(0.5)
        self.assertEqual(len(p.eval('__bridge.notify.flat()')), 1, 'each reminder is shown once')
        # Export goes to the bridge; importing it into an empty device restores everything.
        p.click('[data-k="side:export"]')
        p.wait_for('__bridge.saveFile.length === 1', timeout=5)
        saved = p.eval('__bridge.saveFile[0]')
        self.assertRegex(saved['name'], r'^todotracker-export-\d{4}-\d{2}-\d{2}\.json$')
        data = json.loads(saved['text'])
        self.assertEqual(sorted(t['title'] for t in data['tasks']), ['Already due', 'Imported overdue', 'Later', 'Set when due'])
        fresh = LocalPageServer()
        self.addCleanup(fresh.cleanup)
        self.browser.close_page(p)
        p = self.open_local(url=fresh.url)
        self.assertEqual(self.local_api('GET', '/api/tasks?view=all')['tasks'], [])
        summary = self.local_api('POST', '/api/import', data)['summary']
        self.assertEqual((summary['tasks'], summary['skipped']), (4, 0))
        p.eval('TT.refresh()')
        p.wait_for('document.querySelectorAll("#list .row").length === 4', timeout=5)

    def test_phone_matrix_in_local_mode(self):
        p = self.open_local(phone=True)
        self.assertIsNone(p.eval('document.activeElement && document.activeElement.id === "qa" ? "qa" : null'),
                          'no keyboard popping up on start')
        a = self.local_api('POST', '/api/tasks', {'title': 'Fix the sink', 'due_at': time.strftime('%Y-%m-%d'), 'priority': 'high'})
        p.eval('TT.showPage("matrix")')
        p.wait_for(f'!!document.querySelector(\'.tray-item[data-key="t{a["id"]}"]\')', timeout=8)
        p.tap(f'.tray-item[data-key="t{a["id"]}"] .tray-title')
        p.wait_for('!!document.querySelector(".mx-actions")')
        p.tap('[data-k="mxa:do"]')
        p.wait_for(f'!!document.querySelector(\'.mx-list.q-do .mx-li[data-key="t{a["id"]}"]\') && !TT.matrixBusy()')
        p.reload(wait=True)
        p.wait_for('!!(window.TT && TT.S.loaded)', timeout=8)
        pos = self.local_api('GET', f'/api/tasks/{a["id"]}')['matrix']
        self.assertTrue(pos[0] >= 0.5 and pos[1] >= 0.5, pos)


class UpdateTests(UICase):
    def server_args(self):
        self.app = copy_app()
        return {'app_dir': self.app}

    def tearDown(self):
        super().tearDown()
        if getattr(self, 'new_srv', None):
            self.new_srv.stop()
        shutil.rmtree(self.app, ignore_errors=True)

    def test_new_build_reloads_the_page_and_saves_pending_text(self):
        t = self.task('Survivor')
        p = self.open()
        p.eval('window.__oldPage = true')
        self.open_editor('Survivor')
        p.click('[data-k="ed:desc"]')
        p.type('written during the update')
        with open(os.path.join(self.app, 'web', 'app.css'), 'a') as f:
            f.write('\n/* new build */\n')
        self.new_srv = AppServer(app_dir=self.app, port=self.srv.port, data_dir=self.srv.data, wait=False)
        self.new_srv.wait_ready(30)
        p.wait_for('!window.__oldPage && !!(window.TT && TT.S.loaded)', timeout=15)
        self.assertEqual(self.new_srv.api('GET', f'/api/tasks/{t["id"]}')['description'], 'written during the update')
        self.srv.proc.wait(10)

    def test_old_server_shows_banner(self):
        p = self.open()
        self.assertTrue(p.eval('document.querySelector("#banner").hidden'))
        # Simulate new page files talking to an older running server.
        self.srv.stop()
        server_py = os.path.join(self.app, 'server.py')
        with open(server_py) as f:
            src = f.read()
        import re
        api = int(re.search(r'^API = (\d+)', src, re.M).group(1))
        with open(server_py, 'w') as f:
            f.write(re.sub(r'^API = \d+', f'API = {api - 1}', src, count=1, flags=re.M))
        self.new_srv = AppServer(app_dir=self.app, port=self.srv.port, data_dir=self.srv.data)
        p.wait_for('!document.querySelector("#banner").hidden && document.querySelector("#banner").textContent.includes("old version is still running")', timeout=15)


# The same tests in the Android app's mode (data in the page, no server).
class LocalQuickAddTests(LocalMode, QuickAddTests):
    pass


class LocalListTests(LocalMode, ListTests):
    pass


class LocalEditorTests(LocalMode, EditorTests):
    pass


class LocalRobustnessTests(LocalMode, RobustnessTests):
    def test_big_pending_edit_asks_before_leaving(self):
        # Without a server there is no size limit and nothing to wait for: a
        # big unsaved edit is saved in place when the page goes away.
        t = self.task('Big')
        p = self.open()
        self.open_editor('Big')
        p.eval('(() => { const ta = document.querySelector("[data-k=\\"ed:desc\\"]"); ta.focus(); })()')
        p.type('x' * 70000)
        p.reload()
        time.sleep(0.4)
        self.assertEqual(p.take_events('Page.javascriptDialogOpening'), [], 'no question before leaving')
        p.wait_for('!!(window.TT && TT.S.loaded)', timeout=8)
        self.assertEqual(len(self.server_task(t['id'])['description']), 70000)


class LocalSubtaskTests(LocalMode, SubtaskTests):
    pass


class LocalSubtaskDetailTests(LocalMode, SubtaskDetailTests):
    pass


class LocalMatrixTests(LocalMode, MatrixTests):
    pass


class LocalImportTests(LocalMode, ImportTests):
    pass


class LocalThemeTests(LocalMode, ThemeTests):
    pass


class LocalPhoneTests(LocalMode, PhoneTests):
    pass


if __name__ == '__main__':
    unittest.main()
