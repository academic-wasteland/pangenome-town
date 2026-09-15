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
from . import ComputeError
from .runner import run_task
from .sites import driver_for, load_sites
from .workflows import SAMPLE_RE, TEMPLATES

COHORT_WORKFLOWS = {'allele-frequency', 'genotype-export'}


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
        if not str(location['vcf']).startswith('/') or '..' in str(location['vcf']):
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
        results = []
        for data in datasets:
            selected = dataclasses.replace(site, datasets=('vcf',), paths={'vcf': data['locations'][site.storage]['vcf']})
            out = town.state_dir / 'executions' / uuid.uuid4().hex
            actual_driver = driver or driver_for(selected)
            if hasattr(actual_driver, 'on_progress'):
                actual_driver.on_progress = lambda dataset=data['id'], **detail: event('scheduler', dataset=dataset, **detail)
            result = run_task(town, template_name=task['workflow'], region=region, out_dir=out,
                              site=selected, driver=actual_driver, dataset_samples=tuple(data['samples']),
                              on_start=lambda detail, dataset=data['id']: event('execution_started', dataset=dataset, **detail))
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
