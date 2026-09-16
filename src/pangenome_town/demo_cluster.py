"""Visitor-owned VCF, isolated Camelot IRB credentials, real Yamatai Slurm execution."""
from __future__ import annotations

import hashlib
import shlex
import threading
import time
import uuid
from datetime import UTC, datetime

from .authority import credentials, keys
from .compute import ComputeError
from .compute.sites import RenderedJob, SshDriver, Step, load_sites
from .demo_stage import HOLDER, KINDS, SCOPE, DemoStage, StageError

IRB = 'urn:wasteland:demo:camelot:irb'
VISITOR = 'urn:wasteland:demo:visitor:data-owner'
SYNTHETIC_VCF = '''##fileformat=VCFv4.2
##contig=<ID=chr6,length=170805979>
##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tvisitor01\tvisitor02
chr6\t29940047\t.\tT\tC\t.\tPASS\t.\tGT\t0/1\t0/0
chr6\t29940056\t.\tG\tT\t.\tPASS\t.\tGT\t1/1\t0/1
chr6\t29940058\t.\tT\tA\t.\tPASS\t.\tGT\t0/0\t0/1
'''
# Deliberately no sample names or individual genotypes in the released output.
AGGREGATE = r'''BEGIN { FS="\t"; OFS="\t"; print "variant","AC","AN" }
{ ac=0; an=0; for(i=5;i<=NF;i++){ n=split($i,a,/[/|]/); for(j=1;j<=n;j++) if(a[j]=="0" || a[j]=="1"){an++;ac+=a[j]} } if(an) print $1 ":" $2 ":" $3 ":" $4,ac,an }'''


def validate_vcf(value):
    if not isinstance(value, str) or not value or len(value.encode()) > 40000:
        raise StageError('Choose a plain-text VCF of at most 40 KB for this short demonstration.')
    lines = value.splitlines()
    headers = [line for line in lines if line.startswith('#CHROM\t')]
    records = [line for line in lines if line and not line.startswith('#')]
    if not value.startswith('##fileformat=VCFv4.') or len(headers) != 1 or not records:
        raise StageError('A VCF header and at least one variant are required.')
    columns = headers[0].split('\t')
    if len(columns) < 10 or len(set(columns[9:])) != len(columns[9:]):
        raise StageError('The VCF must contain uniquely named genotype samples.')
    import re
    for row in records:
        fields = row.split('\t')
        if (len(fields) != len(columns) or not fields[1].isdigit() or int(fields[1]) < 1
                or not re.fullmatch(r'[A-Za-z0-9_.-]+', fields[0])
                or any(not re.fullmatch(r'[ACGTN]+', fields[i]) for i in (3, 4))
                or fields[8] != 'GT' or any(not re.fullmatch(r'[01.](?:[/|][01.])?', gt) for gt in fields[9:])):
            raise StageError('Demo VCFs must contain biallelic A/C/G/T/N variants and GT-only haploid or diploid calls.')
    return value.rstrip() + '\n', len(records), len(columns) - 9


