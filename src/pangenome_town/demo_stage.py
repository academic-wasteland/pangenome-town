"""Hackathon stage: real local cohort queries behind signed, exact-task permissions.

Isolated demonstration authorities, never production credentials. The narrative
is deterministic; events are emitted by actual checks and subprocess completion.
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from .authority import certification, credentials, keys

REGION = 'chr6:29940000-29950000'
SCOPE = 'urn:wasteland:demo:aggregate'
RAW = 'urn:wasteland:demo:individual-genotypes'
HOLDER = 'urn:wasteland:demo:comparison-agent'
PROVIDER = 'urn:wasteland:demo:sakura-board'
KINDS = ('Qualification', 'EthicsApproval', 'ComputeAuthorization', 'DataAccessAuthorization')


class StageError(ValueError):
    pass


class DemoStage:
    def __init__(self, towns, *, region=REGION, storage=None):
        self.towns = {name: towns[name] for name in ('ubar', 'yamatai') if name in towns}
        self.region = region
        self.storage = Path(storage) if storage else (next(iter(towns.values())).state_dir / 'demo-stage')
        self.lock = threading.RLock()
        self.run = None
        self.worker = None
        self.signers = {}
        self.documents = {}
        self.tasks = {}
        self.policies = {}
        self.statuses = {}

    def preflight(self):
        checks = [{'label': 'Local bcftools', 'ok': bool(shutil.which('bcftools'))}]
        for name in ('ubar', 'yamatai'):
            town = self.towns.get(name)
            checks.append({'label': f'{name.title()} cohort and indexed VCF',
                           'ok': bool(town and town.samples and town.vcf and town.vcf.is_file()
                                      and any(Path(str(town.vcf) + ext).is_file() for ext in ('.tbi', '.csi')))})
        return {'ok': all(c['ok'] for c in checks), 'checks': checks,
                'region': 'GRCh38 ' + self.region,
                'cohorts': {name: len(t.samples) for name, t in self.towns.items()},
                'execution': 'Live local workers · public JaSaPaGe data · demonstration access policy'}

    def snapshot(self):
        with self.lock:
            return {'preflight': self.preflight(), 'run': copy.deepcopy(self.run)}

    def _save(self):
        self.storage.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.storage / (self.run['id'] + '.json')
        temp = path.with_suffix('.tmp')
        temp.touch(mode=0o600)
        temp.write_text(json.dumps(self.run, indent=2))
        temp.replace(path)

    def _event(self, sender, recipient, title, text, *, kind='message', detail=None):
        with self.lock:
            event = {'seq': len(self.run['events']) + 1, 'id': str(uuid.uuid4()),
                     'at': datetime.now(UTC).isoformat(), 'elapsed': round(time.monotonic() - self.started, 3),
                     'from': sender, 'to': recipient, 'title': title, 'text': text, 'kind': kind,
                     'detail': detail or {}}
            self.run['events'].append(event)
            self._save()
            return event

    def _issue(self, town, kind):
        issuer = PROVIDER if kind == 'Qualification' else f'urn:wasteland:demo:{town}:{kind}'
        task = self.tasks[town]
        subject = {'id': HOLDER, 'holderKey': keys.public_key_text(self.holder), 'scope': SCOPE,
                   'dataset': task['usesDataset'][0]}
        if kind in {'DataAccessAuthorization', 'ComputeAuthorization'}:
            subject.update(taskDigest=credentials.task_digest(task), audience=f'urn:wasteland:demo:{town}')
        doc = credentials.issue(issuer=issuer, issuer_key=self.signers[issuer], types=kind, subject=subject,
                               now=datetime.now(UTC), valid_days=1)
        # Exact validity is ten minutes, signed again after setting the shorter window.
        from datetime import timedelta
        doc['validUntil'] = credentials.iso(datetime.now(UTC) + timedelta(minutes=10))
        doc = keys.sign({k: v for k, v in doc.items() if k != 'proof'}, self.signers[issuer], f'{issuer}#key-1')
        self.statuses[doc['id']] = 'active'
        return doc

    def _assess(self, town, task=None):
        task = task or self.tasks[town]
        presentation = credentials.present(holder=HOLDER, holder_key=self.holder, credentials=self.documents[town],
                                             accreditations=[], task=task, audience=f'urn:wasteland:demo:{town}')
        return certification.evaluate(self.policies[town], task, presentation, audience=f'urn:wasteland:demo:{town}',
                                      anchors={i: keys.public_key_text(k) for i, k in self.signers.items()},
                                      status_checker=lambda doc: self.statuses.get(doc['id'], 'unknown'))

    def start(self):
        with self.lock:
            if self.run:
                raise StageError('Reset the stage before starting another comparison.')
            if not self.preflight()['ok']:
                raise StageError('Preflight failed. Check the local VCF, index and bcftools before presenting.')
            self.started = time.monotonic()
            self.holder = keys.generate()
            self.signers, self.documents, self.tasks, self.policies, self.statuses = {}, {}, {}, {}, {}
            self.run = {'id': uuid.uuid4().hex, 'created': datetime.now(UTC).isoformat(), 'state': 'preparing', 'events': [], 'decisions': {},
                        'cohorts': {}, 'result': None, 'export_refused': False, 'region': self.region}
            self._event('human', 'analyst', 'A scientific question',
                        'Compare variant frequencies in the Saudi and Japanese JaSaPaGe cohorts. Return aggregate counts only.',
                        detail={'region': 'GRCh38 ' + self.region, 'workflow': 'Biallelic alternate-allele frequencies',
                                'data': 'Public research data. Access restrictions and certification authorities are for this demonstration.',
                                'execution': 'Two local cohort workers; no remote job or LLM is implied by this transcript.'})
            for name, town in self.towns.items():
                dataset = f'urn:wasteland:demo:{name}:jasapage'
                task = {'@id': 'urn:uuid:' + str(uuid.uuid4()), 'requestedBy': HOLDER,
                        'taskType': 'aggregate', 'usesDataset': [dataset], 'region': self.region,
                        'samples': list(town.samples), 'executor': name, 'site': 'local-stage'}
                self.tasks[name] = task
                rules = []
                for kind in KINDS:
                    issuer = PROVIDER if kind == 'Qualification' else f'urn:wasteland:demo:{name}:{kind}'
                    self.signers.setdefault(issuer, keys.generate())
                    rules.append({'issuer': issuer, 'types': [kind], 'scopes': [SCOPE], 'datasets': [dataset]})
                self.policies[name] = {'task_scopes': {'aggregate': SCOPE, 'genotype-export': RAW},
                                      'requirements': [{'type': k, 'per_dataset': k == 'DataAccessAuthorization'} for k in KINDS],
                                      'issuers': rules}
                self.documents[name] = [self._issue(name, k) for k in KINDS if name == 'yamatai' or k != 'DataAccessAuthorization']
                self.run['cohorts'][name] = {'samples': len(town.samples), 'state': 'waiting'}
                self._event('analyst', name, f'{name.title()}: analysis request',
                            f'Please count alternate alleles for your {len(town.samples)} samples in this region. No individual genotypes may leave the worker.',
                            detail={'task': task, 'task_digest': credentials.task_digest(task)})
                decision = self._assess(name)
                self.run['decisions'][name] = decision
                self._event('provider', name, 'Qualification verified',
                            'Sakura Board certifies this analysis agent. This town accepts Sakura for aggregate analysis; Camelot is not required.',
                            kind='credential', detail={'decision': decision, 'credential': self.documents[name][0],
                                                       'policy': certification.describe(self.policies[name])})
            if self.run['decisions']['ubar']['ok'] or not self.run['decisions']['yamatai']['ok']:
                raise StageError('Unexpected credential setup; reset and inspect the demo configuration.')
            self.run['state'] = 'awaiting-approval'
            self._event('ubar', 'human', 'Ubar needs your permission',
                        'The analyst is qualified, but that does not grant Saudi data access. Approve this region and aggregate output for ten minutes?',
                        kind='approval', detail={'decision': self.run['decisions']['ubar'], 'task': self.tasks['ubar'],
                                                 'valid_minutes': 10, 'allowed_output': 'Aggregate allele counts only'})
            return self.snapshot()

    def approve(self, run_id):
        with self.lock:
            self._current(run_id)
            if self.run['state'] != 'awaiting-approval':
                raise StageError('This run is not awaiting approval.')
            doc = self._issue('ubar', 'DataAccessAuthorization')
            self.documents['ubar'].append(doc)
            for town in self.towns:
                self.run['decisions'][town] = self._assess(town)
                if not self.run['decisions'][town]['ok']:
                    raise StageError('Credentials are no longer current; reset and start a fresh run.')
            self.run['state'] = 'running'
            self._event('human', 'ubar', 'Permission signed',
                        f'Approved: this exact region, these {len(self.towns["ubar"].samples)} Saudi samples, aggregate counts only. Changing the task will invalidate this permission.',
                        kind='credential', detail={'credential': doc, 'decision': self.run['decisions']['ubar']})
            self.worker = threading.Thread(target=self._execute, daemon=True)
            self.worker.start()
            return self.snapshot()

    def _query(self, name):
        town = self.towns[name]
        with self.lock:
            check = self._assess(name)
            if not check['ok']:
                raise StageError(f'{name.title()} permission did not pass before execution.')
            self.run['cohorts'][name]['state'] = 'running'
            self._event(name, 'analyst', 'Local worker started',
                        f'{name.title()} is reading the indexed VCF and counting alleles for its own cohort.', kind='compute',
                        detail={'decision': check, 'region': self.region, 'engine': 'bcftools query', 'site': 'local-stage'})
        started = time.monotonic()
        args = ['bcftools', 'query', '-r', self.region, '-s', ','.join(town.samples),
                '-f', '%CHROM\t%POS\t%REF\t%ALT[\t%GT]\n', str(town.vcf)]
        result = subprocess.run(args, capture_output=True, text=True, timeout=30, check=True)
        rows = []
        for line in result.stdout.splitlines():
            chrom, pos, ref, alt, *genotypes = line.split('\t')
            if ',' in alt:
                continue
            called = [allele for gt in genotypes for allele in gt.replace('|', '/').split('/') if allele in ('0', '1')]
            if called:
                ac, an = called.count('1'), len(called)
                rows.append({'variant': f'{chrom}:{pos}:{ref}:{alt}', 'position': int(pos),
                             'ref': ref, 'alt': alt, 'ac': ac, 'an': an, 'af': ac / an})
        if not rows:
            raise StageError(f'{name.title()} returned no callable biallelic sites in this window.')
        with self.lock:
            fresh = self._assess(name)
            self.run['decisions'][name] = fresh
            if not fresh['ok']:
                raise StageError(f'{name.title()} approval failed before release; results withheld.')
            digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
            self.run['cohorts'][name].update(state='completed', sites=len(rows), seconds=round(time.monotonic() - started, 3),
                                            output_digest=digest)
            self._event(name, 'analyst', 'Aggregate result released',
                        f'{name.title()} counted {len(rows)} biallelic sites. Credentials were checked again before releasing aggregate counts.',
                        kind='result', detail={'decision': fresh, 'output_digest': digest,
                                              'sites': len(rows), 'individual_genotypes_released': False})
        return name, rows

    def _execute(self):
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = dict(pool.map(self._query, self.towns))
            by_town = {name: {r['variant']: r for r in rows} for name, rows in results.items()}
            shared = sorted(set(by_town['ubar']) & set(by_town['yamatai']), key=lambda v: (by_town['ubar'][v]['position'], v))
            if not shared:
                raise StageError('The cohorts have no shared callable sites; no comparison can be released.')
            paired = [{'variant': v, 'position': by_town['ubar'][v]['position'],
                       'ubar': by_town['ubar'][v], 'yamatai': by_town['yamatai'][v]} for v in shared]
            with self.lock:
                # Recheck both custodians at the final combined release boundary.
                if not all(self._assess(name)['ok'] for name in self.towns):
                    raise StageError('A permission changed; combined results withheld.')
                self.run['result'] = {'sites': len(paired), 'rows': paired,
                                      'selection': 'First shared callable biallelic sites in genomic order; not selected by effect size.',
                                      'caveat': 'These small research cohorts do not estimate population-wide differences.'}
                self.run['state'] = 'completed'
                self._event('analyst', 'human', 'Comparison ready',
                            f'The two cohorts have {len(paired)} shared callable sites. Compare allele counts below; individual-level records stayed inside the workers.',
                            kind='complete', detail={'shared_sites': len(paired), 'output': 'Aggregate counts and frequencies',
                                                     'selection': self.run['result']['selection']})
        except Exception as error:  # noqa: BLE001 - always surface worker failure in the stage.
            with self.lock:
                self.run['state'] = 'failed'
                self.run['result'] = None
                message = str(error) if isinstance(error, StageError) else f'{type(error).__name__}: local analysis failed; inspect local data and reset.'
                self._event('analyst', 'human', 'Results withheld', message, kind='error')

    def attempt_export(self, run_id):
        with self.lock:
            self._current(run_id)
            if self.run['state'] != 'completed':
                raise StageError('Complete the aggregate comparison before trying an export.')
            if self.run['export_refused']:
                return self.snapshot()
            changed = {**self.tasks['ubar'], '@id': 'urn:uuid:' + str(uuid.uuid4()), 'taskType': 'genotype-export'}
            self._event('human', 'ubar', 'A different request',
                        'Can I now download the individual Saudi genotypes using the permission I already have?',
                        detail={'new_task': changed, 'old_task_digest': credentials.task_digest(self.tasks['ubar'])})
            decision = self._assess('ubar', changed)
            if decision['ok']:
                raise StageError('Unexpected authorization: export was not executed.')
            self.run['export_refused'] = True
            self._event('ubar', 'human', 'Individual export refused',
                        'No. Your permission covers aggregate analysis of the approved task. It does not authorize individual-level export.',
                        kind='refusal', detail={'decision': decision, 'compute_started': False,
                                               'reason': 'Different scope and different task digest; no genotype export command was run.'})
            return self.snapshot()

    def _current(self, run_id):
        if not self.run or run_id != self.run['id']:
            raise StageError('This page refers to an old run. Reload before taking action.')

    def reset(self, run_id):
        with self.lock:
            if self.run:
                self._current(run_id)
                if self.run['state'] == 'running' or (self.worker and self.worker.is_alive()):
                    raise StageError('Wait for the running analysis before resetting.')
            self.run = None
            self.documents, self.statuses = {}, {}
            return self.snapshot()

    def act(self, action, payload):
        if action == 'start':
            return self.start()
        if action == 'approve':
            return self.approve(payload.get('run_id'))
        if action == 'export':
            return self.attempt_export(payload.get('run_id'))
        if action == 'reset':
            return self.reset(payload.get('run_id'))
        raise StageError('Unknown stage action.')
