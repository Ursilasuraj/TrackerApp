"""Everything that differs between Windows, macOS and Linux.

- notify(title, body): a desktop notification (Windows toast, macOS
  Notification Centre, Linux notify-send / D-Bus).
- open_window(url): the UI in an app window of Edge, Chrome, Chromium or
  Brave (--app=URL), else in the default browser.
- activate_window(): bring an existing TodoTracker window to the front.
- process helpers used by the single-instance logic.

External programs are always started with an argument list (no shell), so
nothing typed into a task can turn into a command.
"""

import base64
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import webbrowser

log = logging.getLogger('todotracker.platform')

IS_WINDOWS = os.name == 'nt'
IS_MAC = sys.platform == 'darwin'
IS_LINUX = not IS_WINDOWS and not IS_MAC

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(APP_DIR, 'web', 'icon-192.png')
WINDOW_TITLE = 'TodoTracker'
WINDOW_SIZE = '1200,820'

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def _run(args, timeout=30, **kw):
    """Run a helper program quietly; returns CompletedProcess or None."""
    if IS_WINDOWS:
        kw.setdefault('creationflags', CREATE_NO_WINDOW)
    try:
        return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=timeout, **kw)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning('%s failed: %s', args[0], e)
        return None


def _spawn(args):
    """Start a program that keeps running after we exit."""
    kw = {}
    if IS_WINDOWS:
        kw['creationflags'] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw['start_new_session'] = True
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, **kw)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

WINDOWS_APP_ID = r'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'

# Texts arrive through environment variables, so nothing typed into a task
# title can change the script.
WINDOWS_TOAST_SCRIPT = r'''
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

MAC_NOTIFY_SCRIPT = ['-e', 'on run argv',
                     '-e', 'display notification (item 2 of argv) with title (item 1 of argv)',
                     '-e', 'end run']


def gvariant_string(text):
    """Quote text as a GVariant string literal (for gdbus)."""
    text = text.replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n').replace('\r', '\\r')
    return "'" + text + "'"


def notification_command(title, body):
    """The command that shows a notification here, or None (pure, testable)."""
    if IS_WINDOWS:
        exe = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                           'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
        encoded = base64.b64encode(WINDOWS_TOAST_SCRIPT.encode('utf-16-le')).decode('ascii')
        return [exe, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                '-WindowStyle', 'Hidden', '-EncodedCommand', encoded]
    if IS_MAC:
        return ['osascript', *MAC_NOTIFY_SCRIPT, title, body]
    if shutil.which('notify-send'):
        cmd = ['notify-send', '--app-name=TodoTracker']
        if os.path.exists(ICON):
            cmd.append('--icon=' + ICON)
        return cmd + ['--', title, body]
    if shutil.which('gdbus'):
        return ['gdbus', 'call', '--session', '--dest', 'org.freedesktop.Notifications',
                '--object-path', '/org/freedesktop/Notifications',
                '--method', 'org.freedesktop.Notifications.Notify',
                gvariant_string('TodoTracker'), '0', gvariant_string(ICON if os.path.exists(ICON) else ''),
                gvariant_string(title), gvariant_string(body), '[]', '{}', '15000']
    return None


def notify(title, body):
    """Show a desktop notification. Returns True when it was handed over."""
    title, body = title[:200], body[:400]
    if os.environ.get('TODOTRACKER_NO_NOTIFY') == '1':
        log.info('notification: %s | %s (suppressed)', title, body)
        return True
    cmd = notification_command(title, body)
    if not cmd:
        log.info('notification (no notifier available): %s | %s', title, body)
        return False
    env = None
    if IS_WINDOWS:
        env = dict(os.environ, TT_TOAST_TITLE=title, TT_TOAST_BODY=body, TT_TOAST_APPID=WINDOWS_APP_ID)
    result = _run(cmd, timeout=60, env=env)
    if result is None or result.returncode != 0:
        log.warning('notification failed: %s', result.stderr.decode('utf-8', 'replace')[:400] if result else '')
        return False
    return True


# ---------------------------------------------------------------------------
# Browser window
# ---------------------------------------------------------------------------

MAC_BROWSERS = ['Microsoft Edge', 'Google Chrome', 'Brave Browser', 'Chromium']
LINUX_BROWSERS = ['microsoft-edge', 'microsoft-edge-stable', 'google-chrome', 'google-chrome-stable',
                  'chromium', 'chromium-browser', 'brave-browser', 'brave']


def _windows_browsers():
    found = []
    try:
        import winreg
        for exe in ('msedge.exe', 'chrome.exe', 'brave.exe'):
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths' + '\\' + exe) as key:
                        found.append(winreg.QueryValue(key, None))
                except OSError:
                    pass
    except ImportError:
        pass
    for env in ('ProgramFiles(x86)', 'ProgramFiles', 'LOCALAPPDATA'):
        base = os.environ.get(env)
        if base:
            found.append(os.path.join(base, 'Microsoft', 'Edge', 'Application', 'msedge.exe'))
            found.append(os.path.join(base, 'Google', 'Chrome', 'Application', 'chrome.exe'))
    return found


def find_browser():
    """('exe', path) or ('mac-app', bundle path) for an app-window browser, or None."""
    override = os.environ.get('TODOTRACKER_BROWSER')
    if override:
        if IS_MAC and override.endswith('.app'):
            return ('mac-app', override)
        return ('exe', override)
    if IS_WINDOWS:
        for path in _windows_browsers():
            if path and os.path.isfile(path):
                return ('exe', path)
        path = shutil.which('msedge') or shutil.which('chrome')
        return ('exe', path) if path else None
    if IS_MAC:
        for name in MAC_BROWSERS:
            for base in ('/Applications', os.path.expanduser('~/Applications')):
                bundle = os.path.join(base, name + '.app')
                if os.path.isdir(bundle):
                    return ('mac-app', bundle)
        return None
    for name in LINUX_BROWSERS:
        path = shutil.which(name)
        if path:
            return ('exe', path)
    return None


def window_command(url, browser):
    """Command line that opens url as an app window with the given browser."""
    kind, path = browser
    args = ['--app=' + url, '--window-size=' + WINDOW_SIZE]
    if kind == 'mac-app':
        return ['open', '-n', '-a', path, '--args', *args]
    return [path, *args]


def open_window(url):
    """Open the UI in an app window (or the default browser)."""
    if os.environ.get('TODOTRACKER_NO_WINDOW') == '1':
        log.info('opening window: %s (suppressed by TODOTRACKER_NO_WINDOW)', url)
        return True
    log.info('opening window: %s', url)
    browser = find_browser()
    if browser:
        try:
            _spawn(window_command(url, browser))
            return True
        except OSError:
            log.exception('could not start %s; using the default browser', browser[1])
    try:
        return webbrowser.open(url)
    except Exception:
        log.exception('could not open a browser')
        return False


MAC_ACTIVATE_SCRIPT = '''
on run argv
  set wanted to item 1 of argv
  repeat with appName in {"Microsoft Edge", "Google Chrome", "Brave Browser", "Chromium"}
    if application appName is running then
      tell application appName
        repeat with w in windows
          if name of w is wanted then
            set index of w to 1
            activate
            return "ok"
          end if
        end repeat
      end tell
    end if
  end repeat
  return "none"
