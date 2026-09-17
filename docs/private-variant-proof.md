# Live private-variant workflow proof

*2026-09-17T04:16:52Z by Showboat 0.6.1*
<!-- showboat-id: 0035e5c4-fe5a-411f-b4e5-17b366d8eab4 -->

Run from the pangenome-town checkout with Ubar/Yamatai configurations in the parent directory, the installed INDIGENA checkpoint, and running relay bridges. This is the synthetic Marfan demonstration; it reproduces pinned expert evidence rather than providing clinical validation.

```bash
.venv/bin/python examples/private_variant_check.py --pdf /tmp/themis-proof.pdf
```

```output
PASS: live INDIGENA FBN1 gene rank 3 / 1529.
PASS: FBN1 rank 2 by REVEL alone; rank 3 by phenotype; rank 1 only after combining both.
PASS: actual human request, contact delegation, Phenomancer and Themis text messages with structured payloads.
PASS: Ubar interpretation receives only selected allele, phenotype identifiers and resident routing.
PASS: PS4 + PM2_Supporting + PP2 + PP3 -> Likely pathogenic; no PP4 or fabricated patient evidence.
PASS: Ubar-generated PDF received and SHA-256 verified.
```
