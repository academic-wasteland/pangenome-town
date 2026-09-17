"""Holder-proved relay applications and fresh, issuer-signed credential status.

Only the local registrar CLI can approve or revoke. Nonces are single-use,
short-lived and bound to the authenticated sending town and exact request.
"""
import json
import os
import secrets
import sqlite3
import time
from datetime import UTC, datetime, timedelta

from . import credentials as cred
from . import keys
from .registrar import RegistryError, _write

OPERATIONS = ('credential-challenge', 'credential-apply', 'credential-fetch', 'credential-status')


class RelayAuthority:
    def __init__(self, registry, town):
        self.registry, self.town = registry, town
        registry.key_dir.mkdir(parents=True, exist_ok=True)
        self.path = registry.key_dir / 'relay-nonces.sqlite'
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS challenges(nonce TEXT PRIMARY KEY, sender TEXT, request TEXT, expires REAL)')
        os.chmod(self.path, 0o600)

    def db(self):
        # Connection context manager below is explicitly closed by caller via closing.
        from contextlib import contextmanager
        @contextmanager
        def connection():
            db = sqlite3.connect(self.path, timeout=10)
            try:
                with db:
                    yield db
            finally:
                db.close()
        return connection()

    def handle(self, sender, body):
        operation = body.get('operation')
        if operation == 'credential-status':
            return self.status(body.get('id'), body.get('issuer'))
        if operation == 'credential-challenge':
            request = body.get('request')
            if not isinstance(request, dict) or request.get('action') not in ('apply', 'fetch'):
                raise RegistryError('request action must be apply or fetch')
            keys.parse_public(request.get('publicKey'))
            if not isinstance(request.get('holder'), str) or not request['holder'].startswith(('urn:', 'https://')):
                raise RegistryError('holder must be a urn: or https: identity')
            encoded = json.dumps(request, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
            if len(encoded.encode()) > 16000:
                raise RegistryError('request too large')
            nonce = secrets.token_urlsafe(32)
            with self.db() as db:
                db.execute('DELETE FROM challenges WHERE expires < ?', (time.time(),))
                if db.execute('SELECT count(*) FROM challenges WHERE sender=?', (sender,)).fetchone()[0] >= 20:
                    raise RegistryError('too many pending challenges')
                db.execute('INSERT INTO challenges VALUES(?,?,?,?)', (nonce, sender, encoded, time.time()+120))
            return {'ok': True, 'challenge': {'type': 'CredentialRelayProof', 'audience': self.town,
                    'sender': sender, 'nonce': nonce, 'request': request}, 'expires_in': 120}
        signed = body.get('presentation')
        if not isinstance(signed, dict) or signed.get('type') != 'CredentialRelayProof':
            raise RegistryError('signed challenge required')
        request = signed.get('request', {})
        if not isinstance(request, dict) or not keys.verify(signed, keys.parse_public(request.get('publicKey'))):
            raise RegistryError('invalid holder proof')
        expected = {'credential-apply': 'apply', 'credential-fetch': 'fetch'}.get(operation)
        if (expected is None or request.get('action') != expected or signed.get('audience') != self.town
                or signed.get('sender') != sender):
            raise RegistryError('wrong action, audience or sender')
        with self.db() as db:
            row = db.execute('DELETE FROM challenges WHERE nonce=? AND sender=? AND expires>=? RETURNING request',
                             (signed.get('nonce'), sender, time.time())).fetchone()
        if row is None or json.loads(row[0]) != request:
            raise RegistryError('expired, changed or consumed challenge')
        if expected == 'apply':
            app = request.get('application', {})
            if not isinstance(app, dict) or set(app) != {'credential_type', 'issuer', 'subject_fields', 'purpose'}:
                raise RegistryError('application fields: credential_type, issuer, subject_fields, purpose')
            record = self.registry.apply(holder=request['holder'], holder_key_text=request['publicKey'],
                                         credential_type=app['credential_type'], issuer_slug=app['issuer'],
                                         subject_fields=app['subject_fields'], purpose=app['purpose'])
            record['relay_sender'] = sender
            _write(self.registry._app_path(record['id']), record)
            return {'ok': True, 'application': record['id'], 'state': 'pending'}
        record = self.registry.application(request.get('application'))
        if (record is None or record.get('relay_sender') != sender or record['holder'] != request['holder']
                or record['holder_key'] != request['publicKey']):
            raise RegistryError('application not available to this holder and town')
        result = {'ok': True, 'application': record['id'], 'state': record['state']}
        if record['state'] == 'approved':
            result['credential'] = self.registry.credential(record['credential'])
        return result

    def status(self, identifier, issuer):
        if not isinstance(identifier, str) or len(identifier) > 1000:
            raise RegistryError('credential or status ID required')
        document = self.registry.credential(identifier)
        if document and identifier not in (document['id'], document['credentialStatus']['id']):
            document = None  # Never resolve an arbitrary namespace solely by its UUID suffix.
        issuer = document['issuer'] if document else issuer
        slug = self.registry._slug_for(issuer)
        if slug is None:
            raise RegistryError('unknown ID requires a locally hosted issuer IRI')
        now = datetime.now(UTC).replace(microsecond=0)
        statement = {'type': 'CredentialStatusStatement', 'issuer': issuer, 'id': identifier,
                     'credential': document['id'] if document else None,
                     'status_id': document['credentialStatus']['id'] if document else None,
                     'status': self.registry.status(document['id']) if document else 'unknown',
                     'as_of': cred.iso(now), 'valid_until': cred.iso(now+timedelta(seconds=60)), 'reason': None}
        return {'ok': True, 'statement': keys.sign(statement, self.registry._key(slug), issuer+'#key-1')}
