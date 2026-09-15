"""Issue credentials, build holder presentations, and verify them.

Verification produces facts only (which credentials are verified, which
accreditation edges hold, where each issuer key came from). Whether an issuer
is trusted, and whether a credential covers a task, is decided by the
reasoner over those facts.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import VC_CONTEXT
from . import keys as keymod

HOLDER_TYPES = ("EthicsApproval", "DataAccessAuthorization", "Qualification", "ComputeAuthorization", "HumanDelegation")


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: Any) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    value = text.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)


def issue(*, issuer: str, issuer_key, types: str, subject: dict[str, Any], valid_days: int = 30,
          now: datetime | None = None, credential_id: str | None = None, namespace: str | None = None) -> dict[str, Any]:
    moment = _now(now)
    token = str(uuid.uuid4())
    base = namespace or (issuer.split("/issuers/", 1)[0] + "/" if "/issuers/" in issuer else issuer.rstrip("/") + "/")
    identifier = credential_id or f"{base}credentials/{token}"
    status_token = identifier.rsplit("/", 1)[-1] if credential_id else token
    document = {
        "@context": list(VC_CONTEXT),
        "id": identifier,
        "type": ["VerifiableCredential", types],
        "issuer": issuer,
        "validFrom": iso(moment),
        "validUntil": iso(moment + timedelta(days=valid_days)),
        "credentialSubject": dict(subject),
        "credentialStatus": {"id": f"{base}status/{status_token}", "type": "RegistrarStatus"},
    }
    return keymod.sign(document, issuer_key, f"{issuer}#key-1", created=iso(moment))


def accreditation(*, accreditor: str, accreditor_key, subject_issuer: str, subject_public_key: str, roles: list[str],
                  valid_days: int = 365, now: datetime | None = None,
                  types: list[str] | None = None, scopes: list[str] | None = None,
                  datasets: list[str] | None = None, delegation_depth: int = 0) -> dict[str, Any]:
    return issue(issuer=accreditor, issuer_key=accreditor_key, types="Accreditation",
                 subject={"id": subject_issuer, "publicKey": subject_public_key, "roles": list(roles),
                          "types": types or [], "scopes": scopes or [], "datasets": datasets or [],
                          "delegation_depth": delegation_depth},
                 valid_days=valid_days, now=now)


def task_digest(task: dict[str, Any]) -> str:
    return keymod.sha256_digest(task)


def present(*, holder: str, holder_key, credentials: list[dict[str, Any]], accreditations: list[dict[str, Any]],
            task: dict[str, Any], audience: str, now: datetime | None = None, nonce: str | None = None,
            identity_tokens: list[dict] | None = None) -> dict[str, Any]:
    moment = _now(now)
    document = {
        "type": "Presentation",
        "holder": holder,
        "holderKey": keymod.public_key_text(holder_key),
        "credentials": list(credentials),
        "accreditations": list(accreditations),
        "task": task["@id"],
        "taskDigest": task_digest(task),
        "audience": audience,
        "created": iso(moment),
        "nonce": nonce or str(uuid.uuid4()),
    }
    if identity_tokens:
        document["identity_tokens"] = identity_tokens
    return keymod.sign(document, holder_key, f"{holder}#holder-key", created=iso(moment))


@dataclass
class CredentialCheck:
    id: str
    types: tuple[str, ...]
    issuer: str
    holder: str | None
    dataset: str | None
    scope: str | None
    verified: bool
    status: str
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "types": list(self.types), "issuer": self.issuer, "holder": self.holder,
                "dataset": self.dataset, "scope": self.scope, "verified": self.verified, "status": self.status,
                "problems": list(self.problems)}


@dataclass
class PresentationResult:
    ok: bool
    holder: str | None
    audience: str | None
    problems: list[str]
    credentials: list[CredentialCheck]
    accreditation_edges: list[tuple[str, str]]
    key_sources: dict[str, str]

    @property
    def verified_credentials(self) -> list[CredentialCheck]:
        return [check for check in self.credentials if check.verified]

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "holder": self.holder, "audience": self.audience, "problems": list(self.problems),
                "credentials": [check.as_dict() for check in self.credentials],
                "accreditation_edges": [list(edge) for edge in self.accreditation_edges],
                "key_sources": dict(self.key_sources)}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _types(document: dict[str, Any]) -> tuple[str, ...]:
    value = document.get("type")
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def _time_problems(document: dict[str, Any], moment: datetime) -> list[str]:
    problems = []
    valid_from = parse_iso(document.get("validFrom"))
    valid_until = parse_iso(document.get("validUntil"))
    if valid_from is None or valid_until is None:
        return ["validity period missing or malformed"]
    if moment < valid_from:
        problems.append(f"not yet valid (valid from {iso(valid_from)})")
    if moment >= valid_until:
        problems.append(f"expired at {iso(valid_until)}")
    return problems


def _status(document: dict[str, Any], status_checker: Callable[[dict], str] | None, revocation: str) -> tuple[str, list[str]]:
    if status_checker is None:
        return "unchecked", []
    try:
        status = status_checker(document)
    except Exception:  # noqa: BLE001 - a broken checker is an unknown status, never a crash
        status = "unknown"
    if status not in {"active", "revoked", "unknown"}:
        status = "unknown"
    if status == "revoked":
        return status, ["revoked"]
    if status == "unknown" and revocation == "required":
        return status, ["status unknown (registrar unreachable)"]
    return status, []


def _public(text: Any) -> Ed25519PublicKey | None:
    try:
        return keymod.parse_public(text)
    except keymod.AuthorityKeyError:
        return None


def verify_presentation(presentation: Any, task: Any, *, audience: str, anchors: dict[str, str],
                        directory: dict[str, str] | None = None, status_checker: Callable[[dict], str] | None = None,
                        revocation: str = "required", now: datetime | None = None, max_depth: int = 3,
                        max_age_seconds: int = 900) -> PresentationResult:
    moment = _now(now)
    problems: list[str] = []
    if not isinstance(presentation, dict):
        return PresentationResult(False, None, None, ["presentation is not a JSON object"], [], [], {})
    holder = _text(presentation.get("holder"))
    holder_key_text = _text(presentation.get("holderKey"))
    presented_audience = _text(presentation.get("audience"))

    # a. presentation-level checks
    if presentation.get("type") != "Presentation":
        problems.append("not a Presentation")
    if holder is None:
        problems.append("presentation has no holder")
    holder_key = _public(holder_key_text)
    if holder_key is None:
        problems.append("presentation holder key missing or malformed")
    elif not keymod.verify(presentation, holder_key):
        problems.append("presentation signature invalid")
    task_id = task.get("@id") if isinstance(task, dict) else None
    if not isinstance(task, dict) or presentation.get("task") != task_id:
        problems.append("presentation is bound to another task")
    else:
        try:
            if presentation.get("taskDigest") != task_digest(task):
                problems.append("task digest mismatch")
        except (TypeError, ValueError):
            problems.append("task digest mismatch")
    if presented_audience != audience:
        problems.append("audience mismatch")
    created = parse_iso(presentation.get("created"))
    if created is None:
        problems.append("presentation creation time missing or malformed")
    else:
        if (moment - created).total_seconds() > max_age_seconds:
            problems.append("presentation too old")
        if (created - moment).total_seconds() > 60:
            problems.append("presentation created in the future")
    presentation_ok = not problems

    # b. key resolution
    keys: dict[str, str] = {}
    sources: dict[str, str] = {}
    conflicted: set[str] = set()
    for issuer, key in (anchors or {}).items():
        if _public(key) is None:
            problems.append(f"anchor key malformed for {issuer}")
            continue
        keys[issuer], sources[issuer] = key, "anchor"
    for issuer, key in (directory or {}).items():
        if not isinstance(issuer, str) or _public(key) is None:
            continue
        if issuer in keys:
            if keys[issuer] != key:
                problems.append(f"key conflict for {issuer}")
                conflicted.add(issuer)
            continue
        keys[issuer], sources[issuer] = key, "directory"

    edges: list[tuple[str, str]] = []
    accreditations = presentation.get("accreditations")
    accreditations = [item for item in accreditations if isinstance(item, dict)] if isinstance(accreditations, list) else []
    done: set[int] = set()
    for _round in range(max(0, max_depth)):
        progressed = False
        for index, document in enumerate(accreditations):
            if index in done:
                continue
            accreditor = _text(document.get("issuer"))
            subject = document.get("credentialSubject") if isinstance(document.get("credentialSubject"), dict) else {}
            subject_issuer = _text(subject.get("id"))
            subject_key = _text(subject.get("publicKey"))
            if accreditor is None or accreditor not in keys or accreditor in conflicted:
                continue
            done.add(index)
            progressed = True
            public = _public(keys[accreditor])
            if public is None or not keymod.verify(document, public):
                problems.append(f"accreditation {document.get('id')} signature invalid")
                continue
            if "Accreditation" not in _types(document) or subject_issuer is None or _public(subject_key) is None:
                problems.append(f"accreditation {document.get('id')} malformed")
                continue
            failures = _time_problems(document, moment)
            status, status_problems = _status(document, status_checker, revocation) if status_checker else ("unchecked", [])
            failures.extend(status_problems)
            if failures:
                problems.append(f"accreditation {document.get('id')}: {'; '.join(failures)}")
                continue
            if subject_issuer in keys and keys[subject_issuer] != subject_key:
                problems.append(f"key conflict for {subject_issuer}")
                conflicted.add(subject_issuer)
                continue
            if subject_issuer not in keys or sources.get(subject_issuer) == "directory":
                if subject_issuer not in keys:
                    keys[subject_issuer] = subject_key
                sources[subject_issuer] = "accreditation" if sources.get(subject_issuer) != "anchor" else "anchor"
            edges.append((subject_issuer, accreditor))
        if not progressed:
            break

    # c. credentials
    checks: list[CredentialCheck] = []
    credentials = presentation.get("credentials")
    for document in credentials if isinstance(credentials, list) else []:
        if not isinstance(document, dict):
            checks.append(CredentialCheck("", (), "", None, None, None, False, "unchecked", ["credential is not a JSON object"]))
            continue
        subject = document.get("credentialSubject") if isinstance(document.get("credentialSubject"), dict) else {}
        issuer = _text(document.get("issuer")) or ""
        types = _types(document)
        check = CredentialCheck(
            id=_text(document.get("id")) or "", types=types, issuer=issuer, holder=_text(subject.get("id")),
            dataset=_text(subject.get("dataset")), scope=_text(subject.get("scope")), verified=False, status="unchecked",
        )
        if not presentation_ok:
            check.problems.append("presentation invalid")
            checks.append(check)
            continue
        if issuer in conflicted:
            check.problems.append(f"key conflict for {issuer}")
        elif issuer not in keys:
            check.problems.append("issuer key unknown")
        else:
            public = _public(keys[issuer])
            if public is None or not keymod.verify(document, public):
                check.problems.append("signature invalid")
        if "VerifiableCredential" not in types or not any(kind in types for kind in HOLDER_TYPES):
            check.problems.append("not a holder credential type")
        check.problems.extend(_time_problems(document, moment))
        if check.holder != holder:
            check.problems.append("holder mismatch")
        if subject.get("holderKey") != holder_key_text:
            check.problems.append("holder key mismatch")
        check.status, status_problems = _status(document, status_checker, revocation)
        check.problems.extend(status_problems)
        check.verified = not check.problems
        checks.append(check)

    ok = presentation_ok and any(check.verified for check in checks)
    return PresentationResult(ok, holder, presented_audience, problems, checks, edges, sources)
