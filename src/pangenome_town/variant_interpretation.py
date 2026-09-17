"""Themis: a bounded, evidence-traceable ACMG/AMP interpretation agent.

Re-evaluates the supported criteria in a pinned FBN1 expert curation. This is
not a general clinical classifier or a new expert-panel assertion.
"""
import base64
import hashlib
import io
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from xml.sax.saxutils import escape

EVIDENCE = Path(__file__).with_name('variant_case') / 'expert-evidence.json'
ACMG = 'https://pubmed.ncbi.nlm.nih.gov/25741868/'


def evaluate(evidence):
    criteria = []
    def add(code, strength, met, explanation):
        criteria.append({'code': code, 'strength': strength, 'met': bool(met), 'explanation': explanation,
                         'source': evidence['source']})
    add('PS4', 'Strong', evidence['ps4_points_min'] >= 4 and not evidence['benign_evidence'],
        'The expert panel reports at least four proband points. No synthetic patient is counted.')
    add('PM2_Supporting', 'Supporting', evidence['population_absent'],
        'The panel reports absence from gnomAD v2.1.1 and v3.1.2; this is historical evidence, not a current database lookup.')
    add('PP2', 'Supporting', evidence['missense'] and evidence['pp2_supported_by_panel'] and not evidence['benign_evidence']
        and any(c['met'] for c in criteria),
        'Missense variation is an established mechanism in FBN1; the panel reports constraint Z=5.06 and no benign criteria.')
    add('PP3', 'Supporting', evidence['revel'] >= .75,
        f"REVEL {evidence['revel']} exceeds the pinned FBN1 specification's 0.75 threshold. Computational evidence is counted once.")
    strong = sum(c['met'] and c['strength'] == 'Strong' for c in criteria)
    supporting = sum(c['met'] and c['strength'] == 'Supporting' for c in criteria)
    classification = 'Likely pathogenic' if strong == 1 and supporting >= 2 and not evidence['benign_evidence'] else 'Uncertain significance'
    return criteria, classification


