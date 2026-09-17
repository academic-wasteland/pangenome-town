"""Plan, validate, execute, and package one compute job for a town.

The runner never trusts a workflow it is handed: a spec passed in by an agent is re-validated against the
template library and the chosen site before anything is rendered, and a spec it plans itself goes through
the same validator. Only outputs the template marks `release = true` leave the work area; intermediates
(for example individual genotypes that feed an aggregate) are deleted after postprocessing.
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import hmac
import json
import shutil
import time
import uuid
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Any

import httpx
from research_commons.contracts import ContractManifest
from research_commons.km import KMRunner, Reasoner
from research_commons.semantic import SemanticValidator

from ..config import TownConfig
from ..exchange import sha256_file
from ..tools.graph import Call, Region, _version
from . import ComputeError, SemanticPolicyError
from .sites import RenderedJob, Site, Step, driver_for, load_sites, reachable
from .workflows import TEMPLATES, plan, render, validate


class ComputeState(str, Enum):
    QUEUED = "QUEUED"
    INITIALIZING = "INITIALIZING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETE = "COMPLETE"
    SYSTEM_ERROR = "SYSTEM_ERROR"
    CANCELED = "CANCELED"
    CANCELING = "CANCELING"
    PREEMPTED = "PREEMPTED"
    EXECUTOR_ERROR = "EXECUTOR_ERROR"
    UNKNOWN = "UNKNOWN"


TES_STATE_MAPPING: dict[str, ComputeState] = {
    "QUEUED": ComputeState.QUEUED,
    "INITIALIZING": ComputeState.INITIALIZING,
    "RUNNING": ComputeState.RUNNING,
    "PAUSED": ComputeState.PAUSED,
    "COMPLETE": ComputeState.COMPLETE,
    "SYSTEM_ERROR": ComputeState.SYSTEM_ERROR,
    "CANCELED": ComputeState.CANCELED,
    "CANCELLED": ComputeState.CANCELED,
    "CANCELING": ComputeState.CANCELING,
    "PREEMPTED": ComputeState.PREEMPTED,
    "EXECUTOR_ERROR": ComputeState.EXECUTOR_ERROR,
    "UNKNOWN": ComputeState.UNKNOWN,
}


class SemanticGate:
    """ADR 0001 Semantic Gate: Gates execution dispatch via SROIQ reasoning.

    The protocol defines a strict five-valued semantic validation result:
    `entailed`, `contradicted`, `unknown`, `invalid`, or `indeterminate`.
    Only `entailed` under a locally trusted manifest satisfies an execution gate.
    Open-world absence (`unknown`) is explicitly never permission.
    """

    def __init__(
        self,
        manifest: ContractManifest | Path | str | None = None,
        reasoner: Reasoner | None = None,
    ) -> None:
        if isinstance(manifest, (str, Path)):
            manifest = ContractManifest.load(Path(manifest))
        self.manifest = manifest
        self.reasoner = reasoner

    def evaluate(
        self,
        rcp_task: dict[str, Any],
        manifest: ContractManifest | Path | str | None = None,
        reasoner: Reasoner | None = None,
    ) -> str:
        target_manifest = manifest or self.manifest
        if isinstance(target_manifest, (str, Path)):
            target_manifest = ContractManifest.load(Path(target_manifest))
        target_reasoner = reasoner or self.reasoner
        if target_manifest is None:
            raise SemanticPolicyError(
                "Semantic gating cannot evaluate without a trusted contract manifest"
            )

        # Bind rcp_task contract declarations against trusted manifest
        task_contract = rcp_task.get("semanticContract")
        if task_contract != target_manifest.id:
            raise SemanticPolicyError(
                f"Semantic gating rejected task: semanticContract '{task_contract}' does not match trusted manifest id '{target_manifest.id}'"
            )
        task_profile = rcp_task.get("ontologyProfile")
        if task_profile != target_manifest.bundle_digest:
            raise SemanticPolicyError(
                f"Semantic gating rejected task: ontologyProfile '{task_profile}' does not match trusted manifest bundleDigest '{target_manifest.bundle_digest}'"
            )

        if callable(target_reasoner) and not hasattr(target_reasoner, "validate") and not hasattr(target_reasoner, "evaluate_gate") and not hasattr(target_reasoner, "classify"):
            result = target_reasoner(rcp_task)
            if isinstance(result, dict) and "status" in result:
                status = result["status"]
            else:
                raise SemanticPolicyError("Semantic gating requires a structured reasoning validation report with a 'status' field")
        elif target_reasoner is not None and hasattr(target_reasoner, "evaluate_gate"):
            result = target_reasoner.evaluate_gate(rcp_task, manifest=target_manifest)
            if isinstance(result, str):
                status = result
            elif isinstance(result, dict) and "status" in result:
                status = result["status"]
            else:
                status = str(result)
        elif target_reasoner is not None and hasattr(target_reasoner, "validate"):
            result = target_reasoner.validate(rcp_task)
            status = result.get("status") if isinstance(result, dict) else str(result)
        elif target_reasoner is not None and hasattr(target_reasoner, "classify"):
            report = SemanticValidator(target_manifest, target_reasoner).validate(rcp_task)
            status = report.get("status", "unknown")
        else:
            try:
                import shutil
                km_exec = shutil.which("km")
                timeout = target_manifest.timeout_seconds if hasattr(target_manifest, "timeout_seconds") else 60
                active_reasoner = target_reasoner or (KMRunner(km_exec, timeout_seconds=timeout) if km_exec else None)
                if active_reasoner is None:
                    raise SemanticPolicyError("Semantic gating failed: no reasoner available (km not installed)")
                validator = SemanticValidator(target_manifest, active_reasoner)
                report = validator.validate(rcp_task)
                status = report.get("status", "unknown")
            except SemanticPolicyError:
                raise
            except Exception as exc:
                raise SemanticPolicyError(f"Semantic gating execution error: {exc}") from exc

        if status != "entailed":
            raise SemanticPolicyError(
                f"Semantic gating rejected execution: status is '{status}' (must be strictly 'entailed')"
            )
        return status

    def __call__(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        import inspect

        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            bound = None
            try:
                bound = sig.bind_partial(*args, **kwargs)
                bound.apply_defaults()
            except TypeError:
                pass

            rcp_task = None
            if bound:
                for param_name in ("rcp_task", "task", "document", "task_doc"):
                    val = bound.arguments.get(param_name)
                    if isinstance(val, dict) and ("@id" in val or "ResearchTask" in str(val.get("@type", ""))):
                        rcp_task = val
                        break
            if rcp_task is None:
                rcp_task = kwargs.get("rcp_task") or kwargs.get("task") or kwargs.get("document")
            if rcp_task is None:
                for arg in args:
                    if isinstance(arg, dict) and (
                        "@id" in arg or "ResearchTask" in str(arg.get("@type", ""))
                    ):
                        rcp_task = arg
                        break
            if rcp_task is None:
                raise SemanticPolicyError(
                    "Semantic gating rejected execution: no recognizable RCP task provided to gated function"
                )
            self.evaluate(rcp_task)
            return fn(*args, **kwargs)

        return wrapper


class ComputeRunner:
    """Base interface for dispatching and polling compute tasks."""

    async def dispatch(self, tes_task_payload: dict[str, Any]) -> str:
        raise NotImplementedError

    async def poll_status(self, task_id: str) -> str:
        raise NotImplementedError

    async def cancel(self, task_id: str) -> None:
        pass


class TESComputeRunner(ComputeRunner):
    """GA4GH Task Execution Service (TES) v1.1 async compute client."""

    def __init__(
        self,
        endpoint_url: str,
        bearer_token: str,
        gate: SemanticGate | None = None,
        client: httpx.AsyncClient | None = None,
        allow_insecure_http: bool = False,
    ) -> None:
        self.endpoint_url = endpoint_url.rstrip("/")
        self.bearer_token = bearer_token
        self.gate = gate
        self._client = client
        self.allow_insecure_http = allow_insecure_http

        from urllib.parse import urlparse
        parsed = urlparse(self.endpoint_url)
        is_loopback = (parsed.hostname or "") in {"localhost", "127.0.0.1", "::1"}
        if not is_loopback and parsed.scheme != "https" and not allow_insecure_http:
            raise ComputeError(
                f"TES endpoint '{self.endpoint_url}' requires HTTPS unless allow_insecure_http is true"
            )

        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if bearer_token:
            self.headers["Authorization"] = f"Bearer {bearer_token}"

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def dispatch(
        self,
        tes_task_payload: dict[str, Any],
        rcp_task: dict[str, Any] | None = None,
    ) -> str:
        """Sends an HTTP POST to {endpoint_url}/v1/tasks. Returns the TES task id string.

        When a SemanticGate is configured or an rcp_task is provided, validates that the task
        is semantically entailed and that the payload's rcp_digest matches the exact task.
        """
        if self.gate is None:
            raise SemanticPolicyError("TES dispatch requires a SemanticGate")

        if rcp_task is None:
            raise SemanticPolicyError(
                "TES dispatch failed semantic gating: no source RCP task provided"
            )

        from research_commons.schema import StructuralValidationError, validate_message

        try:
            validate_message(rcp_task)
        except StructuralValidationError as err:
            raise SemanticPolicyError(f"TES dispatch rejected task: structural validation failed: {err}") from err

        tags = tes_task_payload.get("tags")
        if not isinstance(tags, dict):
            raise SemanticPolicyError(
                "TES dispatch failed: payload tags must be a dictionary containing 'rcp_digest'"
            )
        expected_digest = tags.get("rcp_digest")
        if not isinstance(expected_digest, str) or not expected_digest:
            raise SemanticPolicyError(
                "TES dispatch failed: payload tags must contain 'rcp_digest' binding it to the task"
            )

        expected_id = tags.get("rcp_id")
        if expected_id is not None and not isinstance(expected_id, str):
            raise SemanticPolicyError("TES dispatch payload rcp_id must be a string")
        if expected_id and expected_id != rcp_task.get("@id"):
            raise SemanticPolicyError(
                f"TES dispatch rcp_id mismatch: payload tags have '{expected_id}', but task @id is '{rcp_task.get('@id')}'"
            )

        from ..exchange import canonical

        canonical_bytes = canonical(rcp_task)
        if isinstance(canonical_bytes, str):
            canonical_bytes = canonical_bytes.encode("utf-8")
        actual_digest = f"sha256:{hashlib.sha256(canonical_bytes).hexdigest()}"
        if not hmac.compare_digest(actual_digest, expected_digest):
            raise SemanticPolicyError(
                f"TES dispatch digest mismatch: payload has {expected_digest}, "
                f"but task computed {actual_digest}"
            )

        # Verify rcp_source_jsonld in tags matches verbatim canonical JSON if present
        source_jsonld = tags.get("rcp_source_jsonld")
        if source_jsonld and source_jsonld != (canonical_bytes.decode("utf-8") if isinstance(canonical_bytes, bytes) else canonical_bytes):
            raise SemanticPolicyError("TES dispatch payload rcp_source_jsonld does not match verbatim canonical task")

        self.gate.evaluate(rcp_task)

        url = f"{self.endpoint_url}/v1/tasks"
        client = await self._get_client()
        try:
            response = await client.post(
                url,
                json=tes_task_payload,
                headers=self.headers,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ComputeError(
                f"TES dispatch failed with HTTP {exc.response.status_code}: {exc.response.text}"
            ) from exc
        except httpx.RequestError as exc:
            raise ComputeError(f"TES dispatch network error: {exc}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise ComputeError("TES dispatch returned invalid JSON") from exc

        if not isinstance(data, dict) or not data.get("id") or not isinstance(data["id"], str):
            raise ComputeError(f"TES dispatch returned invalid response: {data}")
        return data["id"]

    async def poll_status(self, task_id: str) -> str:
        """Sends an HTTP GET to {endpoint_url}/v1/tasks/{task_id}. Returns ComputeState enum value."""
        url = f"{self.endpoint_url}/v1/tasks/{task_id}"
        client = await self._get_client()
        try:
            response = await client.get(
                url,
                headers=self.headers,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return ComputeState.UNKNOWN.value
            raise ComputeError(
                f"TES poll failed with HTTP {exc.response.status_code}: {exc.response.text}"
            ) from exc
        except httpx.RequestError as exc:
            raise ComputeError(f"TES poll network error: {exc}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise ComputeError("TES poll returned invalid JSON") from exc

        raw_state = data.get("state", "UNKNOWN") if isinstance(data, dict) else "UNKNOWN"
        state = TES_STATE_MAPPING.get(raw_state, ComputeState.UNKNOWN)
        return state.value

    async def cancel(self, task_id: str) -> None:
        """Sends an HTTP POST to {endpoint_url}/v1/tasks/{task_id}:cancel."""
        url = f"{self.endpoint_url}/v1/tasks/{task_id}:cancel"
        client = await self._get_client()
        try:
            response = await client.post(url, headers=self.headers)
            response.raise_for_status()
        except (httpx.HTTPError, httpx.RequestError):
            return  # Best effort cancellation

AGGREGATE_HEADER = "CHROM\tPOS\tREF\tALT\tallele_count\tallele_number\talt_frequency\n"
# The same aggregation as `aggregate_genotypes`, run on a remote site so individual genotypes never leave it.
AWK_AGGREGATE = (
    'BEGIN{print "CHROM\tPOS\tREF\tALT\tallele_count\tallele_number\talt_frequency"} '
    '/^#/{next} NF>=4{n=0;c=0;for(i=5;i<=NF;i++){k=split($i,a,/[\\/|]/);for(j=1;j<=k;j++){if(a[j]!="."&&a[j]!=""){n++;if(a[j]+0>0)c++}}} '
    'print $1,$2,$3,$4,c,n,(n?sprintf("%.6f",c/n):"NA")}'
)


def aggregate_on_site(job: RenderedJob) -> RenderedJob:
    """Append site-side aggregation and deletion of the genotype table; only the aggregate table is fetched."""
    work = job.work_dir
    steps = [*job.steps,
             Step(id="aggregate", argv=["awk", "-F", "\t", "-v", "OFS=\t", AWK_AGGREGATE, f"{work}/genotypes.tsv"], stdout=f"{work}/allele_frequencies.tsv"),
             Step(id="drop-genotypes", argv=["rm", "-f", f"{work}/genotypes.tsv"], stdout=None)]
    outputs = []
    for output in job.outputs:
        if output["name"] == "genotypes.tsv":
            outputs.append({**output, "stage": "site"})
        elif output.get("stage") == "postprocess":
            outputs.append({**output, "stage": "job"})
        else:
            outputs.append(output)
    return dataclasses.replace(job, steps=steps, outputs=outputs)


def summarize_aggregate(table: Path, sample_count: int) -> dict[str, Any]:
    rows = [line.split("\t") for line in table.read_text(encoding="utf-8").splitlines()[1:] if line.strip()]
    frequencies = [float(row[6]) for row in rows if len(row) > 6 and row[6] != "NA"]
    return {"variant_count": len(rows), "sample_count": sample_count,
            "mean_alt_frequency": round(sum(frequencies) / len(frequencies), 6) if frequencies else None}


def pick_site(town: TownConfig, template_name: str, extra_datasets: tuple[str, ...] = ()) -> tuple[Site | None, list[dict[str, Any]]]:
    """The first enabled, reachable site that holds the template's datasets (plus `extra_datasets`, such as the
    controlled dataset a task uses, so data that resides only at one site is only used there) and tools."""
    template = TEMPLATES.get(template_name)
    if template is None:
        raise ComputeError(f"unknown workflow template {template_name!r}")
    chosen: Site | None = None
    diagnostics: list[dict[str, Any]] = []
    for site in load_sites(town):
        required = (*template.datasets, *extra_datasets)
        holds = all(key in site.datasets for key in required)
        missing_tools = [tool for tool in template.tools if tool not in site.tools]
        if not site.enabled:
            ok, detail = False, "disabled in town.toml"
        elif not holds:
            ok, detail = False, f"does not hold {', '.join(key for key in required if key not in site.datasets)}"
        elif missing_tools:
            ok, detail = False, f"does not allow {', '.join(missing_tools)}"
        else:
            ok, detail = reachable(site)
        diagnostics.append({"site": site.name, "reachable": ok, "detail": detail, "holds": holds})
        if chosen is None and ok:
            chosen = site
    return chosen, diagnostics


def run_task(
    town: TownConfig,
    *,
    template_name: str,
    region: Region | None,
    out_dir: Path,
    site: Site | None = None,
    spec: dict[str, Any] | None = None,
    threads: int = 1,
    driver: Any | None = None,
    extra_datasets: tuple[str, ...] = (),
    dataset_samples: tuple[str, ...] | None = None,
    on_start: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    template = TEMPLATES.get(template_name)
    if template is None:
        raise ComputeError(f"unknown workflow template {template_name!r}")
    if site is None:
        site, diagnostics = pick_site(town, template_name, extra_datasets)
        if site is None:
            reasons = "; ".join(f"{item['site']}: {item['detail']}" for item in diagnostics) or "no sites declared"
            raise ComputeError(f"no reachable site holds {', '.join((*template.datasets, *extra_datasets))} ({reasons})")
    samples = list(town.samples if dataset_samples is None else dataset_samples)
    if spec is None:
        spec = plan(template_name, region=region, site=site, samples=samples, threads=threads, town=town)
    elif spec.get("workflow") != template_name:
        raise ComputeError(f"workflow spec is for {spec.get('workflow')!r}, not {template_name!r}")
    problems = validate(spec, site, samples=samples)
    if problems:
        raise ComputeError("workflow rejected: " + "; ".join(problems))

    out_dir.mkdir(parents=True, exist_ok=True)
    if site.driver == "local":
        work_dir = str((out_dir / "work").resolve())
        fetch_to = Path(work_dir)
    else:
        work_dir = f"{str(site.workdir).rstrip('/')}/{uuid.uuid4().hex}"
        fetch_to = out_dir / "fetched"
    job = render(spec, site, work_dir=work_dir, samples=samples, reference_path_template=town.reference_path_template)
    remote_aggregate = site.driver != "local" and template.postprocess == "aggregate_genotypes"
    if remote_aggregate:
        job = aggregate_on_site(job)
    driver = driver or driver_for(site)
    started = time.time()
    try:
        if on_start:
            on_start({"site": site.name, "driver": site.driver, "scheduler": site.scheduler})
        result = driver.run(job, fetch_to=fetch_to)
        local: dict[str, Path] = dict(result.outputs)
        summary: dict[str, Any] = {}
        if remote_aggregate:
            summary = summarize_aggregate(local["allele_frequencies.tsv"], len(samples))
        elif template.postprocess == "aggregate_genotypes":
            aggregate = fetch_to / "allele_frequencies.tsv"
            summary = aggregate_genotypes(local["genotypes.tsv"], aggregate)
            local["allele_frequencies.tsv"] = aggregate
        released: list[dict[str, Any]] = []
        for output in job.outputs:
            if not output["release"]:
                continue
            source = local.get(output["name"])
            if source is None or not source.exists():
                raise ComputeError(f"released output {output['name']} was not produced")
            final = out_dir / output["name"]
            if source.resolve() != final.resolve():
                shutil.move(str(source), final)
            released.append({"name": output["name"], "path": str(final), "class": output["class"], "digest": sha256_file(final)})
    finally:
        shutil.rmtree(fetch_to, ignore_errors=True)
        if site.driver == "local":
            shutil.rmtree(work_dir, ignore_errors=True)

    if template_name == "genotype-export":
        summary = summarize_genotypes(Path(released[0]["path"]))
    elif template_name == "deconstruct-region":
        summary = {"variant_count": count_vcf_records(Path(released[0]["path"]))}

    versions = {tool: (_version(tool) if site.driver == "local" else "remote (not recorded)") for tool in template.tools}
    payload: dict[str, Any] = {
        "kind": template_name,
        "task_class": template.task_class,
        "region": spec.get("region"),
        "site": site.name,
        "workflow": spec,
        "outputs": released,
        **summary,
        "provenance": {
            "town": town.name,
            "site": site.as_dict(),
            "inputs": {key: site.paths.get(key) for key in template.datasets},
            "samples": samples,
            "commands": result.log,
            "versions": versions,
            "backend_id": result.backend_id,
            "wall_seconds": round(time.time() - started, 3),
        },
    }
    artifact = out_dir / f"{template_name}.json"
    artifact.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    payload["artifact"] = str(artifact)
    return payload


def _header_samples(line: str) -> list[str]:
    columns = [column.split("]", 1)[-1] for column in line.lstrip("#").strip().split("\t")]
    return [column.removesuffix(":GT") for column in columns[4:]]


def aggregate_genotypes(genotypes: Path, destination: Path) -> dict[str, Any]:
    """Collapse a per-sample genotype table into per-site allele counts. No sample column survives."""
    samples: list[str] = []
    rows: list[str] = []
    frequencies: list[float] = []
    with genotypes.open(encoding="utf-8") as stream:
        for line in stream:
            if line.startswith("#"):
                samples = _header_samples(line)
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 4:
                continue
            alleles = [value for genotype in fields[4:] for value in Call.parse(genotype).alleles if value is not None]
            allele_number = len(alleles)
            allele_count = sum(1 for value in alleles if value > 0)
            frequency = allele_count / allele_number if allele_number else None
            if frequency is not None:
                frequencies.append(frequency)
            rows.append("\t".join([*fields[:4], str(allele_count), str(allele_number), "NA" if frequency is None else f"{frequency:.6f}"]))
    destination.write_text(AGGREGATE_HEADER + "".join(f"{row}\n" for row in rows), encoding="utf-8")
    return {
        "variant_count": len(rows),
        "sample_count": len(samples),
        "mean_alt_frequency": round(sum(frequencies) / len(frequencies), 6) if frequencies else None,
    }


def summarize_genotypes(genotypes: Path) -> dict[str, Any]:
    samples: list[str] = []
    count = 0
    with genotypes.open(encoding="utf-8") as stream:
        for line in stream:
            if line.startswith("#"):
                samples = _header_samples(line)
            elif line.strip():
                count += 1
    return {"variant_count": count, "sample_count": len(samples)}


def count_vcf_records(vcf: Path) -> int:
    with vcf.open(encoding="utf-8", errors="replace") as stream:
        return sum(1 for line in stream if line.strip() and not line.startswith("#"))
