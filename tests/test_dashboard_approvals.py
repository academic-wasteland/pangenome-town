"""Actual HTTP review/issuance, with isolated keys and synthetic applications."""
import json
import threading
from http.server import ThreadingHTTPServer

import pytest
from test_cockpit import request

from pangenome_town import config, dashboard
from pangenome_town.authority import keys


@pytest.fixture
def authority(tmp_path):
    cfg = tmp_path / 'town.toml'
    cfg.write_text(f'''[town]
name="camelot"
kind="authority"
[authority]
registry="registry"
key_dir="{tmp_path / "keys"}"
operator="urn:person:reviewer"
[exchange]
db="{tmp_path / "exchange.db"}"
[rcp]
commons_dir="{tmp_path / "absent-commons"}"
''')
    town = config.load(cfg)
    state = dashboard.DashboardState([town])
    state.mail_list = lambda _: {'messages': []}
    registry = state._registry(town)
    registry.init_issuer('relay-test-only', 'Synthetic tests', 'QualificationIssuer')
    registry.init_issuer('review-board', 'Review board', 'QualificationIssuer')
    def application(synthetic=True):
        record = registry.apply(holder='urn:person:applicant', holder_key_text=keys.public_key_text(keys.generate()),
                                credential_type='Qualification', issuer_slug='relay-test-only' if synthetic else 'review-board',
                                subject_fields={'qualification':'SyntheticRelayTestOnly' if synthetic else 'CredentialedPhysioNetUser'},
                                purpose='Test application')
        record['relay_sender'] = 'zerzura'
        registry._app_path(record['id']).write_text(json.dumps(record))
        return record['id']
    server = ThreadingHTTPServer(('127.0.0.1',0), dashboard.make_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield state, registry, application, f'http://127.0.0.1:{server.server_port}'
    server.shutdown(); server.server_close()


def test_review_issues_real_credential_and_rejects_repeat(authority):
    state, registry, application, base = authority
    app = application()
    body = {'town':'camelot','application':app,'reviewed':True,'valid_days':1,'operator':'urn:forged'}
    assert request(base+'/api/authority/approve', body=body)[0] == 403
    headers = {'X-Cockpit-Token':state.token}
    assert request(base+'/api/authority/approve', body=body, headers={**headers,'Origin':'https://evil.example'})[0] == 403
    assert request(base+'/api/authority/approve', body={**body,'reviewed':False}, headers=headers)[0] == 400
    assert request(base+'/api/authority/approve', body={**body,'valid_days':True}, headers=headers)[0] == 400
    status, result = request(base+'/api/authority/approve', body=body, headers=headers)
    assert status == 200 and result['state'] == 'approved'
    issued = registry.credential(result['credential'])
    assert keys.verify(issued, keys.parse_public(registry.issuer('relay-test-only')['publicKey']))
    assert registry.application(app)['decided_by'] == 'urn:person:reviewer'
    assert request(base+'/api/authority/approve', body=body, headers=headers)[0] == 400
    assert request(base+'/api/authority/deny', body={**body,'reason':'too late'}, headers=headers)[0] == 400
    audit = json.loads((registry.key_dir/'reviews'/f'{app}.json').read_text())
    assert audit['evidence_ref'].startswith('dashboard-review:')
    review = next((registry.key_dir/'dashboard-reviews').glob('*.json'))
    assert review.stat().st_mode & 0o777 == 0o600
    assert 'synthetic' in json.loads(review.read_text())['note']
    assert not state.decisions()['authorities'][0]['pending']


def test_real_claim_needs_evidence_and_denial_persists(authority):
    state, registry, application, base = authority
    app = application(False)
    headers = {'X-Cockpit-Token':state.token}
    body = {'town':'camelot','application':app,'reviewed':True,'valid_days':7}
    assert request(base+'/api/authority/approve', body=body, headers=headers)[0] == 400
    status, result = request(base+'/api/authority/approve', body={**body,'evidence_ref':'private:reviewed-proof'}, headers=headers)
    assert status == 200
    assert 'private:reviewed-proof' not in json.dumps(registry.credential(result['credential']))
    assert 'private:reviewed-proof' not in json.dumps(state.decisions())
    denied = application(False)
    body = {'town':'camelot','application':denied}
    assert request(base+'/api/authority/deny',body=body,headers=headers)[0] == 400
    assert request(base+'/api/authority/deny',body={**body,'reason':'Insufficient evidence'},headers=headers)[0] == 200
    assert registry.application(denied)['reason'] == 'Insufficient evidence'
    assert registry.application(denied)['state'] == 'denied'


def test_browser_review_without_cli(authority, tmp_path):
    import os
    import subprocess
    import time
    if os.environ.get('RUN_BROWSER_CHECKS') != '1':
        pytest.skip('Set RUN_BROWSER_CHECKS=1 for the real Chromium check')
    _state, registry, application, base = authority
    approved, denied = application(), application(False)
    env = dict(os.environ, RODNEY_HOME=str(tmp_path/'browser'))
    def browser(*args):
        result = subprocess.run(['uvx','rodney',*args], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=90, check=False)
        assert result.returncode == 0, result.stdout+result.stderr
        return result.stdout.strip()
    def wait(expression):
        for _ in range(40):
            if browser('js', expression) == 'true': return
            time.sleep(.25)
        pytest.fail(expression)
    try:
        browser('start'); browser('open',base+'/#decisions')
        wait(f'!!document.querySelector(\'[data-key="camelot/{approved}"]\')')
        browser('click',f'[data-decision="approve"][data-key="camelot/{approved}"]')
        browser('assert','document.querySelector("#decision-dialog").open')
        browser('assert','document.querySelector("#decision-operator").textContent.includes("urn:person:reviewer")')
        browser('click','#decision-reviewed')
        browser('screenshot','/tmp/cockpit-credential-review.png')
        browser('click','#decision-submit')
        wait('document.querySelector("#decision-result").textContent.includes("approved")')
        assert registry.application(approved)['state'] == 'approved'
        browser('click',f'[data-decision="deny"][data-key="camelot/{denied}"]')
        browser('select','#decision-reason','Insufficient evidence')
        browser('click','#decision-submit')
        wait('document.querySelector("#decision-result").textContent.includes("denied")')
        browser('open',base+'/#decisions')
        wait('document.querySelector("#decisions-body").textContent.includes("nothing waiting")')
        assert registry.application(denied)['state'] == 'denied'
    finally:
        browser('stop')
