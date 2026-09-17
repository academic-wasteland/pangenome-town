import json

import pytest

from pangenome_town.phenotype_labels import prepare, resolve
from pangenome_town.phenotypes import search


@pytest.fixture
def settings(tmp_path):
    (tmp_path / 'phenotype-labels.json').write_text(json.dumps({'source': 'fixture:2026', 'sha256': 'digest', 'terms': {
        'HP:0001083': {'label': 'Ectopia lentis', 'synonyms': ['Lens dislocation']},
        'HP:0001166': {'label': 'Arachnodactyly'},
        'MP:0006296': {'label': 'arachnodactyly'},
    }}))
    return {'data': str(tmp_path)}


def test_labels_synonyms_identifiers_and_both_ontologies(settings):
    ids, evidence = resolve(settings, ['lens dislocation', 'HPO: arachnodactyly', 'MP: arachnodactyly', 'HP:0001083'])
    assert ids == ['HP:0001083', 'HP:0001166', 'MP:0006296']
    assert evidence['terms'][0]['match'] == 'exact synonym'
    assert evidence['sha256'] == 'digest'


@pytest.mark.parametrize('label, message', [('arachnodactyly', 'Ambiguous'), ('ectopia lenti', 'Possible matches'), ('made up phenotype', 'Unknown')])
def test_no_silent_guessing(settings, label, message):
    with pytest.raises(ValueError, match=message):
        resolve(settings, [label])


def test_legacy_ids_without_index(tmp_path):
    assert resolve({'data': str(tmp_path)}, ['HP:0001083']) == (['HP:0001083'], None)
    with pytest.raises(ValueError, match='not installed'):
        resolve({'data': str(tmp_path)}, ['Ectopia lentis'])


def test_prepare_excludes_obsolete_and_broad_synonyms(tmp_path):
    source, dest = tmp_path / 'source.owl', tmp_path / 'labels.json'
    source.write_text('''<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:owl="http://www.w3.org/2002/07/owl#" xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#" xmlns:o="http://www.geneontology.org/formats/oboInOwl#">
    <owl:Ontology><owl:versionIRI rdf:resource="urn:test:version"/></owl:Ontology>
    <owl:Class rdf:about="http://purl.obolibrary.org/obo/HP_0001083"><rdfs:label>Ectopia lentis</rdfs:label><o:hasExactSynonym>Lens dislocation</o:hasExactSynonym><o:hasBroadSynonym>Eye defect</o:hasBroadSynonym></owl:Class>
    <owl:Class rdf:about="http://purl.obolibrary.org/obo/MP_0000001"><rdfs:label>obsolete</rdfs:label><owl:deprecated>true</owl:deprecated></owl:Class></rdf:RDF>''')
    assert prepare(source, dest) == 1
    data = json.loads(dest.read_text())
    assert data['terms']['HP:0001083']['synonyms'] == ['Lens dislocation']
    assert len(data['sha256']) == 64


def test_service_resolves_before_scoring(settings, monkeypatch):
    from types import SimpleNamespace
    seen = []
    def learned(config, query):
        seen.append(query)
        return {'ok': True, 'results': [{'gene': 'MGI:1', 'score': .6}]}
    monkeypatch.setattr('pangenome_town.indigena.search', learned)
    town = SimpleNamespace(extra={'phenotype_search': dict(settings, enabled=True, backend='indigena', model='fixture')})
    result = search(town, {'phenotypes': ['Lens dislocation']})
    assert seen == [{'phenotypes': ['HP:0001083'], 'limit': 10}]
    assert result['resolution']['terms'][0]['label'] == 'Ectopia lentis'
    with pytest.raises(ValueError, match='Ambiguous'):
        search(town, {'phenotypes': ['arachnodactyly']})
    assert len(seen) == 1
