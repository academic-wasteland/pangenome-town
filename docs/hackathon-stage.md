# Three-minute pangenome demo

Open **http://127.0.0.1:8393/demo** on the presentation laptop. A dedicated local
service keeps the stage available; it does not replace the existing cockpit on
8390. The stage's own cockpit also links to **Open live demo**.

## Present it in 2 minutes 40 seconds

| Time | Action | What to say |
| --- | --- | --- |
| 0:00–0:20 | Start with a clean stage. | “Ubar has the Saudi cohort. Yamatai has the Japanese cohort. Let's compare the same genomic region.” |
| 0:20–0:45 | Click **Compare cohorts**. Let the green transcript build. | “The request splits into two cohort analyses. The agent's qualification is accepted from Sakura; Camelot is not required. But a qualification is not permission to use Ubar's data.” |
| 0:45–1:10 | Click Ubar's **Dataset permission** row. Close the explanation, then click **Approve this analysis**. | “I approve this exact region and aggregate counts for ten minutes. This creates a real signed credential.” |
| 1:10–1:50 | Follow the routes and the chart. Click a result message if useful. | “Both workers calculate allele counts from the real JaSaPaGe VCF. They check permissions again before releasing results. We get a comparison without releasing individual genotypes.” |
| 1:50–2:20 | Click **Try individual genotype export**. | “Can the same permission be reused to download individuals? No. The scope and the exact task no longer match. The export never starts.” |
| 2:20–2:40 | Point to the chart and refusal. | “We got a scientific result across two independently governed cohorts. The permissions enabled that work and still enforced a boundary.” |

Leave twenty seconds spare. Do not open multiple technical drawers on stage.
Click **Show all now** if the transcript is behind your narration. At 1920×1080,
the results view compacts the map to keep the chart and refusal visible together.
On smaller laptop displays you can scroll down to the results. Browser fullscreen
is useful on the projector.

## What is live

- Real `bcftools query` over local indexed public JaSaPaGe VCF data.
- GRCh38 `chr6:29940000-29950000`; Ubar's configured 9 samples and Yamatai's 10.
- Two concurrent local cohort workers, each selecting its own sample manifest.
- Ed25519-signed credentials, agent/holder binding, receiver-owned issuer rules,
  exact-task data/compute permissions, validity/status checks, and release checks
  using the production certification evaluator.
- The initial Ubar data grant is absent. The operator's click issues it. Qualification,
  demo ethics, compute permission and Yamatai's data permission are prepared at start.
- Export refusal is an actual policy evaluation against a new task with a different
  scope and digest. No export subprocess is run.
- Chronological event records and aggregate result rows, archived per run under
  Ubar's configured state directory in `demo-stage/<run-id>.json` (0600).

The stage uses **isolated demonstration authorities and workers**, not production
ethics approvals, live Keycloak, remote Gas City residents, or a Slurm job. Its
message wording is scripted from actual workflow events, not LLM-generated text
or evidence of network delivery. The page labels these boundaries explicitly.
Existing town credentials and trust settings are unchanged. The source data is
public; the access restriction is for demonstrating policy enforcement.

The initial rehearsal found **510 shared callable biallelic sites** in under a
second of local computation. The chart shows the first five in genomic order,
not sites selected for the largest difference. Counts are alternate alleles /
called alleles; missing alleles are excluded, and multiallelic sites are omitted.
These small cohorts do not support population-wide estimates or clinical claims.

## Dynamic behaviour and inspection

The browser polls live event state. Green monospace messages type progressively;
completed execution is never held back to match the animation. Queue status says
when already-recorded messages are still being revealed. New routes accumulate
as events appear; a packet traverses the latest route. Click a message, route,
town or permission to inspect the associated check. The dialog explains the
result first; identifiers, credential documents, task digests and raw evidence
are inside a closed **Technical evidence and identifiers** disclosure.

Opening an inspector pauses the remaining transcript reveal, not the running
analysis. **Motion off** and the operating system's reduced-motion preference
remove typing and packet animation. **Show all now** catches up immediately.
Refresh reconstructs the active run and all recorded routes. **Reset stage**
clears the presentation for another rehearsal; it refuses to interrupt a running
job. Prior run archives remain on disk. A server restart starts a fresh stage;
archives are not automatically replayed as live work.

## Before going on stage

1. Open the page and check the three green local preflight indicators.
2. Rehearse the three clicks once on the actual presentation laptop and projector.
3. Check that the chart has real results; never substitute canned numbers for a failed query.
4. Reset and leave the clean page open. No venue network is needed after installation
   and provisioning the indexed VCF locally.
5. Do not restart the service during an active demonstration. Credentials last ten
   minutes; if an old rehearsal has expired, reset and start again.

Start manually from the repository root:

```sh
.venv/bin/pangenome-town dashboard \
  --towns ../ubar/town.toml ../yamatai/town.toml ../camelot/town.toml \
  --port 8393
```

On the configured laptop the persistent user service is:

```sh
systemctl --user status wasteland-demo-stage.service
systemctl --user restart wasteland-demo-stage.service
journalctl --user -u wasteland-demo-stage.service -n 40
```

The server binds only to loopback. Present through the laptop/projector; this
page is not anonymously published to the venue network. Actions require the
cockpit token and inherit its local-origin checks. Client inputs cannot select
an arbitrary command, data path, region or export operation.

## Reproducible proof

```sh
.venv/bin/python -m pytest -q tests/test_demo_stage.py
.venv/bin/python examples/stage_browser_demo.py
uvx showboat verify docs/demos/hackathon-stage.md
```

The browser rehearsal resets the demo on port 8393, performs the real three-click
flow, inspects permission evidence, verifies the chart and refused export, and
refreshes to confirm restoration. It launches a private headless browser and
removes it afterward. It requires `uvx` and a first-run Chromium download, so
run it **before** arriving at the venue. The unit tests instead use a tiny local
VCF fixture; they also test forged task scope, revoked permission during compute,
duplicate approval, stale run identifiers, and HTTP action guards.
