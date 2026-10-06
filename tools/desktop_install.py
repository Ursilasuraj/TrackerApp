"""Installs TodoTracker for the current user on Linux or macOS (no root needed).

Run it through install.sh / uninstall.sh, which find a suitable python3:

    sh install.sh                    menu entry, start at login (no window), start now
    sh install.sh --open-at-login    open the window at login too
    sh install.sh --no-autostart     menu entry only
    sh uninstall.sh                  remove both and stop the app (keeps your data)

Nothing is copied: the app runs from this folder and keeps its data in ./data.

Linux:  ~/.local/share/applications/todotracker.desktop     (app menu)
        ~/.config/autostart/todotracker.desktop             (login)
macOS:  ~/Applications/TodoTracker.app                      (Launchpad, Spotlight, Dock)
        ~/Library/LaunchAgents/local.todotracker.plist      (login)
"""

import argparse
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(TOOLS_DIR)
sys.path.insert(0, TOOLS_DIR)

import icons  # noqa: E402

BUNDLE_ID = 'local.todotracker'
DESKTOP_NAME = 'todotracker.desktop'
DEFAULT_PORT = 8765


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def home():
    return os.path.expanduser('~')


def linux_paths():
    data = os.environ.get('XDG_DATA_HOME') or os.path.join(home(), '.local', 'share')
    config = os.environ.get('XDG_CONFIG_HOME') or os.path.join(home(), '.config')
    return {'menu': os.path.join(data, 'applications', DESKTOP_NAME),
            'autostart': os.path.join(config, 'autostart', DESKTOP_NAME)}


def mac_paths():
    return {'bundle': os.path.join(home(), 'Applications', 'TodoTracker.app'),
            'agent': os.path.join(home(), 'Library', 'LaunchAgents', BUNDLE_ID + '.plist')}


# ---------------------------------------------------------------------------
# Linux: desktop entries (freedesktop.org Desktop Entry Specification)
# ---------------------------------------------------------------------------

RESERVED = set(' \t\n"\'\\><~|&;$*?#()`')


def exec_argument(arg):
    """Quote one argument for an Exec= line."""
    if '\n' in arg or '\r' in arg:
        raise ValueError('a path with a line break cannot be used in a desktop entry')
    if any(ch in RESERVED for ch in arg):
        arg = '"' + ''.join('\\' + ch if ch in '"`$\\' else ch for ch in arg) + '"'
    return arg.replace('%', '%%')


def string_value(text):
    """Escape a value of type string (applies before the Exec quoting rule)."""
    if '\n' in text or '\r' in text:
        raise ValueError('a path with a line break cannot be used in a desktop entry')
    return text.replace('\\', '\\\\')


def desktop_entry(python, background, autostart=False):
    args = [python, os.path.join(APP_DIR, 'todo.pyw')] + (['--background'] if background else [])
    lines = [
        '[Desktop Entry]',
        'Type=Application',
        'Version=1.0',
        'Name=TodoTracker',
        'GenericName=TODO list',
        'Comment=Your personal TODO list',
        'Exec=' + string_value(' '.join(exec_argument(a) for a in args)),
        'Path=' + string_value(APP_DIR),
        'Icon=' + string_value(os.path.join(APP_DIR, 'web', 'icon-512.png')),
        'Terminal=false',
        'StartupNotify=false',
        'Categories=Office;ProjectManagement;',
        'Keywords=todo;task;planner;reminder;',
    ]
    if autostart:
        lines.append('X-GNOME-Autostart-enabled=true')
    return '\n'.join(lines) + '\n'


