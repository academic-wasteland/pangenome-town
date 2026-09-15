# Resources, credentials, and compute in pangenome towns

Status: design for step 4, implemented as a minimal working example (2026-09-14).
Scope: Ubar and Yamatai (pangenome towns) plus Camelot, a new authority town that hosts demo authorities.

## 1. Problem

Steps 1 to 3 gave each town a semantic contract (OWL, decided by `km`) and a reputation ledger
(Wasteland stamps). That covers two questions: is this a task we serve, and has the requester earned
our compute. Real research infrastructure asks more:

1. **Compute lives somewhere else.** The JaSaPaGe pangenomes (and more) sit on the DDBJ gateway under
   `/home/asianhla/`. Work should run where the data are. A town reaches that platform only because
   someone gave it an account and a key. Not every town or agent has that access.
2. **Some data need credentials that no amount of reputation can buy.** Individual-level genotypes
   need an ethics approval (IRB) and a data access committee (DAC) decision with an approved research
   scope. Those decisions come from institutions, not from peers, and they expire or get revoked.
3. **Description logic reasoning is one gate, not the whole policy.** Signatures, expiry, revocation,
   secret custody, and resource quotas are not entailment problems.

## 2. Design principles

- **P1. Authority is presented; capability is exercised.** A requester credential (IRB approval, DAC
  authorization, accreditation) is a signed, public document that travels with a request and can be
  checked by anyone. A holder capability (SSH key, cluster account, API token) never appears in a
  message. It lives in exactly one secrets store and is only used by the town that holds it.
- **P2. Lending compute means offering execution, never lending access.** A town without a capability
  for a site does not get the key. It gets a referral to a town that can run the work there.
- **P3. Four gates, each decided by the right mechanism.**

  | Gate | Question | Decided by | Can be bought with reputation |
  |---|---|---|---|
  | Admissibility | Is this a well-formed task we serve on data we hold? | `km` over the contract | no |
  | Authority | Does the principal hold verified, in-scope, trusted credentials? | code verifies facts, `km` decides trust and scope | **never** |
  | Standing | Should we spend scarce compute on this requester? | commons stamps, `km` decides the consequence | yes |
  | Capability | Can we actually run it, within limits? | site driver, OS limits, scheduler | no |

- **P4. Code produces facts, the reasoner decides policy.** Cryptographic verification, clock checks,
  revocation lookups, and health probes run in Python. Their *results* enter the ontology as receiver
  assertions (`VerifiedCredential(c)`, `accreditedBy(x, y)`, `ReachableSite(s)`). Trust transitivity,
  scope coverage, and acceptance are entailments over those facts.
- **P5. Scope is a class, not a string.** A DAC approves a class of tasks drawn from a scope library in
  the contract. Coverage is an instance check; an out-of-scope request is a contradiction, not a
  missing string match. The same scope also constrains what may leave the town as output.
- **P6. Refusals are machine-actionable.** Every refusal names its gate and what would fix it: which
  credential from which trust anchor, which peer can run the work, how much standing is missing.
- **P7. Enforcement is layered.** Gates 1 to 3 decide whether to use a capability. Gate 4 bounds what
  the capability can do even if everything above it was fooled: tool allowlist, argument checks,
  resource limits, a dedicated account, scheduler quotas.

## 3. Components

```
                    Camelot (authority town, demo authorities)
                    +-------------------------------------------+
                    | envoy: registrar + Agent Card              |
                    |  issuers: ethics-council (trust anchor)    |
                    |           wasteland-irb, ubar-dac          |
                    |  applications -> human approval -> signed  |
                    |  credentials, status (revocation), keys    |
                    +-------------------------------------------+
                          ^ apply / fetch            ^ status lookups
                          |                          |
 researcher (ORCID) --- holder key --- presentation (signed, bound to one task)
                          |
                          v
 Yamatai town  --A2A task + presentation-->  Ubar town
                                              envoy -> pipeline
                                                1 admissibility (km)
                                                2 authority (verify -> assert -> km)
                                                3 standing (commons -> assert -> km)
                                                4 capability (site health -> assert -> km)
                                              -> rigger / compute runner
                                                   workflow template -> validator -> site driver
                                                   (local | ssh [+ slurm])
                                              -> contribution -> output scope check -> release
```

