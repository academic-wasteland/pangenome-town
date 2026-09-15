"""OIDC/Keycloak service-account adapter using fresh, authenticated introspection.

Only operator-configured issuers/endpoints are contacted. Tokens are opaque and
never used to discover a URL. Exact issuer + subject + presentation key bindings
prevent a same-name resident or imported group from acquiring another identity.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def checker(providers, *, transport=None):
    def fetch(provider, token):
        endpoint = provider['introspection_url']
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme != 'https' and not (provider.get('allow_loopback_http') and parsed.scheme == 'http'
                                              and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}):
            raise ValueError('introspection requires HTTPS')
        secret = os.environ[provider['client_secret_env']]
        auth = base64.b64encode((provider['client_id'] + ':' + secret).encode()).decode()
        request = urllib.request.Request(endpoint, data=urllib.parse.urlencode({'token': token}).encode(),
                                         headers={'Authorization': 'Basic ' + auth,
                                                  'Content-Type': 'application/x-www-form-urlencoded'})
        with urllib.request.build_opener(NoRedirect).open(request, timeout=5) as response:
            return json.loads(response.read(1024 * 1024))

    def check(presented, holder, holder_key, moment):
        issuer = presented['issuer']
        provider = providers[issuer]
        claims = (transport or fetch)(provider, presented['token'])
        audience = claims.get('aud', [])
        audience = [audience] if isinstance(audience, str) else audience
        if (claims.get('active') is not True or claims.get('iss') != issuer
                or provider['audience'] not in audience
                or not isinstance(claims.get('exp'), (float, int))
                or claims['exp'] <= moment.timestamp()
                or not isinstance(claims.get('iat'), (float, int))
                or not -60 <= moment.timestamp() - claims['iat'] <= provider.get('max_token_age_seconds', 300)
                or claims.get('nbf', 0) > moment.timestamp()):
            raise ValueError('invalid identity token')
        binding = provider['subjects'].get(claims.get('sub'))
        if not binding or binding.get('holder') != holder or binding.get('holder_key') != holder_key:
            raise ValueError('identity does not match the presenting agent')
        groups = claims.get('groups', [])
        if not isinstance(groups, list):
            raise TypeError('invalid groups')
        evidence = []
        for group, grant in provider.get('groups', {}).items():
            if group not in groups:
                continue
            # This adapter certifies qualifications only: a group cannot mint task grants.
            for scope in grant['scopes']:
                evidence.append({'issuer': issuer, 'types': ['Qualification'], 'scope': scope, 'dataset': None,
                                 'subject': {'id': holder}, 'id': issuer + '#group=' + urllib.parse.quote(group, safe='')})
        return evidence
    return check
