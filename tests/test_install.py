"""Installer tests for Linux and macOS (install.sh, uninstall.sh, tools/).

Run:  python -m unittest tests.test_install -v   (from the app folder)
Each test installs into a scratch HOME from a copy of the app whose folder
name contains spaces, quotes, $ and %, and never starts the app.
"""

import os
import plistlib
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from helpers import copy_app, free_port, scratch_dir  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'tools'))
import icons  # noqa: E402

AWKWARD = 'My "Todo" $HOME `x` 100% app'


def parse_exec(value):
    """argv of an Exec= value (Desktop Entry Specification, in reverse)."""
    unescaped, i = '', 0
    while i < len(value):                       # 1. string escapes
        ch = value[i]
        if ch == '\\' and i + 1 < len(value):
            nxt = value[i + 1]
            unescaped += {'s': ' ', 'n': '\n', 't': '\t', 'r': '\r', '\\': '\\'}.get(nxt, '\\' + nxt)
            i += 2
            continue
        unescaped += ch
        i += 1
    args, cur, quoted, have, i = [], '', False, False, 0
    while i < len(unescaped):                   # 2. quoting
        ch = unescaped[i]
        if quoted:
            if ch == '\\':
                assert i + 1 < len(unescaped) and unescaped[i + 1] in '"`$\\', 'stray backslash: ' + value
                cur += unescaped[i + 1]
                i += 2
                continue
            assert ch not in '`$', 'unescaped ' + ch + ' in quotes: ' + value
            if ch == '"':
                quoted = False
            else:
                cur += ch
        elif ch == '"':
            quoted, have = True, True
        elif ch == ' ':
            if cur or have:
                args.append(cur)
            cur, have = '', False
        else:
            cur += ch
        i += 1
    if cur or have:
        args.append(cur)
    return [a.replace('%%', '%') for a in args]   # 3. field codes


def read_entry(path):
    entry = {}
    with open(path, encoding='utf-8') as f:
        for line in f.read().splitlines()[1:]:
            key, _, value = line.partition('=')
            entry[key] = value
    return entry


