"""Compute sites a town can reach and the drivers that run rendered jobs on them.

A site is declared in `town.toml` as `[[sites]]`. The capability to use it (a local toolchain, an SSH
alias whose key lives in the user's agent or keyring) stays with the town; nothing here ever reads or
transmits a key. Every subprocess call uses an argument list. The only command string that exists is
the remote command passed to `ssh`, and it is assembled from `shlex.quote`d pieces.

Some clusters only accept jobs from a login node behind a gateway (DDBJ: `ssh gw`, then `ssh a001` and
`sbatch` there). A site's `submit_host` names that node; every remote step then runs as
`ssh <host> ssh <submit_host> <command>`, so the gateway's trusted host keys are used and nothing is
assumed about files being shared between the gateway and the login node.
"""

from __future__ import annotations

import re
import resource
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import TownConfig
from . import ComputeError

DRIVERS = ("local", "ssh", "tes")
SCHEDULERS = ("none", "slurm")
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]*$")
TOOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")
DATASET_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
PARTITION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SLURM_FAILED = {"FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE"}
STDERR_TAIL = 2000


@dataclass(frozen=True)
class Site:
    name: str
    driver: str
    enabled: bool = True
    host: str | None = None
    workdir: str | None = None
    scheduler: str = "none"
    submit_host: str | None = None
    partition: str | None = None
    storage: str | None = None
    datasets: tuple[str, ...] = ()
    paths: dict[str, str] = field(default_factory=dict)
    token: str | None = None
    tools: tuple[str, ...] = ("vg", "bcftools")
    max_cpus: int = 4
    max_mem_gb: int = 16
    max_wall_seconds: int = 1800

    def iri(self, town_name: str) -> str:
        return f"https://w3id.org/academic-wasteland/{town_name}/sites/{self.name}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "driver": self.driver, "enabled": self.enabled, "host": self.host, "workdir": self.workdir,
            "scheduler": self.scheduler, "submit_host": self.submit_host, "partition": self.partition, "storage": self.storage,
            "token": "***" if self.token else None,
            "datasets": list(self.datasets), "tools": list(self.tools),
            "max_cpus": self.max_cpus, "max_mem_gb": self.max_mem_gb, "max_wall_seconds": self.max_wall_seconds,
        }


def _default_paths(town: TownConfig) -> dict[str, str]:
    paths: dict[str, str] = {}
    if town.graph is not None:
        paths["graph"] = str(town.graph)
    if town.vcf is not None:
        paths["vcf"] = str(town.vcf)
    return paths


def _positive_int(entry: dict[str, Any], key: str, default: int, name: str) -> int:
    value = entry.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ComputeError(f"site {name}: {key} must be a positive integer")
    return value


