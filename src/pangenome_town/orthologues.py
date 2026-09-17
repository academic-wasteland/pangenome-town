"""Annotate mouse rankings using an operator-installed MGI one-to-one report."""
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=2)
def load(path, modified):
    return json.loads(Path(path).read_text())


def annotate(result, path):
    path = Path(path).expanduser().resolve()
    info = load(str(path), path.stat().st_mtime_ns)
    return {**result, 'orthology': {k: info[k] for k in ('source', 'retrieved', 'sha256', 'relationship')},
            'interpretation': 'Scores rank mouse phenotype profiles. Human orthologues are candidate annotations, not a validated human diagnostic ranking.',
            'results': [{**row, 'human_orthologue': info['mapping'].get(row['gene'])} for row in result['results']]}
