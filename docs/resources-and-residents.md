# Sharing generated resources and meeting the residents

Both Ubar and Yamatai serve selected generated outputs from
`~/Public/software/pangenome/hla-viz`: top-level TSV/PDF results, stage-3 tables,
figures, plots and per-gene statistics. Ubar additionally serves local temporal-KG
reviews, formal definitions, example ontologies and validation reports from
`~/Public/software/temporal-kg`.

`[[resources]]` entries in each `town.toml` explicitly select collections and
patterns. The source trees are read-only; existing producer agents continue
working normally. New matching results appear without importing or copying them.
Files must be within the selected root; hidden files and escaping symlinks are
excluded. Download chunks carry a version digest and refuse changes mid-transfer.
Results are current producer outputs, not automatically scientific validations.
Cached third-party papers and arbitrary source-tree files are not published by
these collections. A publisher should finish files with an atomic rename so
readers cannot observe a partially written output between write operations.

## Local access

```sh
pangenome-town --town ubar/town.toml resources
pangenome-town --town yamatai/town.toml resources
```

Cockpit → System → Published results and documents provides search and direct
browser downloads. The same collection/path has the same resource ID in both
towns. Each download reflects the file version read at that time.

## From a hackathon laptop

```sh
python3 -m wasteland send ubar --operation resources --wait 60
python3 -m wasteland resource ubar RESOURCE_ID --out result.tsv
```

Catalog responses contain up to 40 resources and a `next_offset`; to page, send
`{"operation":"resources","offset":40}` with `send ubar --body request.json`.
Resources travel in 24,000-byte base64 chunks through authenticated mail. The
client verifies offsets, total length and SHA-256, never overwrites an existing
output, and publishes the completed download atomically. Large downloads are
slower than a direct file server. The serving limit is 50 MiB per resource.

## Q — Ubar

Q, from the Q Continuum, specializes in time: temporal knowledge graphs, formal
definitions, validation reports and the project's open questions. Q reads the
local temporal-KG documents and cites their filenames/sections/resource IDs.
Scientific answers distinguish definitions, validation evidence and speculation.
Q uses the existing local Qwen provider on unimatrix01 and has no custody role.

## Bloodninja — Ubar and Yamatai

Both towns have a local-Qwen Bloodninja for comic role-play. Replies begin:

> I put on my robe and wizard hat

Subsequent turns escalate into increasingly ridiculous fictional wizardry.
“Defense” means imaginary town defense only. These residents have no data access
approval, compute, security or administration role and do not autonomously
message one another. These restrictions are persona/tool-use instructions, not
an OS sandbox around the shared local user account.

Use Cockpit → Mail & residents to chat locally. From another town, send:

```json
{"operation":"message","resident":"q","text":"Explain the current temporal definition and cite its validation report."}
```

or use `"resident":"bloodninja"` in either town. The immediate response confirms
delivery only; the resident's actual answer arrives separately. Keep the local
workstation and Qwen service available for resident replies. Resident `agent.toml`
files explicitly set `session = "acp"` for the local OpenCode/Qwen transport;
after configuration changes, allow Gas City to drain and restart the session
before sending another prompt.

The bridge service pins `PT_GC_BIN` to the installed Gas City executable. This is
important because `/usr/bin/gc` can be Graphviz's unrelated graph-counting tool.
Named-resident delivery requires a positive JSON receipt with a mail ID; a zero
exit code alone does not count as delivery. These requests are claimed before
the generic research dispatcher can turn them into scientific workflows.
