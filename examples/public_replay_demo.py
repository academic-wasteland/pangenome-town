"""Verify the public static replay in a private browser, without submitting any compute."""
import argparse
import os
import subprocess
import tempfile
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='https://leechuck.de/academic-wasteland/')
    parser.add_argument('--screenshot', type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='public-replay-browser-', ignore_cleanup_errors=True) as home:
        env = dict(os.environ, RODNEY_HOME=home)
        def browser(*parts):
            result = subprocess.run(['uvx', 'rodney', *parts], cwd=home, env=env, capture_output=True,
                                    text=True, timeout=90, check=False)
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)
            return result.stdout.strip()
        def wait(expr):
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if browser('js', expr) == 'true':
                    return
                time.sleep(.3)
            raise AssertionError(expr + '\n' + browser('text', '#narration-note'))
        def run():
            browser('wait', '#autorun')
            browser('js', '(()=>{let a=document.querySelector("audio");a.defaultPlaybackRate=4;a.playbackRate=4;return true})()')
            browser('click', '#autorun')
            wait('document.querySelector("audio").currentTime>0 && !document.querySelector("audio").paused')
            browser('click', '#pause-autorun')
            pos=float(browser('js', 'document.querySelector("audio").currentTime'))
            time.sleep(.5)
            assert abs(float(browser('js', 'document.querySelector("audio").currentTime'))-pos)<.1
            browser('assert', 'window.demoNarrationPaused')
            browser('click', '#pause-autorun')
            wait('document.querySelector("#narration-note").textContent.startsWith("Auto-run complete")')
        def inspect(rows):
            browser('click', '#inspect-trust')
            browser('wait', '.trust-card')
            browser('click', '#verify-signatures')
            wait('document.querySelector("#trust-summary").textContent.includes("All signatures match")')
            browser('click', '#tamper-proof')
            wait('document.querySelector("#trust-summary").textContent.startsWith("Tampered copy rejected")')
            browser('click', '[data-close="trust-dialog"]')
            browser('click', '#view-all')
            browser('wait', '#all-variants[open]')
            browser('assert', f'document.querySelectorAll("#variant-table tbody tr").length==={rows}')
            browser('js', '(()=>{window.__download=null;const original=URL.createObjectURL;URL.createObjectURL=function(b){b.text().then(t=>window.__download=t);return original.call(this,b)};return true})()')
            browser('click', '#download-table')
            wait('window.__download!==null')
            browser('assert', f'window.__download.trim().split("\\n").length==={rows+1}')
            browser('click', '[data-close="all-variants"]')
        try:
            browser('start')
            browser('open', args.url)
            browser('assert', 'document.body.textContent.includes("Recorded real runs")')
            run();inspect(510)
            browser('assert', 'sessionStorage.getItem("wasteland-public-replay-v1:demo-cluster")===null')
            browser('js', 'window.__first=sessionStorage.getItem("wasteland-public-replay-v1:demo")')
            first=browser('js', 'sessionStorage.getItem("wasteland-public-replay-v1:demo")')
            print('Public cohorts: narrated playback, pause/resume, signature verification and all 510 rows passed.', flush=True)
            browser('click', 'nav a[href$="visitor.html"]')
            run();inspect(3)
            assert browser('js', 'sessionStorage.getItem("wasteland-public-replay-v1:demo")')==first
            browser('click', '#reset')
            wait('!document.querySelector("#start").disabled')
            assert browser('js', 'sessionStorage.getItem("wasteland-public-replay-v1:demo")')==first
            print('Public visitor: independent playback and reset; three real recorded aggregate rows passed.', flush=True)
            if args.screenshot:
                browser('open', args.url)
                browser('wait', '#inspect-trust')
                browser('click', '#inspect-trust')
                browser('wait', '.trust-card')
                browser('click', '#verify-signatures')
                wait('document.querySelector("#trust-summary").textContent.includes("All signatures match")')
                browser('screenshot', '-w', '1920', '-h', '1080', str(args.screenshot.resolve()))
        finally:
            subprocess.run(['uvx', 'rodney', 'stop'], cwd=home, env=env, capture_output=True, timeout=20, check=False)


if __name__ == '__main__':
    main()
