"""Exercise both independent auto-runs, bundled audio, browser crypto and all-row downloads.

Submits one real tiny DDBJ job. Resets each demo explicitly and separately.
Audio runs at 4x during the automated rehearsal; normal website playback is 1x.
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
    parser.add_argument('--screenshot', type=Path)
    args = parser.parse_args()
    base = args.url.rstrip('/')
    def state(case):
        with urllib.request.urlopen(base + '/api/' + case, timeout=10) as r:
            return json.load(r)['run']
    with tempfile.TemporaryDirectory(prefix='inspection-browser-', ignore_cleanup_errors=True) as home:
        env = dict(os.environ, RODNEY_HOME=home)
        def browser(*parts):
            result = subprocess.run(['uvx', 'rodney', *parts], env=env, cwd=home, capture_output=True, text=True, timeout=90, check=False)
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)
            return result.stdout.strip()
        def wait_js(expr, timeout=100):
            until = time.monotonic() + timeout
            while time.monotonic() < until:
                if browser('js', expr) == 'true':
                    return
                time.sleep(.5)
            raise AssertionError('Browser condition timed out: ' + expr + '\n' + browser('text', '#narration-note'))
        def open_case(path, case):
            browser('open', base + path)
            browser('wait', '#autorun')
            if state(case):
                assert state(case)['state'] != 'running', 'Do not interrupt an active job.'
                browser('click', '#reset')
                wait_js('!document.querySelector("#start").disabled')
        def autorun(case):
            browser('js', '(()=>{document.querySelector("#narration-audio").defaultPlaybackRate=4;document.querySelector("#narration-audio").playbackRate=4;return true})()')
            browser('click', '#autorun')
            wait_js('document.querySelector("#narration-audio").currentTime > 0 && !document.querySelector("#narration-audio").paused', 20)
            browser('click', '#pause-autorun')
            before = state(case)
            position = float(browser('js', 'document.querySelector("audio").currentTime'))
            time.sleep(1)
            browser('assert', 'document.querySelector("audio").paused && window.demoNarrationPaused')
            assert state(case) == before
            assert abs(float(browser('js', 'document.querySelector("audio").currentTime')) - position) < .1
            browser('click', '#pause-autorun')
            wait_js('document.querySelector("#narration-note").textContent.startsWith("Auto-run complete")', 330)
            assert state(case)['state'] == 'completed'
        def inspect():
            browser('click', '#inspect-trust')
            browser('wait', '.trust-card')
            browser('click', '#verify-signatures')
            wait_js('document.querySelector("#trust-summary").textContent.includes("All signatures match")')
            browser('click', '.trust-chain button:nth-of-type(2)')
            browser('assert', 'document.querySelector("#proof-text").textContent.includes("accreditation")')
            browser('click', '#tamper-proof')
            wait_js('document.querySelector("#trust-summary").textContent.startsWith("Tampered copy rejected")')
            browser('click', '[data-close="trust-dialog"]')
        def all_rows(case):
            n = len(state(case)['result']['rows'])
            browser('click', '#view-all')
            browser('wait', '#all-variants[open]')
            browser('assert', f'document.querySelectorAll("#variant-table tbody tr").length === {n}')
            browser('js', '(()=>{window.__downloadText=null;const original=URL.createObjectURL;URL.createObjectURL=function(blob){blob.text().then(t=>window.__downloadText=t);return original.call(this,blob)};return true})()')
            browser('click', '#download-table')
            wait_js('window.__downloadText !== null')
            browser('assert', f'window.__downloadText.trim().split("\\n").length === {n+1}')
            browser('input', '#variant-filter', 'not-a-variant')
            browser('assert', 'document.querySelectorAll("#variant-table tbody tr").length === 0')
            browser('click', '#download-json')
            wait_js('window.__downloadText.startsWith("{")')
            browser('assert', f'JSON.parse(window.__downloadText).result.rows.length === {n}')
            browser('click', '[data-close="all-variants"]')
        try:
            browser('start')
            # Explicitly reset the visitor first; running case one must leave it idle.
            open_case('/demo/visitor', 'demo-cluster')
            open_case('/demo', 'demo')
            # Stopping during the introduction must prevent the future request entirely.
            browser('click', '#autorun')
            wait_js('document.querySelector("#narration-audio").currentTime > 0', 20)
            browser('click', '#stop-autorun')
            time.sleep(1)
            assert state('demo') is None and state('demo-cluster') is None
            autorun('demo')
            assert state('demo-cluster') is None
            first = state('demo')
            inspect();all_rows('demo')
            print('Cohorts: audio playback, synchronized pause/resume and stop verified; visitor case stayed idle.')
            print('Trust: browser verified Ed25519 chains and rejected a tampered scope.')
            print('Data: every aggregate row is inspectable; filtered views still download all TSV and JSON rows.')
            open_case('/demo/visitor', 'demo-cluster')
            autorun('demo-cluster')
            assert state('demo') == first
            inspect();all_rows('demo-cluster')
            second = state('demo-cluster')
            assert second['job_id'].isdigit() and second['scheduler_state'] == 'COMPLETED'
            print('Visitor: separate narrated auto-run completed on real DDBJ Slurm; cohort state unchanged.')
            # Reset the cohort page only, leaving the completed visitor run untouched.
            open_case('/demo', 'demo')
            assert state('demo-cluster') == second
            browser('click', '#narration-enabled')
            browser('click', '#autorun')
            wait_js('document.querySelector("#narration-note").textContent.startsWith("Auto-run complete")')
            assert state('demo-cluster') == second
            first = state('demo')
            browser('open', base + '/demo/visitor')
            browser('wait', '#autorun')
            browser('click', '#reset')
            wait_js('!document.querySelector("#start").disabled')
            assert state('demo') == first and state('demo-cluster') is None
            print('Reset isolation: each reset leaves the other case and its results unchanged.')
            if args.screenshot:
                browser('open', base + '/demo')
                browser('wait', '#view-all')
                browser('click', '#inspect-trust')
                browser('wait', '.trust-card')
                browser('click', '#verify-signatures')
                wait_js('document.querySelector("#trust-summary").textContent.includes("All signatures match")')
                browser('screenshot', '-w', '1920', '-h', '1080', str(args.screenshot.resolve()))
        finally:
            subprocess.run(['uvx', 'rodney', 'stop'], cwd=home, env=env, capture_output=True, timeout=20, check=False)


if __name__ == '__main__':
    main()
