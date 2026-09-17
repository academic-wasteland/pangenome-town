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
        not isinstance(x, str) or not x.strip() or len(x) > 160 or any(ord(c) < 32 for c in x)
        or (re.match(r'^(HP|MP|UPHENO):\d', x) and not re.fullmatch(r'(HP|MP|UPHENO):\d{7}', x))
        or (':' in x and not re.match(r'^(HP|HPO|MP|UPHENO):', x, re.IGNORECASE) and not re.fullmatch(r'.+?\s*\((HP|MP):\d{7}\)', x)) for x in terms
    ):
        raise ValueError('phenotypes must contain 1–30 HPO/MP labels or HP, MP, UPHENO identifiers')
    limit = body.get('limit', 10)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('limit must be an integer from 1 to 50')
    measure = body.get('measure', 'lin')
    if measure not in {'lin', 'resnik'}:
        raise ValueError('measure must be lin or resnik')
    return {'phenotypes': sorted({term.strip() for term in terms}), 'limit': limit, 'measure': measure}


def search(town, body):
    query = validate(body)
    settings = town.extra.get('phenotype_search', {})
    if not settings.get('enabled'):
        raise ValueError('phenotype search is not enabled in this town')
    from .phenotype_labels import resolve
    query['phenotypes'], resolution = resolve(settings, query['phenotypes'])
    method = body.get('method') or settings.get('backend', 'baseline')
    if method not in {'baseline', 'indigena'}:
        raise ValueError('method must be baseline or indigena')
    if method == 'indigena':
        from .indigena import search as learned_search
        if not settings.get('model'):
            raise ValueError('a trained INDIGENA model is not installed yet')
        result = learned_search(settings, {k: v for k, v in query.items() if k != "measure"})
        include = body.get('include_human_orthologues', False)
        if not isinstance(include, bool):
            raise ValueError('include_human_orthologues must be boolean')
        if include:
            from .orthologues import annotate
            if not settings.get('orthologues'):
                raise ValueError('human orthologue mapping is not installed')
            result = annotate(result, settings['orthologues'])
        result['text'] = f"I resolved your phenotype query and ranked {result['candidate_count']} mouse gene profiles with INDIGENA. Here are {len(result['results'])} candidates, with human orthologue annotations when requested. Use these as research priorities; the scores are not diagnostic probabilities."
        result['resident'] = 'phenomancer'
        result['resolution'] = resolution
        return result
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
                    payload['resolution'] = resolution
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