### 3.1 Sites and the rigger (compute agent)

Each town declares the compute sites it can reach in `town.toml`:

```toml
[[sites]]
name = "workstation"
driver = "local"
datasets = ["graph", "vcf", "ksa-individual-genotypes"]
tools = ["vg", "bcftools", "tabix"]
max_cpus = 8
max_mem_gb = 32
max_wall_seconds = 1800

[[sites]]
name = "ddbj"                    # Slurm route declared by Yamatai only; storage is shared
driver = "ssh"
host = "ddbj"                    # ~/.ssh/config alias for the gateway; the key never leaves the agent/keyring
submit_host = "a001"             # login node behind the gateway that runs sbatch
workdir = "/home/leechuck/wasteland/yamatai"
scheduler = "slurm"
enabled = true
datasets = ["graph", "vcf"]
paths = { graph = "/home/asianhla/data/JaSaPaGe/JaSaPaGe.gbz", vcf = "/home/asianhla/data/JaSaPaGe/JaSaPaGe.GRCh38.vcf.gz" }
```

A site is **reachable** when it is enabled and its driver health check passes (local: tools present;
ssh: `ssh -o BatchMode=yes host true`). Reachability is a receiver assertion, recomputed per task.

The **rigger** is a town resident with two halves:

- a deterministic half (`pangenome_town.compute`): a workflow template library, a validator, site
  drivers, and a runner that packages artifacts into a contribution;
