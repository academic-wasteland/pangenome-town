
import pytest
from wasteland.conversations import CONTEXT, child_context

from pangenome_town.conversations import CockpitConversations
from pangenome_town.dashboard import DashboardState
from pangenome_town.exchange import Envelope


@pytest.fixture
def hub(towns, monkeypatch):
    state = DashboardState([towns['ubar'], towns['yamatai']])
    monkeypatch.setattr(state, 'federation', lambda: {'towns': []})
    monkeypatch.setattr(state, 'resource_catalog', lambda: {'resources': []})
    h = CockpitConversations(state)
    person = h.store.save_person({'display': 'Robert', 'memberships': [{'town': 'ubar', 'role': 'owner'}, {'town': 'yamatai', 'role': 'owner'}]}, state.towns)
    yield h, person
    state.log.close()


def test_local_contact_real_directory_and_named_person(hub):
    h, person = hub
    result = h.action('send', {'person_id': person['id'], 'via': 'yamatai', 'to': 'ubar', 'resident': 'contact', 'text': 'What can your town do?'})
    cid = result['conversation']
    h.workers[cid].join(5)
    data = h.snapshot(cid)
    assert data['conversation']['person']['memberships'][0] == {'town': 'ubar', 'role': 'owner'}
    assert data['events'][0]['sender'] == 'Robert via yamatai'
    assert any(e['state'] == 'replied' and 'automated general contact' in e['text'] for e in data['events'])
    assert all(e['sender'] != 'human' for e in data['events'])
    assert CockpitConversations(h.state).store.get(cid)['person_id'] == person['id']


def test_causal_delegation_and_reply_are_traced_without_guessed_links(hub):
    h, person = hub
    t = h.store.create(person['id'], 'yamatai', 'ubar', 'contact', 'Investigate')
    parent = Envelope.new('question', 'yamatai', 'ubar', {'text': 'Investigate', CONTEXT: h.store.context(t)})
    h.store.add(t['id'], event_id=parent.id, sender='Robert via yamatai', recipient='ubar/contact', text='Investigate', state='sent')
    delegated = Envelope.new('question', 'ubar', 'yamatai', {'text': 'Analyse here', 'resident': 'bob', CONTEXT: child_context(h.store.context(t), parent.id)})
    reply = Envelope.new('answer', 'yamatai', 'ubar', {'text': 'Result', 'resident': 'bob'}, in_reply_to=delegated.id)
    unrelated = Envelope.new('answer', 'unrelated', 'yamatai', {'text': 'False result'}, in_reply_to=parent.id)
    for e in (delegated, reply, unrelated):
        h.state.log.record(e, town='ubar', direction='received', status='received')
    h.sync(t)
    events = h.store.events(t['id'])
    assert [e['text'] for e in events] == ['Investigate', 'Analyse here', 'Result']
    assert events[1]['parent'] == parent.id and events[2]['parent'] == delegated.id


def test_named_local_mail_receipt_and_reply(hub, monkeypatch):
    h, person = hub
    root = h.state.towns['ubar'].city_root / 'agents' / 'sam'
    root.mkdir(parents=True)
    (root / 'agent.toml').write_text('description = "Genomics agent"')
    calls = []
    monkeypatch.setattr(h.state, '_gc', lambda *a, **k: (calls.append(a) or {'ok': True, 'json': {'ok': True, 'id': 'mail-1', 'thread_id': 'thread-abc'}}))
    monkeypatch.setattr('pangenome_town.mail.wake_resident', lambda *a: True)
    monkeypatch.setattr(h.state, 'mail_list', lambda town: {'messages': [{'id': 'reply-1', 'thread_id': 'thread-abc', 'from': 'sam', 'body': 'I can help', 'created_at': 'today'}]})
    result = h.action('send', {'person_id': person['id'], 'via': 'ubar', 'to': 'ubar', 'resident': 'sam', 'text': 'Hello'})
    cid = result['conversation']
    h.workers[cid].join(5)
    assert calls[0][2][1] == 'human'  # Gas City operator mailbox; person attribution is in the envelope and body
    assert 'Robert' in calls[0][2][7] and person['id'] in calls[0][2][7]
    h.sync(h.store.get(cid))
    assert any(e['text'] == 'I can help' for e in h.store.events(cid))
    h.sync(h.store.get(cid))
    assert sum(e['text'] == 'I can help' for e in h.store.events(cid)) == 1


def test_profiles_do_not_allow_arbitrary_owner_towns(hub):
    h, person = hub
    with pytest.raises(ValueError):
        h.action('person', {'display': 'Pretend owner', 'memberships': [{'town': 'outside', 'role': 'owner'}]})
    with pytest.raises(ValueError):
        h.action('send', {'person_id': person['id'], 'via': 'ubar', 'to': 'missing', 'text': 'Hi'})


def test_stop_waits_for_submitted_operation_and_prevents_next_step(hub, monkeypatch, tmp_path):
    import dataclasses
    import threading

    from wasteland.conversations import identifier
    h, person = hub
    h.state.towns['yamatai'] = dataclasses.replace(h.state.towns['yamatai'], extra={'federation': {'state': str(tmp_path)}})
    asked, release = threading.Event(), threading.Event()
    requests = []
    class Client:
        def __init__(self, *args):
            pass
        def ask(self, town, **kwargs):
            mid = identifier()
            requests.append((mid, kwargs))
            asked.set()
            return mid
        def wait(self, mid, **kwargs):
            assert release.wait(5)
            return [{'id': identifier(), 'in_reply_to': mid}]
    monkeypatch.setattr('wasteland.client.Client', Client)
    result = h.action('send', {'person_id': person['id'], 'via': 'yamatai', 'to': 'ubar', 'resident': 'contact',
                               'text': 'Investigate this phenotype', 'phenotypes': ['Ectopia lentis (HP:0001083)']})
    cid = result['conversation']
    assert asked.wait(5)
    h.action('stop', {'conversation': cid, 'person_id': person['id']})
    assert any(e['state'] == 'stop-pending' for e in h.store.events(cid))
    release.set()
    h.workers[cid].join(8)
    assert len(requests) == 1
    assert any(e['state'] == 'stopped' for e in h.store.events(cid))
