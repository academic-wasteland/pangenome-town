"""Resolve labels from the HPO/MP release used by the local INDIGENA model.

No fuzzy match is submitted to inference: suggestions and ambiguities require an
explicit choice. Exact synonyms are accepted; broad/related synonyms are not.
"""
import argparse
import difflib
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path

ID = re.compile(r'(HP|MP|UPHENO):\d{7}')
PAIR = re.compile(r'^(.+?)\s*\(((?:HP|MP):\d{7})\)$')


def normalized(value):
    return ' '.join(value.casefold().split())


def prepare(source, destination):
    source, destination = Path(source), Path(destination)
    rdf = '{http://www.w3.org/1999/02/22-rdf-syntax-ns#}'
    owl = '{http://www.w3.org/2002/07/owl#}'
    rows, version = {}, None
    for _, element in ET.iterparse(source, events=['end']):
        if element.tag == owl + 'versionIRI' and version is None:
            version = element.get(rdf + 'resource')
        if element.tag != owl + 'Class':
            continue
        uri = element.get(rdf + 'about', '')
        identifier = uri.rsplit('/', 1)[-1].replace('_', ':')
        label = element.find('{http://www.w3.org/2000/01/rdf-schema#}label')
        obsolete = element.find(owl + 'deprecated')
        if (ID.fullmatch(identifier) and identifier.startswith(('HP:', 'MP:'))
                and label is not None and label.text
                and (obsolete is None or obsolete.text not in ('true', '1'))):
            rows[identifier] = {'label': label.text, 'synonyms': [s.text for s in element.findall(
                '{http://www.geneontology.org/formats/oboInOwl#}hasExactSynonym') if s.text]}
        element.clear()
    if not rows or not version:
        raise ValueError('ontology has no HPO/MP labels or version IRI')
    with source.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    payload = {'source': version, 'sha256': digest, 'terms': rows}
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload))
    temporary.replace(destination)
    return len(rows)


@lru_cache(maxsize=2)
def index(path, signature):
    data = json.loads(Path(path).read_text())
    names = {}
    for identifier, row in data['terms'].items():
        for label in [row['label'], *row.get('synonyms', [])]:
            names.setdefault(normalized(label), set()).add(identifier)
    return data, names


def resolve(settings, terms):
    path = Path(settings.get('labels') or Path(settings.get('data', '.')) / 'phenotype-labels.json').expanduser()
    # Existing identifier-only callers do not depend on an optional label index.
    if not path.exists():
        if all(ID.fullmatch(t) for t in terms):
            return terms, None
        raise ValueError('HPO/MP label index is not installed; prepare phenotype-labels.json first')
    data, names = index(str(path.resolve()), path.stat().st_mtime_ns)
    resolved, details = [], []
    for term in terms:
        pair = PAIR.fullmatch(term)
        if pair:
            label, identifier = pair.groups()
            row = data['terms'].get(identifier)
            if row is None or normalized(label) not in {normalized(x) for x in [row['label'], *row.get('synonyms', [])]}:
                raise ValueError(f'Phenotype label/ID mismatch: {term}')
            resolved.append(identifier)
            details.append({'input': term, 'id': identifier, 'label': row['label'],
                            'ontology': identifier.split(':')[0], 'match': 'label and identifier'})
            continue
        prefix, value = None, term
        match = re.match(r'^(HPO|HP|MP):\s*(.+)$', term, re.IGNORECASE)
        if match and not ID.fullmatch(term):
            prefix, value = ('HP' if match[1].upper() in ('HPO', 'HP') else 'MP'), match[2]
        candidates = {term} if ID.fullmatch(term) else names.get(normalized(value), set())
        if prefix:
            candidates = {c for c in candidates if c.startswith(prefix + ':')}
        if not candidates:
            suggestions = difflib.get_close_matches(normalized(value), names, n=3, cutoff=.65)
            hints = [f"{data['terms'][c]['label']} ({c})" for name in suggestions for c in sorted(names[name])
                     if not prefix or c.startswith(prefix + ':')]
            raise ValueError(f'Unknown phenotype label: {term}. ' + ('Possible matches: ' + '; '.join(hints) if hints else 'Use an HPO/MP label or identifier.'))
        if len(candidates) > 1:
            choices = '; '.join(f"{data['terms'][c]['label']} ({c})" for c in sorted(candidates))
            raise ValueError(f'Ambiguous phenotype: {term}. Choose an identifier or prefix HPO: / MP: . Matches: {choices}')
        identifier = next(iter(candidates))
        row = data['terms'].get(identifier)
        resolved.append(identifier)
        details.append({'input': term, 'id': identifier, 'label': row['label'] if row else identifier,
                        'ontology': identifier.split(':')[0],
                        'match': 'identifier' if ID.fullmatch(term) else ('label' if normalized(row['label']) == normalized(value) else 'exact synonym')})
    return sorted(set(resolved)), {'source': data['source'], 'sha256': data.get('sha256'), 'terms': details}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('ontology')
    parser.add_argument('output')
    args = parser.parse_args()
    print(f'Indexed {prepare(args.ontology, args.output)} HPO/MP terms')
