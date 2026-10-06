"""HTTP server for TodoTracker: JSON API, static files and pasted images.

Binds 127.0.0.1 only. Requests whose Host header is not localhost/127.0.0.1
are refused (DNS rebinding), and every write needs the header `X-Todo: 1`,
which a cross-site form or a simple cross-origin request cannot send.
"""

import json
import logging
import os
import re
import secrets
import socketserver
import threading
import time
import uuid
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import db as dbmod

APP_NAME = 'TodoTracker'
# Bump when the API or the schema changes. The page compares it with the
# version it was written for (CLIENT_API in web/app.js).
API = 4

MAX_JSON = 4 * 1024 * 1024
MAX_IMAGE = 25 * 1024 * 1024
MAX_QUERY = 200

log = logging.getLogger('todotracker.server')

STATIC_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.ico': 'image/x-icon',
}
IMAGE_TYPES = {'png': 'image/png', 'jpg': 'image/jpeg', 'gif': 'image/gif',
               'webp': 'image/webp', 'bmp': 'image/bmp'}
STATIC_NAME_RE = re.compile(r'^/([A-Za-z0-9_-]+\.(?:html|js|css|svg|png|ico))$')
IMAGE_NAME_RE = re.compile(r'^/images/([0-9a-f]{32}\.(?:png|jpg|gif|webp|bmp))$')


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def sniff_image(data):
    """File extension for the image bytes, or None if not a supported image."""
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'jpg'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'webp'
    if data[:2] == b'BM' and len(data) > 26:
        return 'bmp'
    return None


class App:
    """State shared by the request threads of one running instance."""

    def __init__(self, store, app_dir, data_dir, build, port, backup_now=None):
        self.store = store
        self.app_dir = app_dir
        self.data_dir = data_dir
        self.images_dir = os.path.join(data_dir, 'images')
        self.web_dir = os.path.join(app_dir, 'web')
        self.build = build
        self.port = port
        self.backup_now = backup_now
        self.epoch = f'{os.getpid()}-{int(time.time() * 1000)}'
        self.changes = 0
        self.hotkey_presses = 0
        self.stopping = threading.Event()
        self.httpd = None
        self._lock = threading.Lock()
        self._local = threading.local()
        store.on_change = self.bump

    def bump(self):
        """Count a committed write; remember the number for the response."""
        with self._lock:
            self.changes += 1
            n = self.changes
        mine = getattr(self._local, 'changes', None)
        if mine is not None:
            mine.append(n)
        return n

    def begin_request(self):
        self._local.changes = []

    def request_changes(self):
        return getattr(self._local, 'changes', None) or []

    def hotkey_pressed(self):
        with self._lock:
            self.hotkey_presses += 1

    def request_shutdown(self):
        if self.stopping.is_set():
            return
        self.stopping.set()
        log.info('shutdown requested')
        if self.httpd is not None:
            threading.Thread(target=self.httpd.shutdown, name='shutdown', daemon=True).start()


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def check_fields(body, allowed, required=()):
    """Reject unknown keys, missing keys and wrong JSON types."""
    if not isinstance(body, dict):
        raise ApiError(400, 'Expected a JSON object.')
    for key in body:
        if key not in allowed:
            raise ApiError(400, f'Unknown field "{key}".')
    for key in required:
        if key not in body:
            raise ApiError(400, f'Missing field "{key}".')
    for key, value in body.items():
        types = allowed[key]
        if not isinstance(types, tuple):
            types = (types,)
        if isinstance(value, bool) and bool not in types:
            raise ApiError(400, f'Field "{key}" has the wrong type.')
        if not isinstance(value, types):
            raise ApiError(400, f'Field "{key}" has the wrong type.')
    return body


def parse_id_list(text, what='labels'):
    if not text:
        return []
    out = []
    for part in text.split(','):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit() or len(part) > 12:
            raise ApiError(400, f'Invalid {what} list.')
        out.append(int(part))
    return out


SUBTASK_FIELDS = {'title': str, 'done': bool, 'due_at': (str, type(None)), 'labels': list, 'notes': str}