- an optional LLM half (a Gas City agent on the lab's local Qwen) that proposes or adjusts workflow
  parameters. Anything it writes is data: the validator re-checks every workflow before execution,
  so the model is a proposer and never an authority.

Workflow validation rejects: unknown template, tools outside the site allowlist, argument tokens with
shell metacharacters, absolute paths or `..`, placeholders other than `{region}`, `{samples}`,
`{work}`, `{threads}`, `{data:<key>}`, outputs outside the work directory, output classes outside the
contract allowlist, and resource requests above site limits. The local driver enforces wall time and
address-space limits with `setrlimit`; the ssh driver runs a generated script under `timeout` or submits
it with `sbatch --time --cpus-per-task --mem`, so the cluster enforces the envelope too.

Data locality is part of the contract: datasets `residesOn` sites (static, closed per dataset), and
`LargeAnalysisTask` and `ControlledAccessTask` acceptance requires a dataset residing on a
`ReachableSite`. When no reachable site holds the data, `NoCapabilityTask` is entailed and the refusal
carries referrals to peers whose Agent Cards advertise a reachable site holding the same graph.

### 3.2 Credentials

Credentials follow the shape of W3C Verifiable Credentials 2.0 without claiming conformance to its
Data Integrity suites. Signatures are Ed25519 over canonical JSON (sorted keys, compact separators)
of the document without its `proof`.

```json
{
  "@context": ["https://www.w3.org/ns/credentials/v2", "https://w3id.org/academic-wasteland/credentials/v0.1"],
  "id": "https://w3id.org/academic-wasteland/camelot/credentials/5f0c...",
  "type": ["VerifiableCredential", "DataAccessAuthorization"],
  "issuer": "https://w3id.org/academic-wasteland/camelot/issuers/ubar-dac",
  "validFrom": "2026-09-14T09:00:00Z",
  "validUntil": "2026-10-14T09:00:00Z",
  "credentialSubject": {
    "id": "https://orcid.org/0000-0001-8149-5890",
    "holderKey": "ed25519:<base64url>",
    "dataset": "https://w3id.org/academic-wasteland/ubar/datasets/ksa-individual-genotypes",
    "scope": "https://w3id.org/academic-wasteland/pangenome-town/contract/AggregateFrequencyScope",
    "purpose": "Allele frequency differences between KSA and JPT in HLA class I"
  },
  "credentialStatus": {"id": "https://w3id.org/academic-wasteland/camelot/status/5f0c...", "type": "RegistrarStatus"},
  "proof": {"type": "PangenomeTownEd25519Jcs2026", "verificationMethod": "https://w3id.org/academic-wasteland/camelot/issuers/ubar-dac#key-1", "created": "2026-09-14T09:00:00Z", "proofValue": "<base64url>"}
}
```

Types in the minimal example:

| Type | Issued by | Subject | Carries |
|---|---|---|---|
| `Accreditation` | a trust anchor or an accredited body | an issuer IRI | the issuer's public key, roles |
| `EthicsApproval` | an ethics board | a researcher | protocol id, scope, holder key |
| `DataAccessAuthorization` | a data access committee | a researcher | dataset, scope, holder key |

**Web of trust.** A town's contract names its trust anchors (for example the demo `ethics-council`),
and their public keys are pinned in `town.toml`. Signatures are checked against keys from three
sources: pinned anchors, verified `Accreditation` credentials (up to depth 3), and the registrar's
published key directory. A directory key proves who signed a credential; it does not make the signer
trusted. Trust is an entailment over accreditation edges: in the ontology `accreditedBy` is transitive
and `issuedBy o accreditedBy` implies `issuedUnderAuthorityOf`, so "issued under the authority of a
trust anchor" is decided by `km`, and a validly signed credential from an unaccredited issuer stays
unvetted. Conflicting keys for one issuer invalidate what depends on them. Towns choose their own
anchors, so disagreement about whom to trust is expressible and visible in each published contract.

**Holder binding.** A credential names a holder key. The requester sends a **presentation** with the
task: the credentials, the accreditations needed to reach an anchor, the task `@id`, the SHA-256 of the
canonical task, the audience town, a nonce, and a signature by the holder key. A copied credential is
useless without the holder key, and a presentation cannot be replayed for another task or another town.
Delegation composes: the Yamatai agent is `requestedBy`, the researcher is `onBehalfOf`, and the
researcher's signature over this task is the delegation statement.

**Revocation and freshness.** Credentials are short-lived (30 days by default). The receiver checks
`credentialStatus` against the registrar through the supervisor proxy. Policy `revocation = "required"`
fails closed when the registrar is unreachable; `"best-effort"` records the gap in the report.

### 3.3 Contract additions

Namespaces: `cred:` = `https://w3id.org/academic-wasteland/credentials/v0.1/`, `pg:` as before.

```
TransitiveObjectProperty(cred:accreditedBy)
SubObjectPropertyOf(cred:issuedBy cred:issuedUnderAuthorityOf)
SubObjectPropertyOf(ObjectPropertyChain(cred:issuedBy cred:accreditedBy) cred:issuedUnderAuthorityOf)
EquivalentClasses(town:VettedDataAccess ObjectIntersectionOf(cred:DataAccessAuthorization cred:VerifiedCredential
    ObjectSomeValuesFrom(cred:issuedUnderAuthorityOf town:TrustAnchor)))
EquivalentClasses(town:VettedEthicsApproval ObjectIntersectionOf(cred:EthicsApproval cred:VerifiedCredential
    ObjectSomeValuesFrom(cred:issuedUnderAuthorityOf town:TrustAnchor)))
EquivalentClasses(town:DataAccessCovered ObjectUnionOf(
    ObjectIntersectionOf(pg:AggregateFrequencyScope
        ObjectSomeValuesFrom(cred:presents ObjectIntersectionOf(town:VettedDataAccess pg:ApprovesAggregateFrequencyScope)))
    ObjectIntersectionOf(pg:IndividualGenotypeScope
        ObjectSomeValuesFrom(cred:presents ObjectIntersectionOf(town:VettedDataAccess pg:ApprovesIndividualGenotypeScope)))))
EquivalentClasses(town:EthicsCovered ...same shape with town:VettedEthicsApproval...)
EquivalentClasses(town:AcceptedControlledTask ObjectIntersectionOf(pg:ControlledAccessTask
    ObjectSomeValuesFrom(:usesDataset ObjectIntersectionOf(town:ServedRestrictedDataset ObjectSomeValuesFrom(pg:residesOn town:ReachableSite)))
    town:DataAccessCovered town:EthicsCovered
    ObjectSomeValuesFrom(:requestedBy ObjectIntersectionOf(:Actor ObjectComplementOf(pg:BlockedRequester)))))
EquivalentClasses(town:UncoveredControlledTask ObjectIntersectionOf(pg:ControlledAccessTask
    ObjectSomeValuesFrom(:usesDataset town:ServedRestrictedDataset) ObjectAllValuesFrom(cred:presents owl:Nothing)))
```

Scope library (task classes, leaves pairwise disjoint). Each scope has a static approval class that a
credential is asserted into, and a release class every released output must belong to:

| Scope | Covers | Approval class | Release class |
|---|---|---|---|
| `pg:AggregateFrequencyScope` | `AlleleFrequencyTask`, `PopulationComparisonTask`, `RegionVariantListingTask`, `GraphSummaryTask` | `pg:ApprovesAggregateFrequencyScope` | `pg:AggregateArtifact` |
| `pg:IndividualGenotypeScope` | the above plus `IndividualGenotypeExportTask` | `pg:ApprovesIndividualGenotypeScope` | `:ResearchArtifact` |

Receiver axioms per task T (never writable by the sender; the allowlist excludes every `cred:` class and
property, the approval classes, and every `town:` decision class):

```
ClassAssertion(cred:VerifiedCredential <c>)                 # signature, time, status, holder binding passed
ClassAssertion(cred:DataAccessAuthorization <c>)
ObjectPropertyAssertion(cred:issuedBy <c> <issuer>)
ClassAssertion(pg:ApprovesAggregateFrequencyScope <c>)       # from the credential's scope
ObjectPropertyAssertion(cred:presents <T> <c>)               # only when holder and dataset match (compared in code)
ObjectPropertyAssertion(cred:accreditedBy <issuer> <anchor>) # per verified accreditation
ClassAssertion(ObjectAllValuesFrom(cred:presents owl:Nothing) <T>)   # only when no credential matches
ClassAssertion(town:ReachableSite <site>) or ClassAssertion(ObjectComplementOf(town:ReachableSite) <site>)
ClassAssertion(ObjectComplementOf(town:ServedRestrictedDataset) <d>) # for datasets this town does not serve
```

Probes (research-commons PR #1 adds checked receiver axioms and probes that never change the status):
`scope:<n>` asks whether T is an instance of the credential's scope class (contradicted when out of
scope, through the disjoint leaves); `vetted:<n>` asks whether the credential individual itself is a vetted
data access authorization or ethics approval. Coverage of T by c is derived in code as "matches, in scope,
and vetted", all three decided or checked before. After execution, each output individual of a controlled
contribution is probed against the release class.

**Why no nominals.** A first formulation tied coverage to each credential with per-task disjunctions over
nominals (`T in not Scope or coveredBy {c}`) and a closure `coveredBy only {c1 ... cn}`. `km` 1.3 then
selected its nominals route and single classifications took more than 90 s. Removing either the nominal
disjunctions (1.0 s) or the `issuedBy o accreditedBy` role chain (0.4 s) fixed it; closure, transitivity, and
accreditation edges alone did not. The chain carries the trust reasoning, so the disjunctions went: scope
approval became a static class per scope and per-credential questions became probes on the credential.
The static site closure `residesOn only {sites}` stays, so `NoCapabilityTask` can be entailed.

### 3.4 Decision procedure

1. Structure, SHACL, or consistency failure: **rejected**.
2. `RejectedTask` entailed (blocked requester, restricted dataset this town does not serve): **rejected**.
3. Admissibility entailed: execute (inline for basic queries; compute runner for large and controlled).
4. `NoCapabilityTask` entailed: **input-required**, gate `capability`, with referrals.
5. Controlled task, a presented credential's scope probe contradicted: **rejected**, gate `authority`,
   reason `out-of-scope`.
6. Controlled task otherwise: **input-required**, gate `authority`, reason `missing-credential` (nothing
   presented), `presentation-invalid` (replay, tampering, stale, wrong audience), or `credential-not-accepted`,
   with the missing credential types, the trust anchors, and per-credential diagnostics (expired, revoked,
   bad signature, holder or dataset mismatch, issuer not under a trust anchor).
7. Large task, `PermissionRequiredTask` entailed: **input-required**, gate `standing`, with the score
   and threshold.
8. Otherwise **input-required**, gate `admissibility`.

After execution of a controlled task every output of the contribution (the node built it, so it can
enumerate them) must be an instance of the release class of the covering scope. If not, the outputs are
deleted, the contribution is withheld, and the task **fails** with reason `output-exceeds-scope`.

### 3.5 Camelot, the authority town

Camelot is a town like Ubar and Yamatai: its own Gas City city and repository, an `envoy` service with an
Agent Card, and residents. Its envoy serves the registrar:

- `GET /v0/keys` issuer public keys and accreditations;
- `POST /v0/applications` an application (holder IRI, holder key, credential type, dataset, scope,
  purpose); `GET /v0/applications/<id>` its state;
- `GET /v0/credentials/<id>`, `GET /v0/status/<id>` (revocation);
- residents `irb` (ethics board) and `dac` (data access committee) run on the lab's local Qwen. They
  review pending applications against the scope library and write a recommendation, but they cannot
  issue anything: issuance, denial, and revocation happen only through the CLI
  (`pangenome-town authority approve|deny|revoke`) run by a human, who is notified by `gc mail send human`.

Issuer private keys live in `~/.gc/authority/<issuer>.key` (mode 0600), outside every repository.
Holder keys live in `~/.gc/holders/<slug>.key`. Public keys and accreditations are committed.

All authorities in this city are **demo stand-ins** with invented names. They do not represent KAUST,
NIG, or any real ethics board or committee.

## 4. Use cases

**U1. Compute where the data are.** Ubar receives a `WholeGraphDeconstructTask` for a region. Ubar holds
a reachable site with the graph, so the rigger renders the `deconstruct-region` template, validates it,
and runs it under limits. Whole-chromosome requests exceed the site's wall-time limit and are refused by
the validator before anything runs.

**U2. No capability, referral instead of borrowing.** Yamatai declares only the `ddbj` site, disabled.
The same task sent to Yamatai returns input-required, gate `capability`, with a referral to Ubar. The
requester resubmits to Ubar.

**U3. Controlled data with IRB and DAC credentials.** A researcher (holder key, ORCID) applies to
`wasteland-irb` and `ubar-dac` in Camelot; the operator approves both with an aggregate scope.
Yamatai's agent, on the researcher's behalf, sends Ubar an `AlleleFrequencyTask` on
`ksa-individual-genotypes` with a presentation. Ubar verifies signatures, accreditation to
`ethics-council`, validity, status, and holder binding, asserts the facts, and `km` entails
`AcceptedControlledTask`. The runner computes per-site allele counts, the contribution is checked to be
aggregate-only, and it is released.

**U4. Scope is enforced.** The same credentials with an `IndividualGenotypeExportTask`: the scope probe
is contradicted, the task is rejected as out-of-scope.

**U5. Trust is local.** A credential signed by `rogue-dac` (valid signature, no accreditation chain to an
anchor) is not vetted: input-required with diagnostic `issuer not under a trust anchor`.

**U6. Reputation cannot buy data.** Robert's ORCID is a trusted requester (reputable) but without
credentials the controlled task is still input-required at the authority gate.

**U7. Replay, tampering, expiry, revocation.** A presentation for another task, an edited credential,
an expired credential, and a revoked credential each fail verification with a specific diagnostic.

## 5. Out of scope for the minimal example

- Dataset custody and execution routes are now separate: both towns have `ddbj-direct`, while only
  Yamatai has the `ddbj` Slurm route through `a001`. See [dataset custody](dataset-custody.md)
  for task-specific signed grants and logical cohort manifests. These additions do not replace
  the controlled RCP authority gates described here.
- `ComputeAllocation` credentials (a requester-held compute budget issued by a site operator).
- Issuance as RCP contributions (the registrar speaks plain JSON over HTTP for now).
- W3C Data Integrity conformance, DIDs, OIDC4VP, status lists with privacy.
- The real JaSaPaGe data are open (CC0). The `ksa-individual-genotypes` dataset is a **simulated
  controlled-access tier** over the same VCF, used to exercise the authority gate.
- Output scope checks rely on the template's declared artifact class; a mislabelled template would pass.
  Content inspection of outputs (for example, refusing per-sample columns in an aggregate release) is
  a follow-up.
