"""Hourly literature inbox with durable search history and a one-paper delivery budget."""
import fcntl
import hashlib
import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta

from .exchange import Envelope, ExchangeLog

TOPICS = {
    'pangenomes': ['pangenome', 'pan-genome', 'pangenomic', 'pangenomics'],
    'phenotypes': ['phenotype', 'phenotypic', 'human phenotype ontology', 'hpo', 'mondo', 'disease ontology'],
}


def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'AcademicWasteland-LiteratureScout/1.0 (https://github.com/academic-wasteland/pangenome-town)'})
    with urllib.request.urlopen(req, timeout=30) as response:
        data = response.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise ValueError('literature response too large')
    return data


def text(value):
    return re.sub(r'<[^>]+>', ' ', str(value or '')).strip()


def topics(value):
    value = value.lower()
    return [topic for topic, words in TOPICS.items() if any(re.search(r'\b' + re.escape(w) + r'\w*\b', value) for w in words)]


def paper(source, ident, title, abstract, url, date, doi=''):
    doi = doi.lower().removeprefix('https://doi.org/').strip()
    key = doi or source + ':' + ident
    return {'id': hashlib.sha256(key.encode()).hexdigest()[:24], 'source': source, 'doi': doi,
            'title': text(title), 'abstract': text(abstract)[:12000], 'url': url,
            'title_key': re.sub(r'[^a-z0-9]', '', text(title).lower()),
            'date': date, 'topics': topics(title + ' ' + text(abstract))}


def search_source(source, topic, start, end):
    """One bounded dated query; bioRxiv pages are one dated snapshot for both topics."""
    if source == 'pmc':
        query = ('(pangenome OR pangenomic OR "pan-genome")' if topic == 'pangenomes' else
                 '(phenotype OR "human phenotype ontology" OR MONDO OR "disease ontology")')
        params = urllib.parse.urlencode({'query': query + f' AND IN_PMC:y AND FIRST_PDATE:[{start} TO {end}]',
                                       'format': 'json', 'resultType': 'core', 'pageSize': 100, 'sort': 'FIRST_PDATE_D desc'})
        payload = json.loads(fetch('https://www.ebi.ac.uk/europepmc/webservices/rest/search?' + params))
        return [paper('pmc', x['pmcid'], x.get('title',''), x.get('abstractText',''),
                      'https://pmc.ncbi.nlm.nih.gov/articles/' + x['pmcid'] + '/', x.get('firstPublicationDate',''), x.get('doi',''))
                for x in payload.get('resultList',{}).get('result',[]) if x.get('pmcid')]
    if source == 'arxiv':
        query = '(all:pangenome OR all:pangenomic)' if topic == 'pangenomes' else '(all:phenotype OR all:"human phenotype ontology" OR all:MONDO)'
        query += ' AND submittedDate:[' + start.replace('-','') + '0000 TO ' + end.replace('-','') + '2359]'
        params = urllib.parse.urlencode({'search_query': query, 'max_results': 100, 'sortBy': 'submittedDate', 'sortOrder': 'descending'})
        root = ET.fromstring(fetch('https://export.arxiv.org/api/query?' + params))
        ns = {'a': 'http://www.w3.org/2005/Atom', 'ar': 'http://arxiv.org/schemas/atom'}
        result = []
        for entry in root.findall('a:entry', ns):
            ident = entry.findtext('a:id', '', ns)
            if '/api/errors' in ident:
                raise ValueError(entry.findtext('a:summary', 'arXiv query failed', ns))
            result.append(paper('arxiv', re.sub(r'v\d+$', '', ident), entry.findtext('a:title','',ns),
                                entry.findtext('a:summary','',ns), ident.replace('http:', 'https:'),
                                entry.findtext('a:published','',ns), entry.findtext('ar:doi','',ns)))
        return result
    result = []
    for cursor in range(0, 1000, 100):
        payload = json.loads(fetch(f'https://api.biorxiv.org/details/biorxiv/{start}/{end}/{cursor}/json'))
        batch = payload.get('collection', [])
        if not batch and payload.get('messages', [{}])[0].get('status') != 'ok':
            raise ValueError('bioRxiv API returned no valid collection')
        for x in batch:
            if topics(x.get('title','') + ' ' + x.get('abstract','')):
                result.append(paper('biorxiv', x['doi'], x.get('title',''), x.get('abstract',''),
                                    'https://doi.org/' + x['doi'], x.get('date',''), x['doi']))
        if len(batch) < 100:
            break
    return result


