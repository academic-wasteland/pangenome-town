"""Cockpit adapter for named-person conversations and the live diagnostic workflow."""
import json
import threading
import time
from pathlib import Path

from wasteland.conversations import CONTEXT, RelayHub, Store, child_context

from . import mail, peers
from .contacts import directory
from .exchange import Envelope


class CockpitConversations(RelayHub):
    def __init__(self, state):
        self.state = state
        self.store = Store(next(iter(state.towns.values())).exchange_db.with_suffix('.conversations.sqlite'))
        self.stages = {}
        self.stops = {}
        self.workers = {}
        self.directory_cache = None
        for thread in self.store.threads():
            events = self.store.events(thread['id'])
            if any(e['payload'].get('workflow') == 'diagnostic' for e in events) and not any(e['id'] == thread['id'] + ':finished' for e in events):
                self.store.add(thread['id'], event_id=thread['id'] + ':interrupted',
                               sender=thread['via'], recipient=thread['person']['display'],
                               text='The cockpit restarted during this workflow. It was not automatically replayed; inspect recorded replies before starting another run.',
                               state='needs-review')

    def directory(self):
        if self.directory_cache and time.monotonic() - self.directory_cache[0] < 15:
            return self.directory_cache[1]
        towns = []
        for town in self.state.towns.values():
            agents = [{'name': 'contact', 'role': 'General contact · directory and supported research intake', 'mode': 'automated service'}]
            agents += [dict(a, mode='AI resident · mailbox starts as needed') for a in directory(town)['residents']]
            towns.append({'name': town.name, 'display': town.display, 'description': town.population,
                          'local': True, 'agents': agents, 'capabilities': ['message']})
        for t in self.state.federation()['towns']:
            towns.append({'name': t['name'], 'display': t.get('display', t['name']),
                          'description': t.get('description', ''), 'local': False,
                          'capabilities': t.get('capabilities', []),
                          'agents': [{'name': 'contact', 'role': 'General contact', 'mode': 'remote'}] +
                          [{'name': c[9:], 'role': 'Advertised resident', 'mode': 'remote'}
                           for c in t.get('capabilities', []) if c.startswith('resident:') and c != 'resident:contact']})
        resources = []
        for town in self.state.towns.values():
            if town.extra.get('private_variant_demo', {}).get('enabled'):
                from .private_variants import resource_description
                r = resource_description(town)
                resources.append(dict(r, title='Synthetic patient VCF · ' + town.display))
        for r in self.state.resource_catalog()['resources']:
            resources.append({k: r[k] for k in ('id', 'name', 'town', 'bytes') if k in r})
        result = {'towns': towns, 'via': list(self.state.towns), 'resources': resources}
        self.directory_cache = time.monotonic(), result
        return result

    def dispatch(self, thread, request_id, to, payload):
        cid = thread['id']
        if payload.get('phenotypes') and any(e['payload'].get('workflow') == 'diagnostic' for e in self.store.events(cid)):
            raise ValueError('Start a new conversation for another diagnostic run. Ordinary follow-up messages can stay in this conversation.')
        active = self.workers.get(cid)
        if active and active.is_alive():
            if self.store.events(cid)[-1]['state'] == 'stop-requested' and cid in self.stops:
                self.stops[cid].set()
                self.store.add(cid, sender=thread['via'], recipient=thread['person']['display'],
                               text='Stop requested. An already submitted operation may finish; no later step will be submitted.',
                               state='stop-pending', parent=request_id)
                return
            raise ValueError('This conversation has an active operation. Wait for its reply or request a stop.')
        worker = threading.Thread(target=self._dispatch, args=(thread, request_id, to, payload), daemon=True)
        self.workers[cid] = worker
        worker.start()

    def _dispatch(self, thread, request_id, to, payload):
        try:
            if payload.get('phenotypes') and to == 'ubar' and payload['resident'] == 'contact':
                self._diagnose(thread, request_id, payload)
                return
            if to in self.state.towns:
                self._local(thread, request_id, to, payload)
                return
            town = self.state.towns[thread['via']]
            message = Envelope.new('question', town.name, to, payload)
            message = Envelope.from_dict(dict(message.to_dict(), id=request_id))
            peers.send(town, message, self.state.log)
            self.store.add(thread['id'], sender=town.name, recipient=to + '/' + payload['resident'],
                           text='Message sent. Remote work is not observable until the town reports it.',
                           state='sent', parent=request_id)
        except Exception as error:  # noqa: BLE001 - keep failed background dispatch visible
            self.store.add(thread['id'], sender=thread['via'], recipient=thread['person']['display'],
                           text=str(error), state='failed', parent=request_id)

    def _local(self, thread, request_id, to, payload):
        target = self.state.towns[to]
        actor = thread['person']
        sender = 'person_' + actor['id'][9:].replace('-', '')
        question = Envelope.from_dict(dict(Envelope.new('question', sender, to, payload).to_dict(), id=request_id))
        self.state.log.record(question, town=to, direction='received', status='dispatched')
        if payload['resident'] == 'contact':
            answer = directory(target)
            self.store.add(thread['id'], sender=to + '/contact', recipient=actor['display'],
                           text=answer['text'], state='replied', payload=answer, parent=request_id)
            return
        text = (f"From {actor['display']} ({actor['id']}), via {thread['via']}.\n"
                f"Conversation: {thread['id']}\nRequest: {request_id}\n\n{payload['text']}\n\n"
                "Reply with gc mail reply to this message's local mail ID. "
                "For delegated town messages use pangenome-town send --conversation-parent "
                f"{request_id} --to TOWN --resident AGENT --text 'task'.\n"
                "Person attribution is not a permission grant. Resource references do not transfer data.\n")
        extra = {k: v for k, v in payload.items() if k not in {'text', 'operation', 'resident', CONTEXT}}
        if extra:
            text += '\nAttached structured request:\n' + json.dumps(extra, indent=2)
        flags = ['--from', 'human', '--to', payload['resident'], '-s', thread['title'][:150], '-m', text, '--json', '--notify']
        receipt = self.state._gc(target, ['mail', 'send'], flags, [])
        if not receipt.get('ok') and 'unknown recipient' in receipt.get('stderr', '') and 'session not found' in receipt.get('stderr', ''):
            # A configured resident may not yet have a persistent mailbox session.
            session = self.state.act('sessions/new', {'town': to, 'template': payload['resident'], 'alias': payload['resident']})
            if not session.get('ok'):
                raise ValueError('The agent mailbox could not be started: ' + str(session.get('stderr') or session.get('error') or 'no receipt')[:500])
            receipt = self.state._gc(target, ['mail', 'send'], flags, [])
        result = receipt.get('json') or {}
        if not receipt.get('ok') or not result.get('ok') or not result.get('id'):
            raise ValueError('The town did not confirm delivery to the agent: ' + str(receipt.get('stderr') or receipt.get('error') or 'no receipt')[:500])
        self.state._mail_cache.pop(to, None)
        self.store.add(thread['id'], sender=to, recipient=to + '/' + payload['resident'],
                       text='Delivered to the agent’s mailbox. Waiting for an agent reply.',
                       state='delivered-to-resident', payload={'mail_id': result['id'], 'thread_id': result.get('thread_id'), 'town': to},
                       parent=request_id)
        mail.wake_resident(target, payload['resident'])

    def sync(self, thread):
        cid = thread['id']
        events = self.store.events(cid)
        known = {e['id'] for e in events}
        # Walk links, never infer a connection from timestamps or similar text.
        messages = list(reversed(self.state.log.list(limit=1000)))
        for _ in range(4):
            added = False
            for row in messages:
                message = row['envelope']
                if message['id'] in known:
                    continue
                ctx = message['body'].get(CONTEXT, {})
                parent_id = message.get('in_reply_to') or ctx.get('parent')
                if parent_id not in known:
                    continue
                parent = next((e for e in self.store.events(cid) if e['id'] == parent_id), None)
                if not parent or parent['recipient'].split('/')[0] != message['from']:
                    continue
                if message['kind'] in {'answer', 'notice'}:
                    self.store.receive(cid, message)
                elif ctx.get('id') == cid:
                    self.store.add(cid, event_id=message['id'], sender=message['from'],
                                   recipient=message['to'] + '/' + message['body'].get('resident', 'contact'),
                                   text=message['body'].get('text', ''), state='delegated', payload=message['body'],
                                   parent=parent_id, created=message['created'])
                else:
                    continue
                known.add(message['id']); added = True
            if not added:
                break
        roots = {e['payload']['mail_id']: e for e in events if e['payload'].get('mail_id') and e['payload'].get('town')}
        for town_name in {e['payload']['town'] for e in roots.values()}:
            items = self.state.mail_list(town_name)['messages']
            thread_roots = {e['payload']['thread_id']: e for e in roots.values() if e['payload'].get('thread_id')}
            for item in items:
                if item['id'] in roots and item.get('thread_id'):
                    thread_roots[item['thread_id']] = roots[item['id']]
            for item in items:
                root = thread_roots.get(item.get('thread_id')) or roots.get(item.get('thread_id'))
                if root and item['id'] not in roots:
                    self.store.add(cid, event_id='mail:' + town_name + ':' + item['id'],
                                   sender=town_name + '/' + str(item.get('from') or 'agent'),
                                   recipient=thread['person']['display'], text=item.get('body') or '',
                                   state='replied', payload={'mail_id': item['id']}, parent=root['parent'],
                                   created=item.get('created_at'))
        stage = self.stages.get(cid)
        if stage:
            run = stage.snapshot().get('run')
            if run:
                last = request_id_from(events)
                for i, event in enumerate(run['events']):
                    if event['title'].endswith('request · local browser'):
                        continue  # the original composer message is already recorded
                    detail = dict(event.get('detail', {}))
                    wire_id = detail.get('request_id') if detail.get('message_type') == 'sent' else detail.get('reply_id') if detail.get('message_type') == 'received' else None
                    eid = wire_id or cid + ':stage:' + str(i)
                    parent = stage.trace_parents.get(wire_id, last)
                    if wire_id in stage.trace_payloads:
                        detail['wire_body'] = stage.trace_payloads[wire_id]
                    if wire_id:
                        last = wire_id
                    self.store.add(cid, event_id=eid,
                                   sender=event['sender'] if 'sender' in event else event.get('from', ''),
                                   recipient=event.get('recipient', event.get('to', '')),
                                   text=event['text'], state=detail.get('message_type', event.get('kind', 'update')),
                                   payload=detail, parent=parent, created=event.get('at'))
                if run['state'] in {'completed', 'failed'}:
                    state = 'stopped' if self.stops[cid].is_set() else run['state']
                    if stage.pdf:
                        path = self.store.path.parent / ('report-' + cid[9:] + '.pdf')
                        if not path.exists():
                            temporary = path.with_suffix('.tmp')
                            temporary.touch(mode=0o600, exist_ok=True)
                            temporary.write_bytes(stage.report_bytes())
                            temporary.replace(path)
                    self.store.add(cid, event_id=cid + ':finished', sender='yamatai/coordinator',
                                   recipient=thread['person']['display'], text='Diagnostic investigation ' + state + '.',
                                   state=state, payload={'report': bool(stage.pdf)}, parent=request_id_from(events))

    def _diagnose(self, thread, request_id, payload):
        if thread['via'] != 'yamatai':
            raise ValueError('This installed diagnostic workflow uses Yamatai as the data custodian; select Yamatai as the sending town.')
        from wasteland.client import Client

        from .demo_phenotypes import PhenotypeStage
        from .private_variants import resource_description
        stop = threading.Event()
        self.stops[thread['id']] = stop
        raw = Client(Path(self.state.towns['yamatai'].extra['federation']['state']).expanduser())
        trace = self.store.context(thread, request_id)

        class TracedClient:
            last = request_id

            def ask(inner, town, *, operation, body, text=''):
                if stop.is_set():
                    raise ValueError('Stopped at the next operation boundary.')
                outgoing = dict(body, **{CONTEXT: child_context(trace, inner.last)})
                mid = raw.ask(town, operation=operation, text=text, body=outgoing)
                stage.trace_parents[mid] = inner.last
                stage.trace_payloads[mid] = dict(outgoing, operation=operation, text=text)
                inner.last = mid
                return mid
            def wait(inner, *args, **kwargs):
                replies = raw.wait(*args, **kwargs)
                for reply in replies:
                    stage.trace_parents[reply['id']] = reply['in_reply_to']
                    inner.last = reply['id']
                if stop.is_set():
                    raise ValueError('Stopped after the submitted operation returned; no later step will run.')
                return replies
            def call(inner, *args, **kwargs):
                return raw.call(*args, **kwargs)

        resources = payload.get('resources', [])
        if resources and (len(resources) != 1 or resources[0]['id'] != resource_description(self.state.towns['yamatai'])['id']):
            raise ValueError('The installed diagnostic workflow accepts only its synthetic patient VCF reference.')
        stage = PhenotypeStage(self.state.towns, client_factory=TracedClient, actor_name=thread['person']['display'])
        stage.trace_parents, stage.trace_payloads = {}, {}
        self.stages[thread['id']] = stage
        stage.start(payload['phenotypes'], private_case=bool(resources), human_message=payload['text'])
        # Browser identity is attribution; authenticated town identity is still Yamatai.
        stage.run['actor'] = trace['actor']
        self.store.add(thread['id'], event_id=thread['id'] + ':started', sender='yamatai/coordinator',
                       recipient=thread['person']['display'], text='Investigating the diagnostic request under the resource restrictions.',
                       state='running', payload={'workflow': 'diagnostic', 'run_id': stage.run['id']}, parent=request_id)
        stage.approve(stage.run['id'])
        stage.worker.join(400)
        self.sync(thread)

    def report(self, cid):
        self.store.get(cid)
        path = self.store.path.parent / ('report-' + cid[9:] + '.pdf')
        if not path.is_file():
            raise ValueError('No report available for this conversation')
        return path.read_bytes()


def request_id_from(events):
    return events[0]['id'] if events else None
