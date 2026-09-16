import dataclasses
import io
import json
from types import SimpleNamespace

import pytest

from pangenome_town import peers
from pangenome_town.exchange import Attachment, Envelope, ExchangeLog


def test_external_peer_uses_explicit_relay_and_local_peers_keep_their_route(towns, tmp_path, monkeypatch):
    state = tmp_path/'private'
    state.mkdir()
    (state/'town.json').write_text(json.dumps({'name':'ubar','hub':'https://relay.example/wasteland','token':'private-token'}))
    town = dataclasses.replace(towns['ubar'], extra={'federation':{'state':str(state)}})
    calls = []

    def opened(req, timeout):
        calls.append(req)
        result = io.BytesIO(b'{"status":"queued"}')
        result.status = 200
        return result

    monkeypatch.setattr(peers.urllib.request, 'build_opener', lambda *args: SimpleNamespace(open=opened))
    monkeypatch.setattr(peers.urllib.request, 'urlopen', opened)
    log = ExchangeLog(towns['db'])
    external = Envelope.new('question','ubar','visiting_lab',{'text':'hello'})
    peers.send(town,external,log)
    assert calls[-1].full_url == 'https://relay.example/wasteland/v1/messages'
    assert calls[-1].get_header('Authorization') == 'Bearer private-token'
    assert log.get(external.id)['status'] == 'sent'
    peers.send(town,Envelope.new('question','ubar','yamatai',{}),log)
    assert '/v0/city/yamatai/svc/envoy/v0/messages' in calls[-1].full_url
    assert calls[-1].get_header('Authorization') is None
    log.close()


def test_federation_never_publishes_local_attachments(towns):
    town = dataclasses.replace(towns['ubar'], extra={'federation':{'state':'/must-not-be-read'}})
    message = Envelope.new('question','ubar','visiting_lab',{}, attachments=(Attachment('data','sha256:abc','/private/data'),))
    with pytest.raises(peers.PeerError,match='inline messages only'):
        peers.send(town,message)


def test_external_discovery_is_read_only_and_never_discloses_token(towns, tmp_path, monkeypatch):
    state = tmp_path / 'private'
    state.mkdir()
    (state / 'town.json').write_text(json.dumps({'name': 'ubar', 'hub': 'https://relay.example', 'token': 'secret'}))
    town = dataclasses.replace(towns['ubar'], extra={'federation': {'state': str(state)}})
    calls = []
    def opened(req, timeout):
        calls.append(req)
        return io.BytesIO(json.dumps({'towns': [{'name': 'zerzura', 'capabilities': ['describe', 'mimic-schema']}]}).encode())
    monkeypatch.setattr(peers.urllib.request, 'urlopen', opened)
    result = peers.town_info(town, 'zerzura')
    assert result['contact'] == {'town': 'zerzura', 'operation': 'describe', 'agents': []}
    assert len(calls) == 1 and calls[0].get_method() == 'GET'
    assert calls[0].get_header('Authorization') is None
    assert 'secret' not in json.dumps(result)
    with pytest.raises(peers.PeerError, match='not advertised'):
        peers.town_info(town, 'missing')
