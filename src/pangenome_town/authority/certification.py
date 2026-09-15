"""Receiver-owned certification policy. Trust is typed, scoped and non-transitive by default.

Keys prove signatures; rules confer authority. Each requirement is ANDed, while
its accepted issuers are alternatives. No rule is inferred from town membership.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from . import credentials as cred
from . import keys


def _iri(value):
    return value.get('@id') if isinstance(value, dict) else value


def _covers(rule, kind, scope, dataset):
    return (kind in rule.get('types', [])
            and scope in rule.get('scopes', [])
            and (dataset is None or dataset in rule.get('datasets', [])))


def accepted(issuer, kind, scope, dataset, rules, delegations, *, trail=()):
    """Return the accepting root; delegation budgets constrain the entire path."""
    if issuer in trail or len(trail) > 8:
        return None
    for rule in rules:
        if (rule['issuer'] == issuer and _covers(rule, kind, scope, dataset)
                and len(trail) <= int(rule.get('delegation_depth', 0))):
            return issuer
    for document in delegations:
        subject = document['credentialSubject']
        if subject.get('id') != issuer or not _covers(subject, kind, scope, dataset):
            continue
        if len(trail) > int(subject.get('delegation_depth', 0)):
            continue
        root = accepted(document['issuer'], kind, scope, dataset, rules, delegations, trail=(*trail, issuer))
        if root:
            return root
    return None


def evaluate(policy: dict, task: dict, presentation: dict | None, *, audience: str,
             anchors: dict, directory=None, status_checker=None, now: datetime | None = None,
             identity_checker=None) -> dict[str, Any]:
    """Assess exact-task evidence. Used before execution AND before output release."""
    if not isinstance(policy, dict) or not isinstance(policy.get('requirements'), list) or not policy['requirements']:
        raise ValueError('certification policy requires a nonempty requirements list')
    rules = policy.get('issuers', [])
    for rule in rules:
        if not isinstance(rule, dict) or not all(rule.get(k) for k in ('issuer', 'types', 'scopes')):
            raise ValueError('each certification issuer needs issuer, types and scopes')
    moment = cred._now(now)
    # Strict mode never treats a missing revocation checker as evidence of validity.
    checker = status_checker or (lambda _: 'unknown')
    result = cred.verify_presentation(presentation, task, audience=audience, anchors=anchors,
                                      directory=directory, status_checker=checker, now=moment,
                                      revocation='required', max_age_seconds=policy.get('max_age_seconds', 900))
    presentation = presentation if isinstance(presentation, dict) else {}
    # Verify delegation independently, including required fresh status. Resolving a key
    # via an accreditation does not itself confer any issuing authority.
    resolved = dict(directory or {}) | anchors
    delegations = []
    pending = list(presentation.get('accreditations') or [])
    for _ in range(9):
        remaining = []
        for doc in pending:
            if not isinstance(doc, dict):
                continue
            issuer = doc.get('issuer')
            if issuer not in resolved:
                remaining.append(doc)
                continue
            subject = doc.get('credentialSubject') or {}
            try:
                valid = (keys.verify(doc, keys.parse_public(resolved[issuer]))
                         and 'Accreditation' in cred._types(doc)
                         and not cred._time_problems(doc, moment)
                         and cred._status(doc, checker, 'required')[0] == 'active')
                keys.parse_public(subject.get('publicKey'))
            except (ValueError, TypeError):
                valid = False
            if valid and subject.get('id') and subject.get('types') and subject.get('scopes'):
                name, key = subject['id'], subject['publicKey']
                if name in resolved and resolved[name] != key:
                    continue
                resolved[name] = key
                delegations.append(doc)
        pending = remaining
    # A structurally valid signed presentation may contain only an OIDC token.
    proof_ok = not result.problems and bool(result.holder)
    holder = result.holder
    expected = _iri(task.get('requestedBy'))
    identity_ok = proof_ok and holder == expected
    evidence = []
    diagnostics = []
    for check, doc in zip(result.credentials, presentation.get('credentials') or []):
        if not isinstance(doc, dict):
            continue
        subject = doc.get('credentialSubject') or {}
        diagnostics.extend(check.problems)
        if check.verified:
            evidence.append({'issuer': check.issuer, 'types': check.types, 'scope': check.scope,
                             'dataset': check.dataset, 'subject': subject, 'id': check.id})
    for token in presentation.get('identity_tokens') or []:
        if not identity_ok or identity_checker is None:
            diagnostics.append('identity provider unavailable')
            continue
        try:
            evidence.extend(identity_checker(token, holder, presentation.get('holderKey'), moment))
        except Exception:  # noqa: BLE001 - provider failures fail closed without exposing credentials.
            diagnostics.append('identity verification unavailable or invalid')
    known_datasets = {d for rule in rules for d in rule.get('datasets', [])}
    def is_dataset(value):
        if isinstance(value, str) or _iri(value) in known_datasets:
            return True
        types = value.get('@type', []) if isinstance(value, dict) else []
        types = [types] if isinstance(types, str) else types
        # Native tasks encode a genomic region in usesDataset too. Only that
        # recognizable auxiliary reference is exempt; untyped data stays protected.
        region = '/regions/' in str(_iri(value)) and any(t.endswith('Region') for t in types)
        return not region or any(t.endswith('Dataset') for t in types)
    datasets = [_iri(d) for d in task.get('usesDataset', []) if is_dataset(d)]
    scope = policy.get('task_scopes', {}).get(task.get('taskType'))
    decisions = []
    for requirement in policy['requirements']:
        kind = requirement['type']
        targets = datasets if requirement.get('per_dataset') or kind == 'DataAccessAuthorization' else [None]
        if not targets:
            targets = [None]
        for dataset in targets:
            match = None
            for item in evidence:
                if kind not in item['types'] or item['scope'] != scope:
                    continue
                if dataset is not None and item['dataset'] != dataset:
                    continue
                sub = item['subject']
                if (kind in {'DataAccessAuthorization', 'ComputeAuthorization', 'HumanDelegation'}
                        and (sub.get('taskDigest') != cred.task_digest(task) or sub.get('audience') != audience)):
                    continue
                if kind == 'HumanDelegation' and item['issuer'] != _iri(task.get('onBehalfOf')):
                    continue
                if dataset is None and item['dataset'] is not None and any(d != item['dataset'] for d in datasets):
                    continue
                targets_for_trust = [dataset] if dataset is not None else (datasets or [None])
                roots = [accepted(item['issuer'], kind, scope, d, rules, delegations) for d in targets_for_trust]
                root = roots[0] if roots and all(roots) else None
                if root and identity_ok:
                    match = {'issuer': item['issuer'], 'root': root, 'credential': item['id']}
                    break
            decisions.append({'type': kind, 'dataset': dataset, 'status': 'pass' if match else 'pending',
                              'evidence': match})
    principal = _iri(task.get('onBehalfOf'))
    if principal and principal != holder and not any(d['type'] == 'HumanDelegation' and d['status'] == 'pass' for d in decisions):
        decisions.append({'type': 'HumanDelegation', 'dataset': None, 'status': 'pending', 'evidence': None})
    ok = identity_ok and scope is not None and all(d['status'] == 'pass' for d in decisions)
    return {'ok': ok, 'identity': 'verified' if identity_ok and any(d['status'] == 'pass' for d in decisions) else 'unverified',
            'holder': holder, 'requirements': decisions, 'problems': sorted(set(result.problems + diagnostics)),
            'message': 'Certification accepted' if ok else 'Waiting for accepted, current certification and permissions'}


def describe(policy):
    """Public requirements, excluding provider secrets and local subject/key mappings."""
    if policy is None:
        return None
    return {'requirements': [{k: r[k] for k in ('type', 'per_dataset') if k in r}
                             for r in policy.get('requirements', [])],
            'issuers': [{k: r[k] for k in ('issuer', 'types', 'scopes', 'datasets', 'delegation_depth') if k in r}
                        for r in policy.get('issuers', [])],
            'task_scopes': dict(policy.get('task_scopes', {})),
            'rechecked': ['admission', 'output-release']}
