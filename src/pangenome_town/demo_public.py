"""Network-facing live demo with isolated visitors and bounded compute.

Only the two demonstration workflows are exposed. The operator cockpit, town
mail, files, shell commands and production authority endpoints are not mounted.
"""
from __future__ import annotations

import argparse
import json
import secrets
import threading
import time
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import config
from .demo_cluster import ClusterStage
from .demo_stage import DemoStage, StageError

ASSETS = Path(__file__).parent


class Visitors:
    def __init__(self, towns, storage, *, slots=2, max_sessions=64, max_pending=8,
                 stage_factory=DemoStage, cluster_factory=ClusterStage):
        self.towns = towns
        self.storage = Path(storage)
        self.semaphore = threading.BoundedSemaphore(slots)
        self.max_sessions = max_sessions
        self.max_pending = max_pending
        self.factories = {'demo': stage_factory, 'demo-cluster': cluster_factory}
        self.lock = threading.RLock()
        self.sessions = {}

    def active(self, session):
        return any(stage.worker and stage.worker.is_alive() for stage in session['stages'].values())

    def session(self, sid=None, *, create=False):
        with self.lock:
            now = time.monotonic()
            for key, value in list(self.sessions.items()):
                if now - value['seen'] > 7200 and not self.active(value):
                    del self.sessions[key]
            if sid in self.sessions:
                result = self.sessions[sid]
            elif create:
                if len(self.sessions) >= self.max_sessions:
                    raise StageError('The demo is full. Please try again later.')
                sid = secrets.token_urlsafe(32)
                result = {'id': sid, 'token': secrets.token_urlsafe(32), 'stages': {}, 'seen': now}
                self.sessions[sid] = result
            else:
                raise StageError('Your session expired. Reload the demo to begin a new session.')
            result['seen'] = now
            return result

    def stage(self, session, case):
        with self.lock:
            if case not in self.factories:
                raise StageError('Unknown use case.')
            if case not in session['stages']:
                stage = self.factories[case](self.towns, storage=self.storage / session['id'] / case)
                execute = stage._execute

                def bounded_execute():
                    acquired = self.semaphore.acquire(blocking=False)
                    if not acquired:
                        stage._event('host', 'human', 'Waiting for a compute slot',
                                     'Other visitors are running analyses. Your approved request is queued; narration waits for its result.',
                                     kind='compute')
                        self.semaphore.acquire()
                    try:
                        execute()
                    finally:
                        self.semaphore.release()
                stage._execute = bounded_execute
                session['stages'][case] = stage
            return session['stages'][case]

    def act(self, session, case, action, payload):
        with self.lock:
            stage = self.stage(session, case)
            if action == 'approve':
                pending = sum(bool(s.worker and s.worker.is_alive())
                              for v in self.sessions.values() for s in v['stages'].values())
                if pending >= self.max_pending:
                    raise StageError('The compute queue is full. Please retry approval shortly.')
            return stage.act(action, payload)


