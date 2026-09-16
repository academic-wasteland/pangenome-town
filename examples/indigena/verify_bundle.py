"""Compare the installed NumPy inference formula with upstream on learned vectors."""
import json
import sys
from pathlib import Path

import numpy as np
import torch

from pangenome_town.indigena import bma_scores, bundle

root=Path(sys.argv[1]).resolve()
info,vectors,names,flat,offsets,counts=bundle(str(root),tuple((root/n).stat().st_mtime_ns for n in ['metadata.json','embeddings.npy']))
terms=['http://purl.obolibrary.org/obo/HP_0001250','http://purl.obolibrary.org/obo/HP_0001249']
query=[info['entities'][t] for t in terms]
# Small nontrivial subset keeps the proof lightweight while using actual learned data.
subset=10
lengths=counts[:subset]
ends=int(lengths.sum())
expected=bma_scores(vectors,query,flat[:ends],offsets[:subset],lengths)
profiles=torch.zeros(subset,int(lengths.max()),vectors.shape[1])
for i in range(subset):
    profiles[i,:int(lengths[i])]=torch.from_numpy(np.array(vectors[flat[offsets[i]:offsets[i]+lengths[i]]]))
from evaluation import compare_vectorized

actual=compare_vectorized(profiles,torch.from_numpy(np.array(vectors[query])),torch.tensor(lengths,dtype=torch.float32)).numpy()
np.testing.assert_allclose(actual,expected,atol=1e-6)
print(json.dumps({'ok':True,'comparison':'upstream INDIGENA compare_vectorized versus installed inference',
                  'genes_checked':subset,'checkpoint_sha256':info['checkpoint_sha256'],
                  'max_absolute_difference':float(np.max(np.abs(actual-expected)))},indent=2))
