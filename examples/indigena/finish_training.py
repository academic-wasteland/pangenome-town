"""After successful training, verify and atomically activate the selected model."""
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from pangenome_town.config import load
from pangenome_town.exchange import ExchangeLog
from pangenome_town.indigena import search


def finish(town_path):
    town=load(Path(town_path))
    log=ExchangeLog(town.exchange_db)
    unit='ubar-indigena-training.service'
    while subprocess.check_output(['systemctl','--user','show',unit,'-p','ActiveState','--value'],text=True).strip() in {'active','activating','deactivating'}:
        time.sleep(20)
    code=subprocess.check_output(['systemctl','--user','show',unit,'-p','ExecMainStatus','--value'],text=True).strip()
    if code!='0':
        raise RuntimeError('training exited '+code+'; existing model remains active')
    pointer=Path(town.extra['phenotype_search']['model'])
    if not pointer.is_symlink():
        raise ValueError('model activation requires an operator-created version pointer')
    model=pointer.parent/'wasteland-model'
    metadata=model/'metadata.json'
    info=json.loads(metadata.read_text())
    checkpoint=model.parent.parent/ info['checkpoint']
    if hashlib.sha256(checkpoint.read_bytes()).hexdigest()!=info['checkpoint_sha256']:
        raise ValueError('checkpoint digest mismatch')
    journal=subprocess.check_output(['journalctl','--user','-u',unit,'--all','-o','cat','--no-pager'],text=True)
    best=re.findall(r'Epoch (\d+) - New best validation inductive MR: ([0-9.]+)\. Model saved',journal)
    if not best:
        raise ValueError('validation selection record is missing')
    info['checkpoint_epoch']=int(best[-1][0]);info['validation_mean_rank']=float(best[-1][1])
    old=json.loads((pointer/'metadata.json').read_text())
    if info['validation_mean_rank']>old['validation_mean_rank']:
        raise ValueError('selected model regresses on validation mean rank')
    metadata.write_text(json.dumps(info))
    result=search({'model':str(model)},{'phenotypes':['HP:0001250','HP:0001249'],'limit':5})
    if result['candidate_count']!=1529 or len(result['results'])!=5:
        raise ValueError('unexpected model candidate set')
    replacement=pointer.with_name(pointer.name+'.next')
    replacement.symlink_to(model.name)
    replacement.replace(pointer)
    result['activation']='selected checkpoint from completed training; version pointer switched atomically'
    (town.state_dir/'indigena-training-status.json').write_text(json.dumps(result,indent=2))
    log.event(town.name,'indigena_training_completed',None,{'checkpoint_epoch':info['checkpoint_epoch'],
              'checkpoint_sha256':info['checkpoint_sha256'],'validation_mean_rank':info['validation_mean_rank']})
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    try:
        finish(sys.argv[1])
    except Exception as error:
        town=load(Path(sys.argv[1]))
        ExchangeLog(town.exchange_db).event(town.name,'indigena_training_failed',None,{'error':str(error)})
        raise
