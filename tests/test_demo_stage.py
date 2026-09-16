"""Real bcftools + signed policy, without touching the presenter's live towns."""
import json
import shutil
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from pangenome_town import dashboard
from pangenome_town.demo_stage import DemoStage, StageError

pytestmark = pytest.mark.skipif(shutil.which('bcftools') is None, reason='bcftools required')


@pytest.fixture
def stage(towns, tmp_path):
    return DemoStage({n: towns[n] for n in ('ubar', 'yamatai')}, region='chr1:1-27', storage=tmp_path / 'stage')


def finish(stage):
    stage.worker.join(10)
    assert not stage.worker.is_alive()
    return stage.snapshot()['run']


def test_three_clicks_real_counts_and_denied_export(stage):
    run = stage.start()['run']
    assert run['state'] == 'awaiting-approval'
    assert not run['decisions']['ubar']['ok']
    assert run['decisions']['yamatai']['ok']
    assert not any(e['kind'] == 'compute' for e in run['events'])
    stage.approve(run['id'])
    complete = finish(stage)
    assert complete['state'] == 'completed', complete['events'][-1]
    assert complete['result']['sites'] == 3
    row = complete['result']['rows'][0]
    assert (row['ubar']['ac'], row['ubar']['an']) == (1, 4)
    assert (row['yamatai']['ac'], row['yamatai']['an']) == (1, 2)
    assert [e['kind'] for e in complete['events']].count('compute') == 2
    assert [e['kind'] for e in complete['events']].count('result') == 2
    denied = stage.attempt_export(run['id'])['run']
    assert denied['export_refused'] and denied['result'] == complete['result']
    assert denied['events'][-1]['detail']['compute_started'] is False
    assert [e['kind'] for e in denied['events']].count('compute') == 2
    assert stage.attempt_export(run['id'])['run'] == denied
    saved = json.loads((stage.storage / (run['id'] + '.json')).read_text())
    assert saved == denied
    assert not any(x in json.dumps(saved) for x in ('PRIVATE KEY', '0|1', '1|0'))
    assert (stage.storage / (run['id'] + '.json')).stat().st_mode & 0o777 == 0o600


def test_duplicate_and_stale_actions_refused(stage):
    run = stage.start()['run']
    with pytest.raises(StageError, match='Reset'):
        stage.start()
    with pytest.raises(StageError, match='old run'):
        stage.approve('some-other-run')
    with pytest.raises(StageError, match='Complete'):
        stage.attempt_export(run['id'])
    stage.approve(run['id'])
    with pytest.raises(StageError, match='not awaiting'):
        stage.approve(run['id'])
    finish(stage)
    assert stage.reset(run['id'])['run'] is None
    assert stage.start()['run']['id'] != run['id']


def test_exact_task_mutation_prevents_approval(stage):
    run = stage.start()['run']
    stage.tasks['ubar']['region'] = 'chr1:1-26'
    with pytest.raises(StageError, match='no longer current'):
        stage.approve(run['id'])
    assert not any(e['kind'] == 'compute' for e in stage.run['events'])


def test_revocation_during_query_withholds_result(stage, monkeypatch):
    import pangenome_town.demo_stage as module
    original = module.subprocess.run
    def revoke(*args, **kwargs):
        result = original(*args, **kwargs)
        with stage.lock:
            for identifier in stage.statuses:
                stage.statuses[identifier] = 'revoked'
        return result
    monkeypatch.setattr(module.subprocess, 'run', revoke)
    run = stage.start()['run']
    stage.approve(run['id'])
    failed = finish(stage)
    assert failed['state'] == 'failed' and failed['result'] is None
    assert not any(e['kind'] == 'complete' for e in failed['events'])
    assert failed['events'][-1]['kind'] == 'error'


def test_http_stage_shares_cockpit_guards(stage, towns):
    state = dashboard.DashboardState([towns['ubar'], towns['yamatai']])
    state._stage = stage
    server = ThreadingHTTPServer(('127.0.0.1', 0), dashboard.make_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'
    def request(path, data=None, token=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-Cockpit-Token'] = token
        req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None, headers=headers)
        return urllib.request.urlopen(req, timeout=5)
    try:
        with request('/demo') as response:
            html = response.read().decode()
            assert 'Compare cohorts' in html and state.token in html
        with pytest.raises(urllib.error.HTTPError) as error:
            request('/api/demo/start', {})
        assert error.value.code == 403 and stage.run is None
        with request('/api/demo/start', {}, state.token) as response:
            assert json.load(response)['run']['state'] == 'awaiting-approval'
        with request('/api/demo') as response:
            assert len(json.load(response)['run']['events']) == 6
        with pytest.raises(urllib.error.HTTPError) as error:
            request('/api/demo/approve', {'run_id': 'stale'}, state.token)
        assert error.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        state.log.close()
