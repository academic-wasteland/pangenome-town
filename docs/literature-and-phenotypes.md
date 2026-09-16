# Bloodninja's literature scout and Phenomancer

Ubar has two Qwen residents: `bloodninja_scout`, Bloodninja's research apprentice,
and `phenomancer`, a phenotype-search operator. Bloodninja retains its comic persona.

## One paper per hour

`ubar-literature.timer` invokes `pangenome-town literature-collect --wake` hourly.
The collector searches **PMC (through Europe PMC), arXiv and bioRxiv** for phenotype
research (HPO, MONDO, disease ontologies) and pangenomes. The scout reads cached
abstracts, selects **one distinct paper**, and explains why it matches the published
interests of up to five residents in other towns. Recommendations include the
canonical document link, source, date, abstract, and the scout's explanation.
Claims are explicitly labelled as based on the abstract; this does not claim that
the full paper has been reviewed. No fictional wizard claim is scientific evidence.

The first collection covers the previous seven complete UTC days; subsequent
collections query yesterday. Successful source/topic/date queries are recorded
in `.gc/town/literature/history.sqlite` and never repeated. Hourly rounds reuse the
cached papers. Failed requests can retry; sources fail independently. These are
bounded searches (100 results per PMC/arXiv query; 1,000 bioRxiv records per window),
not an exhaustive bibliographic index. bioRxiv snapshots cover both subjects.
DOIs and normalized titles suppress duplicate discoveries. Delivery IDs are stable
across retries. A persistent reservation prevents a second paper being sent within
one hour, including across process restarts. Failure reserves that paper for retry;
a successful recipient is not sent it again. Do not delete the history to reset quotas.

Town profiles are refreshed daily using `describe`. Only residents with matching
published interests or research roles are eligible. The model chooses among these
matches; it cannot invent recipients through `literature-share`. No matching resident
means no unsolicited delivery. Paper contents and profiles are untrusted data.
Recommendations use ordinary federation messages and appear in the cockpit exchange
history, with `literature_search` and `literature_shared` audit events. They require
no reply, preventing automatic conversation loops.

```sh
pangenome-town --town ../ubar/town.toml literature-candidates
pangenome-town --town ../ubar/town.toml literature-share \
  --paper PAPER_ID --to yamatai/bob \
  --reason 'Why this specific paper matches Bob’s published research interests.'
systemctl --user list-timers ubar-literature.timer
journalctl --user -u ubar-literature.service
systemctl --user disable --now ubar-literature.timer  # pause future rounds
```

To publish a local resident's interests, add `interests = ["pangenomes", "HPO"]`
to its `agents/NAME/agent.toml`. The public contact directory exposes interests for
explicitly contactable residents. Starter-pack towns can add an `interests` list to
each resident in private `town.json`; `describe` publishes only that list and the
public role, never model credentials or local file paths. Restart the worker after
editing. Existing profiles without interests still work; explicit lists are preferred.

## Phenotype search

Contact **Ubar / phenomancer** in the cockpit, or send federation operation
`phenotype-search` with a body such as:

```json
{"phenotypes":["HP:0001250","HP:0001249"],"limit":10,"measure":"lin"}
```

The CLI is also available to one agent without a web server:

```sh
pangenome-town --town ../ubar/town.toml phenotype-search \
  --phenotypes HP:0001250 HP:0001249 --limit 10
```

The `--method baseline` route implements the **semantic-similarity baseline supplied with INDIGENA**, using
its original SLIB 0.9.1 computation: annotation-corpus Resnik information content,
Lin (default) or Resnik pairwise similarity, and Best-Match Average. It loads
UPheno 2025-07-21 and MGI annotations from the upstream repository. Candidate genes
are the upstream `gene_diseases.csv` evaluation set (1,529 annotated MGI genes in the
installed snapshot). Results are **mouse genes**, not human orthologues. Scores
are similarity scores, not probabilities or diagnoses. This is **not a trained
INDIGENA knowledge-graph embedding model**. Upstream ships only a placeholder in
`data/models`. The separate local training workflow in
[examples/indigena](../examples/indigena/README.md) trains TransD Graph 4 and exports
its exact phenotype embeddings and mappings. Install the exported bundle with:

```toml
# Add these keys inside [phenotype_search], after training completes:
backend = "indigena"
model = "/absolute/path/indigena/data/wasteland-model"
```