class ClusterStage(DemoStage):
    def __init__(self, towns, *, storage=None, driver_factory=SshDriver):
        super().__init__(towns, storage=storage)
        self.storage = self.storage.parent / 'demo-cluster' if storage is None else self.storage
        self.driver_factory = driver_factory
        self.site = next((s for s in load_sites(towns['yamatai']) if s.name == 'ddbj' and s.enabled
                          and s.driver == 'ssh' and s.scheduler == 'slurm'), None) if 'yamatai' in towns else None

    def preflight(self):
        return {'ok': self.site is not None, 'checks': [{'label': 'Yamatai DDBJ Slurm route configured', 'ok': self.site is not None}],
                'execution': 'Real DDBJ Slurm job · isolated demonstration credentials · queue time varies'}

    def _issue(self, town, kind):
        # Reuse the same holder-bound, signed credential format as the original stage.
        doc = super()._issue(town, kind)
        if kind in ('EthicsApproval', 'DataAccessAuthorization'):
            issuer = IRB if kind == 'EthicsApproval' else VISITOR
            doc['issuer'] = issuer
            doc['credentialSubject']['taskDigest'] = credentials.task_digest(self.tasks[town])
            doc = keys.sign({k: v for k, v in doc.items() if k != 'proof'}, self.signers[issuer], f'{issuer}#key-1')
        return doc

    def _assess(self, town='yamatai', task=None):
        task = task or self.tasks[town]
        decision = super()._assess(town, task)
        # IRB consent is also bound to this exact input digest, destination and analysis.
        for doc in self.documents[town]:
            if 'EthicsApproval' in doc['type'] and doc['credentialSubject'].get('taskDigest') != credentials.task_digest(task):
                decision = {**decision, 'ok': False, 'irb_error': 'IRB approval is for a different task or file.'}
        return decision

    def start(self, vcf=None):
        with self.lock:
            if self.run:
                raise StageError('Reset this use case before starting another request.')
            if not self.preflight()['ok']:
                raise StageError('Yamatai needs an enabled DDBJ Slurm site.')
            self.vcf, variants, samples = validate_vcf(SYNTHETIC_VCF if vcf is None else vcf)
            digest = hashlib.sha256(self.vcf.encode()).hexdigest()
            self.started = time.monotonic()
            self.holder = keys.generate()
            self.signers, self.documents, self.tasks, self.policies, self.statuses = {}, {}, {}, {}, {}
            self.run = {'id': uuid.uuid4().hex, 'created': datetime.now(UTC).isoformat(), 'state': 'awaiting-approval',
                        'events': [], 'decisions': {}, 'result': None, 'job_id': None, 'scheduler_state': None,
                        'input': {'sha256': digest, 'variants': variants, 'samples': samples,
                                  'source': 'Synthetic visitor VCF' if vcf is None else 'Visitor-provided VCF'}}
            dataset = 'urn:sha256:' + digest
            self.tasks['yamatai'] = {'@id': 'urn:uuid:' + str(uuid.uuid4()), 'requestedBy': HOLDER,
                                    'taskType': 'aggregate', 'usesDataset': [dataset], 'input_sha256': digest,
                                    'site': self.site.as_dict(), 'workflow': 'bcftools biallelic allele counts',
                                    'resources': {'cpus': 1, 'mem_gb': 1, 'wall_seconds': 60}}
            from .demo_stage import PROVIDER
            rules = []
            for kind in KINDS:
                issuer = PROVIDER if kind == 'Qualification' else f'urn:wasteland:demo:yamatai:{kind}'
                self.signers[issuer] = keys.generate()
                if kind in ('EthicsApproval', 'DataAccessAuthorization'):
                    issuer = IRB if kind == 'EthicsApproval' else VISITOR
                    self.signers[issuer] = keys.generate()
                rules.append({'issuer': issuer, 'types': [kind], 'scopes': [SCOPE], 'datasets': [dataset]})
            self.policies['yamatai'] = {'task_scopes': {'aggregate': SCOPE},
                                       'requirements': [{'type': k, 'per_dataset': k == 'DataAccessAuthorization'} for k in KINDS],
                                       'issuers': rules}
            self.documents['yamatai'] = [self._issue('yamatai', k) for k in KINDS if k != 'EthicsApproval']
            self.run['decisions']['yamatai'] = self._assess()
            self._event('visitor', 'yamatai', 'My data. Your compute.',
                        f'I bring {variants} variants from {samples} samples. Please compute aggregate allele counts on DDBJ.',
                        detail={'input': self.run['input'], 'task': self.tasks['yamatai']})
            self._event('yamatai', 'visitor', 'Ownership is not ethics approval',
                        'Your dataset permission and Yamatai compute permission pass. Camelot IRB approval is still required. No file has been transferred and no job submitted.',
                        kind='refusal', detail={'decision': self.run['decisions']['yamatai'], 'compute_started': False})
            self._event('yamatai', 'camelot', 'Review this exact analysis',
                        'Approve this file, this analysis and this DDBJ destination for ten minutes. Only aggregate counts may return.',
                        kind='approval', detail={'task': self.tasks['yamatai'], 'demonstration_only': True})
            return self.snapshot()

    def approve(self, run_id):
        with self.lock:
            self._current(run_id)
            if self.run['state'] != 'awaiting-approval':
                raise StageError('This request is not awaiting IRB approval.')
            self.documents['yamatai'].append(self._issue('yamatai', 'EthicsApproval'))
            self._check()
            self.run['state'] = 'running'
            self._event('camelot', 'yamatai', 'Camelot IRB approval signed',
                        'Demonstration approval granted for this exact file and aggregate analysis. Yamatai may now transfer it and submit the job.',
                        kind='credential', detail={'credential': self.documents['yamatai'][-1], 'decision': self.run['decisions']['yamatai']})
            self.worker = threading.Thread(target=self._execute, daemon=True)
            self.worker.start()
            return self.snapshot()

    def _check(self):
        decision = self._assess()
        self.run['decisions']['yamatai'] = decision
        if (not decision['ok'] or hashlib.sha256(self.vcf.encode()).hexdigest() != self.tasks['yamatai']['input_sha256']
                or self.site.as_dict() != self.tasks['yamatai']['site']):
            raise StageError('Permissions or input changed; transfer, compute and release are withheld.')

    def _execute(self):
        driver = self.driver_factory(self.site, poll_seconds=2)
        work = self.site.workdir.rstrip('/') + '/visitor-demo-' + self.run['id']
        def progress(*, job_id, status):
            with self.lock:
                self.run.update(job_id=job_id, scheduler_state=status)
                self._event('ddbj', 'yamatai', 'Scheduler: ' + status.lower(),
                            'DDBJ reports ' + status.lower() + '. Queue time is controlled by Slurm.',
                            kind='compute', detail={'job_id': job_id, 'scheduler_state': status})
        driver.on_progress = progress
        try:
            with self.lock:
                self._check()
            uploaded = driver._ssh(f'umask 077; mkdir -p {shlex.quote(work)} && cat > {shlex.quote(work + "/input.vcf")}', input_text=self.vcf)
            if uploaded.returncode:
                raise StageError('DDBJ input transfer failed; no job submitted.')
            with self.lock:
                self._check()
                self._event('visitor', 'ddbj', 'Approved file transferred',
                            'The visitor file is staged in a private job directory. Yamatai submits a one-CPU, one-minute analysis.',
                            detail={'input_sha256': self.tasks['yamatai']['input_sha256'], 'decision': self.run['decisions']['yamatai']})
            job = RenderedJob('visitor-allele-counts', [
                Step('query', ['bcftools', 'query', '-f', '%CHROM\t%POS\t%REF\t%ALT[\t%GT]\n', work + '/input.vcf'], work + '/calls.tsv'),
                Step('aggregate', ['awk', AGGREGATE, work + '/calls.tsv'], work + '/counts.tsv'),
                Step('remove-individuals', ['rm', '-f', work + '/input.vcf', work + '/calls.tsv']),
            ], work, [{'name': 'counts.tsv', 'path': work + '/counts.tsv'}], self.tasks['yamatai']['resources'])
            result = driver.run(job, fetch_to=self.storage / self.run['id'])
            rows = []
            for line in result.outputs['counts.tsv'].read_text().splitlines()[1:]:
                variant, ac, an = line.split('\t')
                rows.append({'variant': variant, 'ac': int(ac), 'an': int(an)})
            with self.lock:
                self._check()
                if not rows:
                    raise StageError('No callable sites were returned.')
                self.run['result'] = {'rows': rows, 'sites': len(rows), 'job_id': result.backend_id,
                                      'seconds': result.seconds, 'individual_genotypes_released': False}
                self.run['state'] = 'completed'
                self._event('yamatai', 'visitor', 'Your results are ready',
                            f'{len(rows)} sites analysed on DDBJ. Permissions passed again before release. Only aggregate allele counts returned.',
                            kind='complete', detail=self.run['result'])
        except Exception as error:  # noqa: BLE001 - surface worker failures in the stage.
            with self.lock:
                self.run.update(state='failed', result=None)
                self._event('yamatai', 'visitor', 'Results withheld', str(error), kind='error')
        finally:
            # Only this run's unique staging directory, never a shared data folder.
            try:
                cleanup = driver._ssh(shlex.join(['rm', '-rf', work]))
                if cleanup.returncode:
                    raise ComputeError('Remote cleanup returned a failure.')
            except ComputeError:
                self._event('ddbj', 'yamatai', 'Staging cleanup needs attention',
                            'The remote run directory could not be removed. Inspect it before using private data.',
                            kind='error', detail={'work_dir': work})
            with self.lock:
                self.vcf = ''

    def reset(self, run_id):
        with self.lock:
            result = super().reset(run_id)
            self.vcf = ''
            return result

    def act(self, action, payload):
        if action == 'start':
            return self.start(payload.get('vcf'))
        if action in ('approve', 'reset'):
            return getattr(self, action)(payload.get('run_id'))
        raise StageError('Unknown visitor stage action.')
