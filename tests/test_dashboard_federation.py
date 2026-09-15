"""Federation discovery and existing external conversations reach the cockpit."""
import dataclasses
import json
from datetime import UTC, datetime

from pangenome_town import dashboard
from pangenome_town.exchange import Envelope


def test_discovery_refresh_outage_and_no_credentials(towns, tmp_path, monkeypatch):
    (tmp_path / 'town.json').write_text(json.dumps({
        'hub': 'https://relay.example/wasteland', 'token': 'SECRET', 'name': 'ubar'}))
    town = dataclasses.replace(towns['ubar'], extra={'federation': {'state': str(tmp_path)}})
    state = dashboard.DashboardState([town])
    calls = []
    document = {'towns': [{'name': 'ubar'}, {'name': 'new_lab', 'display': 'New lab',
                 'last_seen': datetime.now(UTC).isoformat(), 'capabilities': ['echo']},
                 {'name': 'never_polled'}]}
    def get(url, timeout):
        calls.append(url)
        return document
    monkeypatch.setattr(dashboard, '_get_json', get)
    result = state.federation()
    assert [p['name'] for p in result['towns']] == ['new_lab', 'never_polled']
    assert result['towns'][0]['recently_seen'] is True
    assert 'SECRET' not in json.dumps(result)
    state.federation()
    assert len(calls) == 1
    assert calls[0] == 'https://relay.example/wasteland/.well-known/wasteland.json'
    document['towns'].append({'name': 'another_lab'})
    state._federation_cache.clear()
    assert 'another_lab' in [p['name'] for p in state.federation()['towns']]
    hub = calls[0].removesuffix('/.well-known/wasteland.json')
    state._federation_cache[hub] = (0, state._federation_cache[hub][1])
    monkeypatch.setattr(dashboard, '_get_json', lambda *a, **kw: None)
    result = state.federation()
    assert not result['relays'][0]['ok']
    assert len(result['towns']) == 3
    assert not any(p['recently_seen'] for p in result['towns'])
    state.log.close()


def test_external_history_and_failed_reply_are_visible_without_discovery(towns, monkeypatch):
    state = dashboard.DashboardState([towns['ubar']])
    question = Envelope.new('question', 'visiting_lab', 'ubar', {'operation': 'variants'})
    answer = Envelope.new('answer', 'ubar', 'visiting_lab', {'ok': False, 'error': 'missing region'}, in_reply_to=question.id)
    state.log.record(question, town='ubar', direction='received', status='received')
    state.log.record(answer, town='ubar', direction='sent', status='sent')
    monkeypatch.setattr(state, 'sites', lambda: {'towns': []})
    result = state.monitor()
    assert any(n['id'] == 'visiting_lab' and n['kind'] == 'federated' for n in result['nodes'])
    message = next(m for m in result['messages'] if m['id'] == question.id)
    assert message['stage'] == 'failed'
    assert message['task'] == 'variants'
    assert message['message'] == 'missing region'
    assert state.exchange(question.id)['answers'][0]['envelope']['body']['error'] == 'missing region'
    state.log.close()


def test_delegation_permission_and_scheduler_are_distinct(towns, monkeypatch):
    state = dashboard.DashboardState([towns['ubar']])
    task = {'id': 'task-1', 'requester': 'ubar', 'executor': 'yamatai', 'site': 'ddbj',
            'workflow': 'allele-frequency', 'region': 'GRCh38:chr1:1-27', 'datasets': {'saudi': 'digest'}}
    message = Envelope.new('question', 'ubar', 'yamatai', {'operation': 'delegated-compute', 'task': task})
    state.log.record(message, town='ubar', direction='sent', status='sent')
    monkeypatch.setattr(state, 'sites', lambda: {'towns': []})
    state.log.event('yamatai', 'delegation_permission_required', message.id,
                    {'phase': 'permission_required', 'message': 'waiting for ubar permission'})
    assert state.monitor()['messages'][0]['stage'] == 'input-required'
    state.log.event('yamatai', 'delegation_scheduler', message.id,
                    {'phase': 'scheduler', 'job_id': '123', 'status': 'PENDING'})
    assert state.monitor()['messages'][0]['stage'] == 'queued'
    state.log.event('yamatai', 'delegation_scheduler', message.id,
                    {'phase': 'scheduler', 'job_id': '123', 'status': 'RUNNING'})
    assert state.monitor()['messages'][0]['stage'] == 'working'
    state.log.event('yamatai', 'delegation_replayed', message.id, {'phase': 'replayed'})
    assert state.monitor()['messages'][0]['stage'] == 'completed'
    state.log.close()