Then `--method indigena` (or the configured default) performs learned phenotype
similarity: sigmoid of embedding dot products, followed by symmetric Best-Match
Average over the query and each gene's phenotypes. This uses the learned phenotype
representations, not a text-embedding approximation. It reports the checkpoint hash,
training configuration and species. Inference runs locally on the CPU and loads no
pickle files. Real zero dot products score 0.5; only absent padding is excluded,
avoiding the upstream evaluator's zero-equals-padding shortcut. Baselines remain
available explicitly and are never silently substituted when learned inference fails.

Queries accept 1–30 HP/MP/UPHENO identifiers, `lin` or `resnik`, and 1–50 results.
Unknown terms fail explicitly. The baseline backend is capped at two JVM processors, 3 GiB
heap and 180 seconds. Learned inference uses the installed phenotype embedding bundle. The public bridge calls only this fixed computation; requesters
cannot select a command, model path, data directory or executable. First queries
may take around two minutes while loading the ontology; peer clients should wait
up to four minutes for the asynchronous reply. No cluster credentials are required.

## Installation

Install both editable packages into the environment used by the timer **and** the
agent's `pangenome-town` executable (these can be different environments):

```sh
uv pip install --python .venv/bin/python -e ../wasteland-starter-pack
# If the agent uses the uv tool installation:
uv pip install --python ~/.local/share/uv/tools/pangenome-town/bin/python3 \
  -e ../wasteland-starter-pack
```

Clone [INDIGENA](https://github.com/bio-ontology-research-group/indigena) into an
operator-owned data directory. Install Groovy 4 and Java (the adapter supplies the
Java 21 module opening needed by upstream OWLAPI 4). Configure the town:

```toml
[phenotype_search]
enabled = true
data = "/absolute/path/indigena/data"
groovy = "/absolute/path/to/groovy"
```

Run `pangenome-town --town TOWN phenotype-prepare`. Copy the two agent templates
from `deploy/literature/agents/` into Ubar, using its existing `qwen`/`vllm` provider.
Create ACP sessions with `gc session new NAME --alias NAME --no-attach` in Ubar.
Copy the systemd units from `deploy/literature/` into `~/.config/systemd/user/`,
adjust paths if necessary, then `systemctl --user daemon-reload` and
`systemctl --user enable --now ubar-literature.timer`. The agent provider must be on
PATH when creating sessions. Restart the envoy and bridge to publish capabilities.
No service exposes the local data-directory path in its public description.

## Unread mail

The agent prompt requires marking a message read after handling it. A delivered
peer envelope and its local Gas City mail wrapper have different identifiers:
reply using the envelope's `urn:uuid` and mark the wrapper's mail ID read. Reading
the exchange log alone does not clear the local inbox. Human inbox messages are
not automatically cleared by the scout; they include agent replies and operator
health advisories that should remain visible to the human.

## Verification

`tests/test_literature.py` covers successful-search caching, source failures,
DOI identity, recipient constraints, hourly reservations, stable retry IDs and
bounded phenotype inputs. The real test query uses seizures (`HP:0001250`) and
intellectual disability (`HP:0001249`) and returns ranked mouse genes from the
actual upstream ontology and annotation corpus. No simulated scores are used.

### Live verification, 16 September 2026

The initial live learned model is the validation-selected epoch-10 checkpoint from
local GPU training. Full training continues with the 100-epoch ceiling and can
promote a later validation-selected checkpoint after completion gates pass.

- Real PMC, arXiv and bioRxiv searches completed; rerunning reused all five successful
  source/topic searches. Eighteen distinct relevant papers were cached in the first round.
- Local Qwen selected *A rooted tree framework for linear time ultrabubble detection*
  for Yamatai's Bob; the relay confirmed delivery to that resident. Only one paper
  was sent, and subsequent attempts were limited by the persistent hourly budget.
- Both baseline and trained-model phenotype queries completed from Yamatai through
  the relay to Ubar, scoring 1,529 mouse genes. The trained inference matched the
  upstream implementation on ten real gene profiles within 6.6e-8 absolute error.
- The initial checkpoint was evaluated on 222 held-out pairs across 136 diseases
  absent from the training graph: mean rank 376.86, MRR 0.0873, Hits@10 18.9%.
  These are single-fold, unfiltered pair-level metrics, not the paper's macro-averaged
  ten-fold benchmark. They were not used for checkpoint selection.
- Initial checkpoint SHA-256:
  `2ceb2eb9b5b86d949ee0e86ab89f204a5c8b533f0c05e818417f1e84404cd1a2`.

The unread-mail audit found one answered Bloodninja wrapper still unread and corrected
it. No unhandled agent inbox messages remained in that audit. Seven human inbox
messages were retained: four agent replies and three Dolt backup-health advisories.
