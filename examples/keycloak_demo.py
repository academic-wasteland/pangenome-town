"""Real Keycloak smoke test. Creates and removes an isolated loopback-only container."""
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pangenome_town.authority import certification, credentials, identity, keys

IMAGE = 'quay.io/keycloak/keycloak:26.7.3'
SUBJECT = 'a5b2fd76-1738-44d3-9e3c-7ce0a4d85e03'


def main():
    secret = uuid.uuid4().hex
    receiver_secret = uuid.uuid4().hex
    name = 'wasteland-cert-demo-' + uuid.uuid4().hex[:10]
    mapper = {'name': 'groups', 'protocol': 'openid-connect', 'protocolMapper': 'oidc-group-membership-mapper',
              'config': {'claim.name': 'groups', 'full.path': 'true', 'access.token.claim': 'true',
                         'introspection.token.claim': 'true'}}
    audience_mapper = {'name': 'audience', 'protocol': 'openid-connect', 'protocolMapper': 'oidc-audience-mapper',
                       'config': {'included.custom.audience': 'wasteland-demo', 'access.token.claim': 'true',
                                  'introspection.token.claim': 'true'}}
    realm = {'realm': 'yamatai-demo', 'enabled': True, 'accessTokenLifespan': 60,
             'groups': [{'name': 'aggregate-certified'}],
             'clients': [{'clientId': 'analyst', 'secret': secret, 'publicClient': False,
                          'serviceAccountsEnabled': True, 'standardFlowEnabled': False,
                          'protocolMappers': [mapper, audience_mapper]},
                         {'clientId': 'wasteland-demo', 'secret': receiver_secret, 'publicClient': False,
                          'standardFlowEnabled': False}],
             'users': [{'id': SUBJECT, 'username': 'service-account-analyst', 'enabled': True,
                        'serviceAccountClientId': 'analyst', 'groups': ['/aggregate-certified']}]}
    def docker(*args):
        return subprocess.check_output(['docker', *args], stderr=subprocess.STDOUT, text=True).strip()
    with tempfile.TemporaryDirectory(prefix='wasteland-keycloak-') as temp:
        path = Path(temp) / 'realm.json'
        path.write_text(json.dumps(realm))
        # Container's unprivileged UID must be able to read the ephemeral import.
        Path(temp).chmod(0o755)
        path.chmod(0o644)
        try:
            docker('run', '-d', '--rm', '--name', name, '-p', '127.0.0.1::8080',
                   '-v', str(path) + ':/opt/keycloak/data/import/realm.json:ro', IMAGE,
                   'start-dev', '--import-realm')
            port = docker('port', name, '8080/tcp').rsplit(':', 1)[1]
            issuer = f'http://127.0.0.1:{port}/realms/yamatai-demo'
            for attempt in range(90):
                try:
                    with urllib.request.urlopen(issuer + '/.well-known/openid-configuration', timeout=2):
                        break
                except (OSError, urllib.error.URLError):
                    time.sleep(1)
            else:
                raise RuntimeError('Keycloak did not become ready')
            def post(suffix, fields):
                req = urllib.request.Request(issuer + '/protocol/openid-connect/' + suffix,
                                             data=urllib.parse.urlencode(fields).encode())
                with urllib.request.urlopen(req, timeout=5) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else None
            token = post('token', {'grant_type': 'client_credentials', 'client_id': 'analyst', 'client_secret': secret})['access_token']
            os.environ['WASTELAND_DEMO_INTROSPECTION_SECRET'] = receiver_secret
            holder_key = keys.generate()
            holder, scope = 'urn:yamatai:analyst', 'urn:scope:aggregate'
            provider = {'introspection_url': issuer + '/protocol/openid-connect/token/introspect',
                        'allow_loopback_http': True, 'client_id': 'wasteland-demo',
                        'client_secret_env': 'WASTELAND_DEMO_INTROSPECTION_SECRET', 'audience': 'wasteland-demo',
                        'subjects': {SUBJECT: {'holder': holder, 'holder_key': keys.public_key_text(holder_key)}},
                        'groups': {'/aggregate-certified': {'scopes': [scope]}}}
            check = identity.checker({issuer: provider})
            now = datetime.now(UTC)
            assert check({'issuer': issuer, 'token': token}, holder, keys.public_key_text(holder_key), now)
            task = {'@id': 'urn:task:demo', 'requestedBy': holder, 'taskType': 'aggregate', 'usesDataset': ['urn:dataset:saudi']}
            policy = {'task_scopes': {'aggregate': scope}, 'requirements': [{'type': 'Qualification'}],
                      'issuers': [{'issuer': issuer, 'types': ['Qualification'], 'scopes': [scope], 'datasets': ['urn:dataset:saudi']}]}
            presentation = credentials.present(holder=holder, holder_key=holder_key, credentials=[], accreditations=[],
                                               task=task, audience='urn:town:ubar',
                                               identity_tokens=[{'issuer': issuer, 'token': token}])
            def assess():
                return certification.evaluate(policy, task, presentation, audience='urn:town:ubar', anchors={}, identity_checker=check)
            assert assess()['ok']
            print('Real Keycloak: service-account identity and certified group accepted.')
            provider['subjects'][SUBJECT]['holder'] = 'urn:yamatai:bloodninja'
            assert not assess()['ok']
            print('Real Keycloak: same token cannot authenticate Bloodninja.')
            provider['subjects'][SUBJECT]['holder'] = holder
            policy['requirements'].append({'type': 'DataAccessAuthorization', 'per_dataset': True})
            assert not assess()['ok']
            print('Real Keycloak: group membership does not grant Saudi data access.')
            policy['requirements'].pop()
            post('revoke', {'token': token, 'client_id': 'analyst', 'client_secret': secret, 'token_type_hint': 'access_token'})
            assert not assess()['ok']
            print('Real Keycloak: revoked token refused by fresh introspection.')
        finally:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, check=False)
            os.environ.pop('WASTELAND_DEMO_INTROSPECTION_SECRET', None)
    print('Temporary Keycloak container and credentials removed.')


if __name__ == '__main__':
    main()
