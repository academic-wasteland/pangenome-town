"""CPU inference from locally trained INDIGENA phenotype embeddings, without pickle."""
import json
from functools import lru_cache
from pathlib import Path


def bma_scores(vectors, query, flat, offsets, counts):
    import numpy as np
    unique, inverse = np.unique(flat, return_inverse=True)
    logits = vectors[unique] @ vectors[query].T
    similarities = 1.0 / (1.0 + np.exp(-np.clip(logits, -80, 80)))
    pairs = similarities[inverse]
    left = np.add.reduceat(pairs.max(axis=1), offsets) / counts
    right = np.maximum.reduceat(pairs, offsets, axis=0).mean(axis=1)
    return (left + right) / 2


@lru_cache(maxsize=1)
def bundle(path, signature):
    import numpy as np
    root = Path(path)
    info = json.loads((root/'metadata.json').read_text())
    vectors = np.load(root/'embeddings.npy', mmap_mode='r', allow_pickle=False)
    mapping = info['entities']
    names = sorted(info['genes'])
    profiles = [[mapping[p] for p in info['genes'][g]] for g in names]
    if not names or any(not p for p in profiles) or vectors.ndim != 2:
        raise ValueError('invalid locally installed INDIGENA bundle')
    flat = np.array([v for p in profiles for v in p],dtype=np.int64)
    counts = np.array([len(p) for p in profiles],dtype=np.int64)
    offsets = np.concatenate(([0],np.cumsum(counts)[:-1]))
    return info,vectors,names,flat,offsets,counts


def search(settings, query):
    import numpy as np
    root = Path(settings['model']).expanduser().resolve()
    signature = tuple((root/name).stat().st_mtime_ns for name in ['metadata.json','embeddings.npy'])
    info,vectors,names,flat,offsets,counts = bundle(str(root),signature)
    ids = ['http://purl.obolibrary.org/obo/'+p.replace(':','_') for p in query['phenotypes']]
    unknown = [p for p in ids if p not in info['entities']]
    if unknown:
        raise ValueError('phenotypes absent from this trained model: '+', '.join(unknown))
    indices = [info['entities'][p] for p in ids]
    scores = bma_scores(vectors,indices,flat,offsets,counts)
    if not np.isfinite(scores).all():
        raise ValueError('trained model produced non-finite scores')
    order = sorted(range(len(names)),key=lambda i:(-float(scores[i]),names[i]))[:query['limit']]
    return {'ok':True, 'method':info['method'], 'species':info['species'], 'candidate_count':len(names),
            'query':query, 'checkpoint_sha256':info['checkpoint_sha256'],
            'training':{k:info.get(k) for k in ['fold','seed','embedding_dim','max_epochs','checkpoint_epoch','validation_mean_rank','upstream_commit']},
            'notice':info['notice'], 'source':'https://github.com/bio-ontology-research-group/indigena',
            'results':[{'gene':names[i].rsplit('/',1)[-1].replace('_',':'), 'score':float(scores[i]),
                        'phenotype_count':int(counts[i])} for i in order]}
