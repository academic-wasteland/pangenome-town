import copy
import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from pangenome_town.authority import keys
from pangenome_town.compute import ComputeError
from pangenome_town.compute import delegation as d


@pytest.fixture
def setup(towns, tmp_path, toy_data):
    catalogs = []
    private = {}
    for name, samples in [('ubar',['ksa001','ksa002']), ('yamatai',['NA18940'])]:
        key = keys.generate()
        path = tmp_path / name / 'custody.key'
        keys.save_private(key, path)
        private[name] = path
        catalogs.append({'id': name + '-cohort', 'version': 'toy-v1', 'custodian': name,
                         'public_key': keys.public_key_text(key), 'samples': samples,
                         'access': 'public-source', 'workflows': ['allele-frequency', 'genotype-export'],
                         'locations': {'shared': {'vcf': str(toy_data['vcf'] or '/missing.vcf.gz')}}})
    result = {}
    for name, key_path in private.items():
        result[name] = dataclasses.replace(towns[name], extra={
            'datasets': catalogs, 'custody': {'key': str(key_path)},
            'sites': [{'name': 'local', 'driver': 'local', 'storage': 'shared', 'tools': ['bcftools']}]
        })
    task = d.prepare(result['ubar'], requester='ubar', executor='yamatai', site='local',
                     datasets=['ubar-cohort'], workflow='allele-frequency', region='GRCh38:chr1:1-27')
    return result, task


def test_permission_before_compute_and_scoped_to_exact_task(setup, monkeypatch):
    towns, task = setup
    monkeypatch.setattr(d, 'run_task', lambda *a, **kw: pytest.fail('unauthorized compute'))
    with pytest.raises(ComputeError, match='waiting for ubar'):
        d.execute(towns['yamatai'], task, [])
    grant = d.approve(towns['ubar'], task)
    assert d.authorize(towns['yamatai'], task, [grant])[0][0]['samples'] == ['ksa001','ksa002']
    for field, value in [('executor','ubar'),('site','slurm'),('workflow','genotype-export'),
                         ('requester','outsider'),('region','GRCh38:chr1:1-10')]:
        changed = {**task, field:value}
        with pytest.raises(ComputeError):
            d.execute(towns['yamatai'], changed, [grant])
    changed = copy.deepcopy(grant)
    changed['release'] = ['anything']
    with pytest.raises(ComputeError):
        d.authorize(towns['yamatai'], task, [changed])
    with pytest.raises(ComputeError, match='waiting for ubar'):
        d.authorize(towns['yamatai'], task, [grant], now=datetime.now(UTC)+timedelta(hours=2))


def test_all_custodians_required_and_manifest_pinned(setup):
    towns, _ = setup
    task = d.prepare(towns['ubar'], requester='ubar', executor='yamatai', site='local',
                     datasets=['ubar-cohort','yamatai-cohort'], workflow='allele-frequency', region='GRCh38:chr1:1-27')
    ubar, yamatai = d.approve(towns['ubar'],task), d.approve(towns['yamatai'],task)
    with pytest.raises(ComputeError, match='waiting for yamatai'):
        d.authorize(towns['yamatai'], task, [ubar])
    assert len(d.authorize(towns['yamatai'], task, [ubar,yamatai])[0]) == 2
    town = dataclasses.replace(towns['yamatai'], extra=copy.deepcopy(towns['yamatai'].extra))
    town.extra['datasets'][0]['samples'] = ['NA18940']
    with pytest.raises(ComputeError, match='manifest mismatch'):
        d.authorize(town, task, [ubar,yamatai])


def test_real_cohort_selection_and_single_execution(setup, toy_data, monkeypatch):
    if not toy_data['vcf']:
        pytest.skip('bcftools required')
    towns, task = setup
    grant = d.approve(towns['ubar'], task)
    result = d.execute(towns['yamatai'], task, [grant])
    assert result['results'][0]['samples'] == ['ksa001','ksa002']
    text = result['results'][0]['outputs'][0]['text']
    assert 'chr1\t14\tC\tA\t1\t4\t0.25' in text  # Japanese sample has zero alternate alleles here.
    assert all(o['class'].endswith('AggregateArtifact') for o in result['results'][0]['outputs'])
    monkeypatch.setattr(d, 'run_task', lambda *a, **kw: pytest.fail('duplicate execution'))
    assert d.execute(towns['yamatai'], task, [grant]) == result


def test_wrong_key_controlled_data_and_unscoped_graph_refused(setup):
    towns, task = setup
    grant = d.approve(towns['ubar'], task)
    grant = keys.sign(grant, keys.generate(), 'fake')
    with pytest.raises(ComputeError, match='waiting for ubar'):
        d.authorize(towns['yamatai'],task,[grant])
    with pytest.raises(ComputeError, match='sample-scoped'):
        d.inspect_task(towns['yamatai'], {**task,'workflow':'deconstruct-region'})
    town = dataclasses.replace(towns['ubar'], extra=copy.deepcopy(towns['ubar'].extra))
    town.extra['datasets'][0]['access'] = 'controlled'
    with pytest.raises(ComputeError, match='RCP authority'):
        d.prepare(town, requester='ubar',executor='yamatai',site='local',datasets=['ubar-cohort'],workflow='allele-frequency',region='GRCh38:chr1:1-27')
