import base64
import copy
import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from pangenome_town.demo_phenotypes import RECORD, PhenotypeStage
from pangenome_town.demo_stage import StageError
from pangenome_town.private_variants import CASE, rank
from pangenome_town.variant_interpretation import EVIDENCE, evaluate, interpret


@pytest.fixture
def configured(towns, tmp_path):
    vcf = tmp_path / 'patient.vcf'
    vcf.write_bytes((CASE / 'patient.vcf').read_bytes())
    metadata = tmp_path / 'resource.json'
    metadata.write_bytes((CASE / 'resource.json').read_bytes())
    result = dict(towns)
    result['yamatai'] = dataclasses.replace(towns['yamatai'], extra={'private_variant_demo': {'enabled': True, 'vcf': str(vcf), 'metadata': str(metadata)}})
    result['ubar'] = dataclasses.replace(towns['ubar'], extra={'variant_interpretation': {'enabled': True}, 'phenotype_search': {'enabled': True}})
    return result


GENES = ['HSF4', 'FOXE3', 'FBN1', 'MGI:87872', 'ADAMTSL4']
TERMS = ['HP:0001083', 'HP:0001166', 'HP:0002616']
VARIANT = {'assembly': 'GRCh38', 'chrom': '15', 'pos': 48421680, 'ref': 'T', 'alt': 'C'}


def test_private_local_rank_filters_and_no_sample_fields(configured):
    result = rank(configured['yamatai'], 'yamatai', {'genes': GENES})
    assert result['input_count'] == 6 and len(result['retained']) == 4
    assert result['retained'][0]['variant'] == VARIANT
    assert result['retained'][0]['gene_rank'] == 3
    assert result['retained'][0]['revel_rank'] == 2
    assert result['retained'][0]['combined_rank'] == 1
    assert max(result['retained'], key=lambda r: r['revel'])['gene'] == 'ADAMTSL4'
    swapped = rank(configured['yamatai'], 'yamatai', {'genes':['HSF4','FOXE3','ADAMTSL4','MGI:87872','FBN1']})
    assert swapped['retained'][0]['gene'] == 'ADAMTSL4'  # phenotype evidence actually changes the winner
    assert any('AF' in x['reason'] for x in result['excluded'])
    assert any('quality' in x['reason'] for x in result['excluded'])
    serialized = json.dumps(result)
    assert 'SYNTHETIC_YAMATAI_ONLY' not in serialized and '0/1' not in serialized
    # Remove phenotype support: the answer is not forced to be FBN1.
    other = rank(configured['yamatai'], 'yamatai', {'genes': ['HSF4', 'FOXE3']})
    assert all(x['gene'] != 'FBN1' for x in other['retained'])
    with pytest.raises(ValueError, match='owning town'):
        rank(configured['yamatai'], 'ubar', {'genes': GENES})
    with pytest.raises(ValueError, match='file paths'):
        rank(configured['yamatai'], 'yamatai', {'genes': GENES, 'vcf': '/etc/passwd'})
    Path(configured['yamatai'].extra['private_variant_demo']['vcf']).write_text('a real patient file')
    with pytest.raises(ValueError, match='synthetic VCF'):
        rank(configured['yamatai'], 'yamatai', {'genes': GENES})


def test_evidence_classification_is_not_a_lookup_of_the_answer():
    evidence = json.loads(EVIDENCE.read_text())
    criteria, classification = evaluate(evidence)
    assert classification == 'Likely pathogenic'
    assert {c['code'] for c in criteria if c['met']} == {'PS4', 'PM2_Supporting', 'PP2', 'PP3'}
    evidence['ps4_points_min'] = 0
    assert evaluate(evidence)[1] == 'Uncertain significance'
    evidence['revel'] = .4
    assert not next(c['met'] for c in evaluate(evidence)[0] if c['code'] == 'PP3')


def test_themis_pdf_and_strict_disclosure(configured):
    body = {'variant': VARIANT, 'phenotypes': TERMS}
    result = interpret(configured['ubar'], body)
    pdf = base64.b64decode(result['pdf_base64'])
    assert pdf.startswith(b'%PDF-') and hashlib.sha256(pdf).hexdigest() == result['pdf_sha256']
    assert result['report']['classification'] == 'Likely pathogenic'
    assert 'PP4' in ' '.join(result['report']['not_applied'])
    for extra in [{'vcf': 'private'}, {'patient': 'name'}, {'text': 'patient history'}]:
        with pytest.raises(ValueError, match='only one variant'):
            interpret(configured['ubar'], dict(body, **extra))
    unknown = dict(VARIANT, pos=48421681)
    result = interpret(configured['ubar'], {'variant': unknown, 'phenotypes': TERMS})
    assert result['report']['classification'] == 'Not classified' and not result['report']['criteria']


