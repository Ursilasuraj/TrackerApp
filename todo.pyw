#!/usr/bin/env python3
"""TodoTracker - a personal TODO app (Python standard library only).

    pythonw todo.pyw                start, or show the window of the running app
    pythonw todo.pyw --background   start without a window (used at login)
    python todo.pyw --port 8899     another port (tests; never test on 8765)

Data lives in the "data" folder next to this file; the environment variable
TODOTRACKER_DATA points it somewhere else.
"""

import argparse
import hashlib
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

DEFAULT_PORT = 8765
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

log = logging.getLogger('todotracker')
# Never use a proxy for 127.0.0.1 (urllib reads the Windows proxy settings).
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------------------
# Paths, logging, build id
# ---------------------------------------------------------------------------

def data_dir():
    return os.path.abspath(os.environ.get('TODOTRACKER_DATA') or os.path.join(APP_DIR, 'data'))


def same_path(a, b):
    if not a or not b:
        return False
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def setup_logging(ddir):
    os.makedirs(ddir, exist_ok=True)
    path = os.path.join(ddir, 'todo.log')
    try:
        if os.path.getsize(path) > 2_000_000:
            os.replace(path, path + '.1')
    except OSError:
        pass
    stream = open(path, 'a', encoding='utf-8', errors='replace', buffering=1)
    # Under pythonw there is no console: sys.stdout/stderr are None and any
    # print or traceback would crash. Send them to the log instead.
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter('%(asctime)s %(process)d %(levelname)s %(name)s: %(message)s'))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    def excepthook(exc_type, exc, tb):
        log.critical('uncaught exception', exc_info=(exc_type, exc, tb))

    def thread_excepthook(args):
        log.critical('uncaught exception in thread %s', args.thread and args.thread.name,
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook


def compute_build():
    """SHA-1 over the app's own Python files and everything in web/."""
    rels = sorted(n for n in os.listdir(APP_DIR) if n.endswith(('.py', '.pyw')))
    web = os.path.join(APP_DIR, 'web')
    for root, dirs, files in os.walk(web):
        dirs.sort()
        for name in sorted(files):
            rels.append(os.path.relpath(os.path.join(root, name), APP_DIR))
    h = hashlib.sha1()
    for rel in rels:
        h.update(rel.replace(os.sep, '/').encode('utf-8') + b'\0')
        with open(os.path.join(APP_DIR, rel), 'rb') as f:
            h.update(f.read())
        h.update(b'\0')
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Talking to a running instance
# ---------------------------------------------------------------------------

def http_json(port, path, method='GET', timeout=2.0):
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', method=method)
    if method != 'GET':
        req.add_header('X-Todo', '1')
        req.add_header('Content-Type', 'application/json')
        req.data = b'{}'
    with _opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def ping(port, timeout=1.5):
    """Info of the TodoTracker on the port, or None (nothing, or something else)."""
    try:
        info = http_json(port, '/api/ping', timeout=timeout)
    except Exception:
        return None
    if isinstance(info, dict) and info.get('app') == 'TodoTracker':
        return info
    return None


def port_in_use(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_port_free(port, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not port_in_use(port):
            return True
        time.sleep(0.2)
    return not port_in_use(port)


def process_cmdline(pid):
    if os.name == 'nt':
        try:
            out = subprocess.run(
                ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                 f'(Get-CimInstance Win32_Process -Filter "ProcessId={int(pid)}").CommandLine'],
                capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW)
            return out.stdout.strip()
        except Exception:
            log.exception('could not read the command line of process %s', pid)
            return ''
    try:
        with open(f'/proc/{int(pid)}/cmdline', 'rb') as f:
            return f.read().replace(b'\0', b' ').decode('utf-8', 'replace')
    except OSError:
        return ''


def port_owner(port):
    """PID of the process listening on 127.0.0.1:port (Windows: netstat)."""
    if os.name != 'nt':
        return None
    try:
        out = subprocess.run(['netstat', '-ano', '-p', 'TCP'], capture_output=True, text=True,
                             timeout=20, stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW).stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1] == f'127.0.0.1:{port}' and parts[3].upper() == 'LISTENING':
            try:
                return int(parts[4])
            except ValueError:
                return None
    return None


def process_alive(pid):
    if os.name == 'nt':
        import ctypes
        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f'/proc/{int(pid)}/stat') as f:
            return f.read().split(')')[-1].split()[0] != 'Z'
    except OSError:
        return True


def wait_process_exit(pid, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.1)
    return not process_alive(pid)