@unittest.skipIf(os.name == 'nt', 'install.sh is for Linux and macOS')
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.root = scratch_dir('tt-install-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.app = copy_app(os.path.join(self.root, AWKWARD), tools=True)
        self.home = os.path.join(self.root, 'home')
        os.makedirs(self.home)
        self.env = dict(os.environ, HOME=self.home, TODOTRACKER_PYTHON=sys.executable, PYTHONDONTWRITEBYTECODE='1')
        for key in ('XDG_DATA_HOME', 'XDG_CONFIG_HOME'):
            self.env.pop(key, None)
        self.port = str(free_port())

    def run_script(self, name, *args, ok=True):
        r = subprocess.run(['sh', os.path.join(self.app, name), '--no-start' if name == 'install.sh' else '--port',
                            *([] if name == 'install.sh' else [self.port]), *args],
                           env=self.env, capture_output=True, text=True, timeout=120)
        if ok:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def test_linux_menu_entry_and_autostart(self):
        r = self.run_script('install.sh', '--system', 'linux')
        menu = os.path.join(self.home, '.local', 'share', 'applications', 'todotracker.desktop')
        login = os.path.join(self.home, '.config', 'autostart', 'todotracker.desktop')
        script = os.path.join(self.app, 'todo.pyw')
        self.assertEqual(parse_exec(read_entry(menu)['Exec']), [sys.executable, script])
        self.assertEqual(parse_exec(read_entry(login)['Exec']), [sys.executable, script, '--background'])
        self.assertEqual(read_entry(menu)['Icon'], os.path.join(self.app, 'web', 'icon-512.png'))
        self.assertTrue(os.path.isfile(read_entry(menu)['Icon']))
        self.assertIn('give this command a keyboard shortcut', r.stdout)
        if shutil.which('desktop-file-validate'):
            for path in (menu, login):
                v = subprocess.run(['desktop-file-validate', path], capture_output=True, text=True)
                self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
        # Running the Exec line really starts todo.pyw (--help: without a server).
        out = subprocess.run(parse_exec(read_entry(menu)['Exec']) + ['--help'], capture_output=True,
                             text=True, timeout=60, env=self.env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('--background', out.stdout)

        self.run_script('install.sh', '--system', 'linux', '--open-at-login')
        self.assertEqual(parse_exec(read_entry(login)['Exec']), [sys.executable, script])
        self.run_script('install.sh', '--system', 'linux', '--no-autostart')
        self.assertTrue(os.path.exists(menu))
        self.assertFalse(os.path.exists(login))

        self.run_script('uninstall.sh', '--system', 'linux')
        self.assertFalse(os.path.exists(menu))
        self.assertTrue(os.path.isdir(self.app))

    def test_xdg_folders_are_respected(self):
        self.env['XDG_DATA_HOME'] = os.path.join(self.root, 'xdg data')
        self.env['XDG_CONFIG_HOME'] = os.path.join(self.root, 'xdg config')
        self.run_script('install.sh', '--system', 'linux')
        self.assertTrue(os.path.exists(os.path.join(self.root, 'xdg data', 'applications', 'todotracker.desktop')))
        self.assertTrue(os.path.exists(os.path.join(self.root, 'xdg config', 'autostart', 'todotracker.desktop')))

    def test_mac_bundle_and_launch_agent(self):
        self.run_script('install.sh', '--system', 'mac')
        bundle = os.path.join(self.home, 'Applications', 'TodoTracker.app')
        with open(os.path.join(bundle, 'Contents', 'Info.plist'), 'rb') as f:
            info = plistlib.load(f)
        self.assertEqual(info['CFBundleIdentifier'], 'local.todotracker')
        self.assertTrue(info['LSUIElement'])
        launcher = os.path.join(bundle, 'Contents', 'MacOS', info['CFBundleExecutable'])
        self.assertTrue(os.access(launcher, os.X_OK))
        out = subprocess.run([launcher, '--help'], capture_output=True, text=True, timeout=60, env=self.env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('--background', out.stdout)
        agent = os.path.join(self.home, 'Library', 'LaunchAgents', 'local.todotracker.plist')
        with open(agent, 'rb') as f:
            job = plistlib.load(f)
        self.assertEqual(job['ProgramArguments'], [launcher, '--background'])
        self.assertTrue(job['RunAtLoad'])
        self.assertNotIn('KeepAlive', job)   # a replaced instance must stay stopped

        self.run_script('uninstall.sh', '--system', 'mac')
        self.assertFalse(os.path.exists(bundle))
        self.assertFalse(os.path.exists(agent))

    def test_foreign_mac_bundle_is_left_alone(self):
        bundle = os.path.join(self.home, 'Applications', 'TodoTracker.app')
        os.makedirs(os.path.join(bundle, 'Contents'))
        with open(os.path.join(bundle, 'Contents', 'Info.plist'), 'wb') as f:
            plistlib.dump({'CFBundleIdentifier': 'com.example.other'}, f)
        r = self.run_script('install.sh', '--system', 'mac', ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('was not made by this installer', r.stderr)
        self.run_script('uninstall.sh', '--system', 'mac')
        self.assertTrue(os.path.exists(os.path.join(bundle, 'Contents', 'Info.plist')))

    def test_missing_python_is_explained(self):
        env = dict(self.env, PATH='/nonexistent', TODOTRACKER_PYTHON='')
        r = subprocess.run(['/bin/sh', os.path.join(self.app, 'install.sh'), '--no-start'],
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn('needs Python 3.9 or newer', r.stderr)


class IconTests(unittest.TestCase):
    def test_png_and_ico(self):
        data = icons.png(icons.render(48))
        self.assertTrue(data.startswith(b'\x89PNG\r\n\x1a\n'))
        rows = icons.render(64)
        corner = rows[0][:4]
        centre = rows[32][32 * 4:32 * 4 + 4]
        self.assertEqual(corner[3], 0, 'corners of the rounded square are transparent')
        self.assertEqual(centre[3], 255)
        ico = icons.ico([16, 256])
        self.assertEqual(ico[:6], b'\0\0\1\0\2\0')

    def test_adaptive_foreground_keeps_the_tick_in_the_safe_zone(self):
        size = 432
        rows = icons.render(size, 'foreground')
        safe = 33 / 108 * size        # radius of the 66dp safe circle
        for y, row in enumerate(rows):
            for x in range(size):
                if row[x * 4 + 3]:
                    self.assertLess(((x + 0.5 - size / 2) ** 2 + (y + 0.5 - size / 2) ** 2) ** 0.5, safe)


if __name__ == '__main__':
    unittest.main()
