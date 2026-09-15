"""Six executable acceptance examples. Ephemeral keys; no network or real data."""
from datetime import UTC, datetime, timedelta

from pangenome_town.authority import certification, keys
from pangenome_town.authority import credentials as c

NOW = datetime(2026, 9, 15, tzinfo=UTC)
SCOPE, RAW = 'urn:scope:aggregate', 'urn:scope:raw'
SAUDI, JAPAN = 'urn:dataset:saudi', 'urn:dataset:japan'
AGENT = 'urn:yamatai:analyst'


class World:
    def __init__(self):
        self.keys = {name: keys.generate() for name in ('camelot', 'fremen', 'board', 'lab', 'ubar', 'yamatai', 'human')}
        self.holder = keys.generate()
        self.task = {'@id': 'urn:task:example', 'requestedBy': AGENT, 'taskType': 'aggregate',
                     'usesDataset': [SAUDI], 'executor': AGENT, 'site': 'a001', 'region': 'chr1:1-100'}
        self.audience = 'urn:town:lisan'
        self.status = lambda _: 'active'

    def rule(self, issuer, kind='Qualification', depth=0):
        return {'issuer': issuer, 'types': [kind], 'scopes': [SCOPE], 'datasets': [SAUDI], 'delegation_depth': depth}

    def policy(self, *rules, kinds=('Qualification',)):
        return {'issuers': list(rules), 'requirements': [{'type': k, 'per_dataset': k == 'DataAccessAuthorization'} for k in kinds],
                'task_scopes': {'aggregate': SCOPE, 'raw': RAW}}

    def credential(self, issuer, kind='Qualification', scope=SCOPE, **extra):
        subject = {'id': AGENT, 'holderKey': keys.public_key_text(self.holder), 'scope': scope, **extra}
        if kind in {'DataAccessAuthorization', 'ComputeAuthorization', 'HumanDelegation'}:
            subject.update(taskDigest=c.task_digest(self.task), audience=self.audience)
        if kind == 'DataAccessAuthorization':
            subject['dataset'] = SAUDI
        return c.issue(issuer=issuer, issuer_key=self.keys[issuer], types=kind, subject=subject, now=NOW)

    def presentation(self, documents, accreditations=(), holder=AGENT, holder_key=None, now=NOW):
        return c.present(holder=holder, holder_key=holder_key or self.holder, credentials=documents,
                         accreditations=list(accreditations), task=self.task, audience=self.audience, now=now)

    def assess(self, policy, documents=(), accreditations=(), presentation=None, now=NOW):
        return certification.evaluate(policy, self.task, presentation or self.presentation(documents, accreditations, now=now),
                                      audience=self.audience, anchors={r['issuer']: keys.public_key_text(self.keys[r['issuer']])
                                                                      for r in policy['issuers']},
                                      directory={k: keys.public_key_text(v) for k, v in self.keys.items()},
                                      status_checker=self.status, now=now)


def independent():
    w = World()
    policy = w.policy(w.rule('fremen'))
    assert not w.assess(policy, [w.credential('camelot')])['ok']
    assert w.assess(policy, [w.credential('fremen')])['ok']
    print('1. Lisan: Camelot refused; local Fremen certification accepted.')


def shared():
    w = World()
    policy = w.policy(w.rule('board'))
    certificate = w.credential('board')
    for town in ('urn:town:ubar', 'urn:town:lisan'):
        w.audience = town
        assert w.assess(policy, [certificate])['ok']
    # A portable qualification still cannot satisfy a separate resource grant.
    policy['requirements'].append({'type': 'DataAccessAuthorization', 'per_dataset': True})
    assert not w.assess(policy, [certificate])['ok']
    print('2. Independent Board accepted by two towns; qualification grants no dataset access.')


def separate():
    w = World()
    kinds = ('Qualification', 'DataAccessAuthorization', 'ComputeAuthorization', 'EthicsApproval')
    issuers = ('board', 'ubar', 'yamatai', 'fremen')
    policy = w.policy(*(w.rule(i, k) for i, k in zip(issuers, kinds)), kinds=kinds)
    documents = [w.credential(i, k) for i, k in zip(issuers, kinds)]
    assert w.assess(policy, documents)['ok']
    for n in range(4):
        assert not w.assess(policy, documents[:n] + documents[n+1:])['ok']
    wrong = list(documents)
    wrong[1] = w.credential('fremen', 'DataAccessAuthorization')
    assert not w.assess(policy, wrong)['ok']
    w.task['region'] = 'chr1:1-200'
    assert not w.assess(policy, documents)['ok']
    print('3. Qualification + Ubar custody + Yamatai compute + ethics required; changed task refused.')


def delegated():
    w = World()
    policy = w.policy(w.rule('board', depth=1))
    delegation = c.accreditation(accreditor='board', accreditor_key=w.keys['board'], subject_issuer='lab',
                                 subject_public_key=keys.public_key_text(w.keys['lab']), roles=[],
                                 types=['Qualification'], scopes=[SCOPE], datasets=[SAUDI], now=NOW)
    documents = [w.credential('lab')]
    assert not w.assess(policy, documents)['ok']
    assert w.assess(policy, documents, [delegation])['ok']
    deeper = c.accreditation(accreditor='lab', accreditor_key=w.keys['lab'], subject_issuer='fremen',
                            subject_public_key=keys.public_key_text(w.keys['fremen']), roles=[],
                            types=['Qualification'], scopes=[SCOPE], now=NOW)
    assert not w.assess(policy, [w.credential('fremen')], [delegation, deeper])['ok']
    w.task['taskType'] = 'raw'
    assert not w.assess(policy, [w.credential('lab', scope=RAW)], [delegation])['ok']
    print('4. Scoped Board-to-lab delegation accepted; raw export and undelegated onward authority refused.')


def residents():
    w = World()
    policy = w.policy(w.rule('board'))
    certificate = w.credential('board')
    assert w.assess(policy, [certificate])['ok']
    thief = keys.generate()
    assert not w.assess(policy, presentation=w.presentation([certificate], holder='urn:yamatai:bloodninja', holder_key=thief))['ok']
    assert not w.assess(policy, presentation=w.presentation([certificate], holder=AGENT, holder_key=thief))['ok']
    w.task['onBehalfOf'] = 'human'
    assert not w.assess(policy, [certificate])['ok']
    policy['requirements'].append({'type': 'HumanDelegation'})
    policy['issuers'].append(w.rule('human', 'HumanDelegation'))
    assert w.assess(policy, [certificate, w.credential('human', 'HumanDelegation')])['ok']
    print('5. Bloodninja cannot borrow an analyst identity; human delegation must approve the exact task.')


def withdrawn():
    w = World()
    policy = w.policy(w.rule('board'))
    certificate = w.credential('board')
    assert w.assess(policy, [certificate])['ok']
    for state in ('revoked', 'unknown'):
        w.status = lambda _, state=state: state
        assert not w.assess(policy, [certificate])['ok']
    w.status = lambda _: 'active'
    assert not w.assess(policy, [certificate], now=NOW + timedelta(days=31))['ok']
    print('6. Active approval accepted; revoked, unavailable and expired certification block protected work.')


SCENARIOS = [independent, shared, separate, delegated, residents, withdrawn]
if __name__ == '__main__':
    import sys
    selected = SCENARIOS if len(sys.argv) == 1 else [SCENARIOS[int(sys.argv[1]) - 1]]
    for scenario in selected:
        scenario()