@pytest.mark.parametrize('revoke', [False, True])
def test_full_stage_transmits_only_selected_allele_and_resets_report(configured, tmp_path, revoke):
    requests = []
    class Client:
        def __init__(self):
            self.replies = {}
        def ask(self, town, *, operation, body, text=""):
            assert text
            mid = str(len(requests)); requests.append((town, operation, copy.deepcopy(body), text))
            if operation == 'message':
                from pangenome_town.research_intake import intake
                records = [{'@id': RECORD, 'kind': 'service', 'access': {'town': 'ubar', 'resident': 'phenomancer', 'operation': 'phenotype-search'}},
                           {'@id': 'urn:wasteland:fair:ubar:variant-interpretation', 'kind': 'service', 'access': {'town': 'ubar', 'resident': 'themis', 'operation': 'variant-interpretation'}}]
                result = intake(configured['ubar'], dict(body, text=text), records=records, sender='yamatai')
            elif operation == 'phenotype-search':
                result = {'ok': True, 'checkpoint_sha256': 'fixture', 'query': {'phenotypes': TERMS},
                          'candidate_count': 1529, 'orthology': {'source': 'fixture'},
                          'results': [{'gene': gene, 'score': .5, 'human_orthologue': {'symbol': gene}} for gene in GENES]}
                if revoke:
                    path = Path(configured['yamatai'].extra['private_variant_demo']['metadata'])
                    resource = json.loads(path.read_text())
                    resource['policy']['selected_variant_recipients'] = []
                    path.write_text(json.dumps(resource))
            else:
                result = interpret(configured['ubar'], dict(body, text=text))
            self.replies[mid] = {'id': 'reply'+mid, 'from': town, 'kind': 'answer', 'in_reply_to': mid, 'body': result}
            return mid
        def wait(self, mid, **kwargs): return [self.replies[mid]]
        def call(self, *args): pass
    description = {'record': {'@id': RECORD, 'version': 'sha256:fixture', 'access': {'town': 'ubar', 'operation': 'phenotype-search'}}, 'publication': 'listed', 'digest': 'fixture', 'checked': {}}
    stage = PhenotypeStage(configured, storage=tmp_path, client_factory=Client, catalogue_reader=lambda: description)
    with pytest.raises(StageError, match='no VCF uploads'):
        stage.act('start', {'phenotypes': TERMS, 'vcf': 'private real patient'})
    stage.start(private_case=True)
    stage.approve(stage.run['id']); stage.worker.join(10)
    if revoke:
        assert stage.run['state'] == 'failed'
        assert 'restrictions changed' in stage.run['events'][-1]['text']
        assert [r[1] for r in requests] == ['message', 'phenotype-search']
        with pytest.raises(StageError):
            stage.report_bytes()
        return
    assert stage.run['state'] == 'completed'
    assert stage.run['variant_benchmark']['recovered'] is True
    assert stage.report_bytes().startswith(b'%PDF-')
    assert len(requests) == 3
    assert 'private_case' not in requests[0][2]
    assert requests[0][2]['resources'][0]['policy']['raw_data_export'] is False
    assert requests[0][1] == 'message' and requests[0][3] == stage.run['human_message']
    assert set(requests[1][2]) == {'resident', 'phenotypes', 'limit', 'method', 'include_human_orthologues'}
    assert requests[2][:3] == ('ubar', 'variant-interpretation', {'resident': 'themis', 'variant': VARIANT, 'phenotypes': TERMS})
    assert 'SYNTHETIC_YAMATAI_ONLY' not in json.dumps(requests)
    sent = [e for e in stage.run['events'] if e['detail'].get('message_type') == 'sent']
    assert [e['text'] for e in sent] == [q[3] for q in requests]
    assert all(e['detail']['wire_body']['text'] == e['text'] for e in sent)
    received = [e for e in stage.run['events'] if e['detail'].get('message_type') == 'received']
    assert received[0]['text'] == stage.run['delegation']['text']
    assert received[-1]['text'] == received[-1]['detail']['body']['text']
    stage.reset(stage.run['id'])
    with pytest.raises(StageError): stage.report_bytes()


def test_contact_does_not_receive_forbidden_phenotypes(configured, tmp_path):
    path = Path(configured['yamatai'].extra['private_variant_demo']['metadata'])
    resource = json.loads(path.read_text())
    resource['policy']['phenotype_recipients'] = []
    path.write_text(json.dumps(resource))
    class Client:
        def ask(self, *args, **kwargs):
            pytest.fail('No relay disclosure should occur')
    stage = PhenotypeStage(configured, storage=tmp_path, client_factory=Client)
    stage.start(private_case=True)
    stage.approve(stage.run['id'])
    stage.worker.join(5)
    assert stage.run['state'] == 'failed'
    assert 'forbid sharing phenotypes' in stage.run['events'][-1]['text']
    assert not any(e['detail'].get('message_type') == 'sent' for e in stage.run['events'])