def report_pdf(report):
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    output = io.BytesIO()
    styles = getSampleStyleSheet()
    styles['BodyText'].spaceAfter = 7
    styles['BodyText'].leading = 14
    styles['BodyText'].wordWrap = 'CJK'
    story = []
    def paragraph(text, style='BodyText'):
        story.append(Paragraph(escape(str(text)), styles[style]))
    paragraph('Academic Wasteland | Variant report', 'Title')
    paragraph('Themis / Ubar — research demonstration', 'Heading2')
    paragraph(report['notice'])
    paragraph(report['classification'], 'Heading1')
    paragraph('Variant: ' + json.dumps(report['variant'], sort_keys=True))
    paragraph(report.get('hgvs', '') + ' ' + report.get('protein', ''))
    paragraph('Phenotypes received: ' + ', '.join(
        t['label'] + ' (' + t['id'] + ')' for t in (report.get('resolution') or {}).get('terms', []))
        if report.get('resolution') else 'Phenotypes received: ' + ', '.join(report['phenotypes']))
    paragraph('Generated: ' + report['generated'])
    paragraph('Scope and data boundary', 'Heading2')
    paragraph('Ubar received one allele and phenotype terms. It received no VCF, sample identifier, genotype, family history, or other variants. Prioritization is performed at Yamatai; classification uses the public evidence below.')
    for criterion in report['criteria']:
        paragraph(f"{criterion['code']} — {criterion['strength']} — {'met' if criterion['met'] else 'not met'}", 'Heading3')
        paragraph(criterion['explanation'])
    paragraph('Combination and limits', 'Heading2')
    paragraph(report['combination'])
    for limit in report['not_applied']:
        paragraph(limit)
    paragraph('Provenance', 'Heading2')
    for key, value in report['sources'].items():
        paragraph(f'{key}: {value}')
    story.append(Spacer(1, 5*mm))
    paragraph('No clinical sign-off. This is a reproducible assessment of a pinned expert curation, not a diagnosis or a complete interpretation of arbitrary variants.')
    def footer(canvas, doc):
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(colors.HexColor('#355c58'))
        canvas.drawString(18*mm, 12*mm, f'Synthetic case | Themis | Page {doc.page}')
    SimpleDocTemplate(output, title='Themis variant report', author='Academic Wasteland',
                      leftMargin=18*mm, rightMargin=18*mm, topMargin=18*mm, bottomMargin=20*mm).build(
                          story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


def request_text(variant, phenotypes, resolution=None):
    labels = {t['id']: t['label'] for t in (resolution or {}).get('terms', [])}
    terms = [f"{labels[p]} ({p})" if p in labels else p for p in phenotypes]
    return (f"Please assess {variant['assembly']} {variant['chrom']}:{variant['pos']} {variant['ref']}>{variant['alt']} "
            f"with phenotype identifiers {', '.join(terms)}. Apply the available ACMG evidence and return a sourced PDF report. "
            "I am sending only this allele and phenotype terms; the VCF remains at Yamatai.")


def interpret(town, body):
    if not town.extra.get('variant_interpretation', {}).get('enabled'):
        raise ValueError('Variant interpretation is not enabled in this town')
    if set(body) - {'operation', 'text', 'resident', 'variant', 'phenotypes'}:
        raise ValueError('Themis accepts only one variant and phenotype identifiers; no VCF or patient metadata')
    if body.get('resident', 'themis') != 'themis':
        raise ValueError('The variant interpretation resident is themis')
    variant, phenotypes = body.get('variant'), body.get('phenotypes')
    if not isinstance(variant, dict) or set(variant) != {'assembly', 'chrom', 'pos', 'ref', 'alt'}:
        raise ValueError('Provide assembly, chrom, pos, ref and alt for one variant')
    if (variant['assembly'] != 'GRCh38' or not isinstance(variant['chrom'], str)
            or not re.fullmatch(r'(?:[1-9]|1[0-9]|2[0-2]|X|Y|MT)', variant['chrom'])
            or type(variant['pos']) is not int or not 1 <= variant['pos'] <= 300_000_000
            or any(not isinstance(variant[k], str) or not re.fullmatch('[ACGT]{1,50}', variant[k]) for k in ['ref', 'alt'])
            or variant['ref'] == variant['alt']):
        raise ValueError('Invalid GRCh38 variant representation')
    if not isinstance(phenotypes, list) or not 1 <= len(phenotypes) <= 30 or any(
            not isinstance(p, str) or not re.fullmatch(r'(HP|MP):\d{7}', p) for p in phenotypes):
        raise ValueError('Provide 1–30 resolved HPO/MP phenotype identifiers')
    from .phenotype_labels import resolve
    _, resolution = resolve(town.extra.get('phenotype_search', {}), phenotypes)
    if body.get('text') not in (None, '', request_text(variant, phenotypes), request_text(variant, phenotypes, resolution)):
        raise ValueError('Themis accepts only one variant and phenotype identifiers; use the bounded request text without patient metadata')
    raw = EVIDENCE.read_bytes()
    evidence = json.loads(raw)
    report = {'agent': 'themis', 'town': town.name, 'variant': variant, 'phenotypes': sorted(set(phenotypes)),
              'resolution': resolution, 'generated': datetime.now(UTC).isoformat(), 'classification': 'Not classified', 'criteria': [],
              'notice': 'Research demonstration. No real patient diagnosis or clinical sign-off. Public evidence is pinned to its stated date.',
              'combination': 'No installed evidence for this allele; no classification can be assigned.',
              'not_applied': ['PP4 is not inferred from the three supplied phenotypes.',
                              'No patient-specific de novo, segregation or functional evidence was supplied.',
                              'INDIGENA scores and the synthetic case are not ACMG evidence.'],
              'sources': {'ACMG/AMP 2015': ACMG}}
    if variant == evidence['variant']:
        criteria, classification = evaluate(evidence)
        report.update(classification=classification, criteria=criteria, hgvs=evidence['hgvs'], protein=evidence['protein'],
                      condition=evidence['condition'], expert_classification=evidence['expert_classification'],
                      combination='ACMG/AMP 2015 Table 5: one Strong plus at least two Supporting criteria yields Likely pathogenic. Recomputed from the pinned expert evidence, not an independent re-curation.',
                      sources={'ACMG/AMP 2015': ACMG, 'FBN1 specification': evidence['specification_url'],
                               'Expert evidence': evidence['source'], 'Expert version': evidence['version'],
                               'Expert publication date': evidence['published'], 'Evidence retrieved': evidence['retrieved'],
                               'Evidence file SHA-256': hashlib.sha256(raw).hexdigest(),
                               'Downloaded expert JSON SHA-256': evidence['source_sha256']})
        report['not_applied'].append('PM1 is excluded by the expert panel for this substitution. PP5 is not used.')
    pdf = report_pdf(report)
    return {'ok': True, 'resident': 'themis',
            'text': f"My assessment is {report['classification']}. The attached report shows the dated public evidence, applied ACMG criteria and exclusions. I received only one allele and phenotype terms, not the VCF or genotype. This is a research assessment without clinical sign-off.", 'report': report, 'pdf_base64': base64.b64encode(pdf).decode(),
            'pdf_sha256': hashlib.sha256(pdf).hexdigest(), 'pdf_media_type': 'application/pdf'}