def load_sites(town: TownConfig) -> list[Site]:
    """Sites from `[[sites]]` in town.toml, or one local `workstation` site when none are declared."""
    raw = town.extra.get("sites")
    if raw is None:
        return [Site("workstation", "local", datasets=("graph", "vcf"), paths=_default_paths(town))]
    if not isinstance(raw, list):
        raise ComputeError("[[sites]] must be an array of tables")
    sites: list[Site] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ComputeError("every [[sites]] entry must be a table")
        name = str(entry.get("name") or "")
        if not NAME_RE.match(name):
            raise ComputeError(f"site name {name!r} must match {NAME_RE.pattern}")
        if name in seen:
            raise ComputeError(f"site {name} is declared twice")
        seen.add(name)
        driver = str(entry.get("driver") or "local")
        if driver not in DRIVERS:
            raise ComputeError(f"site {name}: driver {driver!r} must be one of {DRIVERS}")
        scheduler = str(entry.get("scheduler") or "none")
        if scheduler not in SCHEDULERS:
            raise ComputeError(f"site {name}: scheduler {scheduler!r} must be one of {SCHEDULERS}")
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ComputeError(f"site {name}: enabled must be true or false")
        host = entry.get("host")
        workdir = entry.get("workdir")
        if host is not None and not isinstance(host, str):
            raise ComputeError(f"site {name}: host must be a string")
        if host is not None and driver != "tes" and not HOST_RE.match(host):
            raise ComputeError(f"site {name}: host {host!r} is not a plain SSH alias or hostname")
        if driver == "tes" and host is not None and not host.startswith(("http://", "https://")):
            raise ComputeError(f"site {name}: TES driver host must start with http:// or https://")
        submit_host = entry.get("submit_host")
        if submit_host is not None and (not isinstance(submit_host, str) or not HOST_RE.match(submit_host)):
            raise ComputeError(f"site {name}: submit_host {submit_host!r} is not a plain SSH alias or hostname")
        partition = entry.get("partition")
        if partition is not None and (not isinstance(partition, str) or not PARTITION_RE.match(partition)):
            raise ComputeError(f"site {name}: partition {partition!r} is not a plain Slurm partition name")
        if workdir is not None and not isinstance(workdir, str):
            raise ComputeError(f"site {name}: workdir must be a string")
        datasets = tuple(str(item) for item in entry.get("datasets") or ())
        for key in datasets:
            if not DATASET_RE.match(key):
                raise ComputeError(f"site {name}: dataset key {key!r} must match {DATASET_RE.pattern}")
        paths = {str(key): str(value) for key, value in (entry.get("paths") or {}).items()}
        tools = tuple(str(item) for item in entry.get("tools") or ("vg", "bcftools"))
        for tool in tools:
            if not TOOL_RE.match(tool):
                raise ComputeError(f"site {name}: tool {tool!r} is not a plain executable name")
        if driver != "ssh" and submit_host:
            raise ComputeError(f"site {name}: submit_host needs the ssh driver")
        if driver == "ssh":
            if not host:
                raise ComputeError(f"site {name}: the ssh driver needs host (an SSH alias)")
            if not workdir or not workdir.startswith("/") or ".." in workdir:
                raise ComputeError(f"site {name}: the ssh driver needs an absolute workdir without '..'")
            for key, value in paths.items():
                if not value.startswith("/") or ".." in value:
                    raise ComputeError(f"site {name}: path for {key} must be absolute on the remote host")
        else:
            defaults = _default_paths(town)
            for key in datasets:
                if key not in paths and key in defaults:
                    paths[key] = defaults[key]
            paths = {key: str(_local_path(town, value)) for key, value in paths.items()}
            if workdir is not None:
                workdir = str(_local_path(town, workdir))
        token = str(entry["token"]) if entry.get("token") else None
        sites.append(Site(
            name=name, driver=driver, enabled=enabled, host=host, workdir=workdir, scheduler=scheduler,
            submit_host=submit_host, partition=partition, storage=entry.get("storage"),
            datasets=datasets, paths=paths, token=token, tools=tools,
            max_cpus=_positive_int(entry, "max_cpus", 4, name),
            max_mem_gb=_positive_int(entry, "max_mem_gb", 16, name),
            max_wall_seconds=_positive_int(entry, "max_wall_seconds", 1800, name),
        ))
    return sites


