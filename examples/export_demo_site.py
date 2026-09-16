"""Capture real local demo runs and build a self-contained, explicitly labelled public replay.

Requires the loopback stage service. Capturing resets each case separately and
submits one synthetic DDBJ job. No private keys or cockpit tokens are exported.
"""
import argparse
import copy
import json
import re
import shutil
import time
import urllib.request
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'src/pangenome_town'


def capture(base, case):
    def get(path):
        with urllib.request.urlopen(base + path, timeout=30) as response:
            return json.load(response)
    with urllib.request.urlopen(base + '/demo', timeout=10) as response:
        token = re.search(r"const TOKEN='([^']+)'", response.read().decode())[1]
    api = '/api/' + case
    def act(action, body):
        request = urllib.request.Request(base + api + '/' + action, data=json.dumps(body).encode(),
                                         headers={'Content-Type': 'application/json', 'X-Cockpit-Token': token})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    initial = get(api)
    if initial['run']:
        if initial['run']['state'] == 'running':
            raise RuntimeError('An active run must finish before recording.')
        act('reset', {'run_id': initial['run']['id']})
    started = act('start', {})
    recording = {'start': copy.deepcopy(started), 'trust_start': get(api + '/trust')}
    run_id = started['run']['id']
    act('approve', {'run_id': run_id})
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        current = get(api)
        if current['run']['state'] != 'running':
            break
        time.sleep(1)
    if current['run']['state'] != 'completed':
        raise RuntimeError(current['run']['events'][-1]['text'])
    recording.update(completed=current, trust_completed=get(api + '/trust'))
    if case == 'demo':
        recording['export'] = act('export', {'run_id': run_id})
    # This is historical, signed evidence, not fresh approval when someone replays it.
    recording['recorded_at'] = recording['trust_completed']['checked_at']
    assert all(r['decision']['ok'] for r in recording['trust_completed']['receivers'])
    return recording


def build(output, records, prefix):
    output.mkdir(parents=True, exist_ok=True)
    assets = output / 'assets'
    assets.mkdir(exist_ok=True)
    shutil.copytree(SOURCE / 'demo_audio', assets / 'audio', dirs_exist_ok=True)
    # Only browser assets; keep internal build instructions out of the web root.
    (assets / 'audio' / 'README.md').unlink(missing_ok=True)
    shutil.copy2(SOURCE / 'demo_vendor/nacl-fast.min.js', assets / 'nacl-fast.min.js')
    shutil.copy2(SOURCE / 'demo_vendor/LICENSE', assets / 'tweetnacl-LICENSE.txt')
    shutil.copy2(SOURCE / 'demo_replay.js', assets / 'replay.js')
    (assets / 'recordings.json').write_text(json.dumps(records))
    for source, dest in [('demo_inspection.js', 'inspection.js'), ('demo_autorun.js', 'autorun.js')]:
        content = (SOURCE / source).read_text().replace("location.pathname === '/demo/visitor'", "location.pathname.endsWith('/visitor.html')")
        content = content.replace("'/demo-audio/", repr(prefix + 'assets/audio/')[0:-1])
        if source == 'demo_inspection.js':
            content = content.replace('Policy rechecked now.', 'Recorded policy evaluation; not a current approval.')
            content = content.replace('permission accepted now', 'permission accepted in recorded run')
            content = content.replace('Not currently authorized', 'Not authorized at this recorded step')
            content = content.replace('Real Ed25519 signatures, isolated demo authorities.', 'Recorded real Ed25519 signatures, isolated demo authorities. Status and acceptance are historical; permissions may now be expired.')
        else:
            content = content.replace('grants its demo approvals', 'replays its recorded demo approvals')
        (assets / dest).write_text(content)
    banner = '<aside style="padding:10px 24px;background:#292c1c;color:#ebce91;font:14px/1.5 sans-serif"><strong>Recorded real runs · interactive playback.</strong> Audio and controls replay captured actions; no new approval, upload or cluster job is performed. Replay timing is shortened; event timestamps are from the real run. Signatures and aggregate results are inspectable. Each case runs and resets independently.</aside>'
    for source, dest in [('demo_stage.html', 'index.html'), ('demo_cluster.html', 'visitor.html')]:
        page = (SOURCE / source).read_text().replace('__COCKPIT_TOKEN__', 'public-replay-no-credentials')
        page = page.replace('<body>', '<body>' + banner)
        page = page.replace(' / live stage', ' / recorded demos')
        page = re.sub(r'<a href="/"[^>]*>Open cockpit</a>', '', page)
        page = page.replace('href="/demo/visitor"', f'href="{prefix}visitor.html"')
        page = page.replace('href="/demo"', f'href="{prefix}"').replace('href="/"', f'href="{prefix}"')
        page = page.replace('/demo-assets/', prefix + 'assets/')
        page = page.replace('<script>\n', f'<script>window.DEMO_REPLAY_BASE={json.dumps(prefix)};</script><script src="{prefix}assets/replay.js"></script><script>\n', 1)
        if source == 'demo_cluster.html':
            page = page.replace('<option value="own">Choose my own demo VCF</option>', '')
            page = page.replace('No private participant data in the bundled example.', 'Recorded synthetic visitor VCF. Uploads are available only in the live local version.')
            page = page.replace('Real Slurm execution · demonstration IRB credentials', 'Recorded real Slurm execution · demonstration IRB credentials')
            page = page.replace('Scheduler states are read from DDBJ, not scripted.', 'Scheduler observations were recorded from the real DDBJ run.')
        (output / dest).write_text(page)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8393')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prefix', default='/academic-wasteland/')
    parser.add_argument('--recordings', type=Path, help='Reuse captured recordings; do not run new compute.')
    args = parser.parse_args()
    if args.recordings:
        records = json.loads(args.recordings.read_text())
    else:
        records = {case: capture(args.url.rstrip('/'), case) for case in ('demo', 'demo-cluster')}
    build(args.output, records, args.prefix)
    print('Built public replay: signed real-run evidence, all aggregate rows, bundled audio; no live control endpoint.')


if __name__ == '__main__':
    main()
