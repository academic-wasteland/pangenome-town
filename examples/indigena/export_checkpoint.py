"""Export an immutable snapshot of a validation-selected upstream checkpoint."""
import argparse
import os
import shutil
import sys
from pathlib import Path

import numpy as np
import torch

parser=argparse.ArgumentParser()
parser.add_argument('--epoch',type=int,required=True)
parser.add_argument('--validation-mr',type=float,required=True)
args=parser.parse_args()
os.environ['INDIGENA_CHECKPOINT_EPOCH']=str(args.epoch)
os.environ['INDIGENA_VALIDATION_MR']=str(args.validation_mr)
os.environ['WANDB_MODE']='offline'
os.environ['INDIGENA_BUNDLE']='data/wasteland-model-bootstrap'
torch.set_num_threads(2)
if not hasattr(np,'trapz'):
    np.trapz=np.trapezoid
original=Path('data/models/transd_inductive_fold_0_seed_0_dim_400_bs_8192_lr_0.001_graph4.pt')
snapshot=Path('data/models/wasteland-validation-snapshot.pt')
shutil.copyfile(original,snapshot)
source=Path('kge_transd.py').read_text()
source=source.replace('mowl.init_jvm("10g")','mowl.init_jvm("4g")')
source=source.replace('.to("cuda")','.to("cpu")')
source=source.replace('model_out_filename = f"data/models/{file_identifier}.pt"', 'model_out_filename = "data/models/wasteland-validation-snapshot.pt"')
source=source.replace('    # Evaluate on test set',
    '    from train_local import export\n    export(model, triples_factory, gene2pheno, eval_genes, model_out_filename)\n    return\n\n    # Evaluate on test set')
sys.argv=['kge_transd.py','--fold','0','--mode','inductive','--graph4','--embedding_dim','400',
          '--batch_size','8192','--learning_rate','0.001','--no_sweep','--only_test']
exec(compile(source,'kge_transd.py','exec'),{'__name__':'__main__','__file__':str(Path('kge_transd.py').resolve())})  # noqa: S102 - trusted upstream
