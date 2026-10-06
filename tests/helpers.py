"""Test helpers: run TodoTracker on scratch data and a test port.

Never test against the real app or data: every server started here gets
TODOTRACKER_DATA=<scratch folder> and an explicit port that is not 8765.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_PORT = 8765
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def free_port():
    while True:
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        if port != REAL_PORT:
            return port


def scratch_dir(prefix='tt-test-'):
    return tempfile.mkdtemp(prefix=prefix)


def request(port, method, path, body=None, headers=None, raw=None, timeout=10):
    """(status, parsed JSON or bytes, headers) for a request to the test server."""
    assert port != REAL_PORT, 'tests must never talk to the real app'
    h = {'Host': f'127.0.0.1:{port}'}
    if method != 'GET':
        h['X-Todo'] = '1'
    h.update(headers or {})
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        h.setdefault('Content-Type', 'application/json')
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=data, method=method)
    for k, v in h.items():
        if v is None:
            continue
        req.add_header(k, v)
    try:
        with _opener.open(req, timeout=timeout) as r:
            payload = r.read()
            status, hdrs = r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        payload, status, hdrs = e.read(), e.code, dict(e.headers)
    ctype = hdrs.get('Content-Type', '')
    if 'json' in ctype and payload:
        return status, json.loads(payload.decode('utf-8')), hdrs
    return status, payload, hdrs


class AppServer:
    """A TodoTracker process on scratch data (always --background, never 8765)."""

    def __init__(self, port=None, data_dir=None, app_dir=APP_DIR, env=None, wait=True, background=True):
        self.port = port or free_port()
        assert self.port != REAL_PORT
        self.data = data_dir or scratch_dir('tt-data-')
        self.app_dir = app_dir
        self.own_data = data_dir is None
        e = dict(os.environ, TODOTRACKER_DATA=self.data, TODOTRACKER_NO_WINDOW='1', TODOTRACKER_NO_NOTIFY='1',
                 PYTHONDONTWRITEBYTECODE='1')
        e.pop('TODOTRACKER_NO_FTS', None)
        e.update(env or {})
        args = [sys.executable, os.path.join(app_dir, 'todo.pyw'), '--port', str(self.port)]
        if background:
            args.append('--background')
        self.proc = subprocess.Popen(args, env=e, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if wait:
            self.wait_ready()

    url_override = None      # tests in local mode open the page from elsewhere

    @property
    def url(self):
        return self.url_override or f'http://127.0.0.1:{self.port}/'

    def wait_ready(self, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError('server exited:\n' + self.log())
            try:
                status, body, _ = request(self.port, 'GET', '/api/ping', timeout=1)
                if status == 200 and body.get('pid') == self.proc.pid:
                    return body
            except OSError:
                pass
            time.sleep(0.05)
        raise RuntimeError('server did not start:\n' + self.log())

    def api(self, method, path, body=None, expect=None):
        status, data, _ = request(self.port, method, path, body)
        if expect is not None:
            assert status == expect, f'{method} {path} -> {status}: {data}'
        return data

    def log(self):
        try:
            with open(os.path.join(self.data, 'todo.log'), encoding='utf-8') as f:
                return f.read()
        except OSError:
            return ''

    def stop(self, timeout=10):
        if self.proc.poll() is None:
            try:
                request(self.port, 'POST', '/api/shutdown', {}, timeout=3)
            except OSError:
                pass
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        if self.proc.stderr:
            self.proc.stderr.close()

    def cleanup(self):
        self.stop()
        if self.own_data:
            shutil.rmtree(self.data, ignore_errors=True)


def copy_app(dest=None, tools=False):
    """A copy of the app folder (code only) for update/replacement tests;
    tools=True adds the installers and tools/."""
    dest = dest or scratch_dir('tt-app-')
    os.makedirs(dest, exist_ok=True)
    for name in os.listdir(APP_DIR):
        if name.endswith(('.py', '.pyw')) or (tools and name.endswith('.sh')):
            shutil.copy2(os.path.join(APP_DIR, name), dest)
    shutil.copytree(os.path.join(APP_DIR, 'web'), os.path.join(dest, 'web'), dirs_exist_ok=True)
    if tools:
        shutil.copytree(os.path.join(APP_DIR, 'tools'), os.path.join(dest, 'tools'), dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__'))
    return dest


class LocalPageServer:
    """Serves web/ the way the Android app does (no API: data stays in the
    page). Same rules as android/src/.../AssetServer.java: index.html with
    mode "local", the API version, a build id and a fresh nonce for the CSP;
    other files as they are; /images/ and /api/ do not exist."""

    def __init__(self, app_dir=APP_DIR):
        import http.server
        import secrets
        import threading
        sys.path.insert(0, app_dir)
        import server as server_mod
        web = os.path.join(app_dir, 'web')
        api = server_mod.API
        types = server_mod.STATIC_TYPES

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = self.path.split('?', 1)[0]
                headers = {}
                if path in ('/', '/index.html'):
                    nonce = secrets.token_urlsafe(16)
                    with open(os.path.join(web, 'index.html'), encoding='utf-8') as f:
                        body = (f.read().replace('__BUILD__', 'local-test').replace('__API__', str(api))
                                .replace('__MODE__', 'local').replace('__NONCE__', nonce)).encode('utf-8')
                    ctype = 'text/html; charset=utf-8'
                    headers['Content-Security-Policy'] = (
                        "default-src 'self'; " f"script-src 'self' 'nonce-{nonce}'; " "style-src 'self' 'unsafe-inline'; "
                        "img-src 'self' data: blob: http: https:; " "connect-src 'self'; object-src 'none'; base-uri 'none'; "
                        "form-action 'none'; frame-ancestors 'none'")
                else:
                    name = path.lstrip('/')
                    fp = os.path.join(web, name)
                    ext = os.path.splitext(name)[1]
                    if '/' in name or '\\' in name or ext not in types or not os.path.isfile(fp):
                        self.send_response(404)
                        self.send_header('Content-Length', '0')
                        self.end_headers()
                        return
                    with open(fp, 'rb') as f:
                        body = f.read()
                    ctype = types[ext]
                self.send_response(200)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

        self.port = free_port()
        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', self.port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.port}/'

    def cleanup(self):
        self.httpd.shutdown()
        self.httpd.server_close()
