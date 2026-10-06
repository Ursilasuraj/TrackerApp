"""Minimal Chrome DevTools Protocol client (Python standard library only).

Drives headless Edge (Windows) or Chromium with real input events:
Input.dispatchMouseEvent with ~100 ms presses and Input.insertText /
Input.dispatchKeyEvent for the keyboard. Set TT_BROWSER to choose the binary.
"""

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class CDPError(Exception):
    pass


def find_browser():
    env = os.environ.get('TT_BROWSER')
    if env:
        return env
    candidates = []
    if os.name == 'nt':
        for var in ('ProgramFiles(x86)', 'ProgramFiles', 'LOCALAPPDATA'):
            base = os.environ.get(var)
            if base:
                candidates.append(os.path.join(base, 'Microsoft', 'Edge', 'Application', 'msedge.exe'))
    candidates += ['/opt/pw-browsers/chromium', shutil.which('microsoft-edge') or '',
                   shutil.which('chromium') or '', shutil.which('chromium-browser') or '',
                   shutil.which('google-chrome') or '']
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    raise CDPError('No Edge/Chromium found; set TT_BROWSER')


class WebSocket:
    def __init__(self, url, timeout=10):
        if not url.startswith('ws://'):
            raise CDPError('bad websocket url ' + url)
        hostport, _, path = url[5:].partition('/')
        host, _, port = hostport.partition(':')
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f'GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n'
                           f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n'
                           'Sec-WebSocket-Version: 13\r\n\r\n').encode())
        buf = b''
        while b'\r\n\r\n' not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError('websocket handshake failed')
            buf += chunk
        head, _, self.buf = buf.partition(b'\r\n\r\n')
        if b' 101 ' not in head.split(b'\r\n')[0]:
            raise CDPError('websocket handshake: ' + head.decode('latin-1'))
        self.sock.settimeout(None)
        self.lock = threading.Lock()

    @staticmethod
    def _mask(data, mask):
        n = len(data)
        if not n:
            return b''
        m = (mask * (n // 4 + 1))[:n]
        return (int.from_bytes(data, 'big') ^ int.from_bytes(m, 'big')).to_bytes(n, 'big')

    def _frame(self, opcode, data):
        header = bytearray([0x80 | opcode])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack('>H', n)
        else:
            header.append(0x80 | 127)
            header += struct.pack('>Q', n)
        mask = os.urandom(4)
        with self.lock:
            self.sock.sendall(bytes(header) + mask + self._mask(data, mask))

    def send(self, text):
        self._frame(0x1, text.encode('utf-8'))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise EOFError
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        message = b''
        while True:
            b0, b1 = self._read(2)
            opcode = b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack('>H', self._read(2))[0]
            elif n == 127:
                n = struct.unpack('>Q', self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None
            payload = self._read(n)
            if mask:
                payload = self._mask(payload, mask)
            if opcode == 0x8:
                raise EOFError
            if opcode == 0x9:
                self._frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            message += payload
            if b0 & 0x80:
                return message.decode('utf-8')

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


KEYS = {
    'Enter': (13, 'Enter', '\r'), 'Escape': (27, 'Escape', None), 'Tab': (9, 'Tab', None),
    'Backspace': (8, 'Backspace', None), 'Delete': (46, 'Delete', None),
    'ArrowUp': (38, 'ArrowUp', None), 'ArrowDown': (40, 'ArrowDown', None),
    'ArrowLeft': (37, 'ArrowLeft', None), 'ArrowRight': (39, 'ArrowRight', None),
    'Home': (36, 'Home', None), 'End': (35, 'End', None), ' ': (32, 'Space', ' '),
    'PageDown': (34, 'PageDown', None), 'PageUp': (33, 'PageUp', None),
}
MOD = {'alt': 1, 'ctrl': 2, 'meta': 4, 'shift': 8}


class Page:
    def __init__(self, ws_url, browser=None):
        self.ws = WebSocket(ws_url)
        self.browser = browser
        self.next_id = 0
        self.results = {}
        self.events = []
        self.closed = False
        self.cond = threading.Condition()
        self.x = self.y = 0
        threading.Thread(target=self._reader, daemon=True).start()
        self.send('Page.enable')
        self.send('Runtime.enable')
        self.send('Log.enable')

    def _reader(self):
        try:
            while True:
                msg = json.loads(self.ws.recv())
                with self.cond:
                    if 'id' in msg:
                        self.results[msg['id']] = msg
                    else:
                        self.events.append(msg)
                    self.cond.notify_all()
        except Exception:
            with self.cond:
                self.closed = True
                self.cond.notify_all()

    def send(self, method, params=None, timeout=20):
        with self.cond:
            self.next_id += 1
            mid = self.next_id
        self.ws.send(json.dumps({'id': mid, 'method': method, 'params': params or {}}))
        deadline = time.time() + timeout
        with self.cond:
            while mid not in self.results:
                if self.closed:
                    raise CDPError(f'connection closed during {method}')
                left = deadline - time.time()
                if left <= 0:
                    raise CDPError(f'timeout waiting for {method}')
                self.cond.wait(left)
            msg = self.results.pop(mid)
        if 'error' in msg:
            raise CDPError(f'{method}: {msg["error"]}')
        return msg.get('result', {})

    def send_nowait(self, method, params=None):
        """Send without waiting (e.g. a click that opens a modal dialog)."""
        with self.cond:
            self.next_id += 1
            mid = self.next_id
        self.ws.send(json.dumps({'id': mid, 'method': method, 'params': params or {}}))
        return mid

    # -- events --------------------------------------------------------------

    def take_events(self, method=None):
        with self.cond:
            if method is None:
                out, self.events = self.events, []
                return out
            out = [e for e in self.events if e.get('method') == method]
            self.events = [e for e in self.events if e.get('method') != method]
            return out

    def console_errors(self):
        errors = []
        for e in self.take_events():
            m, p = e.get('method'), e.get('params', {})
            if m == 'Runtime.exceptionThrown':
                errors.append('exception: ' + json.dumps(p.get('exceptionDetails', {}))[:800])
            elif m == 'Runtime.consoleAPICalled' and p.get('type') in ('error', 'assert'):
                errors.append('console: ' + json.dumps(p.get('args', []))[:800])
            elif m == 'Log.entryAdded' and p.get('entry', {}).get('level') == 'error':
                errors.append('log: ' + p['entry'].get('text', '')[:800] + ' ' + p['entry'].get('url', ''))
        return errors

    # -- evaluation ----------------------------------------------------------

    def eval(self, expr, await_promise=True):
        r = self.send('Runtime.evaluate', {'expression': expr, 'returnByValue': True,
                                           'awaitPromise': await_promise, 'userGesture': False})
        if r.get('exceptionDetails'):
            d = r['exceptionDetails']
            text = (d.get('exception') or {}).get('description') or d.get('text')
            raise CDPError(f'JS error: {text}\n  in: {expr[:300]}')
        return r.get('result', {}).get('value')

    def wait_for(self, expr, timeout=5.0, interval=0.05, message=None):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                last = self.eval(expr)
            except CDPError as e:
                last = str(e)
            else:
                if last:
                    return last
            time.sleep(interval)
        raise AssertionError(message or f'timed out waiting for: {expr} (last: {last!r})')

    def navigate(self, url, wait=True):
        self.take_events()
        self.send('Page.navigate', {'url': url})
        if wait:
            self.wait_for('document.readyState === "complete"', timeout=10)

    def reload(self):
        self.send('Page.reload', {'ignoreCache': True})

    # -- geometry ------------------------------------------------------------

    def rect(self, selector, scroll=True):
        js = ('(() => { const el = document.querySelector(%s); if (!el) return null;'
              '%s const r = el.getBoundingClientRect();'
              ' return {x: r.left, y: r.top, w: r.width, h: r.height}; })()'
              % (json.dumps(selector), ' el.scrollIntoView({block: "nearest", inline: "nearest"});' if scroll else ''))
        r = self.eval(js)
        if not r:
            raise AssertionError(f'element not found: {selector}')
        return r

    def center(self, selector, scroll=True):
        r = self.rect(selector, scroll)
        return r['x'] + r['w'] / 2, r['y'] + r['h'] / 2

    # -- mouse ---------------------------------------------------------------

    def mouse(self, kind, x, y, button='left', buttons=0, clicks=1, modifiers=0):
        self.send('Input.dispatchMouseEvent', {
            'type': kind, 'x': x, 'y': y, 'button': button, 'buttons': buttons,
            'clickCount': clicks, 'modifiers': modifiers})
        self.x, self.y = x, y

    def move(self, x, y, steps=1, buttons=0):
        x0, y0 = self.x, self.y
        for i in range(1, steps + 1):
            self.mouse('mouseMoved', x0 + (x - x0) * i / steps, y0 + (y - y0) * i / steps,
                       button='left' if buttons else 'none', buttons=buttons)

    def click_at(self, x, y, hold=0.1, clicks=1, modifiers=0):
        self.move(x, y)
        self.mouse('mousePressed', x, y, buttons=1, clicks=clicks, modifiers=modifiers)
        time.sleep(hold)
        self.mouse('mouseReleased', x, y, buttons=0, clicks=clicks, modifiers=modifiers)

    def click(self, selector, hold=0.1, modifiers=0):
        x, y = self.center(selector)
        self.click_at(x, y, hold=hold, modifiers=modifiers)

    def click_dialog(self, selector, accept=True, timeout=5):
        """Click something that opens alert/confirm; answer it. Returns the dialog."""
        x, y = self.center(selector)
        self.move(x, y)
        self.mouse('mousePressed', x, y, buttons=1)
        time.sleep(0.1)
        self.send_nowait('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': x, 'y': y,
                                                      'button': 'left', 'buttons': 0, 'clickCount': 1})
        deadline = time.time() + timeout
        while time.time() < deadline:
            dialogs = self.take_events('Page.javascriptDialogOpening')
            if dialogs:
                self.send('Page.handleJavaScriptDialog', {'accept': accept})
                return dialogs[0]['params']
            time.sleep(0.05)
        raise AssertionError('no dialog opened')

    def dblclick(self, selector):
        x, y = self.center(selector)
        self.click_at(x, y, hold=0.05, clicks=1)
        time.sleep(0.05)
        self.click_at(x, y, hold=0.05, clicks=2)

    def drag(self, x0, y0, x1, y1, steps=12, hold=0.1, pause=0.02):
        self.move(x0, y0)
        self.mouse('mousePressed', x0, y0, buttons=1)
        time.sleep(hold)
        for i in range(1, steps + 1):
            self.mouse('mouseMoved', x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps,
                       buttons=1)
            time.sleep(pause)
        time.sleep(hold)
        self.mouse('mouseReleased', x1, y1, buttons=0)

    # -- keyboard ------------------------------------------------------------

    def key(self, key, *mods, repeat=False):
        modifiers = sum(MOD[m] for m in mods)
        if key in KEYS:
            vk, code, text = KEYS[key]
        elif len(key) == 1:
            vk = ord(key.upper())
            code = 'Key' + key.upper() if key.isalpha() else ('Digit' + key if key.isdigit() else '')
            text = key
        else:
            raise ValueError(key)
        if modifiers & (MOD['ctrl'] | MOD['alt'] | MOD['meta']):
            text = None
        down = {'type': 'keyDown' if text else 'rawKeyDown', 'key': key, 'code': code,
                'windowsVirtualKeyCode': vk, 'nativeVirtualKeyCode': vk, 'modifiers': modifiers,
                'autoRepeat': repeat}
        if text:
            down['text'] = text
            down['unmodifiedText'] = text
        self.send('Input.dispatchKeyEvent', down)
        self.send('Input.dispatchKeyEvent', {'type': 'keyUp', 'key': key, 'code': code,
                                             'windowsVirtualKeyCode': vk, 'nativeVirtualKeyCode': vk,
                                             'modifiers': modifiers})

    def type(self, text):
        self.send('Input.insertText', {'text': text})

    def paste(self, text, origin):
        """Put text on the clipboard and press Ctrl+V (a real paste event)."""
        self.send('Browser.grantPermissions', {'permissions': ['clipboardReadWrite', 'clipboardSanitizedWrite'],
                                               'origin': origin.rstrip('/')})
        self.eval('navigator.clipboard.writeText(%s)' % json.dumps(text))
        self.send('Input.dispatchKeyEvent', {'type': 'rawKeyDown', 'key': 'v', 'code': 'KeyV',
                                             'windowsVirtualKeyCode': 86, 'modifiers': MOD['ctrl'],
                                             'commands': ['paste']})
        self.send('Input.dispatchKeyEvent', {'type': 'keyUp', 'key': 'v', 'code': 'KeyV',
                                             'windowsVirtualKeyCode': 86, 'modifiers': MOD['ctrl']})

    def active(self):
        return self.eval('(() => { const a = document.activeElement; if (!a) return null;'
                         ' return a === document.body ? "BODY" : (a.dataset && a.dataset.k) || a.id || a.tagName; })()')

    def screenshot(self, path):
        data = self.send('Page.captureScreenshot', {'format': 'png'})['data']
        with open(path, 'wb') as f:
            f.write(base64.b64decode(data))

    def close(self):
        self.ws.close()


class Browser:
    def __init__(self, width=1200, height=820, app_url=None):
        self.exe = find_browser()
        self.profile = tempfile.mkdtemp(prefix='tt-browser-')
        args = [self.exe, '--headless=new', '--remote-debugging-port=0',
                f'--user-data-dir={self.profile}', '--no-first-run', '--no-default-browser-check',
                f'--window-size={width},{height}', '--disable-extensions', '--disable-sync',
                '--disable-background-networking', '--disable-component-update', '--mute-audio',
                '--disable-features=Translate,MediaRouter,OptimizationHints', '--no-pings',
                '--disable-domain-reliability', '--disable-client-side-phishing-detection',
                '--disable-default-apps', '--no-proxy-server', '--lang=en-US', '--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1',
                f'--app={app_url}' if app_url else 'about:blank']
        if os.name != 'nt' and hasattr(os, 'geteuid') and os.geteuid() == 0:
            args.insert(1, '--no-sandbox')
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        port_file = os.path.join(self.profile, 'DevToolsActivePort')
        deadline = time.time() + 20
        while not os.path.exists(port_file) or os.path.getsize(port_file) == 0:
            if time.time() > deadline or self.proc.poll() is not None:
                self.close()
                raise CDPError('browser did not start')
            time.sleep(0.05)
        time.sleep(0.05)
        with open(port_file) as f:
            self.port = int(f.readline().strip())
        self.base = f'http://127.0.0.1:{self.port}'

    def _json(self, path, method='GET'):
        req = urllib.request.Request(self.base + path, method=method)
        with _opener.open(req, timeout=10) as r:
            return json.loads(r.read().decode())

    def new_page(self, url='about:blank'):
        info = self._json('/json/new?' + urllib.request.quote(url, safe=':/?=&'), method='PUT')
        page = Page(info['webSocketDebuggerUrl'], self)
        page.target_id = info['id']
        return page

    def close_page(self, page):
        page.close()
        try:
            self._json('/json/close/' + page.target_id)
        except Exception:
            pass

    def targets(self):
        return self._json('/json/list')

    def first_page(self):
        """The window the browser opened itself (used with app_url)."""
        deadline = time.time() + 10
        while time.time() < deadline:
            pages = [t for t in self.targets() if t['type'] == 'page']
            if pages:
                page = Page(pages[0]['webSocketDebuggerUrl'], self)
                page.target_id = pages[0]['id']
                return page
            time.sleep(0.05)
        raise CDPError('no page target')

    def close(self):
        try:
            self.proc.terminate()
            self.proc.wait(10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        shutil.rmtree(self.profile, ignore_errors=True)