def handler(visitors):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, value, content_type='application/json', cookie=None):
            raw = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'same-origin')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            if cookie:
                self.send_header('Set-Cookie', f'wasteland_visitor={cookie}; HttpOnly; SameSite=Strict; Path=/; Max-Age=7200')
            self.end_headers()
            self.wfile.write(raw)

        def visitor(self, create=False):
            cookies = SimpleCookie()
            cookies.load(self.headers.get('Cookie', ''))
            item = cookies.get('wasteland_visitor')
            return visitors.session(item.value if item else None, create=create)

        def do_GET(self):
            path = urlsplit(self.path).path
            try:
                if path in ('/', '/demo', '/demo/visitor'):
                    session = self.visitor(create=True)
                    filename = 'demo_cluster.html' if path == '/demo/visitor' else 'demo_stage.html'
                    page = (ASSETS / filename).read_text().replace('__COCKPIT_TOKEN__', session['token'])
                    page = page.replace('Open cockpit', 'Your live demo')
                    page = page.replace('<main>', '<main><p style="padding:10px 16px;background:#20382e;color:#d5f0df;border-radius:8px">Live visitor session · New analyses run on this laptop or DDBJ. Other visitors have separate runs. Approvals use isolated demo authorities. Use synthetic or public data only.</p>', 1)
                    self.send(200, page.encode(), 'text/html; charset=utf-8', cookie=session['id'])
                elif path in ('/demo-assets/inspection.js', '/demo-assets/autorun.js', '/demo-assets/nacl-fast.min.js'):
                    asset = {'/demo-assets/inspection.js': 'demo_inspection.js',
                             '/demo-assets/autorun.js': 'demo_autorun.js',
                             '/demo-assets/nacl-fast.min.js': 'demo_vendor/nacl-fast.min.js'}[path]
                    self.send(200, (ASSETS / asset).read_bytes(), 'text/javascript; charset=utf-8')
                elif path.startswith('/demo-audio/'):
                    filename = path.removeprefix('/demo-audio/')
                    manifest = json.loads((ASSETS / 'demo_audio/playbook.json').read_text())
                    allowed = {'playbook.json', *(n['id'] + '.mp3' for group in manifest.values() for n in group)}
                    if filename not in allowed:
                        return self.send(404, {'error': 'Not found'})
                    self.send(200, (ASSETS / 'demo_audio' / filename).read_bytes(),
                              'application/json' if filename.endswith('.json') else 'audio/mpeg')
                elif path in ('/api/demo', '/api/demo-cluster', '/api/demo/trust', '/api/demo-cluster/trust'):
                    session = self.visitor()
                    stage = visitors.stage(session, path.split('/')[2])
                    self.send(200, stage.trust_bundle() if path.endswith('/trust') else stage.snapshot())
                elif path == '/healthz':
                    self.send(200, {'ok': True, 'mode': 'live-isolated-visitors'})
                else:
                    self.send(404, {'error': 'Not found'})
            except StageError as error:
                self.send(400, {'error': str(error)})
            except Exception:  # noqa: BLE001 - keep internal paths out of public errors
                traceback.print_exc()
                self.send(500, {'error': 'Demo unavailable. Ask the host to check its service log.'})

        def do_POST(self):
            try:
                session = self.visitor()
                origin = self.headers.get('Origin')
                if (not secrets.compare_digest(self.headers.get('X-Cockpit-Token', ''), session['token'])
                        or (origin and origin != 'http://' + self.headers.get('Host', ''))):
                    return self.send(403, {'error': 'This control belongs to another visitor. Reload your page.'})
                parts = urlsplit(self.path).path.strip('/').split('/')
                if (len(parts) != 3 or parts[0] != 'api' or parts[1] not in visitors.factories
                        or parts[2] not in ('start', 'approve', 'reset', 'export')):
                    return self.send(404, {'error': 'Not found'})
                length = int(self.headers.get('Content-Length', '0'))
                if (self.headers.get('Transfer-Encoding') or not 0 < length <= 65536
                        or self.headers.get_content_type() != 'application/json'):
                    return self.send(413, {'error': 'Expected JSON under 64 KiB.'})
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict) or set(payload) - {'run_id', 'vcf'}:
                    raise StageError('Only the fixed demo inputs are accepted.')
                self.send(200, visitors.act(session, parts[1], parts[2], payload))
            except (StageError, ValueError) as error:
                self.send(400, {'error': str(error)})
            except Exception:  # noqa: BLE001 - keep internal paths out of public errors
                traceback.print_exc()
                self.send(500, {'error': 'Action failed. Ask the host to check its service log.'})
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--towns', type=Path, nargs='+', required=True)
    parser.add_argument('--storage', type=Path, required=True)
    parser.add_argument('--bind', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8395)
    parser.add_argument('--slots', type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.slots <= 4:
        parser.error('--slots must be between 1 and 4')
    towns = [config.load(path) for path in args.towns]
    visitors = Visitors({town.name: town for town in towns}, args.storage, slots=args.slots)
    server = ThreadingHTTPServer((args.bind, args.port), handler(visitors))
    print(f'Live visitor demo: http://{args.bind}:{server.server_port}/demo', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
