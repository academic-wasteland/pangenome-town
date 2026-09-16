import json

import numpy as np
import pytest

from pangenome_town.indigena import bma_scores, search


def test_bma_matches_explicit_pairwise_best_match_average():
    vectors=np.array([[1.,0.],[0.,1.],[-1.,0.],[.5,.5]])
    profiles=[[0,1],[2],[1,3]]
    flat=np.array([x for p in profiles for x in p])
    counts=np.array([len(p) for p in profiles]);offsets=np.array([0,2,3])
    query=[0,3]
    expected=[]
    for p in profiles:
        matrix=1/(1+np.exp(-(vectors[p]@vectors[query].T)))
        expected.append((matrix.max(axis=1).mean()+matrix.max(axis=0).mean())/2)
    np.testing.assert_allclose(bma_scores(vectors,query,flat,offsets,counts),expected)


def test_bundle_mapping_and_unknown_terms(tmp_path):
    np.save(tmp_path/'embeddings.npy',np.array([[1.,0.],[0.,1.],[-1.,0.]]))
    root='http://purl.obolibrary.org/obo/'
    info={'entities':{root+'HP_0001250':0,root+'MP_0000001':1,root+'MP_0000002':2},
          'genes':{'http://mowl.borg/MGI_1':[root+'MP_0000001'],'http://mowl.borg/MGI_2':[root+'MP_0000002']},
          'method':'test fixture','species':'Mus musculus','checkpoint_sha256':'fixture',
          'fold':0,'seed':0,'embedding_dim':2,'max_epochs':1,'upstream_commit':'fixture','notice':'fixture'}
    (tmp_path/'metadata.json').write_text(json.dumps(info))
    result=search({'model':str(tmp_path)},{'phenotypes':['HP:0001250'],'limit':1})
    assert result['candidate_count']==2 and result['results'][0]['gene']=='MGI:1'
    assert result['results'][0]['score']==.5  # real zero dot product is not padding
    with pytest.raises(ValueError,match='absent'):
        search({'model':str(tmp_path)},{'phenotypes':['HP:9999999'],'limit':1})
