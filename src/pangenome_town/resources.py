"""Read-only, explicitly selected local result collections; never execute their contents."""
import base64
import hashlib
import mimetypes
from pathlib import Path

from .compute import ComputeError

CHUNK = 24000


def entries(town):
    result = {}
    for collection in town.extra.get('resources', []):
        root = Path(collection['root']).expanduser().resolve()
        for pattern in collection.get('include', []):
            if Path(pattern).is_absolute() or '..' in Path(pattern).parts:
                raise ComputeError('resource patterns must remain inside their root')
            for candidate in sorted(root.glob(pattern)):
                path = candidate.resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    continue
                relative = candidate.relative_to(root).as_posix()
                if any(p.startswith('.') for p in Path(relative).parts):
                    continue
                identifier = hashlib.sha256((collection['name'] + '/' + relative).encode()).hexdigest()[:24]
                result[identifier] = (path, {'id': identifier, 'collection': collection['name'], 'name': relative,
                    'resident': collection.get('resident'), 'source': collection.get('source', collection['name']),
                    'media_type': mimetypes.guess_type(path.name)[0] or 'application/octet-stream'})
    return result


def read(town, identifier):
    item = entries(town).get(identifier)
    if not item:
        raise ComputeError('unknown published resource')
    path, metadata = item
    before = path.stat()
    if before.st_size > 50 * 1024 * 1024:
        raise ComputeError('resource exceeds the 50 MiB serving limit')
    data = path.read_bytes()
    after = path.stat()
    if (before.st_mtime_ns, before.st_size, before.st_ino) != (after.st_mtime_ns, after.st_size, after.st_ino):
        raise ComputeError('resource changed while reading; retry after the producer finishes')
    return data, {**metadata, 'bytes': len(data), 'sha256': 'sha256:' + hashlib.sha256(data).hexdigest(),
                  'modified': after.st_mtime, 'town': town.name}


def listing(town):
    result = []
    for path, metadata in entries(town).values():
        stat = path.stat()
        result.append({**metadata, 'bytes': stat.st_size, 'modified': stat.st_mtime, 'town': town.name})
    return result


def chunk(town, body):
    identifier = body.get('id')
    offset = body.get('offset', 0)
    if not isinstance(identifier, str) or type(offset) is not int or offset < 0:
        raise ComputeError('resource ID and nonnegative integer offset required')
    data, metadata = read(town, identifier)
    if offset and body.get('sha256') != metadata['sha256']:
        raise ComputeError('resource version changed; restart download with offset 0')
    if body.get('sha256') and body['sha256'] != metadata['sha256']:
        raise ComputeError('resource version changed')
    if offset > len(data):
        raise ComputeError('offset beyond resource')
    end = min(offset + CHUNK, len(data))
    return {'ok': True, **metadata, 'offset': offset, 'next_offset': end, 'eof': end == len(data),
            'encoding': 'base64', 'data': base64.b64encode(data[offset:end]).decode()}
