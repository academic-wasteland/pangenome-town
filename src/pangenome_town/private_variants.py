"""Yamatai-only synthetic VCF analysis. No raw VCF or sample fields leave this worker."""
import hashlib
import json
import math
from pathlib import Path

CASE = Path(__file__).with_name('variant_case')


def rank(town, sender, body):
    settings = town.extra.get('private_variant_demo', {})
    if not settings.get('enabled') or sender != town.name:
        raise ValueError('Private variant analysis is restricted to the owning town identity')
    if set(body) - {'operation', 'text', 'resident', 'genes'}:
        raise ValueError('Only a gene ranking is accepted; file paths and VCF uploads are forbidden')
    genes = body.get('genes')
    if not isinstance(genes, list) or not 1 <= len(genes) <= 50 or any(
            not isinstance(g, str) or not g or len(g) > 40 for g in genes):
        raise ValueError('Provide 1–50 ranked gene symbols')
    # This operation deliberately accepts only the packaged synthetic demonstration.
    path = Path(settings['vcf']).expanduser()
    raw = path.read_bytes()
    if raw != (CASE / 'patient.vcf').read_bytes():
        raise ValueError('This public demonstration accepts only its synthetic VCF fixture')
    annotations = json.loads((CASE / 'annotations.json').read_text())
    gene_rank = {g: i+1 for i, g in reversed(list(enumerate(genes)))}
    retained, excluded = [], []
    for line in raw.decode().splitlines():
        if line.startswith('#'):
            continue
        chrom, pos, _, ref, alt, _qual, filt, _info, fmt, sample = line.split('\t')
        key = f'{chrom}:{pos}:{ref}:{alt}'
        annotation = annotations['variants'][key]
        local = dict(zip(fmt.split(':'), sample.split(':')))
        reason = None
        if filt != 'PASS' or int(local['DP']) < 10 or int(local['GQ']) < 20:
            reason = 'Failed local quality/depth threshold'
        elif local['GT'] not in {'0/1', '1/0', '0|1', '1|0'}:
            reason = 'Not a heterozygous call in this dominant teaching case'
        elif annotation['af'] > .001:
            reason = 'Population AF exceeds 0.001 screening threshold'
        elif annotation['gene'] not in gene_rank:
            reason = 'Gene outside the returned INDIGENA candidate set'
        if reason:
            excluded.append({'reason': reason})  # no unselected alleles leave Yamatai
            continue
        position = gene_rank[annotation['gene']]
        priority = 1 / (1 + math.log2(position))
        score = .45 * priority + .55 * annotation['revel']
        retained.append({'variant': {'assembly': 'GRCh38', 'chrom': chrom, 'pos': int(pos), 'ref': ref, 'alt': alt},
                         'gene': annotation['gene'], 'gene_rank': position, 'gene_priority': priority,
                         'af': annotation['af'], 'revel': annotation['revel'], 'score': score,
                         'annotation_provenance': annotation['provenance']})
    retained.sort(key=lambda r: (-r['score'], r['gene'], r['variant']['pos']))
    return {'ok': True, 'owner': town.name, 'synthetic': True, 'input_count': len(retained)+len(excluded),
            'vcf_sha256': hashlib.sha256(raw).hexdigest(), 'retained': retained, 'excluded': excluded,
            'method': 'PASS; DP>=10; GQ>=20; heterozygous; AF<=0.001; gene in INDIGENA top list. Score = 0.45/(1+log2(gene rank)) + 0.55*REVEL.',
            'notice': annotations['notice'], 'privacy': 'VCF, sample ID, genotype, read depth and other calls stay at Yamatai. This response is for the Yamatai demonstrator; only the selected allele and phenotypes may be forwarded to Ubar.'}
