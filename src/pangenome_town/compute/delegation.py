"""Explicit dataset custody and task-bound execution grants over existing drivers.

This application boundary does not restrict a user's independent SSH shell. Only
public-source cohort workflows are supported here; controlled RCP tasks keep their
existing ethics and data-access gates and cannot enter through this adapter.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..authority import keys
from ..authority.credentials import iso, parse_iso
from ..exchange import Envelope, ExchangeLog
from ..tools.graph import Region
from . import ComputeError, SemanticPolicyError
from .runner import SemanticGate, run_task
from .sites import driver_for, load_sites
from .workflows import SAMPLE_RE, TEMPLATES

COHORT_WORKFLOWS = {'allele-frequency', 'genotype-export'}
WORKFLOW_TASK_CLASSES = {
    'allele-frequency': 'AlleleFrequencyTask',
    'genotype-export': 'IndividualGenotypeExportTask',
    'region-extract': 'RegionExtractionTask',
    'region-variants': 'RegionVariantListingTask',
    'graph-summary': 'GraphSummaryTask',
    'haplotype-presence': 'HaplotypePresenceTask',
    'gene-lookup': 'GeneLookupTask',
}

__all__ = [
    "COHORT_WORKFLOWS",
    "SemanticGate",
    "SemanticPolicyError",
    "approve",
    "authorize",
    "catalog",
    "execute",
    "inspect_task",
    "prepare",
    "record_request",
]


def catalog(town):
    result = {}
    for entry in town.extra.get('datasets', []):
        item = dict(entry)
        identifier = item.get('id')
        if not isinstance(identifier, str) or not identifier or identifier in result:
            raise ComputeError('datasets need unique nonempty IDs')
        if not item.get('custodian') or not item.get('version') or not item.get('public_key'):
            raise ComputeError(f'dataset {identifier}: custodian, version and public_key required')
        if not item.get('samples') or any(not isinstance(s, str) or not SAMPLE_RE.fullmatch(s) for s in item['samples']):
            raise ComputeError(f'dataset {identifier}: explicit valid sample manifest required')
        if len(set(item['samples'])) != len(item['samples']):
            raise ComputeError('sample manifest has duplicates')
        # Content identity covers custody, cohort, allowed workflows, locations and key pin.
        result[identifier] = {**item, 'manifest': keys.sha256_digest(item)}
    return result


def prepare(town, *, requester, executor, site, datasets, workflow, region):
    entries = catalog(town)
    if not datasets or len(set(datasets)) != len(datasets) or any(d not in entries for d in datasets):
        raise ComputeError('choose distinct configured datasets')
    task = {'schema': 'wasteland-execution/1', 'id': 'urn:uuid:' + str(uuid.uuid4()),
            'requester': requester, 'executor': executor, 'site': site, 'workflow': workflow,
            'region': str(Region.parse(region, town.default_assembly)),
            'datasets': {d: entries[d]['manifest'] for d in datasets}}
    inspect_task(town, task)
    return task


def inspect_task(town, task):
    if not isinstance(task, dict) or set(task) != {'schema','id','requester','executor','site','workflow','region','datasets'}:
        raise ComputeError('invalid execution task fields')
    if task['schema'] != 'wasteland-execution/1' or not all(isinstance(task[k], str) and task[k] for k in ('id','requester','executor','site','workflow','region')):
        raise ComputeError('invalid execution task')
    if task['workflow'] not in COHORT_WORKFLOWS:
        raise ComputeError('only sample-scoped cohort workflows can be delegated')
    region = Region.parse(task['region'], town.default_assembly)
    if region.span <= 0 or region.span > TEMPLATES[task['workflow']].max_span:
        raise ComputeError('region outside workflow limits')
    entries = catalog(town)
    if not isinstance(task['datasets'], dict) or not task['datasets']:
        raise ComputeError('task must name datasets and manifest digests')
    chosen = []
    for identifier, digest in task['datasets'].items():
        data = entries.get(identifier)
        if not data or data['manifest'] != digest:
            raise ComputeError(f'dataset manifest mismatch: {identifier}')
        if data.get('access') != 'public-source':
            raise ComputeError('controlled data requires the existing RCP authority gates; this adapter refuses it')
        if task['workflow'] not in data.get('workflows', []):
            raise ComputeError(f'workflow not permitted for dataset {identifier}')
        chosen.append(data)
    return chosen, region


def approve(town, task, *, minutes=15):
    datasets, _ = inspect_task(town, task)
    owned = [d['id'] for d in datasets if d['custodian'] == town.name]
    if not owned or not 1 <= minutes <= 60:
        raise ComputeError('approval requires custody and a lifetime of 1–60 minutes')
    path = (town.extra.get('custody') or {}).get('key')
    if not path:
        raise ComputeError('no custody signing key configured')
    key = keys.load_private(Path(path).expanduser())
    if any(d['public_key'] != keys.public_key_text(key) for d in datasets if d['id'] in owned):
        raise ComputeError('custody signing key does not match dataset key pin')
    now = datetime.now(UTC)
    grant = {'type': 'DatasetExecutionGrant', 'id': 'urn:uuid:' + str(uuid.uuid4()),
             'custodian': town.name, 'task_digest': keys.sha256_digest(task), 'datasets': owned,
             'valid_from': iso(now), 'valid_until': iso(now + timedelta(minutes=minutes)),
             'release': [o['class'] for o in TEMPLATES[task['workflow']].outputs if o['release']]}
    return keys.sign(grant, key, town.name + '#custody')


def authorize(town, task, grants, *, now=None):
    datasets, region = inspect_task(town, task)
    if task['executor'] != town.name:
        raise ComputeError('task names another executor')
    site = next((s for s in load_sites(town) if s.name == task['site'] and s.enabled), None)
    if not site:
        raise ComputeError('executor has no enabled permission for the requested site')
    now = now or datetime.now(UTC)
    digest = keys.sha256_digest(task)
    release = [o['class'] for o in TEMPLATES[task['workflow']].outputs if o['release']]
    if not isinstance(grants, list):
        raise ComputeError('grants must be a list')
    for data in datasets:
        valid = False
        for grant in grants:
            if not isinstance(grant, dict):
                continue
            start, end = parse_iso(grant.get('valid_from')), parse_iso(grant.get('valid_until'))
            if (grant.get('type') == 'DatasetExecutionGrant' and grant.get('custodian') == data['custodian']
                    and data['id'] in (grant.get('datasets') or []) and grant.get('task_digest') == digest
                    and start and end and start <= now < end and end - start <= timedelta(hours=1)
                    and grant.get('release') == release
                    and keys.verify(grant, keys.parse_public(data['public_key']))):
                valid = True
                break
        if not valid:
            raise ComputeError(f'waiting for {data["custodian"]} permission for {data["id"]}')
        location = data.get('locations', {}).get(site.storage)
        if not isinstance(location, dict) or not location.get('vcf'):
            raise ComputeError(f'dataset {data["id"]} not available at storage {site.storage}')
        loc_vcf = str(location['vcf'])
        if site.driver == 'tes':
            if not loc_vcf.startswith(('http://', 'https://', 's3://', 'file://', '/')) or '..' in loc_vcf:
                raise ComputeError('dataset location for TES must be an absolute path or URI without ".."')
        else:
            if not loc_vcf.startswith('/') or '..' in loc_vcf:
                raise ComputeError('dataset location must be an absolute configured path')
    return datasets, region, site


def execute(town, task, grants, *, log=None, message_id=None, driver=None):
    """Verify before any driver call; claim once, preserve provenance and exact retry result."""
    own_log = log is None
    log = log or ExchangeLog(town.exchange_db)
    message_id = message_id or task.get('id')
    def event(phase, **detail):
        log.event(town.name, 'delegation_' + phase, message_id,
                  {'task_id': task.get('id'), 'requester': task.get('requester'),
                   'executor': town.name, 'site': task.get('site'), 'phase': phase, **detail})
    try:
        datasets, region, site = authorize(town, task, grants)
        if site.driver == "tes" and driver is not None:
            raise ComputeError("Custom driver injection is not permitted for TES execution")
    except (ComputeError, ValueError) as error:
        event('permission_required' if str(error).startswith('waiting for') else 'rejected', message=str(error))
        if own_log:
            log.close()
        raise
    event('authorized', custodians=sorted({d['custodian'] for d in datasets}), datasets=[d['id'] for d in datasets])
    database = town.state_dir / 'execution.sqlite'
    database.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(database, timeout=30)
    db.execute('CREATE TABLE IF NOT EXISTS executions (id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT)')
    try:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT digest, result FROM executions WHERE id=?', (task['id'],)).fetchone()
        if existing:
            db.rollback()
            if existing[0] != keys.sha256_digest(task):
                raise ComputeError('task ID reused with different contents')
            if existing[1]:
                event('replayed', message='Returning recorded result; no new job submitted')
                return json.loads(existing[1])
            raise ComputeError('execution already claimed; inspect its job before preparing a new task')
        db.execute('INSERT INTO executions VALUES (?, ?, NULL)', (task['id'], keys.sha256_digest(task)))
        db.commit()
        dispatched_task = False
        results = []
        for data in datasets:
            selected = dataclasses.replace(site, datasets=('vcf',), paths={'vcf': data['locations'][site.storage]['vcf']})
            out = town.state_dir / 'executions' / uuid.uuid4().hex
            if selected.driver == "tes":
                if driver is not None:
                    raise ComputeError("Custom driver injection is not permitted for TES execution")
                from ..rcp import capability, contract, pipeline
                manifest_path = town.city_root / "contract" / f"{town.name}.contract.json"
                if not manifest_path.exists():
                    raise SemanticPolicyError(f"TES execution requires town contract manifest at {manifest_path}")
                manifest = contract.ContractManifest.load(manifest_path)
                # Build admission assertions based on authorized delegation facts
                from research_commons import axioms
                town_prefix = f"https://w3id.org/academic-wasteland/{town.name}/"
                admission_axioms = [
                    axioms.class_assertion(f"{town_prefix}DataAccessCovered", task["id"]),
                    axioms.class_assertion(f"{town_prefix}EthicsCovered", task["id"]),
                ]
                site_axioms, _ = capability.site_facts(town)
                admission_axioms.extend(site_axioms)

                import shutil

                from research_commons.km import KMRunner
                from research_commons.semantic import SemanticValidator
                km_bin = shutil.which("km")
                reasoner = KMRunner(km_bin or "km", timeout_seconds=manifest.timeout_seconds) if km_bin else None
                validator = SemanticValidator(manifest, reasoner) if reasoner is not None else None
                gate = SemanticGate(
                    manifest=manifest,
                    reasoner=lambda doc, v=validator, a=admission_axioms: v.validate(doc, receiver_assertions=a) if v else {"status": "indeterminate"},
                )
                # Align dataset individual with town contract restricted dataset declarations
                configured_datasets = contract.restricted_datasets(town)
                match = next((entry for entry in configured_datasets if entry["key"] == "vcf" or entry["name"] == data.get("id") or entry["key"] == data.get("id")), None)
                ds_iri = match["iri"] if match else f"https://w3id.org/academic-wasteland/{town.name}/dataset/{data['id']}"
                ds_entity = {
                    "@id": ds_iri,
                    "@type": ["RestrictedDataset", "IndividualGenotypeData"],
                }
                task_doc = pipeline.task_document(
                    town,
                    f"{contract.PG}{WORKFLOW_TASK_CLASSES.get(task['workflow'], 'ResearchTask')}",
                    datasets=(ds_entity,),
                    region=region,
                    requester=task["requester"],
                    request_id=task["id"],
                    include_graph=False,
                )
                task_doc["@id"] = task["id"]
                task_doc["semanticContract"] = manifest.id
                task_doc["ontologyProfile"] = manifest.bundle_digest
                actual_driver = driver_for(selected, gate=gate, rcp_task=task_doc)
            else:
                actual_driver = driver or driver_for(selected)
            if hasattr(actual_driver, 'on_progress'):
                actual_driver.on_progress = lambda dataset=data['id'], **detail: event('scheduler', dataset=dataset, **detail)

            def _on_dispatched():
                nonlocal dispatched_task
                dispatched_task = True

            if hasattr(actual_driver, "on_dispatched"):
                actual_driver.on_dispatched = _on_dispatched

            is_tes_driver = (selected.driver == "tes")

            def _on_start(detail, dataset=data['id'], is_tes=is_tes_driver):
                nonlocal dispatched_task
                if not is_tes:
                    dispatched_task = True
                event('execution_started', dataset=dataset, **detail)

            result = run_task(town, template_name=task['workflow'], region=region, out_dir=out,
                              site=selected, driver=actual_driver, dataset_samples=tuple(data['samples']),
                              on_start=_on_start)
            dispatched_task = True
            outputs = []
            for output in result['outputs']:
                path = Path(output['path'])
                if path.stat().st_size > 12000:
                    raise ComputeError('output exceeds inline release limit; retained locally for operator review')
                outputs.append({'name': output['name'], 'class': output['class'], 'digest': output['digest'],
                                'text': path.read_text()})
            results.append({'dataset': data['id'], 'manifest': data['manifest'], 'custodian': data['custodian'],
                            'samples': data['samples'], 'outputs': outputs, 'job_id': result['provenance']['backend_id']})
        payload = {'ok': True, 'state': 'completed', 'task_id': task['id'], 'requester': task['requester'],
                   'executor': town.name, 'site': site.name, 'storage': site.storage, 'results': results}
        if len(json.dumps(payload).encode()) > 45000:
            raise ComputeError('combined result exceeds relay limit; retained locally for operator review')
        # An expired grant may not authorize release after a long queue/job.
        authorize(town, task, grants)
        db.execute('UPDATE executions SET result=? WHERE id=?', (json.dumps(payload), task['id']))
        db.commit()
        event('released', custodians=sorted({d['custodian'] for d in datasets}), message='Permitted outputs released')
        return payload
    except Exception as error:
        try:
            db.rollback()
            # Only clean up un-dispatched / un-submitted claims so retry isn't permanently locked out.
            # If execution was dispatched but failed/timed out, record a failure tombstone so
            # future retries are aware of the prior failure without remaining as an unresolvable NULL lock.
            if not locals().get("dispatched_task", False):
                db.execute('DELETE FROM executions WHERE id=? AND result IS NULL', (task['id'],))
                db.commit()
            else:
                fail_payload = json.dumps({"ok": False, "state": "failed", "error": str(error)[:500]})
                db.execute('UPDATE executions SET result=? WHERE id=? AND result IS NULL', (fail_payload, task['id']))
                db.commit()
        except sqlite3.Error:
            log.event(town.name, 'delegation_cleanup_error', message_id, {'task_id': task.get('id')})
        event('failed', message=str(error)[:500])
        raise
    finally:
        db.close()
        if own_log:
            log.close()


def record_request(town, task, log):
    existing = log.envelope(task['id'])
    if existing:
        if existing.body.get('task') != task:
            raise ComputeError('task ID reused with different contents')
        return existing
    envelope = Envelope(id=task['id'], kind='question', sender=task['requester'], recipient=task['executor'],
                        created=keys.now_iso(), body={'operation': 'delegated-compute', 'task': task})
    log.record(envelope, town=town.name, direction='sent', status='sent')
    return envelope