def connect(town):
    directory = town.state_dir / 'literature'
    directory.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(directory / 'history.sqlite', timeout=30)
    db.executescript('''
    CREATE TABLE IF NOT EXISTS searches(key TEXT PRIMARY KEY, completed REAL, count INTEGER);
    CREATE TABLE IF NOT EXISTS papers(id TEXT PRIMARY KEY, document TEXT NOT NULL, shared REAL);
    CREATE TABLE IF NOT EXISTS directories(town TEXT PRIMARY KEY, checked REAL, document TEXT);
    CREATE TABLE IF NOT EXISTS deliveries(paper TEXT, town TEXT, resident TEXT, envelope TEXT, sent REAL,
       PRIMARY KEY(paper,town,resident));
    CREATE TABLE IF NOT EXISTS slots(hour INTEGER PRIMARY KEY, paper TEXT NOT NULL);
    ''')
    for ident, raw in db.execute('SELECT id,document FROM papers').fetchall():
        value = json.loads(raw)
        if 'title_key' not in value:
            value['title_key'] = re.sub(r'[^a-z0-9]', '', value['title'].lower())
            db.execute('UPDATE papers SET document=? WHERE id=?', (json.dumps(value), ident))
    db.commit()
    return db


def client(town):
    from pathlib import Path

    from wasteland.client import Client
    return Client(Path(town.extra['federation']['state']).expanduser())


def discover(town, db, now):
    from wasteland.client import request
    c = client(town)
    listing = request(c.config['hub'], '/.well-known/wasteland.json')['towns']
    errors = []
    for entry in listing:
        name = entry['name']
        if name == town.name or 'message' not in entry.get('capabilities', []):
            continue
        old = db.execute('SELECT checked FROM directories WHERE town=?', (name,)).fetchone()
        if old and now - old[0] < 86400:
            continue
        try:
            mid = c.ask(name, operation='describe', text='Publish your residents and research interests for relevant literature recommendations.')
            replies = c.wait(mid, timeout=12, acknowledge=False)
            document = replies[-1]['body']
            document = document.get('description') if isinstance(document.get('description'), dict) else document
            db.execute('INSERT OR REPLACE INTO directories VALUES(?,?,?)', (name, now, json.dumps(document)))
            db.commit()
        except Exception as error:  # noqa: BLE001 - isolate independently failing sources
            errors.append({'town': name, 'error': str(error)[:200]})
    return errors


def recipients(db, p):
    result = []
    for name, checked, raw in db.execute('SELECT town,checked,document FROM directories'):
        if time.time() - checked > 2 * 86400:
            continue
        doc = json.loads(raw)
        common = doc.get('interests', [])
        for resident in doc.get('residents', []):
            interests = resident.get('interests', common)
            description = json.dumps(interests) + ' ' + resident.get('role', '')
            matched = sorted(set(topics(description)) & set(p['topics']))
            if matched:
                result.append({'town': name, 'resident': resident['name'], 'published_interests': interests,
                               'role': resident.get('role',''), 'matching_topics': matched})
    return result[:20]


def candidates(town):
    with connect(town) as db:
        result = []
        for raw, in db.execute('SELECT document FROM papers p WHERE shared IS NULL OR NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.paper=p.id AND d.sent IS NOT NULL) OR EXISTS (SELECT 1 FROM deliveries d WHERE d.paper=p.id AND d.sent IS NULL) ORDER BY rowid DESC LIMIT 300'):
            p = json.loads(raw)
            matches = recipients(db, p)
            if matches:
                result.append({**p, 'recipients': matches})
        return result[:15]


def collect(town, wake=False):
    now = time.time()
    # Query only closed date windows. Each successful source/topic/window runs once.
    end = datetime.now(UTC).date() - timedelta(days=1)
    errors, counts = [], {}
    with connect(town) as db:
        first = db.execute('SELECT count(*) FROM searches').fetchone()[0] == 0
        start = end - timedelta(days=6) if first else end
        for source in ('pmc', 'arxiv', 'biorxiv'):
            for topic in (('all',) if source == 'biorxiv' else TOPICS):
                # A bootstrap range includes yesterday; record the daily keys too.
                key = f'{source}:{topic}:{end}'
                if db.execute('SELECT 1 FROM searches WHERE key=?', (key,)).fetchone():
                    continue
                try:
                    found = search_source(source, topic, start.isoformat(), end.isoformat())
                    existing_titles = {json.loads(row[0]).get('title_key') for row in db.execute('SELECT document FROM papers')}
                    for p in found:
                        if p['topics'] and p['title_key'] not in existing_titles:
                            existing_titles.add(p['title_key'])
                            db.execute('INSERT OR IGNORE INTO papers(id,document) VALUES(?,?)', (p['id'], json.dumps(p)))
                    db.execute('INSERT INTO searches VALUES(?,?,?)', (key, now, len(found)))
                    db.commit()
                    counts[key] = len(found)
                except Exception as error:  # noqa: BLE001 - isolate independently failing sources
                    errors.append({'source': source, 'topic': topic, 'error': str(error)[:200]})
        errors.extend(discover(town, db, now))
    report = {'searched': counts, 'errors': errors, 'candidates': candidates(town),
              'instruction': 'Choose at most ONE paper, read its abstract and published recipient interests, then literature-share. No invented results.'}
    (town.state_dir / 'literature' / 'candidates.json').write_text(json.dumps(report, indent=2))
    ExchangeLog(town.exchange_db).event(town.name, 'literature_search', None,
                                       {'searched': counts, 'errors': errors, 'candidates': len(report['candidates'])})
    if wake and report['candidates']:
        from .mail import wake_resident
        report['wake_requested'] = wake_resident(town, 'bloodninja_scout', message=
            'Hourly literature round: run pangenome-town literature-candidates. Read the abstracts and published interests. '
            'Choose ONE relevant paper and use literature-share as instructed in your prompt. Do not repeat searches.')
    return report


