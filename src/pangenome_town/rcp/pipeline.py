"""RCP task pipeline of a town: four gates, then execution and release.

Gates (see docs/resources-and-credentials.md):

1. admissibility: the semantic contract, decided by `km`
2. authority: verified credentials become receiver facts; trust and scope are entailments
3. standing: commons reputation becomes a receiver fact
4. capability: site reachability becomes a receiver fact; limits are enforced by the workflow validator,
   the operating system, and the scheduler

State per task lives in <state_dir>/rcp/tasks/<task-uuid>/ with task.jsonld, presentation.json (if any),
status.json, and contribution.jsonld once produced. Every task is mirrored into the shared exchange log
with its JSON-LD in the `rcp` column so the dashboard shows RCP traffic next to envelope traffic.
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from research_commons.contracts import ContractManifest
from research_commons.km import KMRunner
from research_commons.schema import StructuralValidationError, validate_message
from research_commons.semantic import SemanticValidator

from ..config import TownConfig
from ..exchange import Envelope, ExchangeLog, canonical
from ..tools import graph
from . import PG, authorization, capability, commons, contract, contribution, iri, reputation, town_iri

BASIC_KINDS = {
    f"{PG}GraphSummaryTask": "summary",
    f"{PG}RegionExtractionTask": "subgraph",
    f"{PG}HaplotypePresenceTask": "haplotypes",
    f"{PG}RegionVariantListingTask": "variants",
}
LARGE_KINDS = {
    f"{PG}PopulationComparisonTask": "compare",
}
INLINE_KINDS = {**BASIC_KINDS, **LARGE_KINDS}
CONTROLLED_KINDS = {
    f"{PG}AlleleFrequencyTask": "allele-frequency",
    f"{PG}IndividualGenotypeExportTask": "genotype-export",
}
COMPUTE_KINDS = {f"{PG}WholeGraphDeconstructTask": "deconstruct-region", **CONTROLLED_KINDS}
LARGE_CLASSES = {f"{PG}{name}" for name in contract.LARGE_TASKS}
CONTROLLED_CLASSES = {f"{PG}{name}" for name in contract.CONTROLLED_TASKS}
STATES = ("submitted", "working", "input-required", "completed", "failed", "rejected", "canceled")


class PipelineError(RuntimeError):
    pass


@dataclass
class TaskRecord:
    id: str
    directory: Path
    state: str
    message: str = ""
    report: dict[str, Any] | None = None
    contribution: dict[str, Any] | None = None
    verdict: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    context_id: str | None = None
    ledger: dict[str, Any] | None = None
    gates: dict[str, str] | None = None
    refusal: dict[str, Any] | None = None
    authority: dict[str, Any] | None = None
    sites: list[dict[str, Any]] | None = None

    FIELDS = ("message", "report", "contribution", "verdict", "artifacts", "context_id", "ledger", "gates", "refusal", "authority", "sites")

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "state": self.state, "directory": str(self.directory), **{name: getattr(self, name) for name in self.FIELDS}}

    def save(self) -> None:
        (self.directory / "status.json").write_text(json.dumps(self.as_dict(), indent=2, default=str) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, directory: Path) -> TaskRecord:
        data = json.loads((directory / "status.json").read_text(encoding="utf-8"))
        record = cls(id=data["id"], directory=directory, state=data["state"])
        for name in cls.FIELDS:
            if name in data and data[name] is not None:
                setattr(record, name, data[name])
        record.artifacts = record.artifacts or []
        return record


class Node:
    """One town's RCP node: contract, validator, task store, gates."""

    def __init__(
        self,
        town: TownConfig,
        *,
        reasoner: Any | None = None,
        log: ExchangeLog | None = None,
        inline_basic: bool = True,
        ledger: commons.Commons | None = None,
        directory: dict[str, str] | None = None,
        status_checker: Callable[[dict], str] | None = None,
        reachable_fn: Callable[[Any], tuple[bool, str]] | None = None,
        referral_fetch: Callable[[str], dict[str, Any] | None] | None = None,
        compute_driver: Any | None = None,
    ):
        self.town = town
        self.log = log
        self.inline_basic = inline_basic
        self.contract_dir = town.city_root / "contract"
        manifest_path = self.contract_dir / f"{town.name}.contract.json"
        if not manifest_path.exists():
            raise PipelineError(f"contract not rendered: {manifest_path} (run `pangenome-town rcp render-contract`)")
        self.manifest = ContractManifest.load(manifest_path)
        if reasoner is None:
            km_bin = shutil.which("km")
            reasoner = KMRunner(km_bin or "km", timeout_seconds=self.manifest.timeout_seconds) if km_bin else None
        self.reasoner = reasoner
        self.validator = SemanticValidator(self.manifest, reasoner) if reasoner is not None else None
        self.tasks_dir = town.state_dir / "rcp" / "tasks"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = ledger if ledger is not None else _default_ledger(town)
        self._directory_override = directory
        self._directory_cache: tuple[float, dict[str, str]] | None = None
        self._status_override = status_checker
        self.reachable_fn = reachable_fn
        self.referral_fetch = referral_fetch
        self.compute_driver = compute_driver
        self.dispatch = str((town.extra.get("compute") or {}).get("dispatch", "inline"))
        self._spec_override: dict[str, Any] | None = None

    # Discovery --------------------------------------------------------------------------
    def agent_card(self, base_url: str) -> dict[str, Any]:
        from ..authority.certification import describe
        manifest_url = f"{base_url.rstrip('/')}/v0/contract/{self.town.name}.contract.json"
        try:
            compute = capability.capabilities(self.town)
        except Exception as error:  # noqa: BLE001
            compute = {"error": f"{type(error).__name__}: {error}"}
        return {
            "name": f"{self.town.display} pangenome town",
            "description": f"Holds the {self.town.population} samples of the JaSaPaGe pangenome and answers Research Commons Protocol tasks about them.",
            "url": f"{base_url.rstrip('/')}/a2a",
            "version": "0.2.0",
            "protocolVersion": "0.3.0",
            "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": True,
                              "extensions": [{"uri": "https://w3id.org/research-commons/v0.1/a2a", "required": True,
                                              "params": contract.agent_card_extension(self.manifest, manifest_url)}]},
            "defaultInputModes": ['application/ld+json;profile="https://w3id.org/research-commons/v0.1/task"', authorization.PRESENTATION_PROFILE],
            "defaultOutputModes": ['application/ld+json;profile="https://w3id.org/research-commons/v0.1/contribution"'],
            "skills": [
                {"id": kind, "name": cls.rsplit('/', 1)[1], "description": f"Deterministic {kind} query over the served graph", "tags": ["pangenome", "basic"]}
                for cls, kind in BASIC_KINDS.items()
            ] + [
                {"id": kind, "name": cls.rsplit('/', 1)[1], "description": f"Reputation-gated {kind} analysis over the served graph", "tags": ["pangenome", "large"]}
                for cls, kind in {**LARGE_KINDS, f"{PG}WholeGraphDeconstructTask": "deconstruct-region"}.items()
            ] + [
                {"id": kind, "name": cls.rsplit('/', 1)[1], "description": f"Controlled-access {kind} job: needs a vetted data access authorization and ethics approval covering the task", "tags": ["pangenome", "controlled"]}
                for cls, kind in CONTROLLED_KINDS.items()
            ],
            "town": {"name": self.town.name, "population": self.town.population, "samples": list(self.town.samples), "citation": self.town.citation,
                     "graph": contract.graph_id(self.town)},
            "trust": {
                "anchors": sorted(contract.trust_anchors(self.town)),
                "restricted_datasets": contract.restricted_datasets(self.town),
                "scopes": {scope: value["label"] for scope, value in contract.SCOPES.items()},
                "required_credentials": list(authorization.REQUIRED_TYPES),
                "certification": describe(authorization.settings(self.town)["certification"]),
            },
            "compute": compute,
        }

    # Task lifecycle --------------------------------------------------------------------
    def submit(self, document: Any, *, context_id: str | None = None, requester_hint: str | None = None,
               presentation: dict[str, Any] | None = None) -> TaskRecord:
        task_id = f"urn:uuid:{uuid.uuid4()}"
        directory = self.tasks_dir / task_id.rsplit(":", 1)[1]
        directory.mkdir(parents=True, exist_ok=True)
        record = TaskRecord(id=task_id, directory=directory, state="submitted", context_id=context_id)
        (directory / "task.jsonld").write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if presentation is not None:
            directory.chmod(0o700)
            (directory / "presentation.json").touch(mode=0o600)
            (directory / "presentation.json").chmod(0o600)
            (directory / "presentation.json").write_text(json.dumps(presentation, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        # Publish before validation/execution: an inline query can run for minutes.
        record.save()
        self._mirror(record, document, requester_hint)
        try:
            self._process(record, document, presentation)
        except Exception as error:  # noqa: BLE001
            record.state = "failed"
            record.message = f"{type(error).__name__}: {error}"
        record.save()
        self._mirror(record, document, requester_hint)
        return record

    def get(self, task_id: str) -> TaskRecord | None:
        directory = self.tasks_dir / task_id.rsplit(":", 1)[1] if task_id.startswith("urn:uuid:") else self.tasks_dir / task_id
        if not (directory / "status.json").exists():
            return None
        return TaskRecord.load(directory)

    def cancel(self, task_id: str) -> TaskRecord | None:
        record = self.get(task_id)
        if record and record.state in {"submitted", "working", "input-required"}:
            record.state = "canceled"
            record.save()
        return record

    def resume(self, task_id: str, *, spec: dict[str, Any] | None = None) -> TaskRecord:
        """Run a task the rigger picked up: every gate is checked again before anything executes."""
        record = self.get(task_id)
        if record is None:
            raise PipelineError(f"unknown task {task_id}")
        if record.state != "working":
            raise PipelineError(f"task {task_id} is {record.state}, not working")
        document = json.loads((record.directory / "task.jsonld").read_text(encoding="utf-8"))
        presentation_path = record.directory / "presentation.json"
        presentation = json.loads(presentation_path.read_text(encoding="utf-8")) if presentation_path.exists() else None
        previous = self.dispatch
        self.dispatch, self._spec_override = "inline", spec
        try:
            self._process(record, document, presentation)
        except Exception as error:  # noqa: BLE001
            record.state, record.message = "failed", f"{type(error).__name__}: {error}"
        finally:
            self.dispatch, self._spec_override = previous, None
        record.save()
        self._mirror(record, document, None)
        return record

    def _directory(self) -> dict[str, str] | None:
        if self._directory_override is not None:
            return self._directory_override
        settings = authorization.settings(self.town)
        registrar = settings["registrar"]
        registrars = settings["registrars"]
        if not registrar and not registrars:
            return None
        if self._directory_cache and time.time() - self._directory_cache[0] < 300:
            return self._directory_cache[1]
        from ..authority.registrar import http_directory

        value = {}
        conflicts = set()
        for base in set(registrars.values()) | ({str(registrar)} if registrar else set()):
            for issuer, key in http_directory(base).items():
                if issuer in value and value[issuer] != key:
                    conflicts.add(issuer)
                value[issuer] = key
        for issuer in conflicts:
            value.pop(issuer, None)
        self._directory_cache = (time.time(), value)
        return value

    def _status_checker(self) -> Callable[[dict], str] | None:
        if self._status_override is not None:
            return self._status_override
        settings = authorization.settings(self.town)
        registrar = settings["registrar"]
        registrars = settings["registrars"]
        if not registrar and not registrars:
            return None
        from ..authority import AUTHORITY
        from ..authority.registrar import http_status_checker

        return http_status_checker(({AUTHORITY: str(registrar)} if registrar else {}) | registrars)

    def _process(self, record: TaskRecord, document: Any, presentation: dict[str, Any] | None) -> None:
        if not isinstance(document, dict):
            record.state, record.message = "rejected", "task part is not a JSON object"
            return
        try:
            validate_message(document)
        except StructuralValidationError as error:
            record.state, record.message = "rejected", f"structural validation failed: {error}"
            record.report = {"status": "invalid", "checks": [{"kind": "structure", "status": "invalid", "durationMs": 0, "diagnostic": str(error)}]}
            record.refusal = {"gate": "admissibility", "reason": "malformed-task"}
            return
        if "ResearchTask" not in _types(document):
            record.state, record.message = "rejected", "only ResearchTask messages are accepted here"
            return
        requester = _iri(document.get("requestedBy"))
        principal = _iri(document.get("onBehalfOf")) or requester
        verdict = reputation.judge(self.town, principal or "urn:unknown", ledger=self.ledger)
        record.verdict = verdict.as_dict()
        if self.validator is None:
            record.state = "failed"
            record.message = "no SROIQ checker available (km not installed); semantic validation is indeterminate and nothing executes"
            record.report = {"status": "indeterminate", "checks": [{"kind": "reasoner", "status": "indeterminate", "durationMs": 0, "diagnostic": "km not installed"}]}
            return
        task_class = str(document.get("taskType"))
        controlled = task_class in CONTROLLED_CLASSES
        heavy = controlled or task_class in LARGE_CLASSES
        started = time.time()

        # Gate 3 facts (standing) --------------------------------------------------------
        assertions: list[Any] = verdict.assertions() if requester else []
        if principal and requester and principal != requester:
            assertions.append((verdict.class_iri, requester))
        # Gate 2 facts (authority) -------------------------------------------------------
        facts = authorization.assess(self.town, document, presentation, directory=self._directory() if presentation else None,
                                     status_checker=self._status_checker() if presentation else None)
        record.authority = facts.as_dict() if (controlled or facts.presented) else None
        assertions.extend(facts.axioms)
        probes: list[tuple[str, str]] = list(facts.probes) if controlled else []
        # Gate 4 facts (capability) ------------------------------------------------------
        if heavy:
            site_axioms, sites = capability.site_facts(self.town, reachable_fn=self.reachable_fn)
            assertions.extend(site_axioms)
            record.sites = sites
            probes.append(("no-capability", f"{town_iri(self.town.name)}NoCapabilityTask"))

        # Gate 1 and the decision ----------------------------------------------------------
        report = self.validator.validate(document, receiver_assertions=assertions, probes=probes)
        record.report = report
        status = report["status"]
        checks = {check["kind"]: check["status"] for check in report["checks"]}
        if record.authority is not None:
            view = authorization.credential_checks(facts, checks)
            for entry in record.authority["credentials"]:
                entry["checks"] = view.get(entry["id"])
        gates = {"admissibility": "pass", "authority": "n/a", "standing": "n/a", "capability": "n/a"}
        if controlled:
            gates["authority"] = "pending"
        if task_class in LARGE_CLASSES:
            gates["standing"] = {"reputable": "pass", "unvetted": "pending", "blocked": "fail"}[verdict.standing]
        if heavy:
            gates["capability"] = "fail" if checks.get("no-capability") == "entailed" else "pass"
        if verdict.standing == "blocked":
            gates["standing"] = "fail"
        record.gates = gates

        if status == "invalid":
            failing = next((check for check in report["checks"] if check["status"] in {"invalid", "contradicted"}), {})
            record.state, record.message = "rejected", f"semantic validation: invalid ({failing.get('kind')}: {failing.get('diagnostic', '')[:300]})"
            record.refusal = {"gate": "admissibility", "reason": "semantic-invalid", "check": failing.get("kind")}
            gates["admissibility"] = "fail"
            return
        if status == "indeterminate":
            record.state, record.message = "failed", "semantic validation indeterminate; execution refused"
            gates["admissibility"] = "indeterminate"
            return
        if checks.get("task-prohibition") == "entailed":
            if verdict.standing == "blocked":
                record.refusal = {"gate": "standing", "reason": "blocked-requester"}
            else:
                gates["admissibility"] = "fail"
                record.refusal = {"gate": "admissibility", "reason": "restricted-dataset", "detail": "the task uses a restricted dataset this town does not serve, or uses restricted data outside a controlled-access task"}
            record.state, record.message = "rejected", f"rejected by the contract ({record.refusal['reason']})"
            return
        if controlled and facts.certification and not facts.certification['ok']:
            gates['authority'] = 'pending'
            record.state, record.message = 'input-required', facts.certification['message']
            record.refusal = {'gate': 'authority', 'reason': 'certification-required', **facts.certification}
            return
        if checks.get("task-admissibility") == "entailed":
            if controlled:
                gates["authority"] = "pass"
            self._execute(record, document, task_class, started, facts, checks)
            return
        if checks.get("no-capability") == "entailed":
            template = capability.TASK_TEMPLATES.get(task_class)
            record.refusal = {"gate": "capability", "reason": "no-capability", "sites": record.sites,
                              "referrals": capability.referrals(self.town, template, datasets=_dataset_iris(document), fetch=self.referral_fetch) if template else []}
            record.state = "input-required"
            record.message = "no reachable compute site holds the data for this task here; see referrals for towns that can run it"
            return
        if controlled:
            diagnosis = authorization.diagnose(facts, checks, self.town)
            record.refusal = diagnosis
            if diagnosis["reason"] == "out-of-scope":
                gates["authority"] = "fail"
                record.state, record.message = "rejected", "the presented credentials do not approve this kind of task (outside the approved scope)"
            else:
                record.state = "input-required"
                needs = ", ".join(item["type"] for item in diagnosis["needs"]) or "credentials the contract can vet"
                record.message = f"controlled-access task: needs {needs} covering this task, issued under a trust anchor of this town ({diagnosis['reason']})"
            return
        if checks.get("permission-requirement") == "entailed":
            record.refusal = {"gate": "standing", "reason": "insufficient-standing", "score": verdict.score,
                              "threshold": (self.town.extra.get("rcp") or {}).get("reputation_threshold", 1.0), "detail": verdict.reason}
            record.state, record.message = "input-required", "this analysis needs approval: the requester's standing is not established in the commons"
            return
        if checks.get("task-admissibility") == "contradicted":
            gates["admissibility"] = "fail"
            record.refusal = {"gate": "admissibility", "reason": "not-admissible"}
            record.state, record.message = "rejected", "the contract rules this task out"
            return
        record.refusal = {"gate": "admissibility", "reason": "unknown"}
        record.state, record.message = "input-required", "the contract cannot establish that this task is admissible; clarify the task class, dataset, or region"

    # Execution ---------------------------------------------------------------------------
    def _execute(self, record: TaskRecord, document: dict[str, Any], task_class: str, started: float,
                 facts: authorization.AuthorityFacts, checks: dict[str, str]) -> None:
        kind = INLINE_KINDS.get(task_class)
        if kind and self.inline_basic:
            self._execute_basic(record, document, kind, started)
        elif task_class in COMPUTE_KINDS:
            if self.dispatch == "agent":
                record.state, record.message = "working", "queued for the rigger (compute agent)"
            else:
                self.execute_compute(record, document, COMPUTE_KINDS[task_class], facts=facts, checks=checks, started=started, spec=self._spec_override)
        else:
            record.state = "working"
            record.message = "queued for the townsfolk agent"

    def _execute_basic(self, record: TaskRecord, document: dict[str, Any], kind: str, started: float) -> None:
        region, subject = _region_and_subject(self.town, document)
        tools = graph.GraphTools(self.town)
        out_dir = record.directory / "artifacts"
        self._execution_started(record, kind, "inline")
        result = tools.run(kind, region, out_dir)
        built = contribution.build(self.town, document, result, subject)
        contribution.write(built, record.directory / "contribution.jsonld")
        report = self.validator.validate(built) if self.validator else None
        record.contribution = built
        record.artifacts = [
            {"name": "contribution.jsonld", "path": str(record.directory / "contribution.jsonld"), "mediaType": 'application/ld+json;profile="https://w3id.org/research-commons/v0.1/contribution"'},
            {"name": Path(result["artifact"]).name, "path": result["artifact"], "mediaType": "application/json"},
        ]
        if report and report["status"] != "entailed":
            record.state, record.message = "failed", f"contribution did not conform: {report['status']}"
            record.report = {"task": record.report, "contribution": report}
            return
        record.report = {"task": record.report, "contribution": report}
        record.state = "completed"
        record.message = f"{kind} query completed in {time.time() - started:.1f}s"
        record.ledger = self._record_completion(document, built)

    def execute_compute(self, record: TaskRecord, document: dict[str, Any], template: str, *, facts: authorization.AuthorityFacts | None = None,
                        checks: dict[str, str] | None = None, started: float | None = None, spec: dict[str, Any] | None = None) -> None:
        """Run a compute template for an admitted task, check the release scope, and package the contribution."""
        from ..compute import ComputeError
        from ..compute import runner as compute_runner

        started = started or time.time()
        task_class = str(document.get("taskType"))
        controlled = task_class in CONTROLLED_CLASSES
        region, subject = _region_and_subject(self.town, document)
        if controlled:
            subject = next((dataset["iri"] for dataset in contract.restricted_datasets(self.town)
                            if dataset["iri"] in {_iri(item) for item in document.get("usesDataset") or []}), subject)
        out_dir = record.directory / "artifacts"
        keys = contract.dataset_keys(self.town)
        served_restricted = {item["iri"] for item in contract.restricted_datasets(self.town)}
        extra = tuple(sorted({keys[iri] for iri in _dataset_iris(document) if iri in served_restricted}))
        driver = self.compute_driver
        site, _ = compute_runner.pick_site(self.town, template, extra)
        if site and site.driver == "tes":
            if driver is not None:
                raise ComputeError("Custom driver injection is not permitted for TES execution")
            # Wire ADR 0001 semantic gate & task context into TES driver, reusing admission evidence
            manifest_path = self.town.city_root / "contract" / f"{self.town.name}.contract.json"
            manifest = contract.ContractManifest.load(manifest_path) if manifest_path.exists() else None
            axioms = list(facts.axioms) if facts else []
            probes = list(facts.probes) if (facts and facts.probes) else []
            # Forward complete standing, authority, and reachable site evidence
            requester_iri = _iri(document.get("requestedBy"))
            principal_iri = _iri(document.get("onBehalfOf")) or requester_iri
            if requester_iri:
                verdict_obj = reputation.judge(self.town, principal_iri or "urn:unknown", ledger=self.ledger)
                assertions: list[Any] = verdict_obj.assertions() if requester_iri else []
                if principal_iri and requester_iri and principal_iri != requester_iri:
                    assertions.append((verdict_obj.class_iri, requester_iri))
                axioms.extend(assertions)
            heavy_task = controlled or task_class in LARGE_CLASSES
            if heavy_task:
                site_axioms, _ = capability.site_facts(self.town, reachable_fn=self.reachable_fn)
                axioms.extend(site_axioms)
                probes.append(("no-capability", f"{town_iri(self.town.name)}NoCapabilityTask"))
            gate_reasoner = lambda doc, a=axioms, p=probes: self.validator.validate(doc, receiver_assertions=a, probes=p) if self.validator else {"status": "indeterminate"}
            driver = compute_runner.driver_for(site, gate=compute_runner.SemanticGate(manifest=manifest, reasoner=gate_reasoner), rcp_task=document)
        try:
            result = compute_runner.run_task(self.town, template_name=template, region=region, out_dir=out_dir, spec=spec,
                                             site=site, driver=driver, extra_datasets=extra,
                                             on_start=lambda s: self._execution_started(record, template, "compute", s))
        except ComputeError as error:
            text = str(error)
            if record.gates is not None:
                record.gates["capability"] = "fail"
            if text.startswith("no reachable site"):
                record.state, record.message = "input-required", text
                record.refusal = {"gate": "capability", "reason": "no-capability", "detail": text,
                                  "referrals": capability.referrals(self.town, template, datasets=_dataset_iris(document), fetch=self.referral_fetch)}
            elif text.startswith("workflow rejected"):
                record.state, record.message = "input-required", text
                reason = "exceeds-site-limits" if ("above site" in text or "exceeds" in text or "limit" in text) else "workflow-rejected"
                record.refusal = {"gate": "capability", "reason": reason, "detail": text}
            else:
                record.state, record.message = "failed", f"compute job failed: {text[:500]}"
                record.refusal = {"gate": "capability", "reason": "job-failed", "detail": text[:2000]}
            return
        built = contribution.build(self.town, document, result, subject)
        release = None
        if controlled:
            # Jobs may outlive an approval or a group membership. Re-read evidence and
            # fresh issuer status before any output is made available to the requester.
            path = record.directory / 'presentation.json'
            current_presentation = json.loads(path.read_text()) if path.exists() else None
            fresh = authorization.assess(self.town, document, current_presentation,
                                         directory=self._directory(), status_checker=self._status_checker())
            if fresh.certification and not fresh.certification['ok']:
                self._withhold(record, result, fresh.certification['message'])
                record.authority = fresh.as_dict()
                return
            fresh_report = self.validator.validate(document, receiver_assertions=fresh.axioms, probes=fresh.probes)
            fresh_checks = {c['kind']: c['status'] for c in fresh_report['checks']}
            if fresh_report['status'] in {'invalid', 'indeterminate'}:
                self._withhold(record, result, 'fresh authorization verification failed')
                return
            if authorization.diagnose(fresh, fresh_checks, self.town)['needs']:
                self._withhold(record, result, 'required approval expired, revoked or no longer accepted')
                return
            release = authorization.release_class(fresh, fresh_checks)
            if release is None:
                self._withhold(record, result, "no vetted data access authorization names a release class for this task")
                return
            # The node built this contribution, so it can enumerate every output; each must be an instance of the
            # scope's release class (checked per output individual, no closure axiom needed).
            probes = [(f"release:{item['name']}", release, item["@id"]) for item in built["hasOutput"]]
            report = self.validator.validate(built, probes=probes)
            statuses = {check["kind"]: check["status"] for check in report["checks"] if check["kind"].startswith("release:")}
            if report["status"] != "entailed" or not statuses or any(value != "entailed" for value in statuses.values()):
                record.report = {"task": record.report, "contribution": report}
                self._withhold(record, result, f"outputs are not all instances of {release.rsplit('/', 1)[1]} ({statuses})")
                return
        else:
            report = self.validator.validate(built)
            if report["status"] != "entailed":
                record.report = {"task": record.report, "contribution": report}
                record.state, record.message = "failed", f"contribution did not conform: {report['status']}"
                return
        contribution.write(built, record.directory / "contribution.jsonld")
        record.contribution = built
        record.report = {"task": record.report, "contribution": report}
        record.artifacts = [
            {"name": "contribution.jsonld", "path": str(record.directory / "contribution.jsonld"), "mediaType": 'application/ld+json;profile="https://w3id.org/research-commons/v0.1/contribution"'},
            {"name": Path(result["artifact"]).name, "path": result["artifact"], "mediaType": "application/json"},
            *({"name": item["name"], "path": item["path"], "mediaType": "text/tab-separated-values" if item["name"].endswith(".tsv") else "text/plain", "class": item["class"]}
              for item in result["outputs"]),
        ]
        record.state = "completed"
        record.message = f"{template} job completed on site {result['site']} in {time.time() - started:.1f}s" + (f"; release checked against {release.rsplit('/', 1)[1]}" if release else "")
        record.ledger = self._record_completion(document, built)

    def _withhold(self, record: TaskRecord, result: dict[str, Any], reason: str) -> None:
        for item in result.get("outputs") or []:
            Path(item["path"]).unlink(missing_ok=True)
        record.contribution = None
        record.artifacts = []
        record.state, record.message = "failed", f"contribution withheld: {reason}"
        record.refusal = {"gate": "authority", "reason": "output-exceeds-scope", "detail": reason}
        if record.gates is not None:
            record.gates["authority"] = "fail"

    def _record_completion(self, task: dict[str, Any], built: dict[str, Any]) -> dict[str, Any] | None:
        """Mirror the task and our contribution into the Wasteland commons (best effort)."""
        if self.ledger is None:
            return None
        requester = _iri(task.get("requestedBy")) or "urn:unknown"
        posted_by = commons.handle_for(requester)
        try:
            self.ledger.ensure_rig(self.town.name, display_name=f"{self.town.display} pangenome town", hop_uri=self._card_url(), rig_type="agent")
            wanted = self.ledger.post_task(task, posted_by=posted_by)
            completion = self.ledger.post_completion(built, completed_by=self.town.name, hop_uri=self._card_url())
        except (commons.CommonsError, ValueError, TypeError) as error:
            return {"error": f"{type(error).__name__}: {error}"}
        return {"wanted": wanted, "completion": completion, "completed_by": self.town.name, "posted_by": posted_by}

    def _card_url(self) -> str:
        return f"{self.town.supervisor_url.rstrip('/')}/v0/city/{self.town.name}/svc/envoy/.well-known/agent-card.json"

    def _execution_started(self, record: TaskRecord, operation: str, executor: str,
                           site: dict[str, Any] | None = None) -> None:
        record.state, record.message = "working", f"executing {operation} ({executor})"
        record.save()
        if self.log is not None:
            current = self.log.get(record.id)
            if current:
                rcp = dict(current.get("rcp") or {})
                rcp.update(state="working", message=record.message, gates=record.gates)
                self.log.set_rcp(record.id, rcp)
                self.log.set_status(record.id, "rcp-working")
            self.log.event(self.town.name, "rcp_execution_started", record.id,
                           {"operation": operation, "executor": executor, **(site or {})})

    def _mirror(self, record: TaskRecord, document: Any, requester_hint: str | None) -> None:
        if self.log is None:
            return
        sender = requester_hint if requester_hint and requester_hint.isidentifier() and requester_hint != self.town.name else "external"
        text = f"RCP {record.state}: {record.message}" if record.message else f"RCP {record.state}"
        task_type = document.get("taskType") if isinstance(document, dict) else None
        body = {"text": text, "rcp_task_type": task_type, "rcp_state": record.state, "region": _region_text(document)}
        envelope = Envelope(id=record.id, kind="question", sender=sender, recipient=self.town.name, created=_now(), body=body)
        if self.log.get(record.id) is None:
            self.log.record(envelope, town=self.town.name, direction="received", status=f"rcp-{record.state}")
        else:
            self.log.set_status(record.id, f"rcp-{record.state}")
        self.log.set_rcp(record.id, {"task": document, "report": record.report, "verdict": record.verdict, "state": record.state,
                                     "contribution": record.contribution, "ledger": record.ledger, "gates": record.gates,
                                     "refusal": record.refusal, "authority": record.authority, "sites": record.sites,
                                     "message": record.message, "context_id": record.context_id})
        self.log.event(self.town.name, f"rcp_{record.state.replace('-', '_')}", record.id, {"message": record.message, "refusal": record.refusal})


def _default_ledger(town: TownConfig) -> commons.Commons | None:
    try:
        return commons.Commons.for_town(town)
    except commons.CommonsError:
        return None


def _types(document: dict[str, Any]) -> list[str]:
    value = document.get("@type", [])
    return [value] if isinstance(value, str) else list(value)


def _iri(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("@id")
    return value if isinstance(value, str) else None


def _dataset_iris(document: dict[str, Any]) -> list[str]:
    return [identifier for identifier in (_iri(item) for item in document.get("usesDataset") or []) if identifier]


def _region_and_subject(town: TownConfig, document: dict[str, Any]) -> tuple[graph.Region | None, str]:
    region = None
    subject = contract.graph_iri(town)
    for dataset in document.get("usesDataset") or []:
        identifier = _iri(dataset)
        if identifier and "/regions/" in identifier:
            region = iri.parse_region_iri(identifier).region
            subject = identifier
    return region, subject


def _region_text(document: Any) -> str | None:
    if not isinstance(document, dict):
        return None
    for dataset in document.get("usesDataset") or []:
        identifier = _iri(dataset)
        if identifier and "/regions/" in identifier:
            try:
                return str(iri.parse_region_iri(identifier).region)
            except iri.IriError:
                return None
    return None


def _now() -> str:
    from ..exchange import now_iso

    return now_iso()


def task_document(town: TownConfig, task_class: str, *, requester: str, request_id: str | None = None, region: graph.Region | None = None,
                  on_behalf_of: str | None = None, datasets: list[dict[str, Any]] | None = None, include_graph: bool = True) -> dict[str, Any]:
    """Author a ResearchTask addressed to `town` (used by peers and tests)."""
    from research_commons.constants import CONTEXT_IRI

    manifest_path = town.city_root / "contract" / f"{town.name}.contract.json"
    manifest = ContractManifest.load(manifest_path)
    entities: list[dict[str, Any]] = []
    if include_graph:
        entities.append({"@id": contract.graph_iri(town), "@type": ["PublicDataset", f"{PG}PangenomeGraph"], "name": f"{town.display} served graph"})
    if region is not None:
        entities.append(iri.region_iri(town.name, region).entity())
    entities.extend(datasets or [])
    document: dict[str, Any] = {
        "@context": CONTEXT_IRI,
        "@id": f"urn:uuid:{uuid.uuid4()}",
        "@type": ["ResearchTask", task_class],
        "semanticContract": manifest.id,
        "ontologyProfile": manifest.bundle_digest,
        "partOfRequest": request_id or f"urn:uuid:{uuid.uuid4()}",
        "taskType": task_class,
        "requestedBy": {"@id": requester, "@type": "Agent"},
        "usesDataset": entities,
    }
    if on_behalf_of:
        document["onBehalfOf"] = {"@id": on_behalf_of, "@type": "Person"}
    return document


def canonical_task(document: dict[str, Any]) -> str:
    return canonical(document)
