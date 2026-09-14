"""RCP task pipeline of a town: validate, gate, execute (basic queries inline), package.

State per task lives in <state_dir>/rcp/tasks/<task-uuid>/ with task.jsonld,
report.json, status.json, and contribution.jsonld once produced. Every task is
also mirrored into the shared exchange log with its JSON-LD in the `rcp` column
so the dashboard shows RCP traffic next to envelope traffic.
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
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
from . import PG, contract, contribution, iri, reputation

BASIC_KINDS = {
    f"{PG}GraphSummaryTask": "summary",
    f"{PG}RegionExtractionTask": "subgraph",
    f"{PG}HaplotypePresenceTask": "haplotypes",
    f"{PG}RegionVariantListingTask": "variants",
}
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "state": self.state, "message": self.message, "directory": str(self.directory),
            "report": self.report, "verdict": self.verdict, "artifacts": self.artifacts, "context_id": self.context_id,
            "contribution": self.contribution,
        }

    def save(self) -> None:
        (self.directory / "status.json").write_text(json.dumps(self.as_dict(), indent=2, default=str) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, directory: Path) -> TaskRecord:
        data = json.loads((directory / "status.json").read_text(encoding="utf-8"))
        return cls(
            id=data["id"], directory=directory, state=data["state"], message=data.get("message", ""),
            report=data.get("report"), contribution=data.get("contribution"), verdict=data.get("verdict"),
            artifacts=data.get("artifacts") or [], context_id=data.get("context_id"),
        )


class Node:
    """One town's RCP node: contract, validator, task store."""

    def __init__(self, town: TownConfig, *, reasoner: Any | None = None, log: ExchangeLog | None = None, inline_basic: bool = True):
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

    # Discovery --------------------------------------------------------------------------
    def agent_card(self, base_url: str) -> dict[str, Any]:
        manifest_url = f"{base_url.rstrip('/')}/v0/contract/{self.town.name}.contract.json"
        return {
            "name": f"{self.town.display} pangenome town",
            "description": f"Holds the {self.town.population} samples of the JaSaPaGe pangenome and answers Research Commons Protocol tasks about them.",
            "url": f"{base_url.rstrip('/')}/a2a",
            "version": "0.1.0",
            "protocolVersion": "0.3.0",
            "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": True,
                              "extensions": [{"uri": "https://w3id.org/research-commons/v0.1/a2a", "required": True,
                                              "params": contract.agent_card_extension(self.manifest, manifest_url)}]},
            "defaultInputModes": ['application/ld+json;profile="https://w3id.org/research-commons/v0.1/task"'],
            "defaultOutputModes": ['application/ld+json;profile="https://w3id.org/research-commons/v0.1/contribution"'],
            "skills": [
                {"id": kind, "name": f"{cls.rsplit('/', 1)[1]}", "description": f"Deterministic {kind} query over the served graph", "tags": ["pangenome", "basic"]}
                for cls, kind in BASIC_KINDS.items()
            ] + [{"id": "large", "name": "LargeAnalysisTask", "description": "Reputation-gated analyses (whole-graph deconstruct, read mapping, population comparison)", "tags": ["pangenome", "large"]}],
            "town": {"name": self.town.name, "population": self.town.population, "samples": list(self.town.samples), "citation": self.town.citation},
        }

    # Task lifecycle --------------------------------------------------------------------
    def submit(self, document: Any, *, context_id: str | None = None, requester_hint: str | None = None) -> TaskRecord:
        task_id = f"urn:uuid:{uuid.uuid4()}"
        directory = self.tasks_dir / task_id.rsplit(":", 1)[1]
        directory.mkdir(parents=True, exist_ok=True)
        record = TaskRecord(id=task_id, directory=directory, state="submitted", context_id=context_id)
        (directory / "task.jsonld").write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            self._process(record, document)
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

    def _process(self, record: TaskRecord, document: Any) -> None:
        if not isinstance(document, dict):
            record.state, record.message = "rejected", "task part is not a JSON object"
            return
        try:
            validate_message(document)
        except StructuralValidationError as error:
            record.state, record.message = "rejected", f"structural validation failed: {error}"
            record.report = {"status": "invalid", "checks": [{"kind": "structure", "status": "invalid", "durationMs": 0, "diagnostic": str(error)}]}
            return
        if "ResearchTask" not in _types(document):
            record.state, record.message = "rejected", "only ResearchTask messages are accepted here"
            return
        requester = _iri(document.get("requestedBy"))
        principal = _iri(document.get("onBehalfOf")) or requester
        verdict = reputation.judge(self.town, principal or "urn:unknown")
        record.verdict = verdict.as_dict()
        if self.validator is None:
            record.state = "failed"
            record.message = "no SROIQ checker available (km not installed); semantic validation is indeterminate and nothing executes"
            record.report = {"status": "indeterminate", "checks": [{"kind": "reasoner", "status": "indeterminate", "durationMs": 0, "diagnostic": "km not installed"}]}
            return
        started = time.time()
        assertions = verdict.assertions() if requester else []
        if principal and requester and principal != requester:
            assertions.append((verdict.class_iri, requester))
        report = self.validator.validate(document, receiver_assertions=assertions)
        record.report = report
        status = report["status"]
        checks = {check["kind"]: check["status"] for check in report["checks"]}
        if status in {"invalid", "contradicted"}:
            record.state, record.message = "rejected", f"semantic validation: {status}"
            return
        if status == "indeterminate":
            record.state, record.message = "failed", "semantic validation indeterminate; execution refused"
            return
        if status == "unknown":
            if checks.get("permission-requirement") == "entailed":
                record.state, record.message = "input-required", "this analysis needs approval: the requester's standing is not established in the commons"
            else:
                record.state, record.message = "input-required", "the contract cannot establish that this task is admissible; clarify the task class, dataset, or region"
            return
        # entailed: admissible.
        kind = BASIC_KINDS.get(str(document.get("taskType")))
        if kind and self.inline_basic:
            self._execute_basic(record, document, kind, started)
        else:
            record.state = "working"
            record.message = "queued for the townsfolk agent"

    def _execute_basic(self, record: TaskRecord, document: dict[str, Any], kind: str, started: float) -> None:
        region = None
        subject = contract.graph_iri(self.town)
        for dataset in document.get("usesDataset") or []:
            identifier = _iri(dataset)
            if identifier and "/regions/" in identifier:
                region = iri.parse_region_iri(identifier).region
                subject = identifier
        tools = graph.GraphTools(self.town)
        out_dir = record.directory / "artifacts"
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

    def _mirror(self, record: TaskRecord, document: Any, requester_hint: str | None) -> None:
        if self.log is None:
            return
        sender = requester_hint if requester_hint and requester_hint.isidentifier() and requester_hint != self.town.name else "external"
        text = f"RCP {record.state}: {record.message}" if record.message else f"RCP {record.state}"
        task_type = document.get("taskType") if isinstance(document, dict) else None
        body = {"text": text, "rcp_task_type": task_type, "rcp_state": record.state, "region": _region_text(document)}
        envelope = Envelope(id=record.id, kind="question", sender=sender, recipient=self.town.name, created=_now(), body=body)
        self.log.record(envelope, town=self.town.name, direction="received", status=f"rcp-{record.state}")
        self.log.set_rcp(record.id, {"task": document, "report": record.report, "verdict": record.verdict, "state": record.state, "contribution": record.contribution})
        self.log.event(self.town.name, f"rcp_{record.state.replace('-', '_')}", record.id, {"message": record.message})


