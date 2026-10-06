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
from helpers import AppServer, copy_app  # noqa: E402

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

    def server_args(self):
        return {}

    def tearDown(self):
        errors = []
        if self.page:
            errors = [e for e in self.page.console_errors() if 'favicon' not in e]
            self.browser.close_page(self.page)
        self.srv.cleanup()
        self.assertEqual(errors, [], 'console errors')

    # -- helpers ---------------------------------------------------------

    def open(self, url=None, dark=False):
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

    def server_task(self, tid):
        return self.api('GET', f'/api/tasks/{tid}')

    def titles(self):
        return self.page.eval('Array.from(document.querySelectorAll("#list .row-title")).map(e => e.textContent)')

    def row_sel(self, title, part='.row-main'):
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


if __name__ == '__main__':
    unittest.main()
