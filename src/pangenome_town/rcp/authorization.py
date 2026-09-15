"""Gate 2 (authority): turn a presentation of credentials into receiver facts for the reasoner.

Python checks what logic cannot: signatures, holder binding, the task digest, validity windows,
revocation status (see `authority.credentials`), and two identity comparisons (the credential's holder is
the task's requester or principal; its dataset is one the task uses). Only credentials that pass become
facts. The reasoner then decides everything that is policy: whether an issuer sits under one of this
town's trust anchors (accreditation is transitive and chains through `issuedBy`), whether the approved
scope covers the task class, and whether the task is accepted.

Facts produced for a task T and each verified credential c (no nominals: `km` switches to a much slower
route when nominal disjunctions meet the accreditation role chain, measured at more than 90 s against
under 1 s without them):

- `cred:VerifiedCredential(c)`, `cred:<Type>(c)`, `cred:issuedBy(c, issuer)`, `cred:heldBy(c, holder)`,
  `cred:forDataset(c, d)`, and `pg:Approves<Scope>(c)` from the scope library
- `cred:presents(T, c)` when holder and dataset match
- `cred:accreditedBy(issuer, accreditor)` for each verified accreditation edge
- `ObjectAllValuesFrom(cred:presents owl:Nothing)(T)` when no credential matches (so the contract entails
  that the controlled task is uncovered)
- `not ServedRestrictedDataset(d)` for every dataset of T this town does not serve

Probes: `scope:<n>` asks whether T is in the credential's scope class (contradicted when out of scope);
`vetted:<n>` asks whether c itself is a vetted data access authorization or ethics approval.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from research_commons import axioms as ax

from ..authority import CRED
from ..authority import credentials as creds
from ..config import TownConfig
from . import PG, contract, town_iri

PRESENTATION_PROFILE = 'application/json;profile="https://w3id.org/academic-wasteland/credentials/v0.1/presentation"'
HOLDER_TYPES = {"DataAccessAuthorization": "VettedDataAccess", "EthicsApproval": "VettedEthicsApproval"}
REQUIRED_TYPES = ("DataAccessAuthorization", "EthicsApproval")


@dataclass
class CredentialFacts:
    id: str
    label: str
    types: tuple[str, ...]
    issuer: str
    holder: str | None
    dataset: str | None
    scope: str | None
    verified: bool
    matches: bool
    status: str
    problems: list[str]
    probes: dict[str, tuple[str, ...]] = field(default_factory=dict)  # probe kind -> (class, [individual])

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "types": list(self.types), "issuer": self.issuer, "holder": self.holder,
                "dataset": self.dataset, "scope": self.scope, "verified": self.verified, "matches": self.matches,
                "status": self.status, "problems": self.problems, "probes": {kind: list(value) for kind, value in self.probes.items()}}


@dataclass
class AuthorityFacts:
    presented: bool
    ok: bool
    holder: str | None
    problems: list[str]
    credentials: list[CredentialFacts]
    edges: list[tuple[str, str]]
    axioms: list[str]
    probes: list[tuple[str, ...]]
    certification: dict | None = None

    def verified(self) -> list[CredentialFacts]:
        return [item for item in self.credentials if item.verified]

    def as_dict(self) -> dict[str, Any]:
        return {"presented": self.presented, "ok": self.ok, "holder": self.holder, "problems": self.problems,
                "credentials": [item.as_dict() for item in self.credentials],
                "accreditation_edges": [list(edge) for edge in self.edges], "certification": self.certification}


def settings(town: TownConfig) -> dict[str, Any]:
    trust = town.extra.get("trust") or {}
    revocation = str(trust.get("revocation") or "required")
    if revocation not in {"required", "best-effort"}:
        raise ValueError("[trust].revocation must be 'required' or 'best-effort'")
    return {
        "anchors": contract.trust_anchors(town),
        "registrar": trust.get("registrar"),
        "registrars": trust.get("registrars", {}),
        "certification": trust.get("certification"),
        "identity_providers": trust.get("identity_providers", {}),
        "revocation": revocation,
        "max_age_seconds": int(trust.get("presentation_max_age_seconds", 900)),
    }


def _label(credential_id: str) -> str:
    return hashlib.sha256(credential_id.encode("utf-8")).hexdigest()[:12]


def _task_iris(task: dict[str, Any]) -> tuple[str, list[str]]:
    datasets = []
    for item in task.get("usesDataset") or []:
        identifier = item.get("@id") if isinstance(item, dict) else item
        if isinstance(identifier, str):
            datasets.append(identifier)
    return str(task["@id"]), datasets


def assess(
    town: TownConfig,
    task: dict[str, Any],
    presentation: dict[str, Any] | None,
    *,
    directory: dict[str, str] | None = None,
    status_checker: Callable[[dict], str] | None = None,
    now: datetime | None = None,
) -> AuthorityFacts:
    config = settings(town)
    task_id, datasets = _task_iris(task)
    served = {item["iri"] for item in contract.restricted_datasets(town)}
    town_base = town_iri(town.name)
    axioms_out: list[str] = []
    for dataset in datasets:
        if dataset not in served:
            axioms_out.append(ax.class_assertion(ax.complement(f"{town_base}ServedRestrictedDataset"), dataset))
    nothing_presented = ax.class_assertion(ax.only(f"{CRED}presents", ax.NOTHING), task_id)

    certification_result = None
    if config["certification"] is not None:
        from ..authority import certification, identity
        certification_result = certification.evaluate(
            config["certification"], task, presentation, audience=town_iri(town.name),
            anchors=config["anchors"], directory=directory, status_checker=status_checker, now=now,
            identity_checker=identity.checker(config["identity_providers"]))
    if not presentation:
        axioms_out.append(nothing_presented)
        return AuthorityFacts(False, False, None, [], [], [], axioms_out, [], certification_result)

    result = creds.verify_presentation(
        presentation, task, audience=town_iri(town.name), anchors=config["anchors"], directory=directory,
        status_checker=status_checker, revocation=config["revocation"], now=now,
        max_age_seconds=config["max_age_seconds"],
    )
    requesters = {value.get("@id") if isinstance(value, dict) else value for value in (task.get("requestedBy"), task.get("onBehalfOf"))}
    facts: list[CredentialFacts] = []
    probes: list[tuple[str, ...]] = []
    presented_any = False
    for check in result.credentials:
        label = _label(check.id)
        problems = list(check.problems)
        holder_types = [name for name in check.types if name in HOLDER_TYPES]
        verified = check.verified
        scope = contract.SCOPES.get(check.scope or "")
        if verified and not check.scope:
            problems.append("credential names no scope")
            verified = False
        elif verified and scope is None:
            problems.append(f"scope {check.scope} is not in this town's scope library")
            verified = False
        matches = False
        if verified:
            matches = True
            if check.holder not in requesters:
                problems.append("holder is neither the requester nor the principal of this task")
                matches = False
            if check.dataset and check.dataset not in datasets:
                problems.append("credential is for a dataset this task does not use")
                matches = False
        item = CredentialFacts(check.id, label, tuple(check.types), check.issuer, check.holder, check.dataset, check.scope,
                               verified, matches, check.status, problems)
        facts.append(item)
        if not verified:
            continue
        if certification_result:
            for decision in certification_result['requirements']:
                if decision['status'] == 'pass' and decision['evidence']['credential'] == check.id:
                    vetted = HOLDER_TYPES.get(decision['type'])
                    if vetted:
                        axioms_out.append(ax.class_assertion(f"{town_base}{vetted}", check.id))
        axioms_out.append(ax.class_assertion(f"{CRED}VerifiedCredential", check.id))
        for name in holder_types:
            axioms_out.append(ax.class_assertion(f"{CRED}{name}", check.id))
        axioms_out.append(ax.property_assertion(f"{CRED}issuedBy", check.id, check.issuer))
        if check.holder:
            axioms_out.append(ax.property_assertion(f"{CRED}heldBy", check.id, check.holder))
        if check.dataset:
            axioms_out.append(ax.property_assertion(f"{CRED}forDataset", check.id, check.dataset))
        axioms_out.append(ax.class_assertion(scope["approves"], check.id))
        if matches:
            presented_any = True
            axioms_out.append(ax.property_assertion(f"{CRED}presents", task_id, check.id))
        item.probes = {f"scope:{label}": (str(check.scope),)}
        for name in holder_types:
            item.probes[f"vetted:{label}"] = (f"{town_base}{HOLDER_TYPES[name]}", check.id)
        probes.extend((kind, *value) for kind, value in item.probes.items())
    for subject, accreditor in result.accreditation_edges:
        axioms_out.append(ax.property_assertion(f"{CRED}accreditedBy", subject, accreditor))
    if not presented_any:
        axioms_out.append(nothing_presented)
    return AuthorityFacts(True, result.ok, result.holder, list(result.problems), facts, list(result.accreditation_edges), axioms_out, probes, certification_result)


def credential_checks(facts: AuthorityFacts, checks: dict[str, str]) -> dict[str, dict[str, str | None]]:
    """Per credential: scope and vetted probe results and the derived coverage (matches and in scope and vetted)."""
    view: dict[str, dict[str, str | None]] = {}
    for item in facts.credentials:
        scope = checks.get(f"scope:{item.label}")
        vetted = checks.get(f"vetted:{item.label}")
        if not item.verified:
            covers = None
        elif item.matches and scope == "entailed" and vetted == "entailed":
            covers = "entailed"
        elif scope == "contradicted" or not item.matches:
            covers = "contradicted"
        else:
            covers = "unknown"
        view[item.id] = {"scope": scope, "vetted": vetted, "covers": covers}
    return view


def diagnose(facts: AuthorityFacts, checks: dict[str, str], town: TownConfig) -> dict[str, Any]:
    """Explain an authority-gate outcome from the probe results (for input-required and rejected refusals)."""
    anchors = sorted(contract.trust_anchors(town))
    served = [item["iri"] for item in contract.restricted_datasets(town)]
    per_credential = []
    covering_types: set[str] = set()
    out_of_scope = []
    view = credential_checks(facts, checks)
    for item in facts.credentials:
        entry = {"id": item.id, "types": list(item.types), "issuer": item.issuer, "scope": item.scope, "problems": list(item.problems)}
        if item.verified:
            results = view[item.id]
            entry.update({"scope_check": results["scope"], "vetted_check": results["vetted"], "covers_check": results["covers"]})
            if results["vetted"] != "entailed":
                entry["problems"].append("issuer is not under one of this town's trust anchors")
            if results["scope"] == "contradicted":
                entry["problems"].append("task is outside the approved scope")
                out_of_scope.append(item.id)
            if results["covers"] == "entailed":
                covering_types.update(name for name in item.types if name in HOLDER_TYPES)
        per_credential.append(entry)
    missing = [name for name in REQUIRED_TYPES if name not in covering_types]
    if not facts.presented:
        reason = "missing-credential"
    elif out_of_scope and not covering_types:
        reason = "out-of-scope"
    elif not facts.ok and facts.problems:
        reason = "presentation-invalid"
    elif missing and not any(item.verified for item in facts.credentials):
        reason = "credential-invalid"
    else:
        reason = "credential-not-accepted"
    return {
        "gate": "authority",
        "reason": reason,
        "needs": [{"type": name, "under_trust_anchors": anchors, "datasets": served, "scopes": sorted(contract.SCOPES)} for name in missing],
        "presentation_problems": facts.problems,
        "credentials": per_credential,
        "out_of_scope": out_of_scope,
    }


def release_class(facts: AuthorityFacts, checks: dict[str, str]) -> str | None:
    """The class every released output must belong to, from the scope of a covering data access authorization."""
    view = credential_checks(facts, checks)
    for item in facts.credentials:
        if "DataAccessAuthorization" in item.types and view.get(item.id, {}).get("covers") == "entailed":
            scope = contract.SCOPES.get(item.scope or "")
            if scope:
                return scope["release"]
    return None


def controlled_dataset_entity(iri: str, name: str | None = None) -> dict[str, Any]:
    entity: dict[str, Any] = {"@id": iri, "@type": ["RestrictedDataset", f"{PG}IndividualGenotypeData"]}
    if name:
        entity["name"] = name
    return entity
