"""Ubar contact's bounded research intake: readable reply plus executable delegation."""
from .phenotypes import validate


def intake(town, body):
    if not town.extra.get('phenotype_search', {}).get('enabled'):
        raise ValueError('Phenotype research intake is unavailable in this town')
    if set(body) - {'operation', 'text', 'resident', 'phenotypes', 'private_case', 'workflow'}:
        raise ValueError('Only the research question, phenotypes and synthetic-case switch are accepted')
    if body.get('resident') != 'contact':
        raise ValueError('Send research intake to the contact resident')
    text = body.get('text')
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1200:
        raise ValueError('Provide a research question of 1–1200 characters')
    if type(body.get('private_case')) is not bool:
        raise ValueError('private_case must be boolean')
    query = validate({'phenotypes': body.get('phenotypes')})
    steps = [{'town': town.name, 'resident': 'phenomancer', 'operation': 'phenotype-search',
              'record': 'urn:wasteland:fair:ubar:indigena'}]
    reply = 'I can route this research request. Phenomancer in Ubar will resolve your phenotype labels and return an INDIGENA gene ranking.'
    if body['private_case']:
        if not town.extra.get('variant_interpretation', {}).get('enabled'):
            raise ValueError('Themis interpretation is unavailable in this town')
        steps.extend([{'town': 'yamatai', 'resident': 'coordinator', 'operation': 'private-variant-rank', 'transport': 'local'},
                      {'town': town.name, 'resident': 'themis', 'operation': 'variant-interpretation',
                       'record': 'urn:wasteland:fair:ubar:variant-interpretation'}])
        reply += ' Keep the VCF at Yamatai. Your coordinator should compare REVEL-only and combined rankings locally, then send just the selected allele and phenotypes to Themis for an ACMG evidence report.'
    return {'ok': True, 'resident': 'contact', 'text': reply, 'plan': steps,
            'phenotypes': query['phenotypes'], 'private_case': body['private_case'],
            'mode': 'Bounded demo intake; workflow selected from structured fields, not unrestricted natural-language planning.'}
