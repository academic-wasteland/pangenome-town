"""Local operator review shared by the cockpit's protected decision actions."""
import json
import os
import uuid
from datetime import UTC, datetime

from .registrar import RegistryError


def synthetic_test(application):
    return (application.get('credential_type') == 'Qualification'
            and application.get('issuer') == 'relay-test-only'
            and application.get('subject_fields') == {'qualification': 'SyntheticRelayTestOnly'})


def decide(registry, action, payload, operator):
    application = registry.application(payload.get('application'))
    if not application or application['state'] != 'pending':
        raise RegistryError('This application is no longer pending; refresh the decisions panel.')
    if not operator:
        raise RegistryError('Configure authority.operator before making dashboard decisions.')
    if action == 'deny':
        reason = payload.get('reason')
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
            raise RegistryError('Give a denial reason of 1–2000 characters.')
        record = registry.deny(application['id'], reason=reason.strip(), decided_by=operator)
        return {'ok': True, 'state': 'denied', 'application': record['id']}
    if action != 'approve':
        raise RegistryError('Unknown authority decision.')
    if payload.get('reviewed') is not True:
        raise RegistryError('Confirm that you reviewed this application and its evidence.')
    days = payload.get('valid_days', 1)
    if type(days) is not int or not 1 <= days <= 365:
        raise RegistryError('Validity must be a whole number from 1 to 365 days.')
    note, reference = payload.get('review_note', ''), payload.get('evidence_ref', '')
    if any(not isinstance(value, str) or len(value) > 4000 for value in (note, reference)):
        raise RegistryError('Review notes and references must be text of at most 4000 characters.')
    note, reference = note.strip(), reference.strip()
    if not synthetic_test(application) and not (note or reference):
        raise RegistryError('Record the evidence you reviewed or its private reference before approving.')
    if synthetic_test(application) and not note:
        note = 'Reviewed as a synthetic interoperability test only; no real qualification or data permission is attested.'
    # Keep review details private; only an opaque review reference enters the registrar audit.
    review_id = str(uuid.uuid4())
    folder = registry.key_dir / 'dashboard-reviews'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / (review_id + '.json')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'application': application['id'], 'reviewer': operator,
                   'reviewed_at': datetime.now(UTC).isoformat(), 'note': note,
                   'evidence_ref': reference, 'valid_days': days}, stream, indent=2)
    credential = registry.approve(application['id'], valid_days=days, decided_by=operator,
                                  evidence_ref='dashboard-review:' + review_id)
    return {'ok': True, 'state': 'approved', 'application': application['id'],
            'credential': credential['id'], 'valid_until': credential['validUntil']}
