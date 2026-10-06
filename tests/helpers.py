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

    @property
    def url(self):
        return f'http://127.0.0.1:{self.port}/'

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
