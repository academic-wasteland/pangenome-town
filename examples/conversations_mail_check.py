"""Verify a real named-person message reaches a local agent and its reply is threaded."""
import argparse
import json
import re
import time
import urllib.request

BASE = 'http://localhost:8393'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--existing', action='store_true', help='verify the most recent recorded acknowledgement test instead of sending another')
    args = parser.parse_args()
    page = urllib.request.urlopen(BASE + '/conversations', timeout=10).read().decode()
    token = re.search(r"'X-Cockpit-Token':'([^']+)'", page)[1]
    state = json.load(urllib.request.urlopen(BASE + '/api/conversations', timeout=30))
    person = next(p for p in state['people'] if p['display'] == 'Robert Hoehndorf')
    if args.existing:
        cid = next(t['id'] for t in state['threads'] if t['resident'] == 'themis' and t['person_id'] == person['id'] and t['title'].startswith('Robert Hoehndorf is testing the new Conversations interface.'))
    else:
        request = urllib.request.Request(BASE + '/api/conversations/send', data=json.dumps({
            'person_id': person['id'], 'via': 'ubar', 'to': 'ubar', 'resident': 'themis',
            'text': 'Robert Hoehndorf is testing the new Conversations interface. Please reply with a short acknowledgement using gc mail reply. Do not run an analysis or contact other agents.'
        }).encode(), headers={'Content-Type': 'application/json', 'X-Cockpit-Token': token})
        cid = json.load(urllib.request.urlopen(request, timeout=30))['conversation']
    until = time.monotonic() + 300
    while time.monotonic() < until:
        data = json.load(urllib.request.urlopen(BASE + '/api/conversations?id=' + cid, timeout=30))
        assert not any(e['state'] == 'failed' for e in data['events']), data['events']
        if any(e['state'] == 'replied' for e in data['events']):
            assert data['events'][0]['sender'] == 'Robert Hoehndorf via ubar'
            assert any(e['state'] == 'delivered-to-resident' for e in data['events'])
            assert any(e['state'] == 'replied' and e['sender'].startswith('ubar/') for e in data['events'])
            print('PASS: named person → local Themis mailbox → real agent reply in the same conversation.')
            return
        time.sleep(2)
    raise AssertionError('The agent did not reply before the test deadline; inspect the durable conversation.')


if __name__ == '__main__':
    main()
