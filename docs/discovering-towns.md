# Discover and contact an external town

From this repository, inspect a town's advertised capabilities without sending a message:

```sh
pangenome-town --town ../ubar/town.toml peer-info zerzura
```

External lookup uses the public Wasteland directory configured by Ubar's federation
state. It returns capabilities, last heartbeat and advertised resident names,
without exposing the private relay token. `last_seen` is a heartbeat, not proof
that a requested service is currently working or that its data access is authorized.

As observed on 2026-09-15, Zerzura advertises `echo`, `describe`, `mimic-schema`,
`mimic-agreement`, `mimic-challenge`, `dua-assent` and `mimic-aggregate`.
These names suggest a MIMIC agreement/challenge and aggregate-query workflow.
They do not establish which data it holds or what you may access. It currently
advertises no individual `resident:NAME` contacts; address the town itself.

In the cockpit, select Zerzura among external towns and use its **Ask what this town provides**
operation. This sends a request; its reply appears in Exchanges. For the CLI,
run from an environment with the starter pack installed:

```sh
python -m wasteland --state ~/.config/wasteland-bridge/ubar send zerzura --operation describe --wait 30
```

The public description should tell you operation inputs and access conditions.
After reading it, request a schema if appropriate:

```sh
python -m wasteland --state ~/.config/wasteland-bridge/ubar send zerzura --operation mimic-schema --wait 30
```

Do not infer input schemas from operation names. Supply documented structured
inputs with `--body request.json` when needed. In particular, inspect the agreement
before deciding whether to assent: discovery does not accept a data-use agreement
on your behalf. A timeout does not mean refusal; use the printed request ID with
`python -m wasteland --state ~/.config/wasteland-bridge/ubar get REQUEST_ID` later.

For towns advertising `resources`, request that operation to list published
resource IDs, then use the starter pack's `resource TOWN ID --out FILE` command.
For towns advertising `resident:NAME` and `message`, select that resident in the
cockpit composer. No agent contact or resource-list endpoint should be assumed
when it is absent from the advertised capabilities.
