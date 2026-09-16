import subprocess
from dataclasses import replace
from typing import ClassVar

import pytest

from pangenome_town.compute.sites import JobResult, Site
from pangenome_town.demo_cluster import SYNTHETIC_VCF, ClusterStage, validate_vcf
from pangenome_town.demo_stage import StageError


class Driver:
    calls: ClassVar[list] = []
    callback = None

    def __init__(self, site, **kwargs):
        pass

    def _ssh(self, command, **kwargs):
        self.calls.append(command)
        return subprocess.CompletedProcess([], 0, '', '')

    def run(self, job, *, fetch_to):
        self.calls.append('submit')
        self.on_progress(job_id='12345', status='SUBMITTED')
        self.on_progress(job_id='12345', status='COMPLETED')
        if self.callback:
            self.callback()
        fetch_to.mkdir(parents=True, exist_ok=True)
        p = fetch_to / 'counts.tsv'
        p.write_text('variant\tAC\tAN\nchr6:29940047:T:C\t1\t4\n')
        return JobResult(0, {'counts.tsv': p}, 1.0, '12345')


@pytest.fixture
def stage(towns, tmp_path):
    Driver.calls = []
    Driver.callback = None
    s = ClusterStage(towns, storage=tmp_path / 'stage', driver_factory=Driver)
    s.site = Site('ddbj', 'ssh', host='ddbj', submit_host='a001', scheduler='slurm', workdir='/test/jobs')
    return s


def finish(s):
    s.worker.join(5)
    assert not s.worker.is_alive()
    return s.snapshot()['run']


def test_irb_blocks_transfer_then_real_policy_allows_result(stage):
    r = stage.start()['run']
    assert Driver.calls == []
    requirements = {v['type']: v['status'] for v in r['decisions']['yamatai']['requirements']}
    assert requirements['EthicsApproval'] == 'pending'
    assert all(v == 'pass' for k, v in requirements.items() if k != 'EthicsApproval')
    stage.approve(r['id'])
    r = finish(stage)
    assert r['state'] == 'completed', r['events'][-1]
    assert r['result']['rows'][0]['ac'] == 1
    assert r['job_id'] == '12345'
    assert Driver.calls.count('submit') == 1
    assert not any('0/1' in p.read_text() for p in stage.storage.glob('*.json'))
    assert r['decisions']['yamatai']['ok']


@pytest.mark.parametrize('change', ['file', 'task', 'site'])
def test_input_or_destination_change_cannot_reuse_permission(stage, change):
    r = stage.start()['run']
    if change == 'file':
        stage.vcf += '\n'
    elif change == 'task':
        stage.tasks['yamatai']['workflow'] = 'other'
    else:
        stage.site = replace(stage.site, host='other')
    with pytest.raises(StageError, match='changed'):
        stage.approve(r['id'])
    assert Driver.calls == []


def test_revocation_withholds_cluster_results(stage):
    r = stage.start()['run']
    def revoke(self):
        for identifier in stage.statuses:
            stage.statuses[identifier] = 'revoked'
    Driver.callback = revoke
    stage.approve(r['id'])
    r = finish(stage)
    assert r['state'] == 'failed' and r['result'] is None


def test_stale_and_duplicate_approval(stage):
    r = stage.start()['run']
    with pytest.raises(StageError):
        stage.approve('old')
    stage.approve(r['id'])
    with pytest.raises(StageError):
        stage.approve(r['id'])
    finish(stage)
    assert Driver.calls.count('submit') == 1


@pytest.mark.parametrize('vcf', ['', None, 'x'*40001, SYNTHETIC_VCF.replace('0/1', '0/2'), SYNTHETIC_VCF.replace('GT\t', 'GT:DP\t')])
def test_reject_invalid_input(vcf):
    with pytest.raises(StageError):
        validate_vcf(vcf)


def test_alternate_visitor_file(stage):
    vcf = SYNTHETIC_VCF.replace('0/1', '1/1')
    run = stage.start(vcf)['run']
    assert run['input']['source'] == 'Visitor-provided VCF'
    assert run['input']['samples'] == 2
    assert run['input']['variants'] == 3


def test_http_separate_case_and_action_guards(stage, towns):
    import json
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from pangenome_town.dashboard import DashboardState, make_handler

    state = DashboardState([towns['ubar'], towns['yamatai']])
    state._cluster_stage = stage
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with urllib.request.urlopen(base + '/demo/visitor') as response:
            assert state.token in response.read().decode()
        request = urllib.request.Request(base + '/api/demo-cluster/start', data=b'{}', headers={'Content-Type': 'application/json'})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 403 and stage.run is None
        request.add_header('X-Cockpit-Token', state.token)
        with urllib.request.urlopen(request) as response:
            assert json.load(response)['run']['state'] == 'awaiting-approval'
        with urllib.request.urlopen(base + '/api/demo') as response:
            assert json.load(response)['run'] is None
        assert Driver.calls == []
    finally:
        server.shutdown()
        server.server_close()
        state.log.close()
