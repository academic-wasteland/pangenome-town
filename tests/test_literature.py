import json
import time

import pytest

from pangenome_town import literature as lit
from pangenome_town.phenotypes import validate


def populate(town):
    with lit.connect(town) as db:
        for n in range(2):
            p = lit.paper('pmc', str(n), 'Human pangenome variation', 'Pangenome methods', 'https://example.org/paper', '2026-09-15')
            db.execute('INSERT INTO papers(id,document) VALUES(?,?)', (p['id'],json.dumps(p)))
        db.execute('INSERT INTO directories VALUES(?,?,?)', ('yamatai', time.time(), json.dumps({
            'residents': [{'name':'sam','interests':['pangenomes'],'role':'Researcher'},
                          {'name':'wizard','interests':['role play'],'role':'Wizard'}]})))
    return lit.candidates(town)


def test_one_paper_budget_and_stable_retry(towns, monkeypatch):
    town = towns['ubar']
    papers = populate(town)
    sent = []
    class Client:
        def send(self, value):
            sent.append(value)
            if len(sent) == 1:
                raise OSError('simulated network failure')
    monkeypatch.setattr(lit,'client',lambda town: Client())
    args = (town,papers[0]['id'],['yamatai/sam'],'A pangenome paper relevant to your published pangenome interests.')
    with pytest.raises(OSError):
        lit.share(*args)
    result = lit.share(*args)
    assert result['sent'] == ['yamatai/sam']
    assert sent[0]['id'] == sent[1]['id']
    assert lit.share(*args)['sent'] == []
    with pytest.raises(ValueError, match='budget'):
        lit.share(town,papers[1]['id'],['yamatai/sam'],args[-1])
    with pytest.raises(ValueError, match='published'):
        lit.share(town,papers[0]['id'],['yamatai/wizard'],args[-1])


def test_search_history_skips_successful_queries(towns, monkeypatch):
    calls = []
    def search(*args):
        calls.append(args)
        return [lit.paper('pmc','a','HPO phenotype ontology','Phenotype research','https://example.org/a','2026-09-15')]
    monkeypatch.setattr(lit,'search_source',search)
    monkeypatch.setattr(lit,'discover',lambda *args: [])
    lit.collect(towns['ubar'])
    lit.collect(towns['ubar'])
    assert len(calls) == 5
    with lit.connect(towns['ubar']) as db:
        assert db.execute('SELECT count(*) FROM papers').fetchone()[0] == 1


def test_failed_search_remains_retryable(towns, monkeypatch):
    def broken(*args):
        raise OSError('offline')
    monkeypatch.setattr(lit,'search_source',broken)
    monkeypatch.setattr(lit,'discover',lambda *args: [])
    report = lit.collect(towns['ubar'])
    assert len(report['errors']) == 5
    with lit.connect(towns['ubar']) as db:
        assert db.execute('SELECT count(*) FROM searches').fetchone()[0] == 0


def test_doi_dedup_and_topics():
    a=lit.paper('pmc','PMC1','A phenotype paper','','https://example.org','', 'https://doi.org/10.1/ABC')
    b=lit.paper('biorxiv','x','A phenotype paper','','https://example.org','', '10.1/abc')
    assert a['id'] == b['id']
    assert lit.topics('MONDO and the human pangenome') == ['pangenomes','phenotypes']
    assert not lit.topics('unrelated wizard report')


@pytest.mark.parametrize('body', [
    {'phenotypes': []}, {'phenotypes':['HP:0001250; rm -rf x']}, {'phenotypes':['HP:0001250'],'limit':True},
    {'phenotypes':['HP:0001250'],'limit':51}, {'phenotypes':['HP:0001250'],'measure':'shell'},
])
def test_phenotype_request_bounds(body):
    with pytest.raises(ValueError):
        validate(body)


def test_valid_phenotype_request():
    assert validate({'phenotypes':['HP:0001250','HP:0001250']}) == {
        'phenotypes':['HP:0001250'],'limit':10,'measure':'lin'}


def test_failed_delivery_is_a_candidate_next_hour(towns, monkeypatch):
    town = towns['ubar']
    papers = populate(town)
    class Offline:
        def send(self, value):
            raise OSError('offline')
    monkeypatch.setattr(lit,'client',lambda town: Offline())
    original = time.time()
    with pytest.raises(OSError):
        lit.share(town,papers[0]['id'],['yamatai/sam'],'Relevant pangenome research for your published interests.')
    monkeypatch.setattr(lit.time,'time',lambda: original+3601)
    assert papers[0]['id'] in [p['id'] for p in lit.candidates(town)]
    sent=[]
    class Online:
        def send(self,value):
            sent.append(value)
    monkeypatch.setattr(lit,'client',lambda town: Online())
    lit.share(town,papers[0]['id'],['yamatai/sam'],'Relevant pangenome research for your published interests.')
    assert len(sent)==1
