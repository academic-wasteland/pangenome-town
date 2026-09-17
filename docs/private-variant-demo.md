# Private VCF → phenotype genes → variant → ACMG report

Open https://leechuck.de/wasteland-live/demo/phenotypes and keep **Continue with
Yamatai’s private synthetic VCF** checked. Click **Run my phenotype query** or
**Play narrated demo**. The existing phenotype-only mode remains available by
unchecking that option. Each visitor has separate results, reset and report.

The default HPO labels describe the selected Marfan teaching case. Real INDIGENA
inference places FBN1 third among 1,529 mouse gene profiles; human names are
orthologue annotations. Yamatai's bridge then opens its local VCF and combines
those gene ranks with variant evidence. The expected allele is tested only after
ranking. It is never passed as a target to either ranking algorithm.

## What runs, and where

1. Human writes a request to Ubar/contact, relayed with Yamatai’s authenticated
   identity. `message` with `resident: contact` and `workflow: phenotype-research` returns actual reply text and an ordered structured
   delegation plan. The coordinator validates the bounded plan, then follows it.
   This contact service selects its plan from the structured workflow fields;
   it does not claim unrestricted natural-language reasoning.
2. Yamatai sends phenotype labels to Ubar's `phenotype-search` operation. No VCF,
   genotype, sample name or variant is in this phenotype-search request.
3. Ubar resolves HPO/MP labels, runs the installed INDIGENA model, and returns
   scored mouse genes with human orthologues.
4. Yamatai passes the ranked symbols to its local `private-variant-rank` task.
   This task has no relay endpoint and checks its owning-town context. It opens a
   fixed local synthetic VCF, applies quality/heterozygosity/population-frequency
   screening and ranks the retained calls. The web display is the Yamatai view;
   its shortlist is not sent to Ubar.
5. Yamatai sends only the highest-ranking allele (GRCh38 chromosome, position,
   REF, ALT) and resolved phenotype identifiers to Ubar's new resident **Themis**,
   through `variant-interpretation`. No genotype, pedigree or other alleles are
   supplied. An allele and phenotypes are a disclosure, not anonymization.
6. Themis evaluates the installed public evidence and generates a PDF at Ubar.
   Yamatai verifies the PDF SHA-256; its visitor can inspect and download it.

Highlighted conversation entries display the text actually sent in relay
requests and returned in service replies. Expand them to inspect data payloads
and transport identifiers. Local execution notes are labelled separately;
PDF bytes are omitted from the event view but available through the download.
Themis’s bounded request text repeats only the selected allele, phenotype terms
and requested action; arbitrary patient narrative remains rejected.
Every request and response ID is inspectable in the event stream. The automated workflow never adds raw VCF text, sample identifiers, genotypes
or read-depth values to relay messages or public run snapshots. The editable
human-message field is sent verbatim; it is not a redaction service. Use only
the synthetic case and do not put patient data in that field. The operation refuses paths/uploads and any VCF that
differs from the synthetic teaching fixture. Towns currently share a host/user:
this demonstrates protocol-level minimization, not OS-level isolation against a
malicious local administrator. The reproducible synthetic fixture is public in
the repository; it is not confidential patient data.

## Ranking and the expected allele

The VCF has six synthetic heterozygous calls. Two fail: one is common (AF=0.02),
and one fails quality/depth checks. Four remain. Filtering requires PASS,
DP≥10, GQ≥20, heterozygosity, AF≤0.001 and inclusion in the returned gene list.
For each retained call:

```
score = 0.45 / (1 + log2(INDIGENA gene rank)) + 0.55 * REVEL
```

This is an explicit illustrative heuristic, not a trained or clinically validated
variant classifier. The AF screen is not the ACMG PM2 threshold. The top call is
**GRCh38 15:48421680 T>C**, **FBN1 NM_000138.5:c.7577A>G (p.Asn2526Ser)**.
The FBN1 allele is **rank 2 by REVEL alone** (0.821 versus the synthetic
ADAMTSL4 decoy’s 0.86), **gene rank 3 by phenotype alone**, and **rank 1 after
combining them**, score approximately **0.6256**. ADAMTSL4 has poorer phenotype
support (gene rank 5) and combined score approximately **0.6085**. The scoring
formula and FBN1’s published score were not changed; the decoy is explicitly
synthetic. A counterfactual test exchanges the genes’ phenotype ranks and
confirms that ADAMTSL4 then wins. The UI can sort by either component or their
combination, displaying all ranks alongside the scores.

