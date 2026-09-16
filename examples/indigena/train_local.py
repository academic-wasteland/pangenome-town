"""Train upstream INDIGENA TransD G4 locally; export a non-pickle inference bundle.

Run from an operator-owned upstream checkout with this script's directory on PYTHONPATH.
The model and disease-disjoint data construction remain upstream's. The epoch ceiling
is configurable; validation still selects the checkpoint without using test outcomes.
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import torch


def export(model, factory, gene2pheno, eval_genes, checkpoint):
    out = Path(os.environ.get('INDIGENA_BUNDLE', 'data/wasteland-model'))
    out.mkdir(parents=True, exist_ok=True)
    with torch.no_grad():
        vectors = model.entity_representations[0](indices=None).detach().cpu().numpy()
    np.save(out / 'embeddings.npy', vectors, allow_pickle=False)
    metadata = {
        'method': 'INDIGENA TransD Graph 4 inductive / sigmoid dot-product phenotype BMA',
        'species': 'Mus musculus', 'fold': 0, 'seed': 0, 'embedding_dim': vectors.shape[1],
        'max_epochs': int(os.environ.get('INDIGENA_EPOCHS','100')),
        'checkpoint_epoch': int(os.environ.get('INDIGENA_CHECKPOINT_EPOCH','0')) or None,
        'validation_mean_rank': float(os.environ.get('INDIGENA_VALIDATION_MR','nan')) if os.environ.get('INDIGENA_VALIDATION_MR') else None,
        'upstream_commit': subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        'checkpoint': str(checkpoint), 'checkpoint_sha256': hashlib.sha256(Path(checkpoint).read_bytes()).hexdigest(),
        'entities': factory.entity_to_id, 'genes': {g: gene2pheno[g] for g in eval_genes},
        'notice': 'Research model trained locally; mouse genes, not human diagnoses. Single fold, not a reproduced ten-fold benchmark.',
    }
    (out/'metadata.json').write_text(json.dumps(metadata))
    print('EXPORTED_INFERENCE_BUNDLE',str(out.resolve()),flush=True)


def main():
    os.environ['WANDB_MODE']='offline'
    if not hasattr(np, 'trapz'):
        np.trapz = np.trapezoid  # upstream metric compatibility with NumPy 2.4
    torch.set_num_threads(4)
    source=Path('kge_transd.py').read_text()
    source=source.replace('mowl.init_jvm("10g")','mowl.init_jvm("4g")')
    source=source.replace('num_epochs=1000,', 'num_epochs=int(os.environ.get("INDIGENA_EPOCHS", "100")),')
    # Preserve mapping/model alignment while in upstream's training scope.
    source=source.replace('    # Evaluate on test set',
        '    from train_local import export\n    export(model, triples_factory, gene2pheno, eval_genes, model_out_filename)\n\n    # Evaluate on test set')
    sys.argv=['kge_transd.py','--fold','0','--mode','inductive','--graph4','--embedding_dim','400',
              '--batch_size','8192','--learning_rate','0.001','--no_sweep','--description','wasteland-local-service']
    exec(compile(source,'kge_transd.py','exec'),{'__name__':'__main__','__file__':str(Path('kge_transd.py').resolve())})  # noqa: S102 - trusted upstream code


if __name__=='__main__':
    main()
