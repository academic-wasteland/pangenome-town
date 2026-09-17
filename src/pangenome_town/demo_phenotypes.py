"""A fictional rare-disease case invokes Ubar's FAIR-described INDIGENA service."""
from __future__ import annotations

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
DEFAULT_TERMS = ['HP:0001250', 'HP:0001249', 'HP:0000252']


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

    def start(self, phenotypes=None):
        with self.lock:
            if self.run:
                raise StageError('Reset this case before starting another query.')
            query = validate({'phenotypes': DEFAULT_TERMS if phenotypes is None else phenotypes, 'limit': 20})
            self.started = time.monotonic()
            self.run = {'id': uuid.uuid4().hex, 'created': datetime.now(UTC).isoformat(), 'state': 'awaiting-run',
                        'events': [], 'phenotypes': query['phenotypes'], 'result': None, 'fair': None,
                        'reuse': REUSE, 'license': 'https://creativecommons.org/licenses/by/4.0/',
                        'attribution': 'Academic Wasteland',
                        'fictional_case': 'Phenotype-only rare-disease gene-prioritization demonstration. No patient data.',
                        'access': 'Registered town identity. This public service does not require an IRB credential; no new approval is issued.'}
            self._event('human', 'yamatai', 'A phenotype-only question',
                        'Find candidate genes from the selected phenotype terms for a fictional research case; do not infer a diagnosis.',
                        detail={'phenotypes': self.run['phenotypes']})
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
            if self.client_factory:
                client = self.client_factory()
            else:
                from wasteland.client import Client
                client = Client(Path(self.requester.extra['federation']['state']).expanduser())
            body = {'phenotypes': self.run['phenotypes'], 'limit': 20, 'method': 'indigena', 'include_human_orthologues': True}
            mid = client.ask('ubar', operation='phenotype-search', body=body)
            self._event('yamatai', 'ubar', 'Send the phenotype profile',
                        'Yamatai sends a real authenticated relay request to Ubar. Only phenotype identifiers are sent; there are no patient names or genomes.',
                        detail={'request_id': mid, 'body': body})
            replies = client.wait(mid, timeout=120, acknowledge=False)
            reply = next((r for r in replies if r['from'] == 'ubar' and r['kind'] == 'answer' and r['in_reply_to'] == mid), None)
            if reply is None or not reply['body'].get('ok'):
                raise StageError('Ubar did not return a successful analysis.')
            result = reply['body']
            if record.get('version') != 'sha256:' + str(result.get('checkpoint_sha256')):
                raise StageError('The returned model differs from the FAIR description; results withheld until metadata is refreshed.')
            if not result.get('orthology') or not result.get('results'):
                raise StageError('The service did not return the expected orthologue annotations.')
            client.call('/v1/ack', {'id': reply['id']})
            with self.lock:
                self.run['result'] = result
                self._event('ubar', 'yamatai', 'Rank mouse profiles; annotate human orthologues',
                            f"INDIGENA scored {result['candidate_count']} mouse gene profiles. The returned top {len(result['results'])} retain their mouse scores and are annotated using an MGI orthology report.",
                            detail={'request_id': mid, 'reply_id': reply['id'], 'checkpoint_sha256': result['checkpoint_sha256'], 'orthology': result['orthology']})
                self.run['state'] = 'completed'
                self._event('yamatai', 'human', 'Candidate genes, with evidence and limits',
                            'Inspect the full ranking, model provenance and FAIR gaps. Human orthologues are research candidates; similarity scores are not disease probabilities and this is not a validated diagnosis.')
        except Exception as error:  # noqa: BLE001 - isolate one visitor's analysis failure
            with self.lock:
                self.run['state'] = 'failed'
                self._event('ubar', 'human', 'Analysis could not complete', str(error), kind='error')

    def trust_bundle(self):
        with self.lock:
            return {'run_id': self.run['id'] if self.run else None, 'receivers': [],
                    'authority_mode': 'Authenticated relay town identity; no clinical or IRB approval is claimed.'}

    def act(self, action, payload):
        if action == 'start':
            return self.start(payload.get('phenotypes'))
        if action in ('approve', 'reset'):
            return getattr(self, action)(payload.get('run_id'))
        raise StageError('Unknown phenotype demo action.')
