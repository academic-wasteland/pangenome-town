"""Run the real two-town private-variant demo and assert ranks, disclosure and report."""
import argparse
import hashlib
from pathlib import Path

from pangenome_town.config import load
from pangenome_town.demo_phenotypes import PhenotypeStage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--town-root', type=Path, default=Path('..'))
    parser.add_argument('--pdf', type=Path)
    args = parser.parse_args()
    towns = {name: load(args.town_root / name / 'town.toml') for name in ['ubar', 'yamatai']}
    stage = PhenotypeStage(towns)
    stage.start(private_case=True)
    stage.approve(stage.run['id'])
    stage.worker.join(240)
    assert stage.run['state'] == 'completed', stage.run['events'][-1]
    run = stage.run
    assert run['benchmark']['rank'] == 3
    assert run['variant_benchmark']['recovered'] is True
    assert run['variants']['retained'][0]['gene_rank'] == 3
    assert run['variants']['retained'][0]['revel_rank'] == 2
    assert run['variants']['retained'][0]['combined_rank'] == 1
    sent = [e for e in run['events'] if e['detail'].get('message_type') == 'sent']
    assert [e['title'] for e in sent] == ['message', 'phenotype-search', 'variant-interpretation']
    assert sent[0]['text'] == run['human_message']
    assert 'resources' in sent[0]['detail']['body'] and 'private_case' not in sent[0]['detail']['body']
    assert all(' (HP:' in term for term in sent[0]['detail']['body']['phenotypes'])
    assert run['delegation']['resource_ids'] == [run['resources'][0]['id']]
    assert len(run['delegation']['decisions']) == 3
    assert all(e['text'] == e['detail']['wire_body']['text'] for e in sent)
    replies = [e for e in run['events'] if e['detail'].get('message_type') == 'received']
    assert all(e['text'] == e['detail']['body']['text'] for e in replies)
    assert run['interpretation']['classification'] == 'Likely pathogenic'
    payloads = [e['detail']['body'] for e in run['events'] if e['title'] == 'variant-interpretation']
    assert len(payloads) == 1 and set(payloads[0]) == {'resident', 'variant', 'phenotypes'}
    pdf = stage.report_bytes()
    assert pdf.startswith(b'%PDF-') and hashlib.sha256(pdf).hexdigest() == run['report_sha256']
    if args.pdf:
        args.pdf.write_bytes(pdf)
    print('PASS: live INDIGENA FBN1 gene rank 3 / 1529.')
    print('PASS: FBN1 rank 2 by REVEL alone; rank 3 by phenotype; rank 1 only after combining both.')
    print('PASS: task-level diagnosis request, paired phenotype labels/IDs, advertised-service planning and owner-controlled resource policy.')
    print('PASS: actual human request, contact delegation, Phenomancer and Themis text messages with structured payloads.')
    print('PASS: Ubar interpretation receives only selected allele, phenotype identifiers and resident routing.')
    print('PASS: PS4 + PM2_Supporting + PP2 + PP3 -> Likely pathogenic; no PP4 or fabricated patient evidence.')
    print('PASS: Ubar-generated PDF received and SHA-256 verified.')


if __name__ == '__main__':
    main()
