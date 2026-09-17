from types import SimpleNamespace

import pytest

from pangenome_town.research_intake import intake


def test_human_intake_routes_only_supported_services():
    town = SimpleNamespace(name='ubar', extra={'phenotype_search': {'enabled': True}, 'variant_interpretation': {'enabled': True}})
    body = {'text': 'Help investigate this synthetic case without receiving my VCF.', 'resident': 'contact',
            'phenotypes': ['HPO: Ectopia lentis'], 'private_case': True}
    result = intake(town, body)
    assert [s['operation'] for s in result['plan']] == ['phenotype-search', 'private-variant-rank', 'variant-interpretation']
    assert 'Keep the VCF at Yamatai' in result['text']
    assert len(intake(town, dict(body, private_case=False))['plan']) == 1
    for extra in [{'vcf': 'patient'}, {'text': ''}, {'text': 'x'*1201}, {'private_case': 'yes'}, {'resident': 'themis'}]:
        with pytest.raises(ValueError):
            intake(town, dict(body, **extra))
