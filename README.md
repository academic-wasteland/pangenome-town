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
| `pangenome-town` CLI | `envoy`, `send`, `query`, `answer`, `inbox`, `messages`, `town-info`, `peer-info`, `doctor`. |

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

## Queries

`pangenome-town query --kind <summary|haplotypes|variants|subgraph> --region GRCh38:chr6:31000000-31050000`

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