The target's REVEL **0.821** and historical absence from gnomAD v2.1.1/v3.1.2 are
from [ClinGen FBN1 VCEP's 2023-06-15 assessment](https://erepo.clinicalgenome.org/evrepo/ui/interpretation/f57714a2-598c-4e48-a07e-6858c037050e).
AF=0 encodes that reported absence, not a newly measured population frequency.
Background coordinates, annotations and scores are synthetic teaching values.
There are no live CADD, AlphaMissense or gnomAD API calls. The case is constructed
to demonstrate recovery; it is not a held-out accuracy benchmark.

## Themis and ACMG interpretation

Themis is an advertised Ubar resident with a deterministic service executor;
classification and PDF contents do not depend on LLM-generated assertions.
The initial evidence package covers the single FBN1 allele above. Other alleles
receive **Not classified**, with an explicit explanation and report. This is
not a complete automated ACMG engine for arbitrary variants.

For the supported allele, Themis evaluates **PS4**, **PM2_Supporting**, **PP2**
and **PP3**, reproducing **Likely pathogenic**. The combination is one Strong
plus at least two Supporting criteria from ACMG/AMP 2015 Table 5, using the pinned
[ClinGen FBN1 specification version 1](https://www.clinicalgenome.org/site/assets/files/7445/clingen_fbn1_acmg_specifications_v1.pdf).
This reproduces a dated expert curation; it is not an independent re-curation or
a claim to use every subsequently revised guideline. The report gives the
source versions, retrieval date, evidence hashes, criteria, rule and exclusions.

The synthetic patient contributes no PS4 proband points. No de novo, segregation,
functional or PP4 evidence is invented. PM1 is excluded for this substitution;
PP5 is not used. REVEL contributes one computational criterion; phenotype-ranking
scores are not counted as ACMG evidence. The classification concerns the
variant–disease association, not a confirmed diagnosis in a patient.

## Install and advertise

Install the updated `pangenome-town` and starter pack; ReportLab is a dependency.
At Yamatai, copy the packaged fixture to a private local directory:

```sh
install -d -m 700 /path/to/yamatai/private-variant-demo
install -m 600 src/pangenome_town/variant_case/patient.vcf /path/to/yamatai/private-variant-demo/patient.vcf
```

Yamatai `town.toml`:

```toml
[private_variant_demo]
enabled = true
vcf = "/path/to/yamatai/private-variant-demo/patient.vcf"
```

Ubar `town.toml`:

```toml
[variant_interpretation]
enabled = true
```

The Ubar agent profile is `agents/themis/agent.toml`. Restart both bridges and the
public demo server. Publish Ubar’s provider-owned FAIR catalogue: Themis’s service
record describes its limited scope. Yamatai’s VCF step is local, with no remotely
callable endpoint. FAIRhaven listings are not clinical certification.

Themis request:

```json
{"operation":"variant-interpretation","resident":"themis","variant":{"assembly":"GRCh38","chrom":"15","pos":48421680,"ref":"T","alt":"C"},"phenotypes":["HP:0001083","HP:0001166","HP:0002616"]}
```

Response: `report`, `pdf_base64`, `pdf_sha256`, `pdf_media_type`. The public
`/api/demo-phenotypes/report.pdf` route serves only the current visitor's completed
run, with no-store caching. Reset makes the previous report unavailable.

## Tests and reproducibility

```sh
python -m pytest -q tests/test_variant_case.py tests/test_demo_public.py
python examples/phenotype_live_demo.py --pdf /tmp/themis-live.pdf --screenshot /tmp/themis-live.png
```

Tests cover correct recovery, removing FBN1 from the input ranking, criteria
removal causing classification to drop, unknown alleles, rejected patient fields,
rejected real/modified VCFs, owner-only access, exact inter-town payloads, report
hashes and cross-visitor/reset isolation. Browser verification performs fresh
relay requests, checks the rank-one allele and classification, and downloads the
actual PDF. Narration waits for the whole workflow; Stop stops narration and
future presentation steps, while already submitted analysis continues.

Project-authored metadata and generated report prose: CC BY 4.0, credit Academic
Wasteland. Upstream evidence retains its own terms. No clinical sign-off.
