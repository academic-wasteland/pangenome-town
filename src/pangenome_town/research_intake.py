"""A bounded diagnostic planner using advertised services and owner-supplied data policy."""
from .phenotype_labels import PAIR
from .phenotypes import validate


def intake(town, body, *, records, sender):
    if set(body) - {'operation', 'text', 'resident', 'phenotypes', 'resources', 'workflow'}:
        raise ValueError('Only the question, phenotypes and resource descriptions are accepted')
    if body.get('resident') != 'contact':
        raise ValueError('Send research intake to the contact resident')
    text = body.get('text')
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1200:
        raise ValueError('Provide a research question of 1–1200 characters')
    query = validate({'phenotypes': body.get('phenotypes')})
    if any(not PAIR.fullmatch(term) for term in query['phenotypes']):
        raise ValueError('Specify each phenotype label and ID together: Label (HP:0000000)')
    resources = body.get('resources', [])
    if not isinstance(resources, list) or len(resources) > 1:
        raise ValueError('This demonstration supports at most one synthetic VCF')
    policy = None
    local = None
    if resources:
        resource = resources[0]
        if not isinstance(resource, dict) or set(resource) != {'id', 'kind', 'synthetic', 'owner', 'policy', 'local_service'}:
            raise ValueError('Invalid resource description; do not send file contents or paths')
        if resource['owner'] != sender or resource['kind'] != 'VCF' or resource['synthetic'] is not True:
            raise ValueError('Only the authenticated owner may describe its synthetic VCF')
        if resource['id'] != f'urn:wasteland:resource:{sender}:synthetic-patient-vcf':
            raise ValueError('Unsupported synthetic resource identifier')
        policy, local = resource['policy'], resource['local_service']
        if not isinstance(policy, dict) or set(policy) != {'raw_data_export', 'compute_towns', 'phenotype_recipients', 'selected_variant_recipients'}:
            raise ValueError('Explicit data access policy required')
        if policy['raw_data_export'] is not False or policy['compute_towns'] != [sender]:
            raise ValueError('This demo requires owner-only compute and forbids raw-data export')
        for field in ('phenotype_recipients', 'selected_variant_recipients'):
            if not isinstance(policy[field], list) or any(not isinstance(x, str) for x in policy[field]):
                raise ValueError('Recipient policy must contain town names')
        if local != {'town': sender, 'resident': 'coordinator', 'operation': 'private-variant-rank', 'transport': 'local'}:
            raise ValueError('No supported owner-local VCF analysis service is described')

    def discover(operation, setting, permission):
        if not town.extra.get(setting, {}).get('enabled'):
            raise ValueError(f'Required service unavailable: {operation}')
        candidates = [r for r in records if r.get('kind') == 'service'
                      and r.get('access', {}).get('operation') == operation
                      and r['access'].get('town') == town.name
                      and r['access'].get('resident')
                      and (policy is None or r['access']['town'] in policy[permission])]
        if len(candidates) != 1:
            raise ValueError(f'Need one advertised and permitted {operation} service; found {len(candidates)}')
        record = candidates[0]
        return {k: record['access'][k] for k in ('town', 'resident', 'operation')} | {'record': record['@id']}

    genes = discover('phenotype-search', 'phenotype_search', 'phenotype_recipients')
    steps = [genes]
    decisions = [f"I found {genes['town']}/{genes['resident']} advertised for phenotype-to-gene prioritization."]
    if resources:
        interpretation = discover('variant-interpretation', 'variant_interpretation', 'selected_variant_recipients')
        steps.extend([dict(local), interpretation])
        decisions.extend([
            f"The VCF metadata allows only {sender} to analyse the file and forbids exporting it. I will bring the gene ranking to the data.",
            f"After local filtering and combined prioritization, the policy permits sending one selected allele and the phenotypes to {interpretation['town']}/{interpretation['resident']} for an evidence report.",
        ])
    else:
        decisions.append('No VCF is attached. I can prioritize genes from the phenotypes, but cannot identify a patient variant.')
    reply = 'I will investigate the genetic cause and explain the evidence. ' + ' '.join(decisions)
    return {'ok': True, 'resident': 'contact', 'text': reply, 'plan': steps,
            'decisions': decisions, 'resource_ids': [r['id'] for r in resources],
            'phenotypes': query['phenotypes'],
            'mode': 'Bounded diagnostic planner: advertised operation adapters plus resource policy; free text is preserved, not interpreted as permission.'}
