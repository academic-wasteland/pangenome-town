import dataclasses

import pytest

from pangenome_town import resources
from pangenome_town.compute import ComputeError


def test_declared_collections_only_and_versioned_chunks(towns, tmp_path):
    root = tmp_path / 'published'
    root.mkdir()
    (root/'result.txt').write_text('x'*30000)
    (root/'.private.txt').write_text('secret')
    outside = tmp_path/'secret.txt'
    outside.write_text('secret')
    (root/'escape.txt').symlink_to(outside)
    town = dataclasses.replace(towns['ubar'],extra={'resources':[{'name':'results','root':str(root),'include':['*.txt']}]})
    listing = resources.listing(town)
    assert [r['name'] for r in listing] == ['result.txt']
    assert str(root) not in str(listing)
    first = resources.chunk(town,{'id':listing[0]['id']})
    assert first['next_offset'] == 24000 and not first['eof']
    second = resources.chunk(town,{'id':listing[0]['id'],'offset':24000,'sha256':first['sha256']})
    assert second['eof']
    (root/'result.txt').write_text('changed')
    with pytest.raises(ComputeError,match='version changed'):
        resources.chunk(town,{'id':listing[0]['id'],'offset':24000,'sha256':first['sha256']})
    with pytest.raises(ComputeError,match='unknown'):
        resources.read(town,'../../secret.txt')
    (root/'new.txt').write_text('new result')
    assert len(resources.listing(town)) == 2
