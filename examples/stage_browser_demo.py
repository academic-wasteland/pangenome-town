"""Rehearse the three-click web demo against a running local stage server.

Resets that server's demonstration run. Creates and removes a private browser.
Requires uvx/rodney and the real local JaSaPaGe data, not a synthetic response.
"""
import argparse
import json
import os
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8393')
    parser.add_argument('--screenshot', type=Path)
    args = parser.parse_args()
    base = args.url.rstrip('/')
    with urllib.request.urlopen(base + '/demo', timeout=5) as response:
        html = response.read().decode()
    token = re.search(r"const TOKEN='([^']+)'", html).group(1)
    with urllib.request.urlopen(base + '/api/demo', timeout=5) as response:
        initial = json.load(response)
    assert initial['preflight']['ok'], 'Stage preflight failed; load local data first.'
    if initial['run']:
        assert initial['run']['state'] != 'running', 'A run is in progress; do not interrupt it.'
        request = urllib.request.Request(base + '/api/demo/reset',
                    data=json.dumps({'run_id': initial['run']['id']}).encode(),
                    headers={'Content-Type': 'application/json', 'X-Cockpit-Token': token})
        with urllib.request.urlopen(request, timeout=5):
            pass
    with tempfile.TemporaryDirectory(prefix='wasteland-stage-browser-') as browser_home:
        env = dict(os.environ, RODNEY_HOME=browser_home)
        def rodney(*parts):
            result = subprocess.run(['uvx', 'rodney', *parts], env=env, cwd=browser_home,
                                    capture_output=True, text=True, timeout=90, check=True)
            return result.stdout.strip()
        try:
            rodney('start')
            rodney('open', base + '/demo')
            rodney('wait', '#start:not([disabled])')
            rodney('click', '#start')
            rodney('wait', '#approval-note:not([hidden])')
            rodney('assert', 'document.querySelector("#approve").disabled === false')
            rodney('wait', '#feed .event')
            rodney('click', '#catchup')
            rodney('wait', '#feed .event:nth-child(6)')
            rodney('click', '[data-gate="ubar"][data-type="DataAccessAuthorization"]')
            rodney('assert', 'document.querySelector("#inspector").open && document.querySelector("#detail-summary").textContent.includes("Not authorized")')
            rodney('assert', '!document.querySelector("#inspector details").open')
            rodney('click', '#inspector summary')
            rodney('assert', 'document.querySelector("#detail-json").textContent.includes("DataAccessAuthorization")')
            rodney('click', '#close')
            print('Browser: missing Saudi permission explained; technical evidence opens on demand.')
            rodney('click', '#approve')
            rodney('wait', '#results:not([hidden])')
            rodney('assert', 'document.querySelectorAll("#chart .variant").length === 5')
            rodney('click', '#catchup')
            rodney('wait', '#feed .event:nth-child(12)')
            rodney('assert', 'document.querySelectorAll("#edges path").length > 5')
            print('Browser: signed approval starts two real cohort queries; comparison chart appears.')
            rodney('click', '#export')
            rodney('wait', '#boundary:not([hidden])')
            rodney('click', '#catchup')
            rodney('wait', '#feed .event:nth-child(14)')
            rodney('click', '#inspect-refusal')
            rodney('assert', 'JSON.parse(document.querySelector("#detail-json").textContent).detail.compute_started === false')
            rodney('click', '#close')
            print('Browser: individual export refused before computation; aggregate result retained.')
            rodney('reload')
            rodney('wait', '#feed .event:nth-child(14)')
            rodney('assert', '!document.querySelector("#boundary").hidden && document.querySelectorAll("#edges path").length > 5')
            print('Browser: refresh restores the conversation, accumulated routes and result.')
            if args.screenshot:
                rodney('screenshot', '-w', '1920', '-h', '1080', str(args.screenshot.resolve()))
        finally:
            subprocess.run(['uvx', 'rodney', 'stop'], env=env, cwd=browser_home,
                           capture_output=True, timeout=20, check=False)
    with urllib.request.urlopen(base + '/api/demo', timeout=5) as response:
        run = json.load(response)['run']
    assert run['state'] == 'completed' and run['export_refused']
    print(f"Live result: {run['result']['sites']} shared biallelic sites; {len(run['events'])} inspectable events.")


if __name__ == '__main__':
    main()
