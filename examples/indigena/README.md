# Local INDIGENA training

`train_local.py` runs the upstream TransD Graph 4 inductive fold-0 model with
400-dimensional embeddings, batch size 8192, learning rate 0.001 and seed 0.
Validation selects the best checkpoint; the held-out test set is not used for
selection. The service-training ceiling is 100 epochs (`INDIGENA_EPOCHS` overrides
it); upstream's patience of five validation rounds and evaluation every ten epochs
are retained. This is one trained fold, not the paper's complete ten-fold experiment.

Run in a clone of bio-ontology-research-group/indigena, with its Python dependencies,
Java and a compatible PyTorch GPU build installed. The laptop run uses Python 3.11
and PyTorch 2.11.0+cu128 on the RTX PRO 2000 Blackwell laptop GPU. Installed package
versions are recorded in `training-environment.txt`; install Torch from the cu128
index before the remaining Python packages:

```sh
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python torch --index-url https://download.pytorch.org/whl/cu128
uv pip install --python .venv/bin/python pykeen mowl-borg pandas click wandb
```

Start training:

```sh
PYTHONPATH=/path/to/pangenome-town/examples/indigena:$PWD \
  .venv/bin/python /path/to/pangenome-town/examples/indigena/train_local.py
```

The adapter reduces the JVM heap to 4 GiB and exports the selected model's phenotype
embeddings, entity mapping, gene annotations and checkpoint hash to
`data/wasteland-model/`. The original PyTorch state dict remains in `data/models/`.
Inference consumes `.npy` with `allow_pickle=False` and a JSON mapping, not executable
pickle content. Keep the two exported files together. Training and inference use
the same mapping from the live upstream triples factory.

A validation-selected intermediate model can be deployed while the longer run
continues. `export_checkpoint.py --epoch 10 --validation-mr 348.9912` snapshots the
checkpoint before reconstructing the upstream triples factory and exports to the
separate `data/wasteland-model-bootstrap/` directory. Use the epoch and validation
metric actually recorded for that checkpoint; do not infer them from the epoch ceiling.
`verify_bundle.py MODEL_DIR` compares ten actual learned gene profiles against the
upstream `compare_vectorized` implementation.

The live configuration points to `data/wasteland-model-current`, a symlink to an
immutable model version. `finish_training.py TOWN_TOML` waits for the local
`ubar-indigena-training.service` to finish successfully, checks the checkpoint hash,
validation record and inference output, and switches that pointer atomically. Failed
training or validation leaves the current model serving requests. No bridge restart
is required when switching versions. Run this watcher with the application Python
environment, not the training environment.

```sh
systemctl --user status ubar-indigena-training ubar-indigena-activation
journalctl --user -u ubar-indigena-training --all
```