TASK_FIELDS = {
    'title': str, 'description': str, 'status': str, 'priority': str,
    'due_at': (str, type(None)), 'labels': list,
}


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = 'TodoTracker'
    sys_version = ''
    protocol_version = 'HTTP/1.0'

    ROUTES = [
        ('GET', r'/api/ping', 'api_ping'),
        ('GET', r'/api/state', 'api_state'),
        ('GET', r'/api/meta', 'api_meta'),
        ('GET', r'/api/tasks', 'api_list_tasks'),
        ('GET', r'/api/tasks/(\d+)', 'api_get_task'),
        ('GET', r'/api/export', 'api_export'),
        ('GET', r'/api/deps', 'api_list_deps'),
        ('POST', r'/api/deps', 'api_add_dep'),
        ('DELETE', r'/api/deps/(\d+)', 'api_delete_dep'),
        ('POST', r'/api/matrix', 'api_matrix'),
        ('POST', r'/api/tasks', 'api_create_task'),
        ('POST', r'/api/tasks/(\d+)/subtasks', 'api_add_subtasks'),
        ('POST', r'/api/tasks/(\d+)/subtasks/order', 'api_order_subtasks'),
        ('PATCH', r'/api/subtasks/(\d+)', 'api_update_subtask'),
        ('DELETE', r'/api/subtasks/(\d+)', 'api_delete_subtask'),
        ('PATCH', r'/api/tasks/(\d+)', 'api_update_task'),
        ('DELETE', r'/api/tasks/(\d+)', 'api_delete_task'),
        ('PATCH', r'/api/labels/(\d+)', 'api_update_label'),
        ('DELETE', r'/api/labels/(\d+)', 'api_delete_label'),
        ('POST', r'/api/images', 'api_upload_image'),
        ('POST', r'/api/backup', 'api_backup'),
        ('POST', r'/api/shutdown', 'api_shutdown'),
    ]
    COMPILED = [(m, re.compile('^' + p + '$'), h) for m, p, h in ROUTES]

    # -- plumbing ----------------------------------------------------------

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):  # keep the 1.5 s polling out of the log
        pass

    def log_error(self, fmt, *args):
        log.warning('http: ' + fmt, *args)

    def do_GET(self):
        self._dispatch('GET')

    def do_POST(self):
        self._dispatch('POST')

    def do_PATCH(self):
        self._dispatch('PATCH')

    def do_DELETE(self):
        self._dispatch('DELETE')

    def do_PUT(self):
        self._dispatch('PUT')

    def do_OPTIONS(self):
        self._dispatch('OPTIONS')

    def _host_ok(self):
        host = (self.headers.get('Host') or '').strip().lower()
        if host.startswith('['):
            return False
        name = host.rsplit(':', 1)[0] if ':' in host else host
        return name in ('127.0.0.1', 'localhost')

    def _dispatch(self, method):
        self.app.begin_request()
        try:
            if not self._host_ok():
                raise ApiError(403, 'Forbidden host.')
            parts = urlsplit(self.path)
            path = parts.path
            if path.startswith('/api/'):
                if method not in ('GET', 'HEAD') and self.headers.get('X-Todo') != '1':
                    raise ApiError(403, 'Missing X-Todo header.')
                self._route(method, path, parts.query)
            elif method == 'GET':
                self._static(path)
            else:
                raise ApiError(405, 'Method not allowed.')
        except ApiError as e:
            self._send_json(e.status, {'error': e.message})
        except dbmod.ValidationError as e:
            self._send_json(400, {'error': str(e)})
        except dbmod.NotFound as e:
            self._send_json(404, {'error': str(e)})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            log.exception('error handling %s %s', method, self.path)
            try:
                self._send_json(500, {'error': 'Internal error (see todo.log).'})
            except Exception:
                pass

    def _route(self, method, path, query):
        allowed = False
        for m, rx, handler in self.COMPILED:
            match = rx.match(path)
            if not match:
                continue
            if m != method:
                allowed = True
                continue
            args = [int(g) if g.isdigit() else g for g in match.groups()]
            getattr(self, handler)(*args, query=query)
            return
        if allowed:
            raise ApiError(405, 'Method not allowed.')
        raise ApiError(404, 'Not found.')

    def _read_body(self, limit):
        length = self.headers.get('Content-Length')
        if length is None:
            if self.headers.get('Transfer-Encoding'):
                raise ApiError(411, 'Content-Length required.')
            return b''
        try:
            n = int(length)
        except ValueError:
            raise ApiError(400, 'Bad Content-Length.')
        if n < 0:
            raise ApiError(400, 'Bad Content-Length.')
        if n > limit:
            # Drain (bounded) so the browser sees the answer, not a reset.
            remaining = min(n, 64 * 1024 * 1024)
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                remaining -= len(chunk)
            raise ApiError(413, f'Too large (limit {limit // (1024 * 1024)} MB).')
        data = self.rfile.read(n)
        if len(data) != n:
            raise ApiError(400, 'Incomplete request body.')
        return data

    def _json_body(self, limit=MAX_JSON):
        data = self._read_body(limit)
        if not data:
            return {}
        try:
            body = json.loads(data.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, 'Request body is not valid JSON.')
        if not isinstance(body, dict):
            raise ApiError(400, 'Expected a JSON object.')
        return body

    def _send(self, status, body, content_type, headers=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        changes = self.app.request_changes()
        if changes:
            self.send_header('X-Todo-Changes', ','.join(map(str, changes)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _send_json(self, status, obj, headers=None):
        body = json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        h = {'Cache-Control': 'no-store'}
        h.update(headers or {})
        self._send(status, body, 'application/json; charset=utf-8', h)

    # -- static files ------------------------------------------------------

    def _static(self, path):
        if path in ('/', '/index.html'):
            return self._index()
        m = IMAGE_NAME_RE.match(path)
        if m:
            fp = os.path.join(self.app.images_dir, m.group(1))
            if not os.path.isfile(fp):
                raise ApiError(404, 'Image not found.')
            with open(fp, 'rb') as f:
                data = f.read()
            ext = m.group(1).rsplit('.', 1)[1]
            return self._send(200, data, IMAGE_TYPES[ext],
                              {'Cache-Control': 'private, max-age=31536000, immutable'})
        m = STATIC_NAME_RE.match(path)
        if m:
            fp = os.path.join(self.app.web_dir, m.group(1))
            if os.path.isfile(fp):
                with open(fp, 'rb') as f:
                    data = f.read()
                ext = os.path.splitext(fp)[1]
                return self._send(200, data, STATIC_TYPES[ext], {'Cache-Control': 'no-cache'})
        if path == '/favicon.ico':
            return self._send(204, b'', 'image/x-icon')
        raise ApiError(404, 'Not found.')

    def _index(self):
        with open(os.path.join(self.app.web_dir, 'index.html'), 'r', encoding='utf-8') as f:
            html = f.read()
        nonce = secrets.token_urlsafe(16)
        html = (html.replace('__BUILD__', self.app.build)
                    .replace('__API__', str(API))
                    .replace('__NONCE__', nonce))
        csp = ("default-src 'self'; "
               f"script-src 'self' 'nonce-{nonce}'; "
               "style-src 'self' 'unsafe-inline'; "
               "img-src 'self' data: blob: http: https:; "
               "connect-src 'self'; object-src 'none'; base-uri 'none'; "
               "form-action 'none'; frame-ancestors 'none'")
        self._send(200, html.encode('utf-8'), 'text/html; charset=utf-8',
                   {'Cache-Control': 'no-store', 'Content-Security-Policy': csp})

    # -- API: instance -----------------------------------------------------

    def api_ping(self, query):
        self._send_json(200, {
            'app': APP_NAME, 'api': API, 'build': self.app.build, 'pid': os.getpid(),
            'app_dir': self.app.app_dir, 'data_dir': self.app.data_dir,
        })

    def api_state(self, query):
        self._send_json(200, {
            'api': API, 'build': self.app.build, 'epoch': self.app.epoch,
            'changes': self.app.changes, 'hotkey': self.app.hotkey_presses,
        })

    def api_shutdown(self, query):
        self._json_body()
        self._send_json(200, {'ok': True})
        self.wfile.flush()
        self.app.request_shutdown()

    def api_backup(self, query):
        self._json_body()
        if not self.app.backup_now:
            raise ApiError(503, 'Backups are not available.')
        path = self.app.backup_now()
        self._send_json(200, {'ok': True, 'file': os.path.basename(path)})

    # -- API: tasks --------------------------------------------------------

    def api_meta(self, query):
        self._send_json(200, self.app.store.meta())

    def api_list_tasks(self, query):
        qs = parse_qs(query, keep_blank_values=True)

        def one(name, default):
            values = qs.get(name)
            return values[-1] if values else default

        q = one('q', '')
        if len(q) > MAX_QUERY:
            raise ApiError(400, 'Search text is too long.')
        tasks = self.app.store.list_tasks(
            view=one('view', 'open'), label_ids=parse_id_list(one('labels', '')),
            match=one('match', 'any'), q=q, sort=one('sort', 'newest'))
        self._send_json(200, {'tasks': tasks})

    def api_get_task(self, task_id, query):
        self._send_json(200, self.app.store.get_task(task_id))

    def api_create_task(self, query):
        body = check_fields(self._json_body(), TASK_FIELDS, required=('title',))
        task = self.app.store.create_task(**body)
        self._send_json(201, task)

    def api_update_task(self, task_id, query):
        body = check_fields(self._json_body(), TASK_FIELDS)
        self._send_json(200, self.app.store.update_task(task_id, body))

    def api_delete_task(self, task_id, query):
        self._json_body()
        self.app.store.delete_task(task_id)
        self._send_json(200, {'ok': True})

    # -- API: subtasks (every write answers with the whole parent task) ----

    def api_add_subtasks(self, task_id, query):
        body = check_fields(self._json_body(), {'title': str, 'items': list})
        if ('title' in body) == ('items' in body):
            raise ApiError(400, 'Send either "title" or "items".')
        items = [body['title']] if 'title' in body else body['items']
        task, ids = self.app.store.add_subtasks(task_id, items)
        task['created_ids'] = ids
        self._send_json(201, task)

    def api_order_subtasks(self, task_id, query):
        body = check_fields(self._json_body(), {'ids': list}, required=('ids',))
        self._send_json(200, self.app.store.reorder_subtasks(task_id, body['ids']))

    def api_update_subtask(self, sub_id, query):
        body = check_fields(self._json_body(), SUBTASK_FIELDS)
        if not body:
            raise ApiError(400, 'Nothing to change.')
        self._send_json(200, self.app.store.update_subtask(sub_id, body))

    def api_delete_subtask(self, sub_id, query):
        self._json_body()
        self._send_json(200, self.app.store.delete_subtask(sub_id))

    # -- API: matrix and dependencies ------------------------------------

    def api_matrix(self, query):
        body = check_fields(self._json_body(), {'items': list}, required=('items',))
        self._send_json(200, {'tasks': self.app.store.set_matrix(body['items'])})

    def api_list_deps(self, query):
        self._send_json(200, {'deps': self.app.store.list_deps()})

    def api_add_dep(self, query):
        body = check_fields(self._json_body(), {'before': str, 'after': str}, required=('before', 'after'))
        dep_id, created, deps = self.app.store.add_dep(body['before'], body['after'])
        self._send_json(201 if created else 200, {'id': dep_id, 'created': created, 'deps': deps})

    def api_delete_dep(self, dep_id, query):
        self._json_body()
        self._send_json(200, {'deps': self.app.store.delete_dep(dep_id)})

    # -- API: labels -------------------------------------------------------

    def api_update_label(self, label_id, query):
        body = check_fields(self._json_body(), {'name': str, 'color': str})
        if not body:
            raise ApiError(400, 'Nothing to change.')
        self._send_json(200, self.app.store.update_label(label_id, **body))

    def api_delete_label(self, label_id, query):
        self._json_body()
        self.app.store.delete_label(label_id)
        self._send_json(200, {'ok': True})

    # -- API: images and export -------------------------------------------

    def api_upload_image(self, query):
        data = self._read_body(MAX_IMAGE)
        if not data:
            raise ApiError(400, 'No image data.')
        ext = sniff_image(data)
        if not ext:
            raise ApiError(400, 'Unsupported image type (use PNG, JPEG, GIF, WebP or BMP).')
        os.makedirs(self.app.images_dir, exist_ok=True)
        name = f'{uuid.uuid4().hex}.{ext}'
        tmp = os.path.join(self.app.images_dir, name + '.part')
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, os.path.join(self.app.images_dir, name))
        self._send_json(201, {'url': f'/images/{name}', 'size': len(data)})

    def api_export(self, query):
        data = self.app.store.export()
        data['api'] = API
        name = f'todotracker-export-{date.today().isoformat()}.json'
        body = json.dumps(data, ensure_ascii=False, indent=1).encode('utf-8')
        self._send(200, body, 'application/json; charset=utf-8', {
            'Cache-Control': 'no-store',
            'Content-Disposition': f'attachment; filename="{name}"',
        })


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second process bind the same port while
    # the first still listens (both would run, doubling reminders). Elsewhere
    # it only allows rebinding over TIME_WAIT connections, which we want.
    allow_reuse_address = os.name != 'nt'
    request_queue_size = 64

    def __init__(self, port, app=None):
        self.app = app
        super().__init__(('127.0.0.1', port), Handler)

    def server_bind(self):
        # HTTPServer.server_bind calls socket.getfqdn(), which can stall for
        # seconds on some Windows machines; the name is not needed.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port
