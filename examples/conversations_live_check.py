"""Exercise named-person UI and a live, traced synthetic diagnostic investigation."""
import argparse
import json
import os
import subprocess
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://localhost:8393/conversations')
    parser.add_argument('--screenshot')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='conversation-browser-', ignore_cleanup_errors=True) as folder:
        env = dict(os.environ, RODNEY_HOME=folder)
        def browser(*parts):
            p = subprocess.run(['uvx', 'rodney', *parts], cwd=folder, env=env, capture_output=True, text=True, timeout=90, check=False)
            if p.returncode:
                raise AssertionError(p.stdout + p.stderr)
            return p.stdout.strip()
        def wait(expression, seconds=240):
            until = time.monotonic() + seconds
            while time.monotonic() < until:
                if browser('js', expression) == 'true':
                    return
                time.sleep(.5)
            raise AssertionError(browser('text', '#error') + '\n' + expression)
        try:
            browser('start')
            browser('open', args.url)
            wait('document.querySelector("#person").options.length>0')
            browser('assert', 'document.querySelector("#person").selectedOptions[0].textContent==="Robert Hoehndorf"')
            browser('assert', 'document.querySelector("#roles").textContent.includes("owner of ubar") && document.querySelector("#roles").textContent.includes("owner of yamatai")')
            browser('select', '#via', 'yamatai')
            browser('select', '#town', 'ubar')
            browser('select', '#agent', 'contact')
            browser('input', '#text', 'What services can your town offer? Please introduce them.')
            browser('click', '#send')
            wait('document.querySelector("#messages").textContent.includes("automated general contact")')
            browser('assert', 'document.querySelector("#messages").textContent.includes("Robert Hoehndorf via yamatai")')
            browser('click', '#new')
            browser('input', '#text', 'Help diagnose this patient using their phenotypes and available VCF. Explain the genetic evidence and respect the data restrictions.')
            browser('js', '(()=>{document.querySelector("#composer details").open=true;return true})()')
            browser('input', '#phenotypes', 'Ectopia lentis (HP:0001083)\nArachnodactyly (HP:0001166)\nAortic root aneurysm (HP:0002616)')
            browser('select', '#resources', 'urn:wasteland:resource:yamatai:synthetic-patient-vcf')
            browser('click', '#send')
            wait('!document.querySelector("#report").hidden && state.events.some(e=>e.state==="completed")')
            data = json.loads(browser('js', 'JSON.stringify(state)'))
            assert data['conversation']['person']['display'] == 'Robert Hoehndorf'
            assert any(e['state'] == 'completed' for e in data['events'])
            assert any('0.821' in json.dumps(e['payload']) for e in data['events'])
            assert all(e['sender'] not in ('human', 'human via yamatai') for e in data['events'])
            assert any(e['payload'].get('wire_body', {}).get('_conversation', {}).get('actor', {}).get('display') == 'Robert Hoehndorf' for e in data['events'])
            pdf = browser('js', '(async()=>{const r=await fetch("/api/conversations/report?id="+encodeURIComponent(cid));return r.status+":"+new TextDecoder().decode((await r.arrayBuffer()).slice(0,5))})()')
            assert pdf == '200:%PDF-'
            browser('assert', 'document.querySelectorAll("#trace li").length>6')
            browser('click', '#trace button')
            browser('js', '(()=>{document.querySelector("#messages details").open=true;return true})()')
            if args.screenshot:
                browser('js', 'window.scrollTo(0,0)')
                browser('screenshot', '-w', '1440', '-h', '1050', args.screenshot)
            conversation = data['conversation']['id']
            browser('reload')
            wait('document.querySelector("#person").options.length>0')
            browser('js', 'openThread(' + json.dumps(conversation) + ')')
            wait('!document.querySelector("#report").hidden && state.events.some(e=>e.state==="completed")')
            print('PASS: Robert’s stable person identity and ownership of Ubar/Yamatai appear in the UI.')
            print('PASS: local town contact returns a real directory response in the conversation.')
            print('PASS: task-level diagnostic request executes live with paired HPO terms and a private VCF reference.')
            print('PASS: agent messages, payloads, causal links and final PDF are inspectable; history survives reload.')
        finally:
            subprocess.run(['uvx', 'rodney', 'stop'], cwd=folder, env=env, capture_output=True, timeout=20, check=False)


if __name__ == '__main__':
    main()