def _types(document: dict[str, Any]) -> list[str]:
    value = document.get("@type", [])
    return [value] if isinstance(value, str) else list(value)


def _iri(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("@id")
    return value if isinstance(value, str) else None


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


def task_document(town: TownConfig, task_class: str, *, requester: str, request_id: str | None = None, region: graph.Region | None = None, on_behalf_of: str | None = None) -> dict[str, Any]:
    """Author a ResearchTask addressed to `town` (used by peers and tests)."""
    from research_commons.constants import CONTEXT_IRI

    manifest_path = town.city_root / "contract" / f"{town.name}.contract.json"
    manifest = ContractManifest.load(manifest_path)
    datasets: list[dict[str, Any]] = [{"@id": contract.graph_iri(town), "@type": ["PublicDataset", f"{PG}PangenomeGraph"], "name": f"{town.display} served graph"}]
    if region is not None:
        datasets.append(iri.region_iri(town.name, region).entity())
    document: dict[str, Any] = {
        "@context": CONTEXT_IRI,
        "@id": f"urn:uuid:{uuid.uuid4()}",
        "@type": ["ResearchTask", task_class],
        "semanticContract": manifest.id,
        "ontologyProfile": manifest.bundle_digest,
        "partOfRequest": request_id or f"urn:uuid:{uuid.uuid4()}",
        "taskType": task_class,
        "requestedBy": {"@id": requester, "@type": "Agent"},
        "usesDataset": datasets,
    }
    if on_behalf_of:
        document["onBehalfOf"] = {"@id": on_behalf_of, "@type": "Person"}
    return document


def canonical_task(document: dict[str, Any]) -> str:
    return canonical(document)
