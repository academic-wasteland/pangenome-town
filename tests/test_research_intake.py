import copy
import json
from types import SimpleNamespace

import pytest

from pangenome_town.private_variants import CASE
from pangenome_town.research_intake import intake


def advertised():
    return [{'@id': 'urn:wasteland:fair:ubar:' + identifier, 'kind': 'service',
             'access': {'town': 'ubar', 'resident': resident, 'operation': operation}}
            for identifier, resident, operation in [
                ('indigena', 'phenomancer', 'phenotype-search'),
                ('variant-interpretation', 'themis', 'variant-interpretation')]]


def test_plan_uses_advertisements_and_resource_policy():
    town = SimpleNamespace(name='ubar', extra={'phenotype_search': {'enabled': True}, 'variant_interpretation': {'enabled': True}})
    resource = json.loads((CASE / 'resource.json').read_text())
    body = {'text': 'Help diagnose this patient and explain the evidence.', 'resident': 'contact',
            'phenotypes': ['Ectopia lentis (HP:0001083)'], 'resources': [resource]}
    def plan(body=body, records=None, sender='yamatai'):
        return intake(town, body, records=advertised() if records is None else records, sender=sender)
    result = plan()
    assert [s['operation'] for s in result['plan']] == ['phenotype-search', 'private-variant-rank', 'variant-interpretation']
    assert 'metadata allows only yamatai' in result['text']
    assert len(plan(dict(body, resources=[]))['plan']) == 1
    # Resident selection really follows the advertised service, not a hardcoded name.
    changed = advertised()
    changed[0]['access']['resident'] = 'new-genetics-service'
    assert plan(records=changed)['plan'][0]['resident'] == 'new-genetics-service'
    for records in [[], advertised()[:1], advertised() + advertised()]:
        with pytest.raises(ValueError, match='advertised and permitted'):
            plan(records=records)
    with pytest.raises(ValueError, match='authenticated owner'):
        plan(sender='another-town')
    for field, value in [('compute_towns', []), ('phenotype_recipients', []),
                         ('selected_variant_recipients', []), ('raw_data_export', True)]:
        restricted = copy.deepcopy(body)
        restricted['resources'][0]['policy'][field] = value
        with pytest.raises(ValueError):
            plan(restricted)
    for extra in [{'vcf': 'patient'}, {'text': ''}, {'text': 'x'*1201}, {'resources': 'file'}, {'resident': 'themis'}]:
        with pytest.raises(ValueError):
            plan(dict(body, **extra))