end run
'''


def activate_window():
    """Bring an open TodoTracker window to the front. True if one was found."""
    if os.environ.get('TODOTRACKER_NO_WINDOW') == '1':
        return False
    if IS_WINDOWS:
        import hotkey
        hwnd = hotkey.find_app_window()
        if hwnd:
            hotkey.focus_window(hwnd)
            return True
        return False
    if IS_MAC:
        result = _run(['osascript', '-e', MAC_ACTIVATE_SCRIPT, WINDOW_TITLE], timeout=15)
        return bool(result and result.returncode == 0 and result.stdout.strip() == b'ok')
    if shutil.which('wmctrl'):
        result = _run(['wmctrl', '-F', '-a', WINDOW_TITLE], timeout=10)
        if result and result.returncode == 0:
            return True
    if shutil.which('xdotool'):
        result = _run(['xdotool', 'search', '--name', '^' + WINDOW_TITLE + '$', 'windowactivate'], timeout=10)
        if result and result.returncode == 0:
            return True
    return False


def message_box(text):
    """Tell the user about a problem that stops the app from starting."""
    log.error(text)
    if IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.WinDLL('user32')
            user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
            user32.MessageBoxW(None, text, 'TodoTracker', 0x10)
        except Exception:
            pass
        return
    if os.environ.get('TODOTRACKER_NO_WINDOW') != '1':
        if IS_MAC:
            _run(['osascript', '-e', 'on run argv', '-e', 'display alert "TodoTracker" message (item 1 of argv)',
                  '-e', 'end run', text], timeout=120)
        elif shutil.which('zenity'):
            _run(['zenity', '--error', '--title=TodoTracker', '--text=' + text], timeout=120)
    if sys.stderr is not None:
        print(text, file=sys.stderr)


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------

def process_cmdline(pid):
    """Command line of a process ('' if unknown)."""
    if IS_WINDOWS:
        result = _run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                       f'(Get-CimInstance Win32_Process -Filter "ProcessId={int(pid)}").CommandLine'], timeout=20)
        return result.stdout.decode('utf-8', 'replace').strip() if result else ''
    try:
        with open(f'/proc/{int(pid)}/cmdline', 'rb') as f:
            return f.read().replace(b'\0', b' ').decode('utf-8', 'replace')
    except OSError:
        pass
    result = _run(['ps', '-o', 'command=', '-p', str(int(pid))], timeout=10)
    return result.stdout.decode('utf-8', 'replace').strip() if result and result.returncode == 0 else ''


def port_owner(port):
    """PID of the process listening on 127.0.0.1:port, or None."""
    if IS_WINDOWS:
        result = _run(['netstat', '-ano', '-p', 'TCP'], timeout=20)
        for line in (result.stdout.decode('utf-8', 'replace') if result else '').splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1] == f'127.0.0.1:{port}' and parts[3].upper() == 'LISTENING':
                try:
                    return int(parts[4])
                except ValueError:
                    return None
        return None
    if shutil.which('lsof'):
        result = _run(['lsof', '-nP', f'-iTCP@127.0.0.1:{port}', '-sTCP:LISTEN', '-t'], timeout=10)
        if result and result.returncode == 0:
            for line in result.stdout.decode().split():
                if line.isdigit():
                    return int(line)
    return None


def process_alive(pid):
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            kernel32.CloseHandle(handle)
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
    if IS_WINDOWS:
        _run(['taskkill', '/PID', str(int(pid)), '/F'], timeout=20)
        return
    try:
        os.kill(int(pid), signal.SIGTERM)
        if not wait_process_exit(pid, 3):
            os.kill(int(pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
