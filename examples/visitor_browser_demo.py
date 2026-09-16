"""Rehearse the visitor case; submits one real, tiny Slurm job after demo IRB approval.

Use --inspect-existing to verify a completed run without submitting another job.
The first demo's state is never reset by this script.
"""
import argparse
import json
import os
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8393')
    parser.add_argument('--inspect-existing', action='store_true')
    parser.add_argument('--screenshot', type=Path)
    args = parser.parse_args()
    base = args.url.rstrip('/')
    def snapshot():
        with urllib.request.urlopen(base + '/api/demo-cluster', timeout=30) as response:
            return json.load(response)['run']
    with tempfile.TemporaryDirectory(prefix='visitor-browser-') as home:
        env = dict(os.environ, RODNEY_HOME=home)
        def browser(*parts):
            return subprocess.run(['uvx', 'rodney', *parts], cwd=home, env=env,
                                  capture_output=True, text=True, timeout=90, check=True).stdout.strip()
        try:
            browser('start')
            browser('open', base + '/demo/visitor')
            browser('wait', '#gates button')
            if not args.inspect_existing:
                initial = snapshot()
                if initial:
                    assert initial['state'] != 'running', 'Wait for the active job.'
                    browser('click', '#reset')
                browser('wait', '#start:not([disabled])')
                browser('click', '#start')
                browser('wait', '#approve:not([disabled])')
                r = snapshot()
                assert not r['job_id'] and not r['decisions']['yamatai']['ok']
                browser('click', '#catchup')
                browser('click', '#gates button:last-child')
                browser('assert', 'document.querySelector("#inspector").open && !document.querySelector("#inspector details").open')
                browser('click', '#close')
                print('Browser: IRB gate blocks file transfer and submission; evidence is inspectable.')
                browser('click', '#approve')
                deadline = time.monotonic() + 400
                while time.monotonic() < deadline:
                    r = snapshot()
                    if r['state'] != 'running':
                        break
                    time.sleep(2)
                assert r['state'] == 'completed', r['events'][-1]
            r = snapshot()
            assert r['state'] == 'completed', 'A completed live run is required.'
            assert r['job_id'].isdigit() and r['scheduler_state'] == 'COMPLETED'
            assert r['result']['rows'] == [
                {'variant': 'chr6:29940047:T:C', 'ac': 1, 'an': 4},
                {'variant': 'chr6:29940056:G:T', 'ac': 3, 'an': 4},
                {'variant': 'chr6:29940058:T:A', 'ac': 1, 'an': 4},
            ]
            browser('wait', '#result:not([hidden])')
            browser('click', '#catchup')
            browser('assert', 'document.querySelectorAll("#rows tr").length === 3 && document.querySelectorAll("#gates .pass").length === 4')
            browser('click', '[data-node="ddbj"]')
            browser('assert', 'JSON.parse(document.querySelector("#detail-json").textContent).scheduler_state === "COMPLETED"')
            browser('click', '#close')
            print('Browser: real Slurm completion and three aggregate allele counts verified.')
            browser('click', 'nav a[href="/demo"]')
            browser('wait', '#flow')
            browser('click', 'nav a[href="/demo/visitor"]')
            browser('wait', '#result:not([hidden])')
            browser('assert', 'document.querySelectorAll("#routes button").length >= 7')
            print('Browser: switching use cases preserves results and inspectable message routes.')
            if args.screenshot:
                browser('screenshot', '-w', '1920', '-h', '1080', str(args.screenshot.resolve()))
        finally:
            subprocess.run(['uvx', 'rodney', 'stop'], cwd=home, env=env, capture_output=True, timeout=20, check=False)


if __name__ == '__main__':
    main()