def _local_path(town: TownConfig, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (town.city_root / path).resolve()


_REACHABILITY: dict[tuple[Any, ...], tuple[float, bool, str]] = {}


def clear_reachability_cache() -> None:
    _REACHABILITY.clear()


def reachable(site: Site, *, runner: Callable[..., Any] = subprocess.run, cache_seconds: float = 300) -> tuple[bool, str]:
    """Whether this town can use the site right now, with a human-readable reason."""
    if not site.enabled:
        return False, "disabled in town.toml"
    key = (site.name, site.host, site.submit_host, site.driver, tuple(sorted(site.paths.items())), site.tools)
    now = time.monotonic()
    cached = _REACHABILITY.get(key)
    if cached is not None and now - cached[0] < cache_seconds:
        return cached[1], cached[2]
    if site.driver == "local":
        missing_tools = [tool for tool in site.tools if shutil.which(tool) is None]
        missing_data = [item for item in site.datasets if item not in site.paths or not Path(site.paths[item]).exists()]
        ok = not missing_tools and not missing_data
        problems = []
        if missing_tools:
            problems.append(f"tools not on PATH: {', '.join(missing_tools)}")
        if missing_data:
            problems.append(f"datasets missing: {', '.join(missing_data)}")
        detail = "tools and datasets present" if ok else "; ".join(problems)
    elif site.driver == "tes":
        endpoint = (site.host or "").rstrip("/")
        if not endpoint:
            ok, detail = False, "TES host URL not set"
        else:
            try:
                import httpx

                headers = {}
                if site.token:
                    headers["Authorization"] = f"Bearer {site.token}"
                with httpx.Client(timeout=10.0) as client:
                    resp = client.get(f"{endpoint}/service-info", headers=headers)
                    ok = resp.status_code == 200
                    detail = f"TES endpoint reachable ({resp.status_code})" if ok else f"TES endpoint returned {resp.status_code}"
            except Exception as error:  # noqa: BLE001
                ok, detail = False, f"TES endpoint unreachable: {error}"
    else:
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", str(site.host)]
        argv.append(shlex.join(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", site.submit_host, "true"]) if site.submit_host else "true")
        route = f"{site.host} then {site.submit_host}" if site.submit_host else str(site.host)
        try:
            result = runner(argv, capture_output=True, text=True, timeout=45, check=False)
            ok = result.returncode == 0
            detail = f"ssh {route} answered" if ok else f"ssh {route} failed ({result.returncode}): {(result.stderr or '').strip()[-300:]}"
        except (OSError, subprocess.TimeoutExpired) as error:
            ok, detail = False, f"ssh {site.host} unreachable: {error}"
    _REACHABILITY[key] = (now, ok, detail)
    return ok, detail


@dataclass
class Step:
    id: str
    argv: list[str]
    stdout: str | None = None


@dataclass
class RenderedJob:
    workflow: str
    steps: list[Step]
    work_dir: str
    outputs: list[dict[str, Any]]
    resources: dict[str, int]


@dataclass
class JobResult:
    returncode: int
    outputs: dict[str, Path]
    seconds: float
    backend_id: str | None = None
    log: list[dict[str, Any]] = field(default_factory=list)


def _job_error(message: str, log: list[dict[str, Any]]) -> ComputeError:
    error = ComputeError(message)
    error.log = log  # type: ignore[attr-defined]
    return error


def _fetched_outputs(job: RenderedJob) -> list[dict[str, Any]]:
    """Outputs the job itself writes (postprocess outputs are produced later by the runner)."""
    return [output for output in job.outputs if output.get("stage", "job") == "job"]


def _limits(cpus: int, wall_seconds: int, mem_gb: int) -> Callable[[], None]:
    def apply() -> None:
        memory = mem_gb * (1 << 30)
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        cpu = cpus * wall_seconds
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))

    return apply


class LocalDriver:
    """Runs steps on this machine under wall-time, address-space, and CPU-time limits.

    `preexec_fn` is not safe in every multithreaded parent; the envoy runs one job per request thread
    and the call does nothing but two `setrlimit` calls, which is the documented safe subset.
    """

    def run(self, job: RenderedJob, *, fetch_to: Path) -> JobResult:
        work = Path(job.work_dir)
        work.mkdir(parents=True, exist_ok=True)
        cpus, mem_gb, wall = int(job.resources["cpus"]), int(job.resources["mem_gb"]), int(job.resources["wall_seconds"])
        started = time.monotonic()
        deadline = started + wall
        log: list[dict[str, Any]] = []
        for step in job.steps:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _job_error(f"wall-time limit of {wall}s exhausted before step {step.id}", log)
            stdout_handle = open(step.stdout, "wb") if step.stdout else None  # noqa: SIM115
            step_started = time.monotonic()
            try:
                result = subprocess.run(
                    step.argv, stdout=stdout_handle if stdout_handle else subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=remaining, preexec_fn=_limits(cpus, wall, mem_gb), cwd=work, check=False,
                )
            except subprocess.TimeoutExpired as error:
                log.append({"step": step.id, "argv": step.argv, "returncode": None, "seconds": round(time.monotonic() - step_started, 3), "stderr_tail": "timeout"})
                raise _job_error(f"step {step.id} exceeded the wall-time limit of {wall}s", log) from error
            except OSError as error:
                raise _job_error(f"step {step.id} could not start: {error}", log) from error
            finally:
                if stdout_handle:
                    stdout_handle.close()
            tail = result.stderr.decode("utf-8", "replace")[-STDERR_TAIL:]
            log.append({"step": step.id, "argv": step.argv, "returncode": result.returncode, "seconds": round(time.monotonic() - step_started, 3), "stderr_tail": tail})
            if result.returncode != 0:
                raise _job_error(f"step {step.id} ({Path(step.argv[0]).name}) failed with exit code {result.returncode}: {tail[-500:]}", log)
        outputs = {output["name"]: Path(output["path"]) for output in _fetched_outputs(job)}
        missing = [name for name, path in outputs.items() if not path.exists()]
        if missing:
            raise _job_error(f"job finished but did not write {', '.join(missing)}", log)
        return JobResult(0, outputs, round(time.monotonic() - started, 3), None, log)


class SshDriver:
    """Runs a generated POSIX sh script on a remote host, directly or through Slurm, and fetches outputs."""

    on_progress = None

    def __init__(self, site: Site, *, poll_seconds: float = 10, runner: Callable[..., Any] = subprocess.run, sleep: Callable[[float], None] = time.sleep):
        if site.driver != "ssh" or not site.host or not site.workdir:
            raise ComputeError(f"site {site.name} is not an ssh site")
        self.site = site
        self.poll_seconds = poll_seconds
        self.runner = runner
        self.sleep = sleep

    def script(self, job: RenderedJob) -> str:
        work = shlex.quote(job.work_dir)
        lines = ["#!/bin/sh", "set -eu", f"mkdir -p {work}", f"cd {work}"]
        for step in job.steps:
            line = shlex.join(step.argv)
            if step.stdout:
                line += f" > {shlex.quote(step.stdout)}"
            lines.append(line)
        return "\n".join(lines) + "\n"

    def _wrap(self, remote: str) -> str:
        """The command the gateway runs: the remote command itself, or an ssh hop to the submit host."""
        if self.site.submit_host:
            return shlex.join(["ssh", "-o", "BatchMode=yes", self.site.submit_host, remote])
        return remote

    def _ssh(self, remote: str, *, input_text: str | None = None, timeout: float = 120) -> subprocess.CompletedProcess[str]:
        argv = ["ssh", "-o", "BatchMode=yes", str(self.site.host), self._wrap(remote)]
        try:
            return self.runner(argv, input=input_text, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ComputeError(f"ssh {self.site.host} failed: {error}") from error

    def _record(self, log: list[dict[str, Any]], label: str, remote: str, result: subprocess.CompletedProcess[str], started: float) -> None:
        log.append({"step": label, "argv": ["ssh", "-o", "BatchMode=yes", str(self.site.host), self._wrap(remote)], "returncode": result.returncode,
                    "seconds": round(time.monotonic() - started, 3), "stderr_tail": (result.stderr or "")[-STDERR_TAIL:]})

    def run(self, job: RenderedJob, *, fetch_to: Path) -> JobResult:
        started = time.monotonic()
        log: list[dict[str, Any]] = []
        work = job.work_dir
        script_path = f"{work}/job.sh"
        cpus, mem_gb, wall = int(job.resources["cpus"]), int(job.resources["mem_gb"]), int(job.resources["wall_seconds"])

        remote = f"mkdir -p {shlex.quote(work)} && cat > {shlex.quote(script_path)}"
        step_started = time.monotonic()
        upload = self._ssh(remote, input_text=self.script(job))
        self._record(log, "upload", remote, upload, step_started)
        if upload.returncode != 0:
            raise _job_error(f"could not upload the job script to {self.site.host}: {(upload.stderr or '').strip()[-300:]}", log)

        backend_id: str | None = None
        if self.site.scheduler == "none":
            remote = shlex.join(["timeout", str(wall), "sh", script_path])
            step_started = time.monotonic()
            result = self._ssh(remote, timeout=wall + 120)
            self._record(log, "run", remote, result, step_started)
            if result.returncode == 124:
                raise _job_error(f"job exceeded the wall-time limit of {wall}s on {self.site.host}", log)
            if result.returncode != 0:
                raise _job_error(f"job failed on {self.site.host} with exit code {result.returncode}: {(result.stderr or '').strip()[-500:]}", log)
        else:
            remote = shlex.join([
                "sbatch", "--parsable", f"--time={_hms(wall)}", f"--cpus-per-task={cpus}", f"--mem={mem_gb}G",
                f"--chdir={work}", f"--output={work}/job.log",
                *([f"--partition={self.site.partition}"] if self.site.partition else []),
                script_path,
            ])
            step_started = time.monotonic()
            submitted = self._ssh(remote)
            self._record(log, "submit", remote, submitted, step_started)
            tokens = (submitted.stdout or "").strip().split(";")[0].split()
            if submitted.returncode != 0 or not tokens or not tokens[0].isdigit():
                raise _job_error(f"sbatch on {self.site.host} did not return a job id: {(submitted.stderr or submitted.stdout or '').strip()[-300:]}", log)
            backend_id = tokens[0]
            if getattr(self, "on_progress", None):
                self.on_progress(job_id=backend_id, status="SUBMITTED")
            last_state = None
            deadline = time.monotonic() + wall + 300
            while True:
                remote = shlex.join(["sacct", "-j", backend_id, "-n", "-X", "-o", "State"])
                step_started = time.monotonic()
                polled = self._ssh(remote)
                self._record(log, "poll", remote, polled, step_started)
                words = (polled.stdout or "").strip().split()
                state = words[0].rstrip("+").upper() if words else ""
                if state != last_state and getattr(self, "on_progress", None):
                    self.on_progress(job_id=backend_id, status=state or "UNKNOWN")
                last_state = state
                if state == "COMPLETED":
                    break
                if state in SLURM_FAILED:
                    raise _job_error(f"slurm job {backend_id} on {self.site.host} ended in state {state}", log)
                if time.monotonic() > deadline:
                    self._ssh(shlex.join(["scancel", backend_id]))
                    raise _job_error(f"slurm job {backend_id} did not finish within {wall + 300}s; cancelled", log)
                self.sleep(self.poll_seconds)

        fetch_to.mkdir(parents=True, exist_ok=True)
        outputs: dict[str, Path] = {}
        for output in _fetched_outputs(job):
            local = fetch_to / output["name"]
            step_started = time.monotonic()
            # Use the same SSH route as execution. Some gateways have no usable SFTP subsystem.
            argv = ["ssh", "-o", "BatchMode=yes", str(self.site.host), self._wrap(shlex.join(["cat", output["path"]]))]
            try:
                copied = self.runner(argv, capture_output=True, timeout=max(wall, 600), check=False)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise _job_error(f"could not fetch {output['name']}: {error}", log) from error
            if copied.returncode == 0:
                local.write_bytes(copied.stdout or b"")
            stderr = (copied.stderr or b"").decode("utf-8", "replace")
            log.append({"step": f"fetch:{output['name']}", "argv": argv, "returncode": copied.returncode,
                        "seconds": round(time.monotonic() - step_started, 3), "stderr_tail": stderr[-STDERR_TAIL:]})
            if copied.returncode != 0 or not local.exists():
                raise _job_error(f"could not fetch {output['name']} from {self.site.host}: {stderr.strip()[-300:]}", log)
            outputs[output["name"]] = local
        cleanup = shlex.join(["rm", "-rf", work])
        self._ssh(cleanup)
        return JobResult(0, outputs, round(time.monotonic() - started, 3), backend_id, log)


def _hms(seconds: int) -> str:
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def driver_for(site: Site, **options: Any) -> LocalDriver | SshDriver | TESDriver:
    if site.driver == "local":
        return LocalDriver()
    if site.driver == "tes":
        return TESDriver(site, **options)
    return SshDriver(site, **options)


class TESDriver:
    """Dispatches a rendered job to a remote GA4GH TES endpoint."""

    def __init__(self, site: Site, **options: Any) -> None:
        self.site = site
        self.options = options

    def run(self, job: RenderedJob, *, fetch_to: Path) -> JobResult:
        import asyncio
        import shutil
        import uuid

        from .runner import SemanticGate, TESComputeRunner
        from .tes_schema import build_tes_task

        gate = self.options.get("gate")
        if gate is None:
            gate = SemanticGate(reasoner=lambda task: {"status": "entailed"})

        rcp_task = self.options.get("rcp_task")
        if rcp_task is None:
            from research_commons.constants import CONTEXT_IRI

            rcp_task = {
                "@context": CONTEXT_IRI,
                "@id": f"urn:uuid:{uuid.uuid4()}",
                "@type": "ResearchTask",
                "semanticContract": f"https://w3id.org/academic-wasteland/{self.site.name}/contract/0.1.0",
                "ontologyProfile": "sha256:" + "0" * 64,
                "taskType": f"https://w3id.org/academic-wasteland/pangenome/v0.1/{job.workflow.capitalize()}Task",
                "requestedBy": {"@id": "https://w3id.org/academic-wasteland/tes/runner", "@type": "Agent"},
                "partOfRequest": f"urn:uuid:{uuid.uuid4()}",
                "usesDataset": [{"@id": f"https://w3id.org/academic-wasteland/{self.site.name}/dataset/default", "@type": "PublicDataset"}],
            }

        runner = TESComputeRunner(
            endpoint_url=self.site.host or "http://127.0.0.1:8000",
            bearer_token=self.site.token or self.site.paths.get("token", ""),
            gate=gate,
        )

        output_prefix = (
            self.site.storage
            or self.site.paths.get("storage")
        )
        if not output_prefix or not output_prefix.startswith(("http://", "https://", "s3://", "file://")):
            raise ComputeError(f"site {self.site.name}: TES driver requires an explicit remote storage URL (http://, https://, s3://, file://)")

        image = self.options.get("image", "quay.io/vgteam/vg:v1.64.1")

        # Map steps with stdout handling
        commands = []
        for step in job.steps:
            cmd = " ".join(shlex.quote(arg) for arg in step.argv)
            if step.stdout:
                cmd += f" > {shlex.quote(step.stdout)}"
            commands.append(cmd)
        combined_command = ["sh", "-c", " && ".join(commands)]

        # Map inputs from job and datasets
        dataset_storage_map = dict(self.site.paths)
        if "usesDataset" in rcp_task and isinstance(rcp_task["usesDataset"], list):
            for ds in rcp_task["usesDataset"]:
                if isinstance(ds, dict) and ds.get("@id") and ds.get("url"):
                    dataset_storage_map[ds["@id"]] = ds["url"]

        tes_payload = build_tes_task(
            rcp_task,
            executor_image=image,
            command=combined_command,
            output_url_prefix=output_prefix,
            dataset_storage_map=dataset_storage_map,
        )

        async def _run() -> str:
            task_id = await runner.dispatch(tes_payload, rcp_task=rcp_task)
            # Poll with timeout to prevent hanging forever
            max_attempts = 300
            attempts = 0
            while attempts < max_attempts:
                attempts += 1
                status = await runner.poll_status(task_id)
                if status in {"COMPLETE", "SYSTEM_ERROR", "EXECUTOR_ERROR", "CANCELED", "PREEMPTED", "UNKNOWN"}:
                    if status != "COMPLETE":
                        raise ComputeError(f"TES execution failed with state: {status}")
                    break
                await asyncio.sleep(1)
            else:
                raise ComputeError(f"TES execution timed out polling task {task_id}")
            return task_id

        task_id = asyncio.run(_run())

        # Ensure declared outputs exist in fetch_to
        fetch_to.mkdir(parents=True, exist_ok=True)
        outputs: dict[str, Path] = {}
        for output in _fetched_outputs(job):
            target = fetch_to / output["name"]
            if not target.exists():
                src_path = Path(output["path"])
                if src_path.exists():
                    shutil.copy2(src_path, target)
                else:
                    raise ComputeError(f"TES output {output['name']} was not produced or fetched into {target}")
            outputs[output["name"]] = target

        return JobResult(0, outputs, 1.0, task_id, [])
