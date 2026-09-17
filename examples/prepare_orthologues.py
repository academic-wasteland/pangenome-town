"""Prepare the MGI protein-coding mouse/human orthology report for INDIGENA.

Download https://www.informatics.jax.org/downloads/reports/HOM_ProteinCoding.rpt
then pass its path and the destination JSON. No network access is performed here.
Duplicate mouse NCBI aliases are collapsed only if HGNC/human mapping agrees;
conflicting human mappings are excluded.
"""
import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path


def prepare(source, destination):
    data = source.read_bytes()
    mapping, ambiguous = {}, set()
    for line in data.decode().splitlines():
        fields = line.split('\t')
        if len(fields) != 6 or not fields[0].startswith('MGI:') or not fields[3].startswith('HGNC:'):
            raise ValueError('Unexpected MGI report format.')
        mouse, symbol, _, hgnc, human, entrez = fields
        row = {'mouse_symbol': symbol, 'symbol': human, 'hgnc': hgnc, 'ncbi_gene': entrez}
        if mouse in mapping and mapping[mouse] != row:
            ambiguous.add(mouse)
        mapping[mouse] = row
    for mouse in ambiguous:
        del mapping[mouse]
    document = {'source': 'https://www.informatics.jax.org/downloads/reports/HOM_ProteinCoding.rpt',
                'retrieved': datetime.now(UTC).date().isoformat(), 'sha256': hashlib.sha256(data).hexdigest(),
                'relationship': 'MGI reported one-to-one protein-coding mouse/human orthology; conflicting mappings excluded',
                'excluded_ambiguous': len(ambiguous), 'mapping': mapping}
    destination.write_text(json.dumps(document))
    print(f'Prepared {len(mapping)} mappings; excluded {len(ambiguous)} ambiguous mouse genes.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    prepare(args.source, args.destination)
