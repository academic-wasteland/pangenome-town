import dataclasses
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

spec=importlib.util.spec_from_file_location('finish_training',Path(__file__).parents[1]/'examples/indigena/finish_training.py')
activation=importlib.util.module_from_spec(spec)
spec.loader.exec_module(activation)


def setup(towns,tmp_path,monkeypatch,code='0',digest_ok=True):
    data=tmp_path/'data';old=data/'old';new=data/'wasteland-model'
    old.mkdir(parents=True);new.mkdir()
    current=data/'current';current.symlink_to(old)
    (old/'metadata.json').write_text(json.dumps({'validation_mean_rank':400.0}))
    checkpoint=data/'selected.pt';checkpoint.write_bytes(b'checkpoint')
    (new/'metadata.json').write_text(json.dumps({'checkpoint':'data/selected.pt',
        'checkpoint_sha256':hashlib.sha256(b'checkpoint').hexdigest() if digest_ok else 'wrong'}))
    town=dataclasses.replace(towns['ubar'],extra={'phenotype_search':{'model':str(current)}})
    town.state_dir.mkdir(parents=True,exist_ok=True)
    monkeypatch.setattr(activation,'load',lambda _:town)
    def command(args,**kwargs):
        if args[0]=='journalctl': return 'Epoch 20 - New best validation inductive MR: 300.0000. Model saved.'
        return 'inactive' if 'ActiveState' in args else code
    monkeypatch.setattr(activation.subprocess,'check_output',command)
    monkeypatch.setattr(activation,'search',lambda *args:{'candidate_count':1529,'results':[{}]*5})
    return current,old,new


def test_successful_training_atomically_promotes_validated_checkpoint(towns,tmp_path,monkeypatch):
    current,old,new=setup(towns,tmp_path,monkeypatch)
    activation.finish('town.toml')
    assert current.resolve()==new
    assert old.is_dir()


@pytest.mark.parametrize('code,digest_ok,match',[('1',True,'training exited'),('0',False,'digest mismatch')])
def test_failure_keeps_serving_previous_model(towns,tmp_path,monkeypatch,code,digest_ok,match):
    current,old,_new=setup(towns,tmp_path,monkeypatch,code,digest_ok)
    with pytest.raises((RuntimeError,ValueError),match=match): activation.finish('town.toml')
    assert current.resolve()==old