def share(town, paper_id, targets, reason):
    """Validate the model's choice, reserve the hour, then send stable-id envelopes."""
    if not isinstance(reason, str) or not 20 <= len(reason) <= 1500:
        raise ValueError('supply a 20–1500 character explanation of relevance')
    if not targets or len(targets) > 5 or len(set(targets)) != len(targets):
        raise ValueError('choose 1–5 distinct town/resident targets')
    directory = town.state_dir / 'literature'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'delivery.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with connect(town) as db:
            row = db.execute('SELECT document,shared FROM papers WHERE id=?', (paper_id,)).fetchone()
            if not row:
                raise ValueError('unknown paper; collect literature first')
            p = json.loads(row[0])
            allowed = {r['town'] + '/' + r['resident'] for r in recipients(db, p)}
            if not set(targets) <= allowed:
                raise ValueError('recipient must have matching published research interests')
            hour = int(time.time() // 3600)
            slot = db.execute('SELECT paper FROM slots WHERE hour=?', (hour,)).fetchone()
            if slot and slot[0] != paper_id:
                raise ValueError('one-paper-per-hour budget already used')
            prior = db.execute('SELECT town,resident,sent FROM deliveries WHERE paper=?', (paper_id,)).fetchall()
            pending = not prior or any(r[2] is None for r in prior)
            if row[1] and (not slot or slot[0] != paper_id) and not pending:
                raise ValueError('this paper was already shared')
            recent = db.execute('SELECT max(shared) FROM papers WHERE id<>?', (paper_id,)).fetchone()[0]
            if recent and time.time() - recent < 3600:
                raise ValueError('wait one hour after the last paper')
            db.execute('INSERT OR IGNORE INTO slots VALUES(?,?)', (hour, paper_id))
            # Reserve durably before network I/O; failures retry the same envelope ID.
            db.execute('UPDATE papers SET shared=coalesce(shared,?) WHERE id=?', (time.time(),paper_id))
            db.commit()
            c, log, sent = client(town), ExchangeLog(town.exchange_db), []
            for target in targets:
                peer, resident = target.split('/', 1)
                old = db.execute('SELECT envelope,sent FROM deliveries WHERE paper=? AND town=? AND resident=?',
                                 (paper_id, peer, resident)).fetchone()
                if old and old[1]:
                    continue
                if old:
                    env = Envelope.from_dict(json.loads(old[0]))
                else:
                    env = Envelope.new('question', town.name, peer, {
                        'operation': 'message', 'resident': resident, 'sender_resident': 'bloodninja_scout',
                        'literature_recommendation': True, 'paper': p,
                        'text': 'I put on my robe and wizard hat\nBloodninja’s literature scout recommends: '
                                + p['title'] + '\n' + p['url'] + '\nWhy this may interest you: ' + reason
                                + '\nBased on the indexed abstract; please inspect the linked paper. No reply required.'})
                    values = env.to_dict()
                    values['id'] = 'urn:uuid:' + str(uuid.uuid5(uuid.NAMESPACE_URL, f'{town.name}:literature:{paper_id}:{target}'))
                    env = Envelope.from_dict(values)
                    db.execute('INSERT INTO deliveries VALUES(?,?,?,?,NULL)', (paper_id, peer, resident, json.dumps(values)))
                    db.commit()
                log.record(env, town=town.name, direction='sent', status='sending')
                c.send(env.to_dict())
                log.set_status(env.id, 'sent')
                log.event(town.name, 'literature_shared', env.id, {'paper': paper_id, 'resident': resident, 'reason': reason})
                if not db.execute('SELECT 1 FROM deliveries WHERE paper=? AND sent IS NOT NULL', (paper_id,)).fetchone():
                    db.execute('UPDATE papers SET shared=? WHERE id=?', (time.time(), paper_id))
                db.execute('UPDATE deliveries SET sent=? WHERE paper=? AND town=? AND resident=?', (time.time(),paper_id,peer,resident))
                db.commit()
                sent.append(target)
            return {'ok': True, 'paper': paper_id, 'sent': sent}
