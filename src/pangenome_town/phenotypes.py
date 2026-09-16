"""Bounded INDIGENA semantic-similarity search using its original SLIB backend."""
import gzip
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

_LOCK = threading.Lock()


def validate(body):
    terms = body.get('phenotypes')
    if not isinstance(terms, list) or not 1 <= len(terms) <= 30 or any(
        not isinstance(x, str) or not re.fullmatch(r'(HP|MP|UPHENO):\d{7}', x) for x in terms
    ):
        raise ValueError('phenotypes must contain 1–30 HP, MP or UPHENO identifiers')
    limit = body.get('limit', 10)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('limit must be an integer from 1 to 50')
    measure = body.get('measure', 'lin')
    if measure not in {'lin', 'resnik'}:
        raise ValueError('measure must be lin or resnik')
    return {'phenotypes': sorted(set(terms)), 'limit': limit, 'measure': measure}


def search(town, body):
    query = validate(body)
    settings = town.extra.get('phenotype_search', {})
    if not settings.get('enabled'):
        raise ValueError('phenotype search is not enabled in this town')
    method = body.get('method') or settings.get('backend', 'baseline')
    if method not in {'baseline', 'indigena'}:
        raise ValueError('method must be baseline or indigena')
    if method == 'indigena':
        from .indigena import search as learned_search
        if not settings.get('model'):
            raise ValueError('a trained INDIGENA model is not installed yet')
        return learned_search(settings, query)
    root = Path(settings['data']).expanduser().resolve()
    groovy = settings.get('groovy', 'groovy')
    town.state_dir.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        if not (root / 'upheno.owl').exists():
            raise ValueError('prepare the local INDIGENA data with phenotype-prepare first')
        with tempfile.TemporaryDirectory(prefix='phenotype-', dir=town.state_dir) as temporary:
            request = Path(temporary) / 'query.json'
            request.write_text(json.dumps(query))
            result = subprocess.run([groovy, str(Path(__file__).with_name('phenotype_search.groovy')),
                                     str(root), str(request)], capture_output=True, text=True, check=False,
                                    timeout=180, env={**os.environ, 'JAVA_OPTS': '-Xmx3g -XX:ActiveProcessorCount=2 --add-opens=java.base/java.lang=ALL-UNNAMED'})
            for line in reversed(result.stdout.splitlines()):
                if result.returncode == 0 and line.startswith('RESULT_JSON='):
                    payload = json.loads(line.removeprefix('RESULT_JSON='))
                    payload['query'] = query
                    payload['source'] = 'https://github.com/bio-ontology-research-group/indigena'
                    return payload
            raise ValueError('INDIGENA backend failed: ' + (result.stderr or result.stdout)[-1500:])


def prepare(town):
    root = Path(town.extra['phenotype_search']['data']).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    with gzip.open(root / 'upheno.owl.gz', 'rb') as source, (root / 'upheno.owl.tmp').open('wb') as out:
        shutil.copyfileobj(source, out)
    (root / 'upheno.owl.tmp').replace(root / 'upheno.owl')
    return {'ok': True, 'data': str(root)}
