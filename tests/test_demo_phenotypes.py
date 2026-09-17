import json
import runpy
from pathlib import Path

import pytest

from pangenome_town.demo_phenotypes import RECORD, PhenotypeStage
from pangenome_town.orthologues import annotate


def test_mapping_preserves_scores_and_excludes_conflicts(tmp_path):
    prepare = runpy.run_path(str(Path(__file__).parents[1] / 'examples/prepare_orthologues.py'))['prepare']
    source, dest = tmp_path / 'report', tmp_path / 'mapping.json'
    source.write_text('MGI:1\tMouse\t1\tHGNC:1\tHUMAN\t2\nMGI:1\tMouse\t3\tHGNC:1\tHUMAN\t2\nMGI:2\tOther\t4\tHGNC:2\tA\t5\nMGI:2\tOther\t4\tHGNC:3\tB\t6\n')
    prepare(source, dest)
    original = {'results': [{'gene': 'MGI:2', 'score': .9}, {'gene': 'MGI:1', 'score': .4}]}
    result = annotate(original, dest)
    assert result['results'][0]['human_orthologue'] is None
    assert result['results'][1]['human_orthologue']['symbol'] == 'HUMAN'
    assert [r['score'] for r in result['results']] == [.9, .4]
    assert 'human_orthologue' not in original['results'][1]
    assert json.loads(dest.read_text())['excluded_ambiguous'] == 1


@pytest.mark.parametrize('wrong_model', [False, True])
def test_live_request_requires_matching_fair_model(towns, tmp_path, wrong_model):
    class Client:
        acknowledged = False
        def ask(self, town, *, operation, body, text=""):
            self.operation = operation
            assert text
            if operation == "message":
                return "intake"
            assert town == 'ubar' and operation == 'phenotype-search'
            assert body['include_human_orthologues'] is True and body['limit'] == 20
            assert body['method'] == 'indigena'
            return 'request'
        def wait(self, mid, **kwargs):
            if mid == 'intake':
                return [{'id':'intake-reply', 'from':'ubar', 'kind':'answer', 'in_reply_to':mid, 'body':{'ok':True, 'text':'Use Phenomancer.', 'plan':[{'town':'ubar','resident':'phenomancer','operation':'phenotype-search','record':RECORD}]}}]
            return [{'id': 'reply', 'from': 'ubar', 'kind': 'answer', 'in_reply_to': mid,
                     'body': {'ok': True, 'checkpoint_sha256': 'other' if wrong_model else 'model',
                              'orthology': {'source': 'MGI'}, 'candidate_count': 100,
                              'results': [{'gene': 'MGI:1', 'score': .4}]}}]
        def call(self, path, body):
            assert path == '/v1/ack'
            if body == {'id': 'reply'}:
                self.acknowledged = True
    client = Client()
    description = {'record': {'@id': RECORD, 'version': 'sha256:model',
                             'access': {'town': 'ubar', 'operation': 'phenotype-search'}},
                   'publication': 'listed', 'digest': 'digest', 'checked': 'today'}
    stage = PhenotypeStage(towns, storage=tmp_path, client_factory=lambda: client,
                           catalogue_reader=lambda: description)
    with pytest.raises(ValueError):
        stage.start(['MONDO:0000001'])
    for unpaired in ['Ectopia lentis', 'HP:0001083']:
        with pytest.raises(ValueError, match='label and identifier together'):
            stage.start([unpaired])
    stage.start()
    run_id = stage.run['id']
    assert stage.run['attribution'] == 'Academic Wasteland'
    stage.approve(run_id)
    stage.worker.join(5)
    assert stage.run['state'] == ('failed' if wrong_model else 'completed')
    assert (stage.run['result'] is None) == wrong_model
    assert client.acknowledged
    assert stage.run['fair'] == description
    stage.reset(run_id)
    assert stage.run is None
