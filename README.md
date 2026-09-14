# pangenome-town

A [Gas City](https://github.com/gastownhall/gascity) pack plus a small Python
package that turns one lab-hosted city into a *pangenome town*: it holds one
population pangenome graph, answers questions from peer towns with
deterministic `vg`/`bcftools` queries, and logs every exchange for inspection.
Towns built from this pack join [The Academic Wasteland](https://github.com/academic-wasteland/research-commons).

First towns: [Ubar](https://github.com/academic-wasteland/ubar) (Saudi
population, KSA samples) and [Yamatai](https://github.com/academic-wasteland/yamatai)
(Japanese population, JPT samples), both serving the JaSaPaGe graph
(Kulmanov et al. 2025, *Scientific Data* 12:1316, doi:10.1038/s41597-025-05652-y).

## What the pack provides

| Piece | Purpose |
|---|---|
| `[[service]] envoy` | Unix-socket HTTP service, proxied by the supervisor at `/v0/city/<city>/svc/envoy`. Receives envelopes from peers, logs them, mails them to the town's agent. |
| `agents/townsfolk` | Rig-scoped agent (OpenRouter, GLM 5.3 Flash by default) that answers peer questions from query artifacts only. |
| `formulas/answer-peer.toml` | Formula v2 workflow for one question; its exec check passes only when an answer is logged. |
| `orders/inbox-dispatch.toml` | Condition order: pending questions become `answer-peer` workflows slung to the agent. |
| `commands/town/*` | `gc town status|send|query|inbox`. |
| `doctor/*` | vg/bcftools, OpenRouter key, graph presence, exchange log. |
| `pangenome-town` CLI | `envoy`, `send`, `query`, `answer`, `inbox`, `messages`, `town-info`, `peer-info`, `doctor`, `dashboard`, `rcp …`, `commons …`. |
| `contract/pangenome.ofn.tmpl` | The semantic contract (OWL 2 functional syntax) rendered per town; see step 3. |
| `pangenome-town dashboard` | Loopback web dashboard: towns, exchanges, agent activity, RCP validation, commons ledger (step 2 and 3c). |

## Exchange protocol (step 1)

Towns exchange JSON envelopes:

```json
{"schema_version": 1, "id": "urn:uuid:…", "kind": "question", "from": "ubar", "to": "yamatai",
 "in_reply_to": null, "created": "2026-09-14T12:00:00Z",
 "body": {"text": "How many JPT samples carry variants in this window?", "region": "GRCh38:chr6:31000000-31050000"},
 "attachments": [{"name": "variants.json", "sha256": "sha256:…", "path": "…", "media_type": "application/json"}]}
```

Every envelope a town sends or receives lands once in the shared SQLite log
(`data/exchange.db`), and each processing step (received, delivered,
dispatched, query, answered, failures) is an event row. The `rcp` column is
reserved for Research Commons Protocol messages and validation reports.

## Dashboard (step 2)

`pangenome-town dashboard --towns ../ubar/town.toml ../yamatai/town.toml` serves
`http://127.0.0.1:8390/` (loopback only): town cards (city, envoy, data, sessions,
agent spend from opencode's database), a two-lane timeline of exchanges with live
updates over SSE, and a detail view per exchange (timeline, attachments rendered,
the agent's tool calls and text, raw envelope, and for RCP tasks the validation
report, verdict, claims, and commons rows). The ledger section shows commons
standing, stamps, wanted items, and completions.

## Research standards (step 3)

The Research Commons Protocol (RCP, `research-commons` package) sits on top of
the envelope plumbing. Each town publishes an A2A Agent Card at
`/.well-known/agent-card.json` whose RCP extension carries the town's semantic
contract (manifest id, ontology bundle digest, accepted and rejected task
classes, assertion policy) and accepts JSON-RPC `message/send`, `tasks/get`,
`tasks/cancel` on `/a2a`.

**Contract.** `contract/pangenome.ofn.tmpl` is rendered per town
(`pangenome-town rcp render-contract`) into `<city>/contract/<town>.ofn` and a
manifest with digests. It defines, in the shared namespace
`https://w3id.org/academic-wasteland/pangenome-town/contract/` (`pg:`):

- task classes `BasicQueryTask` (GraphSummary, RegionExtraction, HaplotypePresence,
  RegionVariantListing, GeneLookup) and `LargeAnalysisTask` (ReadMappingVariantCalling,
  WholeGraphDeconstruct, PopulationComparison), disjoint; region-based tasks must
  use a `GenomicRegion`, read mapping a `ReadSet`;
- data classes `PangenomeGraph ⊑ rc:PublicDataset`, `GenomicRegion` with disjoint
  `GRCh38Region` and `CHM13Region`, `Gene`, `ReadSet`;
- receiver-only requester classes `ReputableRequester`, `UnvettedRequester`,
  `BlockedRequester` (pairwise disjoint), which a sender may not assert;

and per town (`town:` = `https://w3id.org/academic-wasteland/<town>/`):

- `town:ServedGraph` with the one served graph individual;
- `AcceptedBasicTask ≡ BasicQueryTask ⊓ ∃usesDataset.ServedGraph ⊓ ∃requestedBy.(Actor ⊓ ¬BlockedRequester)`;
- `AcceptedLargeTask ≡ LargeAnalysisTask ⊓ ∃usesDataset.ServedGraph ⊓ ∃requestedBy.ReputableRequester`;
- `PermissionRequiredTask ≡ LargeAnalysisTask ⊓ ∃usesDataset.ServedGraph ⊓ ∃requestedBy.UnvettedRequester`;
- `RejectedTask ≡ (ResearchTask ⊓ ∃usesDataset.RestrictedDataset) ⊔ (ResearchTask ⊓ ∃requestedBy.BlockedRequester)`;
- `ConformingContribution ≡ ResearchContribution ⊓ ∃producedBy.Agent ⊓ ∃hasOutput.ResearchArtifact ⊓ ∃hasEvidence.Evidence`.

**Pipeline** (`pangenome_town.rcp.pipeline`). A task goes through structure
(JSON Schema), SHACL, the sender's semantic assertions (allowlisted classes and
properties only), the receiver's assertion of the requester's standing, and
SROIQ reasoning with `km` (Kobayashi-MaRust): consistency, then instance checks
for admissibility, prohibition, and permission requirement. Outcomes map to A2A
states: `entailed` executes (basic queries and the population comparison run
inline, the rest is queued for the townsfolk agent), `contradicted`/`invalid`
reject, `unknown` with an entailed permission requirement is `input-required`,
`indeterminate` (no reasoner) fails and nothing executes. Every contribution is
validated against `ConformingContribution` before it is returned.

**Reputation as currency** (`pangenome_town.rcp.commons`, `reputation`). The
Wasteland commons (a Dolt database created with `wl create <org>/commons
--local-only`, shared by both towns on one workstation) is the ledger. When a
town completes a task it writes the `wanted` row (posted by the requester's
handle) and its `completions` row; the requester verifies what came back
(structure, artifact digests) and writes a `stamps` row on the contributor's
handle carrying the semantic validation report. Standing of a handle is

```
score = Σ severity_weight · confidence · (2·quality − 1) · exp(−age_days / 180)
```

over stamps on that handle (quality 1.0 for `entailed`, 0.5 for `unknown`,
0 otherwise; self-stamps are impossible by schema). A requester with
`score ≥ reputation_threshold` from at least `reputation_min_authors` distinct
stampers is asserted `ReputableRequester`, otherwise `UnvettedRequester`;
`[rcp].blocked_requesters` gives `BlockedRequester`. So a town earns the right
to run large analyses on its peers by answering their basic questions well.

```bash
pangenome-town rcp submit --to yamatai --kind variants --region GRCh38:chr6:29940000-29950000
pangenome-town rcp submit --to ubar --kind compare --region GRCh38:chr2:135800000-135900000
pangenome-town commons leaderboard
pangenome-town commons rows stamps
```

Not yet done: federation of the commons across institutions (Dolt fork and pull
request through `wl join`), large analyses beyond the population comparison
(whole-graph deconstruct, read mapping) which stay queued for an agent, and
requester-side semantic re-validation of returned contributions against the
peer's contract (the requester currently checks structure and digests and
carries the peer's own report in the stamp).

## Queries

`pangenome-town query --kind <summary|haplotypes|variants|subgraph|compare> --region GRCh38:chr6:31000000-31050000`

All queries are deterministic wrappers around `vg` (GBZ graph) and
`bcftools` (the shipped GRCh38 VCF), restricted to the town's samples, with
full provenance (argv, versions, input fingerprints, timings) in the JSON
artifact. Region strings are validated and never shell-interpolated.

## Install

```bash
uv tool install --editable .        # gives you `pangenome-town` on PATH
uv run --extra dev pytest
```

Gas City resolves formula check scripts against the rig, so copy `scripts/pt-reply-check.sh` into `<city>/rig/scripts/` (the city bootstrap does this).

Gas City side: import the pack from a city (`[imports.pangenome-town]` in
`pack.toml`), add `townsfolk` to a rig, set `PT_TOWN_TOML` in the workspace
env, and put `OPENROUTER_API_KEY` in `~/.gc/secrets.env`. See the Ubar repo
for a complete city.

## Security posture

Everything binds to loopback or unix sockets. The supervisor's service proxy
has no authentication, so never expose port 8372 beyond localhost without a
reverse proxy. Envelope bodies are size-limited and treated as untrusted
data; the agent prompt says so explicitly.
