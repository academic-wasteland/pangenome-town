import copy
import json
import secrets
import threading
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from pangenome_town.authority import credentials as cred
from pangenome_town.authority import keys
from pangenome_town.authority.registrar import Registry, RegistryError
from pangenome_town.authority.relay import RelayAuthority


@pytest.fixture
def authority(tmp_path):
    reg = Registry(tmp_path/'registry', tmp_path/'keys')
    issuer = reg.init_issuer('test-board', 'Test review board', 'QualificationIssuer')
    return reg, issuer, RelayAuthority(reg, 'camelot')


def request(key):
    return {'action': 'apply', 'holder': 'urn:participant:alice', 'publicKey': keys.public_key_text(key),
            'application': {'credential_type': 'Qualification', 'issuer': 'test-board',
                            'subject_fields': {'qualification': 'SyntheticTestResearcher'}, 'purpose': 'Test only'}}


def apply(reg, relay, key):
    challenge = relay.handle('town_a', {'operation': 'credential-challenge', 'request': request(key)})['challenge']
    proof = keys.sign(challenge, key, 'urn:participant:alice#key')
    result = relay.handle('town_a', {'operation': 'credential-apply', 'presentation': proof})
    with pytest.raises(RegistryError, match='evidence'):
        reg.approve(result['application'], decided_by='urn:reviewer:operator')
    doc = reg.approve(result['application'], decided_by='urn:reviewer:operator', evidence_ref='private:test-fixture')
    return result, proof, doc


def test_holder_proof_review_aliases_and_replay(authority):
    reg, _, relay = authority
    key = keys.generate()
    result, proof, doc = apply(reg, relay, key)
    assert doc['credentialSubject']['holderKey'] == doc['credentialSubject']['publicKey'] == keys.public_key_text(key)
    assert doc['credentialSubject']['roles'] == ['SyntheticTestResearcher']
    assert 'private:test-fixture' not in json.dumps(doc)
    audit = reg.key_dir/'reviews'/(result['application']+'.json')
    assert audit.stat().st_mode & 0o777 == 0o600
    with pytest.raises(RegistryError, match='consumed'):
        relay.handle('town_a', {'operation': 'credential-apply', 'presentation': proof})
    challenge = relay.handle('town_a', {'operation': 'credential-challenge', 'request': request(key)})['challenge']
    forged = keys.sign(challenge, keys.generate(), 'fake')
    with pytest.raises(RegistryError, match='holder proof'):
        relay.handle('town_a', {'operation': 'credential-apply', 'presentation': forged})
    signed = keys.sign(challenge, key, 'holder')
    with pytest.raises(RegistryError, match='sender'):
        relay.handle('other_town', {'operation': 'credential-apply', 'presentation': signed})
    with relay.db() as db:
        db.execute('UPDATE challenges SET expires=0')
    with pytest.raises(RegistryError, match='expired'):
        relay.handle('town_a', {'operation': 'credential-apply', 'presentation': signed})


def test_status_exact_id_signature_revocation_and_unknown(authority):
    reg, issuer, relay = authority
    _, _, doc = apply(reg, relay, keys.generate())
    active = relay.status(doc['id'], None)['statement']
    assert keys.verify(active, keys.parse_public(issuer['publicKey']))
    assert active['status'] == 'active'
    assert cred.parse_iso(active['valid_until'])-cred.parse_iso(active['as_of']) == timedelta(seconds=60)
    wrong = 'https://attacker.example/credentials/'+doc['id'].rsplit('/',1)[1]
    assert relay.status(wrong, issuer['id'])['statement']['status'] == 'unknown'
    reg.revoke(doc['id'])
    assert relay.status(doc['credentialStatus']['id'], None)['statement']['status'] == 'revoked'


def test_real_relay_holder_retrieval_and_receiver_release_gate(authority, tmp_path):
    pytest.importorskip('wasteland')
    from wasteland import credentials as wc
    from wasteland.bridge import Bridge
    from wasteland.client import Client, Worker, join
    from wasteland.hub import Store, handler
    reg, issuer, _ = authority
    store = Store(tmp_path/'hub.sqlite')
    invite = secrets.token_urlsafe(32)
    server = ThreadingHTTPServer(('127.0.0.1',0), handler(store,invite,'http://127.0.0.1'))
    thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    for name in ('camelot','town_a','town_b'):
        join(tmp_path/name,url,name,invite)
    bridge = Bridge.__new__(Bridge)
    bridge.town = SimpleNamespace(name='camelot',kind='authority',city_root=tmp_path,
                                 extra={'authority':{'registry':str(reg.root),'key_dir':str(reg.key_dir)}})
    worker = Worker(tmp_path/'camelot',bridge.handle)
    # Pump a real relay HTTP request/reply synchronously before each client's wait.
    def client(name):
        c = Client(tmp_path/name)
        original = c.wait
        def wait(*args, **kwargs):
            worker.tick()
            return original(*args, **kwargs)
        c.wait = wait
        return c
    try:
        a,b = client('town_a'),client('town_b')
        holder = keys.generate()
        payload = request(holder)
        app = wc.holder_request(a,'camelot',holder,payload['holder'],application=payload['application'])
        assert app['state'] == 'pending'
        reg.approve(app['application'],decided_by='urn:reviewer:test',evidence_ref='private:test-only')
        reply = wc.holder_request(a,'camelot',holder,payload['holder'],application=app['application'],action='fetch')
        doc = reply['credential']
        with pytest.raises(ValueError,match='not available'):
            wc.holder_request(b,'camelot',holder,payload['holder'],application=app['application'],action='fetch')
        trusted = {issuer['id']:issuer['publicKey']}
        checker = wc.status_checker(b,{issuer['id']:'camelot'},trusted)
        task = {'@id':'urn:task:one','@type':['ResearchTask']}
        presentation = cred.present(holder=payload['holder'],holder_key=holder,credentials=[doc],accreditations=[],
                                    task=task,audience='town_b')
        def gate():
            return cred.verify_presentation(presentation,task,audience='town_b',anchors=trusted,status_checker=checker)
        assert gate().ok  # Before execution
        statement = wc.call(b,'camelot','credential-status',{'id':doc['id']})['statement']
        assert wc.checked_status(statement,doc,trusted,now=datetime.now(UTC)+timedelta(minutes=2)) == 'unknown'
        tampered = copy.deepcopy(statement);tampered['status']='revoked'
        assert wc.checked_status(tampered,doc,trusted) == 'unknown'
        assert wc.checked_status(statement,doc,{issuer['id']:keys.public_key_text(keys.generate())}) == 'unknown'
        conflicting = copy.deepcopy(doc);conflicting['credentialSubject']['publicKey']=keys.public_key_text(keys.generate())
        conflicting = keys.sign(conflicting,reg._key('test-board'),issuer['id']+'#key-1')
        bad = cred.present(holder=payload['holder'],holder_key=holder,credentials=[conflicting],accreditations=[],task=task,audience='town_b')
        assert not cred.verify_presentation(bad,task,audience='town_b',anchors=trusted,status_checker=checker).ok
        reg.revoke(doc['id'])
        assert not gate().ok  # Before release: freshly observed revocation blocks it.
    finally:
        worker.db.close();server.shutdown();server.server_close();thread.join();store.db.close()
