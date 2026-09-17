"""Check the selected Marfan demo against the installed model (no ranking overrides).

Run: python examples/indigena_rank_check.py /path/to/indigena/data
This is a regression demonstration, not a held-out accuracy estimate.
"""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from pangenome_town.phenotypes import search


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('data', type=Path)
    args = parser.parse_args()
    town = SimpleNamespace(extra={'phenotype_search': {
        'enabled': True, 'backend': 'indigena', 'data': str(args.data),
        'model': str(args.data / 'wasteland-model-current'),
        'orthologues': str(args.data / 'orthologues.json')}})
    labels = ['HPO: Ectopia lentis', 'HPO: Arachnodactyly', 'HPO: Aortic root aneurysm']
    result = search(town, {'phenotypes': labels, 'limit': 20, 'include_human_orthologues': True})
    by_id = search(town, {'phenotypes': ['HP:0001083', 'HP:0001166', 'HP:0002616'],
                         'limit': 20, 'include_human_orthologues': True})
    assert result['results'] == by_id['results'], 'label resolution changed ranking'
    rank = next((i+1 for i, row in enumerate(result['results'])
                 if (row.get('human_orthologue') or {}).get('symbol') == 'FBN1'), None)
    assert rank is not None and rank <= 5, f'FBN1 rank regressed: {rank}'
    assert len(result['results']) == 20 and result['candidate_count'] == 1529
    print(json.dumps({'case': 'Marfan syndrome', 'expected_gene': 'FBN1', 'actual_rank': rank,
                      'candidate_count': result['candidate_count'], 'checkpoint_sha256': result['checkpoint_sha256'],
                      'resolution': result['resolution'], 'top_5': result['results'][:5]}, indent=2))


if __name__ == '__main__':
    main()
