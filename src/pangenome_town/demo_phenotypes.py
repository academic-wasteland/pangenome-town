"""A fictional rare-disease case invokes Ubar's FAIR-described INDIGENA service."""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .demo_stage import DemoStage, StageError
from .phenotypes import validate

RECORD = 'urn:wasteland:fair:ubar:indigena'
CATALOGUE = 'https://leechuck.de/wasteland-fair/'
REUSE = 'Demo metadata and generated result tables: CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/), credit Academic Wasteland. Upstream model and source-data rights are separate; no additional rights over them are granted.'
DEFAULT_TERMS = ['HPO: Ectopia lentis', 'HPO: Arachnodactyly', 'HPO: Aortic root aneurysm']
DEFAULT_MESSAGE = 'Please help investigate a synthetic rare-disease case using the phenotype labels below. Find candidate genes. If the private-VCF workflow is enabled, keep the VCF at Yamatai, compare variant-only and phenotype-informed rankings locally, and ask an appropriate agent for an ACMG report on the selected allele.'
EXAMPLE_IDS = {'HP:0001083', 'HP:0001166', 'HP:0002616'}


class PhenotypeStage(DemoStage):
    def __init__(self, towns, *, storage=None, client_factory=None, catalogue_reader=None):
        super().__init__(towns, storage=storage)
        self.client_factory = client_factory
        self.catalogue_reader = catalogue_reader
        self.requester = towns.get('yamatai')

    def preflight(self):
        ready = bool(self.requester and self.requester.extra.get('federation', {}).get('state'))
        return {'ok': ready, 'execution': 'Live authenticated Yamatai → Ubar relay request; locally trained INDIGENA',
                'checks': [{'label': 'Registered requester town', 'ok': ready}]}

    def start(self, phenotypes=None, private_case=False, human_message=None):
        if type(private_case) is not bool:
            raise StageError("private_case must be boolean")
        human_message = DEFAULT_MESSAGE if human_message is None else human_message
        if not isinstance(human_message, str) or not 1 <= len(human_message.strip()) <= 1200:
            raise StageError('Write a human request of 1–1200 characters.')
        with self.lock:
            if self.run:
                raise StageError('Reset this case before starting another query.')
            query = validate({'phenotypes': DEFAULT_TERMS if phenotypes is None else phenotypes, 'limit': 20})
            self.pdf = None
            self.started = time.monotonic()
            self.run = {'id': uuid.uuid4().hex, 'created': datetime.now(UTC).isoformat(), 'state': 'awaiting-run',
                        'events': [], 'human_message': human_message.strip(), 'private_case': private_case, 'phenotypes': query['phenotypes'], 'result': None, 'fair': None,
                        'reuse': REUSE, 'license': 'https://creativecommons.org/licenses/by/4.0/',
                        'attribution': 'Academic Wasteland',
                        'fictional_case': 'Synthetic patient VCF retained by Yamatai; phenotype ranking and optional single-variant interpretation. No real patient data.',
                        'access': 'Registered town identity. This public service does not require an IRB credential; no new approval is issued.'}
            self._event('human', 'yamatai/coordinator', 'Human request · local browser',
                        self.run['human_message'],
                        detail={'transport': 'local browser to Yamatai', 'payload': {'phenotypes': self.run['phenotypes'], 'private_case': private_case}})
            return self.snapshot()

    def approve(self, run_id):
        with self.lock:
            self._current(run_id)
            if self.run['state'] != 'awaiting-run':
                raise StageError('This query has already been submitted.')
            self.run['state'] = 'running'
            self.worker = threading.Thread(target=self._execute, daemon=True)
            self.worker.start()
            return self.snapshot()

    def _catalogue(self):
        if self.catalogue_reader:
            return self.catalogue_reader()
        url = CATALOGUE + 'api/record?id=' + urllib.parse.quote(RECORD)
        with urllib.request.urlopen(url, timeout=15) as response:
            return json.loads(response.read(1024 * 1024))

    def _execute(self):
        try:
            if self.client_factory:
                client = self.client_factory()
            else:
                from wasteland.client import Client
                client = Client(Path(self.requester.extra['federation']['state']).expanduser())
            delegation = self._request(client, 'ubar', 'message',
                {'resident': 'contact', 'workflow': 'phenotype-research', 'phenotypes': self.run['phenotypes'], 'private_case': self.run['private_case']},
                text=self.run['human_message'], sender='human via yamatai', recipient='ubar/contact')
            plan = delegation.get('plan', [])
            expected = [('ubar', 'phenomancer', 'phenotype-search')]
            if self.run['private_case']:
                expected += [('yamatai', 'coordinator', 'private-variant-rank'), ('ubar', 'themis', 'variant-interpretation')]
            if [(p.get('town'), p.get('resident'), p.get('operation')) for p in plan] != expected or plan[0].get('record') != RECORD:
                raise StageError('Contact returned an unsupported delegation plan.')
            with self.lock:
                self.run['delegation'] = delegation
            description = self._catalogue()
            record = description['record']
            if (record['@id'] != RECORD or description['publication'] != 'listed'
                    or record['access']['town'] != 'ubar' or record['access']['operation'] != 'phenotype-search'):
                raise StageError('The expected Ubar service is not currently listed.')
            with self.lock:
                self.run['fair'] = description
                self._event('fairhaven', 'yamatai', 'A service with a description',
                            'FAIRhaven describes Ubar’s phenotype search: inputs, species, model version, access rules and reuse gaps. A listing is not permission or scientific validation.',
                            detail={'record_id': RECORD, 'description_digest': description['digest'], 'checked': description['checked']})
            body = {'resident': plan[0]['resident'], 'phenotypes': self.run['phenotypes'], 'limit': 20, 'method': 'indigena', 'include_human_orthologues': True}
            result = self._request(client, plan[0]['town'], plan[0]['operation'], body,
                text='Contact has delegated phenotype prioritization to you. Please resolve these labels and return ranked genes using INDIGENA. No VCF or variant data are attached.',
                recipient='ubar/phenomancer')
            if record.get('version') != 'sha256:' + str(result.get('checkpoint_sha256')):
                raise StageError('The returned model differs from the FAIR description; results withheld until metadata is refreshed.')
            if not result.get('orthology') or not result.get('results'):
                raise StageError('The service did not return the expected orthologue annotations.')
            with self.lock:
                self.run['result'] = result
                if result.get('resolution'):
                    self._event('ubar', 'yamatai', 'Resolve labels in HPO and MP',
                                'Exact labels and synonyms resolve to ontology identifiers before INDIGENA inference.',
                                detail=result['resolution'])
                if set(result.get('query', {}).get('phenotypes', [])) == EXAMPLE_IDS:
                    rank = next((i + 1 for i, row in enumerate(result['results'])
                                 if (row.get('human_orthologue') or {}).get('symbol') == 'FBN1'), None)
                    self.run['benchmark'] = {'gene': 'FBN1', 'rank': rank, 'disease': 'Marfan syndrome',
                        'source': 'https://www.ncbi.nlm.nih.gov/books/NBK1335/',
                        'note': 'Selected demonstration case, not an independent clinical evaluation. Expected gene is never sent to the scoring service.'}
                self._event('ubar', 'yamatai', 'Rank mouse profiles; annotate human orthologues',
                            f"INDIGENA scored {result['candidate_count']} mouse gene profiles. The returned top {len(result['results'])} retain their mouse scores and are annotated using an MGI orthology report.",
                            detail={'checkpoint_sha256': result['checkpoint_sha256'], 'orthology': result['orthology']})
            if self.run['private_case']:
                self._private_analysis(client, result)
            with self.lock:
                self.run['state'] = 'completed'
                self._event('yamatai', 'human', 'Candidate genes, with evidence and limits',
                            'Inspect the full ranking, model provenance and FAIR gaps. Human orthologues are research candidates; similarity scores are not disease probabilities and this is not a validated diagnosis.')
        except Exception as error:  # noqa: BLE001 - isolate one visitor's analysis failure
            with self.lock:
                self.run['state'] = 'failed'
                self._event('ubar', 'human', 'Analysis could not complete', str(error), kind='error')

    def _request(self, client, town, operation, body, *, text, sender='yamatai/coordinator', recipient=None):
        mid = client.ask(town, operation=operation, text=text, body=body)
        self._event(sender, recipient or town, operation, text,
                    detail={'request_id': mid, 'operation': operation, 'body': body,
                            'wire_body': {'text': text, 'operation': operation, **body}, 'transport': 'authenticated town relay', 'message_type': 'sent'})
        replies = client.wait(mid, timeout=120, acknowledge=False)
        reply = next((r for r in replies if r['from'] == town and r['kind'] == 'answer' and r['in_reply_to'] == mid), None)
        if reply is None:
            raise StageError(f'{town} did not answer {operation} within 120 seconds.')
        client.call('/v1/ack', {'id': reply['id']})
        if not reply['body'].get('ok'):
            raise StageError(f"{town}: {reply['body'].get('error', 'analysis failed')}")
        response = reply['body']
        self._event(recipient or town, 'yamatai/coordinator', operation + ' returned',
                    response.get('text') or 'The service returned structured results without a text message.',
                    detail={'request_id': mid, 'reply_id': reply['id'], 'message_type': 'received',
                            'body': {k: v for k, v in response.items() if k != 'pdf_base64'},
                            'omitted_fields': ['pdf_base64 (available through PDF download)'] if 'pdf_base64' in response else []})
        return reply['body']

    def _private_analysis(self, client, genes):
        symbols = [(row.get('human_orthologue') or {}).get('symbol') or row['gene'] for row in genes['results']]
        from .private_variants import rank
        self._event('yamatai', 'yamatai', 'Local private-variant-rank',
                    'Yamatai opens its local synthetic VCF. This step sends no relay message.',
                    detail={'operation': 'private-variant-rank', 'genes': symbols})
        local = rank(self.requester, self.requester.name, {'genes': symbols})
        with self.lock:
            self.run['variants'] = local
        self._event('yamatai', 'yamatai', 'Private VCF: quality, frequency, then REVEL',
                    f"Yamatai screened {local['input_count']} synthetic calls and retained {len(local['retained'])}. The VCF and sample/genotype fields stay at Yamatai.",
                    detail={'vcf_sha256': local['vcf_sha256'], 'method': local['method'], 'excluded': local['excluded']})
        if not local['retained']:
            raise StageError('No local variants survived filtering for this gene list. Edit the phenotypes or use the Marfan example.')
        selected = local['retained'][0]['variant']
        # A new, allowlisted request: never forward the local result object or VCF.
        request = {'resident': 'themis', 'variant': selected, 'phenotypes': genes['query']['phenotypes']}
        from .variant_interpretation import request_text
        interpretation = self._request(client, 'ubar', 'variant-interpretation', request,
                                       text=request_text(selected, genes['query']['phenotypes']), recipient='ubar/themis')
        report = interpretation['report']
        if report.get('variant') != selected or report.get('agent') != 'themis':
            raise StageError('Themis returned a report for a different variant or agent.')
        encoded = interpretation['pdf_base64']
        if not isinstance(encoded, str) or len(encoded) > 2_000_000:
            raise StageError('Invalid report size')
        pdf = base64.b64decode(encoded, validate=True)
        if not pdf.startswith(b'%PDF-') or hashlib.sha256(pdf).hexdigest() != interpretation['pdf_sha256']:
            raise StageError('Report PDF integrity check failed')
        with self.lock:
            self.pdf = pdf
            self.run['interpretation'] = report
            self.run['report_sha256'] = interpretation['pdf_sha256']
            self.run['variant_benchmark'] = {
                'expected': 'GRCh38 15:48421680 T>C / FBN1 c.7577A>G',
                'revel_rank': local['retained'][0]['revel_rank'],
                'combined_rank': local['retained'][0]['combined_rank'],
                'recovered': selected == {'assembly': 'GRCh38', 'chrom': '15', 'pos': 48421680, 'ref': 'T', 'alt': 'C'},
                'note': 'Known synthetic-case truth checked after ranking; never an input to the ranking formula.'}
        self._event('ubar', 'human', 'Themis: ACMG evidence and PDF',
                    report['classification'] + '. Inspect the applied criteria, missing evidence and versioned sources. No patient genotype or VCF was supplied to Themis.',
                    detail={'agent': 'themis', 'pdf_sha256': interpretation['pdf_sha256'], 'sources': report['sources']})

    def report_bytes(self):
        with self.lock:
            if not self.run or self.run['state'] != 'completed' or not getattr(self, 'pdf', None):
                raise StageError('Complete this visitor’s variant case before downloading its report.')
            return self.pdf

    def trust_bundle(self):
        with self.lock:
            return {'run_id': self.run['id'] if self.run else None, 'receivers': [],
                    'authority_mode': 'Authenticated relay town identity; no clinical or IRB approval is claimed.'}

    def act(self, action, payload):
        if action == 'start':
            if set(payload) - {'phenotypes', 'private_case', 'human_message'}:
                raise StageError('Only a human request, phenotype terms and the synthetic-case switch are accepted; no VCF uploads.')
            return self.start(payload.get('phenotypes'), payload.get('private_case', False), payload.get('human_message'))
        if action in ('approve', 'reset'):
            return getattr(self, action)(payload.get('run_id'))
        raise StageError('Unknown phenotype demo action.')