def write_file(path, data, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data.encode('utf-8') if isinstance(data, str) else data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    print('Created:', path)


def install_linux(python, autostart, open_at_login):
    paths = linux_paths()
    write_file(paths['menu'], desktop_entry(python, background=False))
    if autostart:
        write_file(paths['autostart'], desktop_entry(python, background=not open_at_login, autostart=True))
    elif os.path.exists(paths['autostart']):
        os.remove(paths['autostart'])
        print('Removed:', paths['autostart'])
    if shutil.which('update-desktop-database'):
        subprocess.run(['update-desktop-database', os.path.dirname(paths['menu'])],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def uninstall_linux():
    for path in linux_paths().values():
        if os.path.exists(path):
            os.remove(path)
            print('Removed:', path)


# ---------------------------------------------------------------------------
# macOS: an app bundle and a LaunchAgent
# ---------------------------------------------------------------------------

def is_our_bundle(bundle):
    try:
        with open(os.path.join(bundle, 'Contents', 'Info.plist'), 'rb') as f:
            return plistlib.load(f).get('CFBundleIdentifier') == BUNDLE_ID
    except (OSError, plistlib.InvalidFileException, ValueError):
        return False


def bundle_launcher(python):
    script = os.path.join(APP_DIR, 'todo.pyw')
    return ('#!/bin/sh\n'
            '# Starts TodoTracker (or brings its window to the front).\n'
            f'cd {shlex.quote(APP_DIR)} || exit 1\n'
            f'exec {shlex.quote(python)} {shlex.quote(script)} "$@"\n')


def info_plist():
    return plistlib.dumps({
        'CFBundleName': 'TodoTracker',
        'CFBundleDisplayName': 'TodoTracker',
        'CFBundleIdentifier': BUNDLE_ID,
        'CFBundleExecutable': 'TodoTracker',
        'CFBundleIconFile': 'TodoTracker',
        'CFBundlePackageType': 'APPL',
        'CFBundleShortVersionString': '1.0',
        'CFBundleVersion': '1',
        'LSMinimumSystemVersion': '10.13',
        'LSUIElement': True,          # no Dock icon: the window is a browser app window
        'NSHighResolutionCapable': True,
    })


def launch_agent(bundle, open_at_login):
    args = [os.path.join(bundle, 'Contents', 'MacOS', 'TodoTracker')]
    if not open_at_login:
        args.append('--background')
    return plistlib.dumps({
        'Label': BUNDLE_ID,
        'ProgramArguments': args,
        'RunAtLoad': True,
        'ProcessType': 'Interactive',
        'LimitLoadToSessionType': 'Aqua',
        'AssociatedBundleIdentifiers': [BUNDLE_ID],
    })


def make_icns(resources):
    if not shutil.which('iconutil'):
        print('iconutil not found; the app keeps the default icon.')
        return
    with tempfile.TemporaryDirectory() as tmp:
        iconset = os.path.join(tmp, 'TodoTracker.iconset')
        icons.make_iconset(iconset)
        out = os.path.join(resources, 'TodoTracker.icns')
        result = subprocess.run(['iconutil', '-c', 'icns', '-o', out, iconset], capture_output=True)
        if result.returncode != 0:
            print('iconutil failed; the app keeps the default icon.', result.stderr.decode(errors='replace'))


def install_mac(python, autostart, open_at_login):
    paths = mac_paths()
    bundle = paths['bundle']
    if os.path.exists(bundle) and not is_our_bundle(bundle):
        raise SystemExit(f'{bundle} exists and was not made by this installer; move it away first.')
    if os.path.exists(bundle):
        shutil.rmtree(bundle)
    contents = os.path.join(bundle, 'Contents')
    write_file(os.path.join(contents, 'Info.plist'), info_plist())
    write_file(os.path.join(contents, 'MacOS', 'TodoTracker'), bundle_launcher(python), mode=0o755)
    os.makedirs(os.path.join(contents, 'Resources'), exist_ok=True)
    make_icns(os.path.join(contents, 'Resources'))
    lsregister = ('/System/Library/Frameworks/CoreServices.framework/Frameworks/'
                  'LaunchServices.framework/Support/lsregister')
    if os.path.exists(lsregister):
        subprocess.run([lsregister, '-f', bundle], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if autostart:
        # Loaded by launchd at the next login; the app is started directly below.
        write_file(paths['agent'], launch_agent(bundle, open_at_login))
    else:
        remove_agent(paths['agent'])


def remove_agent(path):
    if shutil.which('launchctl'):
        subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{BUNDLE_ID}'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.path.exists(path):
        os.remove(path)
        print('Removed:', path)


def uninstall_mac():
    paths = mac_paths()
    remove_agent(paths['agent'])
    if os.path.exists(paths['bundle']):
        if is_our_bundle(paths['bundle']):
            shutil.rmtree(paths['bundle'])
            print('Removed:', paths['bundle'])
        else:
            print('Left alone (not made by this installer):', paths['bundle'])


# ---------------------------------------------------------------------------
# Running app
# ---------------------------------------------------------------------------

def same_path(a, b):
    return bool(a and b) and os.path.realpath(a) == os.path.realpath(b)


def request_shutdown(port):
    """Ask the TodoTracker from this folder to stop. True if it answered."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f'http://127.0.0.1:{port}/api/ping', timeout=2) as resp:
            info = json.loads(resp.read().decode('utf-8'))
        if info.get('app') != 'TodoTracker' or not same_path(info.get('app_dir'), APP_DIR):
            return False
        req = urllib.request.Request(f'http://127.0.0.1:{port}/api/shutdown', data=b'{}', method='POST',
                                     headers={'X-Todo': '1', 'Content-Type': 'application/json'})
        with opener.open(req, timeout=5):
            pass
        return True
    except Exception:
        return False


def start_app(python, system):
    if system == 'mac':
        subprocess.Popen(['open', mac_paths()['bundle']])
    else:
        subprocess.Popen([python, os.path.join(APP_DIR, 'todo.pyw')], cwd=APP_DIR,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description='Install TodoTracker for this user (Linux, macOS).')
    p.add_argument('action', choices=['install', 'uninstall'])
    p.add_argument('--python', default=sys.executable, help='the python3 that runs the app')
    p.add_argument('--open-at-login', action='store_true', help='open the window at login too')
    p.add_argument('--no-autostart', action='store_true', help='do not start at login')
    p.add_argument('--no-start', action='store_true', help='do not start the app now')
    p.add_argument('--port', type=int, default=DEFAULT_PORT, help=argparse.SUPPRESS)
    p.add_argument('--system', choices=['linux', 'mac'],
                   default='mac' if sys.platform == 'darwin' else 'linux', help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if os.name == 'nt':
        raise SystemExit('On Windows run install.ps1 instead.')
    python = os.path.abspath(args.python)

    if args.action == 'install':
        print('Python: ', python)
        print('App:    ', APP_DIR)
        if args.system == 'mac':
            install_mac(python, not args.no_autostart, args.open_at_login)
        else:
            install_linux(python, not args.no_autostart, args.open_at_login)
        if not args.no_start:
            start_app(python, args.system)
        print()
        print('TodoTracker is installed. Open it from your app menu'
              + (' (Launchpad / Spotlight).' if args.system == 'mac' else '.'))
        if not args.no_autostart:
            print('It starts at login' + ('.' if args.open_at_login else ' in the background.'))
        print('To open it with a key of your choice, give this command a keyboard shortcut in your system settings:')
        print('    ' + ' '.join(shlex.quote(a) for a in (python, os.path.join(APP_DIR, 'todo.pyw'))))
        print('Your data stays in', os.path.join(APP_DIR, 'data'))
    else:
        if args.system == 'mac':
            uninstall_mac()
        else:
            uninstall_linux()
        if request_shutdown(args.port):
            print('Asked TodoTracker to stop.')
            time.sleep(1)
        print()
        print('TodoTracker is uninstalled. Your data is still in', os.path.join(APP_DIR, 'data'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
