"""Exercise the public INDIGENA demo with real relay inference and narrated playback."""
import argparse
import json
import os
import subprocess
import tempfile
import time
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='https://leechuck.de/wasteland-live/demo/phenotypes')
    parser.add_argument('--screenshot')
    args = parser.parse_args()
    prefix = urlsplit(args.url).path.partition('/demo')[0]
    with tempfile.TemporaryDirectory(prefix='phenotype-browser-', ignore_cleanup_errors=True) as home:
        env = dict(os.environ, RODNEY_HOME=home)
        def browser(*parts):
            p = subprocess.run(['uvx', 'rodney', *parts], cwd=home, env=env, text=True,
                               capture_output=True, timeout=90, check=False)
            if p.returncode:
                raise AssertionError(p.stdout + p.stderr)
            return p.stdout.strip()
        def wait(expr):
            until = time.monotonic() + 150
            while time.monotonic() < until:
                if browser('js', expr) == 'true':
                    return
                time.sleep(.4)
            raise AssertionError(expr + '\n' + browser('text', '#error'))
        def api(case):
            return f'(async()=>(await (await fetch({json.dumps(prefix+"/api/"+case)})).json()).run)()'
        try:
            browser('start')
            browser('open', args.url)
            browser('wait', '#autorun')
            wait('!document.querySelector("#autorun").disabled')
            browser('js', '(()=>{document.querySelector("audio").defaultPlaybackRate=4;document.querySelector("audio").playbackRate=4;return true})()')
            browser('click', '#autorun')
            wait('document.querySelector("audio").currentTime>0 && !document.querySelector("audio").paused')
            browser('click', '#pause')
            pos = float(browser('js', 'document.querySelector("audio").currentTime'))
            time.sleep(.5)
            assert abs(float(browser('js', 'document.querySelector("audio").currentTime')) - pos) < .1
            browser('click', '#pause')
            wait('document.querySelector("#note").textContent.startsWith("Demo complete")')
            browser('assert', 'document.querySelectorAll("#rows tr").length===20')
            run = json.loads(browser('js', api('demo-phenotypes')))
            assert run['state'] == 'completed' and run['result']['candidate_count'] >= 20
            assert run['fair']['record']['version'] == 'sha256:' + run['result']['checkpoint_sha256']
            assert run['license'] == 'https://creativecommons.org/licenses/by/4.0/'
            assert any(r['human_orthologue'] for r in run['result']['results'])
            assert browser('js', api('demo')) == 'null'
            assert browser('js', api('demo-cluster')) == 'null'
            browser('js', '(()=>{window.saved=null;const original=URL.createObjectURL;URL.createObjectURL=b=>{b.text().then(t=>window.saved=t);return original(b)};return true})()')
            browser('click', '#download')
            wait('window.saved!==null')
            browser('assert', 'window.saved.includes("CC BY 4.0") && window.saved.trim().split("\\n").length===23')
            if args.screenshot:
                browser('screenshot', '-w', '1440', '-h', '1100', args.screenshot)
            browser('click', '#reset')
            wait('document.querySelectorAll("#rows tr").length===0')
            assert browser('js', api('demo-phenotypes')) == 'null'
            # Manual execution and a second edited query must work without narration or reset.
            browser('open', args.url)
            browser('wait', '#run')
            browser('js', 'document.querySelector("#terms").value="HP:0001250, HP:0001249"')
            browser('click', '#run')
            wait('document.querySelectorAll("#rows tr").length===20 && !document.querySelector("#run").disabled')
            manual = json.loads(browser('js', api('demo-phenotypes')))
            assert manual['phenotypes'] == ['HP:0001249', 'HP:0001250']
            browser('assert', 'document.querySelector("audio").currentTime===0 && !document.querySelector("audio").getAttribute("src")')
            browser('js', 'document.querySelector("#terms").value="HP:0000252"')
            browser('click', '#run')
            wait('document.querySelectorAll("#rows tr").length===20 && !document.querySelector("#run").disabled')
            rerun = json.loads(browser('js', api('demo-phenotypes')))
            assert rerun['id'] != manual['id'] and rerun['phenotypes'] == ['HP:0000252']
            browser('assert', 'document.querySelector("audio").currentTime===0')
            print('PASS: manual phenotype edits, two distinct live runs without audio or reset.')
            print('PASS: live relay inference, FAIR model match, 20 gene candidates, narration/pause/resume, licensed download and independent reset.')
        finally:
            subprocess.run(['uvx', 'rodney', 'stop'], cwd=home, env=env, capture_output=True, timeout=20, check=False)


if __name__ == '__main__':
    main()
