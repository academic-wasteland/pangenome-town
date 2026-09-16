import http.cookiejar
import json
import re
import threading
import urllib.error
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer

import pytest

from pangenome_town.demo_public import Visitors, handler
from pangenome_town.demo_stage import DemoStage, StageError


def test_separate_visitors_runs_evidence_and_reset(towns, tmp_path):
    visitors = Visitors(towns, tmp_path, stage_factory=partial(DemoStage, region='chr1:1-27'))
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler(visitors))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    a, b = [urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())) for _ in range(2)]
    def page(client):
        with client.open(base + '/demo') as response:
            return re.search("const TOKEN='([^']+)'", response.read().decode())[1]
    def call(client, path, body=None, token=None, origin=None):
        headers = {'Content-Type': 'application/json', 'X-Cockpit-Token': token or ''}
        if origin:
            headers['Origin'] = origin
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        with client.open(req, timeout=10) as response:
            return json.load(response)
    try:
        ta, tb = page(a), page(b)
        assert ta != tb
        ra = call(a, '/api/demo/start', {}, ta)['run']
        assert call(b, '/api/demo')['run'] is None
        rb = call(b, '/api/demo/start', {}, tb)['run']
        assert ra['id'] != rb['id']
        with pytest.raises(urllib.error.HTTPError) as error:
            call(b, '/api/demo/approve', {'run_id': ra['id']}, ta)
        assert error.value.code == 403
        with pytest.raises(urllib.error.HTTPError):
            call(b, '/api/demo/reset', {'run_id': ra['id']}, tb)
        with pytest.raises(urllib.error.HTTPError) as error:
            call(a, '/api/demo/approve', {'run_id': ra['id']}, ta, 'https://evil.example')
        assert error.value.code == 403
        call(a, '/api/demo/approve', {'run_id': ra['id']}, ta)
        for session in visitors.sessions.values():
            stage = visitors.stage(session, 'demo')
            if stage.worker:
                stage.worker.join(10)
        assert call(a, '/api/demo')['run']['state'] == 'completed'
        assert call(b, '/api/demo')['run']['state'] == 'awaiting-approval'
        assert call(a, '/api/demo/trust')['run_id'] == ra['id']
        assert call(b, '/api/demo/trust')['run_id'] == rb['id']
        call(a, '/api/demo/reset', {'run_id': ra['id']}, ta)
        assert call(b, '/api/demo')['run']['id'] == rb['id']
        for path in ('/api/mail', '/api/resources', '/api/towns', '/demo-audio/../config.py'):
            with pytest.raises(urllib.error.HTTPError) as error:
                call(a, path)
            assert error.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_capacity_and_queue_preserve_isolation(towns, tmp_path):
    entered = threading.Event()
    release = threading.Event()
    class SlowStage(DemoStage):
        def _execute(self):
            entered.set()
            release.wait(10)
            super()._execute()
    visitors = Visitors(towns, tmp_path, slots=1, max_pending=2, max_sessions=3,
                        stage_factory=partial(SlowStage, region='chr1:1-27'))
    sessions = [visitors.session(create=True) for _ in range(3)]
    with pytest.raises(StageError, match='full'):
        visitors.session(create=True)
    stages = [visitors.stage(s, 'demo') for s in sessions]
    try:
        for s in stages:
            s.start()
        visitors.act(sessions[0], 'demo', 'approve', {'run_id': stages[0].run['id']})
        assert entered.wait(5)
        visitors.act(sessions[1], 'demo', 'approve', {'run_id': stages[1].run['id']})
        with pytest.raises(StageError, match='queue is full'):
            visitors.act(sessions[2], 'demo', 'approve', {'run_id': stages[2].run['id']})
        assert stages[2].run['state'] == 'awaiting-approval'
    finally:
        release.set()
        for s in stages:
            if s.worker:
                s.worker.join(15)
    assert all(s.run['state'] == 'completed' for s in stages[:2])
    assert any(e['title'] == 'Waiting for a compute slot' for e in stages[1].run['events'])
