"""Acceptance and adversarial cases for independent certification providers."""
import runpy
from pathlib import Path

import pytest

from pangenome_town.authority import certification, identity, keys
from pangenome_town.authority import credentials as c

examples = runpy.run_path(str(Path(__file__).parents[1] / 'examples/certification_scenarios.py'))
World, NOW = examples['World'], examples['NOW']


@pytest.mark.parametrize('scenario', examples['SCENARIOS'], ids=lambda fn: fn.__name__)
def test_acceptance_scenario(scenario):
    scenario()


def test_unknown_status_delegation_cannot_confer_trust():
    w = World()
    policy = w.policy(w.rule('board', depth=1))
    delegation = c.accreditation(accreditor='board', accreditor_key=w.keys['board'], subject_issuer='lab',
                                 subject_public_key=keys.public_key_text(w.keys['lab']), roles=[],
                                 types=['Qualification'], scopes=[examples['SCOPE']], now=NOW)
    w.status = lambda doc: 'unknown' if 'Accreditation' in doc['type'] else 'active'
    assert not w.assess(policy, [w.credential('lab')], [delegation])['ok']


def test_missing_status_checker_is_not_acceptance():
    w = World()
    policy = w.policy(w.rule('board'))
    assert not certification.evaluate(policy, w.task, w.presentation([w.credential('board')]), audience=w.audience,
                                      anchors={'board': keys.public_key_text(w.keys['board'])}, now=NOW)['ok']


def test_oidc_group_provenance_subject_key_and_freshness():
    w = World()
    issuer = 'https://keycloak.example/realms/yamatai'
    provider = {'audience': 'wasteland', 'subjects': {'service-sub': {'holder': examples['AGENT'],
                 'holder_key': keys.public_key_text(w.holder)}}, 'groups': {'/aggregate-certified': {'scopes': [examples['SCOPE']]}}}
    claims = {'active': True, 'iss': issuer, 'sub': 'service-sub', 'aud': ['wasteland'],
              'iat': NOW.timestamp(), 'exp': NOW.timestamp() + 60, 'groups': ['/aggregate-certified']}
    check = identity.checker({issuer: provider}, transport=lambda *_: claims)
    presentation = c.present(holder=examples['AGENT'], holder_key=w.holder, credentials=[], accreditations=[],
                             task=w.task, audience=w.audience, now=NOW,
                             identity_tokens=[{'issuer': issuer, 'token': 'opaque-test-token'}])
    policy = w.policy(w.rule(issuer))
    def assess():
        return certification.evaluate(policy, w.task, presentation, audience=w.audience, anchors={}, now=NOW,
                                      identity_checker=check)
    assert assess()['ok']
    for field, invalid in [('iss', 'https://impostor'), ('sub', 'bloodninja'), ('aud', ['another-town']),
                           ('active', False), ('exp', NOW.timestamp()), ('groups', [])]:
        old = claims[field]
        claims[field] = invalid
        assert not assess()['ok'], field
        claims[field] = old
    provider['subjects']['service-sub']['holder_key'] = keys.public_key_text(keys.generate())
    assert not assess()['ok']


def test_empty_policy_rejected():
    w = World()
    with pytest.raises(ValueError):
        certification.evaluate({}, w.task, None, audience=w.audience, anchors={})


def test_issuer_specific_urls():
    doc = World().credential('fremen')
    assert doc['id'].startswith('fremen/credentials/')
    assert doc['credentialStatus']['id'].startswith('fremen/status/')


def test_delegation_cannot_escape_dataset_by_omitting_types():
    w = World()
    policy = w.policy(w.rule('board', depth=1))
    delegate = c.accreditation(accreditor='board', accreditor_key=w.keys['board'], subject_issuer='lab',
                               subject_public_key=keys.public_key_text(w.keys['lab']), roles=[],
                               types=['Qualification'], scopes=[examples['SCOPE']], datasets=[examples['SAUDI']], now=NOW)
    certificate = w.credential('lab')
    w.task['usesDataset'] = [{'@id': examples['SAUDI']}]
    assert w.assess(policy, [certificate], [delegate])['ok']
    policy['issuers'][0]['datasets'].append(examples['JAPAN'])
    w.task['usesDataset'] = [{'@id': examples['JAPAN']}]
    assert not w.assess(policy, [certificate], [delegate])['ok']


def test_public_policy_excludes_private_provider_configuration():
    w = World()
    policy = w.policy(w.rule('board'))
    policy['secret'] = 'secret-value'
    policy['issuers'][0]['client_secret'] = 'secret-value'
    advertised = certification.describe(policy)
    assert 'secret-value' not in str(advertised)
    assert advertised['issuers'][0]['issuer'] == 'board'
    assert advertised['rechecked'] == ['admission', 'output-release']
