"""Global hotkey Ctrl+Alt+T on Windows (ctypes, no packages).

RegisterHotKey is called from a dedicated thread that then runs a message
loop; WM_HOTKEY arrives in that thread's queue. At start the registration is
retried for about 10 seconds, because an instance that is being replaced may
still hold the key.
"""

import ctypes
import logging
import os
import threading
import time

log = logging.getLogger('todotracker.hotkey')

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
VK_T = 0x54
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
SW_SHOW = 5
SW_RESTORE = 9
HOTKEY_ID = 0x7454

WINDOW_TITLE = 'TodoTracker'
WINDOW_CLASS = 'Chrome_WidgetWin_1'

if os.name == 'nt':
    from ctypes import wintypes

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    user32.RegisterHotKey.restype = wintypes.BOOL
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UnregisterHotKey.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostThreadMessageW.restype = wintypes.BOOL
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
    user32.keybd_event.restype = None
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def find_app_window():
    """Handle of the Edge app window showing TodoTracker, or None."""
    if os.name != 'nt':
        return None
    found = []

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value != WINDOW_CLASS:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if title == WINDOW_TITLE or title.startswith(WINDOW_TITLE + ' '):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found[0] if found else None


def focus_window(hwnd):
    """Restore (if minimised) and bring the window to the front."""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    else:
        user32.ShowWindow(hwnd, SW_SHOW)
    if not user32.SetForegroundWindow(hwnd):
        # Windows may refuse a foreground switch; a synthetic Alt tap lifts
        # the foreground lock for this process.
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)


class HotkeyThread(threading.Thread):
    def __init__(self, on_press, retry_seconds=10):
        super().__init__(name='hotkey', daemon=True)
        self.on_press = on_press
        self.retry_seconds = retry_seconds
        self.thread_id = None
        self.registered = False
        self.ready = threading.Event()

    def run(self):
        if os.name != 'nt':
            self.ready.set()
            return
        self.thread_id = kernel32.GetCurrentThreadId()
        deadline = time.monotonic() + self.retry_seconds
        try:
            while not user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_T):
                if time.monotonic() > deadline:
                    log.warning('Ctrl+Alt+T is held by another program (error %s); hotkey disabled',
                                ctypes.get_last_error())
                    return
                time.sleep(0.5)
            self.registered = True
            log.info('hotkey Ctrl+Alt+T registered')
        finally:
            self.ready.set()
        msg = wintypes.MSG()
        try:
            while True:
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == 0 or result == -1:
                    break
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    try:
                        self.on_press()
                    except Exception:
                        log.exception('hotkey handler failed')
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)

    def stop(self):
        if os.name == 'nt' and self.thread_id:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
