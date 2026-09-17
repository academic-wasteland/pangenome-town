"""Exercise the public INDIGENA demo with real relay inference and narrated playback."""
import argparse
import base64
import hashlib
from pathlib import Path
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
    parser.add_argument('--pdf')
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
            assert run['benchmark']['gene'] == 'FBN1' and 1 <= run['benchmark']['rank'] <= 5
            assert len(run['result']['resolution']['terms']) == 3
            assert run['variant_benchmark']['recovered'] is True
            assert run['variant_benchmark']['revel_rank'] == 2
            browser('assert', 'document.querySelector("#variant-truth").textContent.includes("REVEL-only rank 2")')
            browser('js', '(()=>{document.querySelector("#variant-order").value="revel_rank";document.querySelector("#variant-order").dispatchEvent(new Event("change"));return true})()')
            browser('assert', 'document.querySelector("#variant-rows tr").dataset.gene==="ADAMTSL4"')
            browser('js', '(()=>{document.querySelector("#variant-order").value="combined_rank";document.querySelector("#variant-order").dispatchEvent(new Event("change"));return true})()')
            browser('assert', 'document.querySelector("#variant-rows tr").dataset.gene==="FBN1"')
            messages=[e for e in run['events'] if e['detail'].get('message_type')]
            assert len(messages)==6
            assert messages[0]['text']==run['human_message']
            assert all(e['text']==e['detail']['body']['text'] for e in messages if e['detail']['message_type']=='received')
            browser('assert', 'document.querySelectorAll("#feed .message").length===6')
            assert run['variants']['retained'][0]['gene'] == 'FBN1'
            assert run['interpretation']['classification'] == 'Likely pathogenic'
            assert {c['code'] for c in run['interpretation']['criteria'] if c['met']} == {'PS4', 'PM2_Supporting', 'PP2', 'PP3'}
            disclosures = [e['detail']['body'] for e in run['events'] if e['title'] == 'variant-interpretation']
            assert len(disclosures) == 1 and set(disclosures[0]) == {'resident', 'variant', 'phenotypes'}
            encoded = browser('js', f'(async()=>{{const r=await fetch({json.dumps(prefix+"/api/demo-phenotypes/report.pdf")});if(!r.ok)throw Error("PDF unavailable");const b=new Uint8Array(await r.arrayBuffer());return btoa(Array.from(b,x=>String.fromCharCode(x)).join(""))}})()')
            pdf = base64.b64decode(encoded)
            assert pdf.startswith(b'%PDF-') and hashlib.sha256(pdf).hexdigest() == run['report_sha256']
            if args.pdf:
                Path(args.pdf).write_bytes(pdf)
            browser('assert', 'document.querySelectorAll("#rows tr.expected").length===1')
            assert browser('js', api('demo')) == 'null'
            assert browser('js', api('demo-cluster')) == 'null'
            browser('js', '(()=>{window.saved=null;const original=URL.createObjectURL;URL.createObjectURL=b=>{b.text().then(t=>window.saved=t);return original(b)};return true})()')
            browser('click', '#download')
            wait('window.saved!==null')
            browser('assert', 'window.saved.includes("CC BY 4.0") && window.saved.trim().split("\\n").length===23')
            if args.screenshot:
                browser('js', 'window.scrollTo(0,0)')
                browser('screenshot', '-w', '1440', '-h', '1100', args.screenshot)
            browser('click', '#reset')
            wait('document.querySelectorAll("#rows tr").length===0')
            assert browser('js', api('demo-phenotypes')) == 'null'
            # Manual execution and a second edited query must work without narration or reset.
            browser('open', args.url)
            browser('wait', '#run')
            browser('js', 'document.querySelector("#human-message").value="Please delegate this synthetic Marfan investigation. Keep the VCF at Yamatai and compare REVEL alone with phenotype-informed ranking."')
            browser('js', 'document.querySelector("#terms").value="HPO: Ectopia lentis; HPO: Arachnodactyly; HPO: Aortic root aneurysm"')
            browser('click', '#run')
            wait('document.querySelectorAll("#rows tr").length===20 && !document.querySelector("#run").disabled')
            manual = json.loads(browser('js', api('demo-phenotypes')))
            assert manual['benchmark']['rank'] <= 5
            assert manual['human_message'].startswith('Please delegate this synthetic Marfan investigation.')
            intake_message=next(e for e in manual['events'] if e['title']=='message' and e['detail'].get('message_type')=='sent')
            assert intake_message['text']==manual['human_message']==intake_message['detail']['wire_body']['text']
            assert manual['result']['query']['phenotypes'] == ['HP:0001083', 'HP:0001166', 'HP:0002616']
            browser('assert', 'document.querySelector("audio").currentTime===0 && !document.querySelector("audio").getAttribute("src")')
            browser('js', 'document.querySelector("#private-case").checked=false')
            browser('js', 'document.querySelector("#terms").value="arachnodactyly"')
            browser('click', '#run')
            wait('document.querySelector("#error").textContent.includes("Ambiguous phenotype") && !document.querySelector("#run").disabled')
            browser('assert', 'document.querySelectorAll("#rows tr").length===0')
            browser('js', 'document.querySelector("#terms").value="MP: arachnodactyly"')
            browser('click', '#run')
            wait('document.querySelectorAll("#rows tr").length===20 && !document.querySelector("#run").disabled')
            rerun = json.loads(browser('js', api('demo-phenotypes')))
            assert rerun['id'] != manual['id'] and rerun['result']['query']['phenotypes'] == ['MP:0006296']
            assert 'benchmark' not in rerun
            browser('assert', 'document.querySelector("audio").currentTime===0')
            assert 'interpretation' not in rerun
            print('PASS: private VCF filtering, FBN1 variant rank 1, allowlisted disclosure, ACMG evidence and PDF integrity.')
            print('PASS: manual phenotype edits, two distinct live runs without audio or reset.')
            print('PASS: live relay inference, FAIR model match, 20 gene candidates, narration/pause/resume, licensed download and independent reset.')
        finally:
            subprocess.run(['uvx', 'rodney', 'stop'], cwd=home, env=env, capture_output=True, timeout=20, check=False)


if __name__ == '__main__':
    main()
