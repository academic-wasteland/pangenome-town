# Dataset custody and delegated execution

A town is a custodian and/or executor, not a physical server. Ubar controls the
Saudi cohort; Yamatai controls the Japanese cohort. Both are logical views of
`/home/asianhla/data/JaSaPaGe/JaSaPaGe.GRCh38.vcf.gz` on DDBJ. The catalogs pin the
custodian key, sample manifest, version, permitted workflows and storage location.
An executor always selects the dataset's samples, never its own town's samples.
No dataset files were copied or split.

| Route | Ubar | Yamatai |
|---|---|---|
| `ddbj-direct` (SSH gateway, bounded direct query) | available | available |
| `ddbj` (`ssh ddbj` → `ssh a001` → Slurm) | unavailable | available |
| Saudi cohort | Ubar grant required | Ubar grant required |
| Japanese cohort | Yamatai grant required | Yamatai grant required |

Direct queries use one CPU, at most 2 GiB in the declared site envelope and a
600-second timeout. The remote direct driver does not enforce a kernel memory
limit: these are small approved template queries, not general batch workloads.
Heavy work goes through Yamatai's Slurm site. Sharing the Unix account is an
application policy boundary, not OS isolation against an independent SSH shell.
For stronger isolation provision distinct accounts/keys and ACLs or an execution
service which alone can read the files. No shared-account ACLs were changed.

## Approve exactly what will run

From the `academic-wasteland` directory (paths may be local to different laptops):

```sh
pangenome-town --town ubar/town.toml delegate catalog
pangenome-town --town ubar/town.toml delegate prepare \
  --requester ubar --executor yamatai --site ddbj \
  --dataset ubar-jasapage --workflow allele-frequency \
  --region GRCh38:chr6:29940000-29940100 --out saudi-task.json
cat saudi-task.json
pangenome-town --town ubar/town.toml delegate approve saudi-task.json \
  --minutes 60 --out ubar-grant.json
pangenome-town --town ubar/town.toml delegate send saudi-task.json --grant ubar-grant.json
```

`approve` is an explicit custodian/operator decision, not an automatic resident
behavior. Keep custody keys outside repos; share only the signed grant. Both town
catalogs must agree on the manifest and public-key pin. `send` uses the public
relay even for neighboring local towns; their running bridges execute it.
`delegate run TASK --grant GRANT` is the equivalent executor-side operator command.

The task binds its ID, original requester, executor, site, workflow, region and
all dataset manifests. The signature binds that entire task plus release classes
and an expiry (default 15 minutes, maximum 60). All custodians must sign a joint
task: repeat `--dataset`, approve the same JSON in each owning town, then repeat
`--grant` when sending. Joint tasks run the same workflow separately per cohort;
they do not claim a combined statistical analysis or merge individual genotypes.
The signed requester is the approved attribution; it does not establish an ORCID
identity or supply an ethics credential.

The executor checks every grant before any compute, and again before releasing
results. An absent grant returns `input-required`. Changing the requester, site,
region, workflow or dataset invalidates the grant. An atomic local execution
claim prevents duplicate submissions for the same task ID. A retry with still
valid grants returns the saved result. A crash or failed claimed execution needs
operator inspection before preparing a new task ID; it is not automatically
resubmitted. Expired grants also refuse cached-result access. There is currently
no distributed early-revocation service for these short-lived grants.

## Scope and compatibility

This adapter accepts only explicitly configured **public-source** logical
cohorts with `allele-frequency` or `genotype-export` workflows. Aggregate grants
cannot be changed into genotype exports. Graph-wide deconstruction is refused
because its current template does not constrain the output to the named cohort.
Existing RCP-controlled tasks still require their ethics/data-access credentials;
this adapter rejects controlled catalog entries rather than bypassing those
gates. Existing local public query routes and RCP contracts remain available.

Results include the custodian, executor, dataset manifest, sample list, site,
Slurm job ID where applicable, output class and output digest. Paths and SSH keys
are not sent to peers. Only declared released outputs leave the execution site;
aggregate genotype intermediates stay remote and are deleted. Inline results are
bounded; oversized outputs are retained locally and the request reports failure
for operator review. Manifest hashes pin configuration, not the bytes of the
entire VCF: update the dataset version/catalog when changing the input release.

The cockpit's task trace separates permission required, authorization, execution
handoff, actual scheduler states and release. System → Datasets and custody shows
configured routes, not evidence that a job has executed.

## Verification on 15 September 2026

- Real DDBJ direct queries for both logical cohorts succeeded.
- Yamatai executed the Saudi cohort using Ubar's grant in Slurm job **20582073**.
- Unsigned requests were denied before execution. Replaying each successful task
  returned its recorded result without another job submission.
- A separate test-town identity sent an unsigned request through the public
  HTTPS relay, received `input-required`, then received a successful Saudi result
  after the exact task was signed by Ubar and executed by Yamatai.
- Tests cover task mutation, forged keys, expiry, all-custodian approval, manifest
  mismatch, controlled-data refusal, graph-scope refusal, true sample selection
  with bcftools, and execution replay.

Remote output fetch now streams through SSH for both direct and nested routes.
This fixes a live gateway SCP/SFTP hang observed during verification.