def kill_process(pid):
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(int(pid)), '/F'], capture_output=True,
                       stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    else:
        try:
            os.kill(int(pid), signal.SIGTERM)
            if not wait_process_exit(pid, 3):
                os.kill(int(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def stop_instance(port, info):
    """Ask an older instance to stop; force it only if it is really todo.pyw."""
    pid = info.get('pid')
    log.info('replacing the running instance (pid %s, api %s, build %s)',
             pid, info.get('api'), str(info.get('build'))[:10])
    try:
        http_json(port, '/api/shutdown', method='POST', timeout=3)
    except Exception as e:
        log.warning('shutdown request failed: %s', e)
    if wait_port_free(port, 10):
        if pid:
            wait_process_exit(pid, 10)
        return True
    owner = port_owner(port) or pid
    if owner and 'todo.pyw' in process_cmdline(owner).lower():
        log.warning('process %s still owns port %s; stopping it', owner, port)
        kill_process(owner)
        wait_process_exit(owner, 5)
        return wait_port_free(port, 10)
    log.error('port %s is still in use after the shutdown request', port)
    return False


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

def find_edge():
    if os.name != 'nt':
        return None
    candidates = []
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe') as key:
                    candidates.append(winreg.QueryValue(key, None))
            except OSError:
                pass
    except ImportError:
        pass
    for env in ('ProgramFiles(x86)', 'ProgramFiles', 'LOCALAPPDATA'):
        base = os.environ.get(env)
        if base:
            candidates.append(os.path.join(base, 'Microsoft', 'Edge', 'Application', 'msedge.exe'))
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return shutil.which('msedge')


def open_window(url):
    """Open the UI as an Edge app window (or in the default browser)."""
    if os.environ.get('TODOTRACKER_NO_WINDOW') == '1':
        log.info('opening window: %s (suppressed by TODOTRACKER_NO_WINDOW)', url)
        return True
    log.info('opening window: %s', url)
    edge = find_edge()
    if edge:
        try:
            subprocess.Popen([edge, f'--app={url}', '--window-size=1200,820'],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True,
                             creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
            return True
        except OSError:
            log.exception('could not start Edge; using the default browser')
    try:
        return webbrowser.open(url)
    except Exception:
        log.exception('could not open a browser')
        return False


def message_box(text):
    log.error(text)
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, 'TodoTracker', 0x10)
        except Exception:
            pass
    elif sys.stderr is not None:
        print(text, file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(description='TodoTracker')
    p.add_argument('--background', action='store_true', help='start without opening a window')
    p.add_argument('--port', type=int, default=DEFAULT_PORT, help='HTTP port (default 8765)')
    return p.parse_args(argv)


def should_replace(info, api, build, ddir):
    """True if the running instance is an older build of this very copy."""
    same = same_path(info.get('app_dir'), APP_DIR) and same_path(info.get('data_dir'), ddir)
    if not same:
        return False
    their_api = info.get('api') if isinstance(info.get('api'), int) else 0
    return their_api < api or (their_api == api and info.get('build') != build)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    ddir = data_dir()
    setup_logging(ddir)
    import backup
    import db as dbmod
    import hotkey
    import reminders
    import server

    build = compute_build()
    port = args.port
    url = f'http://127.0.0.1:{port}/'
    log.info('starting pid %s, build %s, port %s, data %s', os.getpid(), build[:10], port, ddir)

    info = ping(port)
    if info:
        if should_replace(info, server.API, build, ddir):
            if not stop_instance(port, info):
                message_box(f'TodoTracker could not replace the running copy on port {port}.')
                return 1
        else:
            log.info('TodoTracker already runs (pid %s); showing its window', info.get('pid'))
            if not args.background:
                open_window(url)
            return 0

    httpd = None
    deadline = time.monotonic() + 15
    while httpd is None:
        try:
            httpd = server.Server(port)
        except OSError as e:
            other = ping(port)
            if other and should_replace(other, server.API, build, ddir):
                stop_instance(port, other)
            elif other:
                # Another launch won the race: show its window instead.
                log.info('another instance (pid %s) started first', other.get('pid'))
                if not args.background:
                    open_window(url)
                return 0
            if time.monotonic() > deadline:
                message_box(f'TodoTracker could not start: port {port} is in use ({e}).')
                return 1
            time.sleep(0.25)

    # Migrations run only now, after any older instance has stopped.
    store = dbmod.Store(os.path.join(ddir, 'todo.db'))
    try:
        store.open()
    except Exception:
        log.exception('database could not be opened')
        httpd.server_close()
        message_box('TodoTracker could not open its database (see data/todo.log).')
        return 1

    db_path = os.path.join(ddir, 'todo.db')
    backups_dir = os.path.join(ddir, 'backups')
    app = server.App(store=store, app_dir=APP_DIR, data_dir=ddir, build=build, port=port,
                     backup_now=lambda: backup.run_backup(db_path, backups_dir, force=True))
    app.httpd = httpd
    httpd.app = app

    backup.BackupThread(db_path, backups_dir, app.stopping).start()
    reminders.ReminderThread(store, app.stopping).start()

    def on_hotkey():
        app.hotkey_pressed()
        hwnd = hotkey.find_app_window()
        if hwnd:
            hotkey.focus_window(hwnd)
        else:
            open_window(url)

    hk = hotkey.HotkeyThread(on_hotkey)
    hk.start()

    if not args.background:
        open_window(url)
    log.info('listening on %s', url)
    try:
        httpd.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        app.stopping.set()
        httpd.server_close()
        hk.stop()
        store.close()
        log.info('stopped')
    return 0


if __name__ == '__main__':
    sys.exit(main())