def test_external_contact_uses_local_identity_and_advertised_operations(towns, monkeypatch):
    import pytest

    from pangenome_town import peers

    state = dashboard.DashboardState([towns['ubar']])
    monkeypatch.setattr(state, 'federation', lambda: {'towns': [
        {'name': 'lisan_al_gaib', 'capabilities': ['echo', 'describe']}]})
    sent = []
    def send(town, envelope, log):
        sent.append(envelope)
        log.record(envelope, town=town.name, direction='sent', status='sent')
        return {'http_status': 200}
    monkeypatch.setattr(peers, 'send', send)
    payload = {'town': 'ubar', 'to': 'lisan_al_gaib', 'operation': 'echo', 'body': 'hello'}
    result = state.act('peers/send', payload)
    assert result['ok'] and sent[0].sender == 'ubar'
    assert sent[0].recipient == 'lisan_al_gaib' and sent[0].body == {'operation': 'echo', 'text': 'hello'}
    assert state.log.get(result['id'])['from'] == 'ubar'
    with pytest.raises(dashboard.ActionError, match='not advertised'):
        state.act('peers/send', dict(payload, operation='message'))
    with pytest.raises(dashboard.ActionError, match='discovered'):
        state.act('peers/send', dict(payload, to='unregistered'))
    state.act('peers/send', dict(payload, operation='describe', body=''))
    assert sent[-1].body == {'operation': 'describe'}
    assert len(sent) == 2
    state.log.close()


def test_human_can_contact_local_town_and_named_resident(towns, monkeypatch):
    state = dashboard.DashboardState([towns['ubar']])
    monkeypatch.setattr(state, '_mail_beads', lambda town: [])
    answer = state.act('contacts/send', {'to': 'ubar', 'operation': 'message', 'body': 'Who is here?'})
    assert answer['ok'] and 'automated general contact' in answer['text']
    messages = state.mail_list('ubar')['messages']
    assert len(messages) == 2 and {m['from'] for m in messages} == {'human', 'ubar'}
    assert any(m['to'] == 'human' and 'Welcome' in m['body'] for m in messages)
    calls = []
    def gc(town, subcommand, flags, positionals, **kwargs):
        calls.append(flags)
        return {'ok': True, 'json': {'ok': True, 'id': 'mail-1'}}
    monkeypatch.setattr(state, '_gc', gc)
    monkeypatch.setattr(dashboard.mail, 'wake_resident', lambda town, resident: resident == 'q')
    sent = state.act('contacts/send', {'to': 'ubar', 'resident': 'q', 'body': 'Hello Q'})
    assert sent['ok'] and sent['wake_requested']
    assert calls[0][calls[0].index('--from')+1] == 'human'
    assert calls[0][calls[0].index('--to')+1] == 'q'
    assert 'Message from human' in calls[0]
    state.log.close()


def test_mail_rejects_success_without_gas_city_receipt(towns, monkeypatch):
    state = dashboard.DashboardState([towns['ubar']])
    monkeypatch.setattr(state, '_gc', lambda *a, **kw: {'ok': True, 'json': None})
    result = state.act('mail/send', {'town': 'ubar', 'to': 'q', 'body': 'hello'})
    assert not result['ok'] and 'confirm' in result['error']
    state.log.close()
