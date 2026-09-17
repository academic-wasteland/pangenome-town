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

import hashlib
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
    tools: tuple[str, ...] = ("vg", "bcftools")
    max_cpus: int = 4
    max_mem_gb: int = 16
    max_wall_seconds: int = 1800
    token: str | None = None
    output_url_prefix: str | None = None
    allow_insecure_http: bool = False

    def iri(self, town_name: str) -> str:
        return f"https://w3id.org/academic-wasteland/{town_name}/sites/{self.name}"

    def as_dict(self) -> dict[str, Any]:
        redacted_output_prefix = None
        if self.output_url_prefix:
            from urllib.parse import urlsplit, urlunsplit
            parsed = urlsplit(self.output_url_prefix)
            # Redact userinfo and query parameters from serialized provenance
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            redacted_output_prefix = urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))

        return {
            "name": self.name, "driver": self.driver, "enabled": self.enabled, "host": self.host, "workdir": self.workdir,
            "scheduler": self.scheduler, "submit_host": self.submit_host, "partition": self.partition, "storage": self.storage,
            "output_url_prefix": redacted_output_prefix,
            "allow_insecure_http": self.allow_insecure_http,
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
        allow_insecure = entry.get("allow_insecure_http")
        if allow_insecure is not None and not isinstance(allow_insecure, bool):
            raise ComputeError(f"site {name}: allow_insecure_http must be true or false")
        if host is not None and driver != "tes" and not HOST_RE.match(host):
            raise ComputeError(f"site {name}: host {host!r} is not a plain SSH alias or hostname")
        if driver == "tes":
            if not host:
                raise ComputeError(f"site {name}: TES driver needs a host URL")
            if not host.startswith(("http://", "https://")):
                raise ComputeError(f"site {name}: TES driver host must start with http:// or https://")
            from urllib.parse import urlparse
            parsed = urlparse(host)
            if parsed.username or parsed.password:
                raise ComputeError(f"site {name}: TES host URL must not contain embedded user credentials; configure token instead")
            if parsed.query or parsed.fragment:
                raise ComputeError(f"site {name}: TES host URL must not contain query parameters or fragments")
            parsed_host = parsed.hostname or ""
            if not parsed_host:
                raise ComputeError(f"site {name}: TES host URL must specify a valid hostname")
            is_loopback = parsed_host in {"localhost", "127.0.0.1", "::1"}
            if not is_loopback and not host.startswith("https://") and allow_insecure is not True:
                raise ComputeError(f"site {name}: remote TES endpoints require HTTPS unless allow_insecure_http is true")
            if not workdir:
                workdir = "/tmp/tes-work"
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
        elif driver == "tes":
            # Preserve raw URLs in paths for TES dataset mapping without resolving to local filesystem
            remote_paths = {str(k): str(v) for k, v in (entry.get("paths") or {}).items()}
            paths = remote_paths
            if workdir is not None:
                workdir = str(workdir)
        else:
            defaults = _default_paths(town)
            for key in datasets:
                if key not in paths and key in defaults:
                    paths[key] = defaults[key]
            paths = {key: str(_local_path(town, value)) for key, value in paths.items()}
            if workdir is not None:
                workdir = str(_local_path(town, workdir))
        token = str(entry["token"]) if entry.get("token") else None
        output_url_prefix = str(entry["output_url_prefix"]) if entry.get("output_url_prefix") else None
        sites.append(Site(
            name=name, driver=driver, enabled=enabled, host=host, workdir=workdir, scheduler=scheduler,
            submit_host=submit_host, partition=partition, storage=entry.get("storage"),
            datasets=datasets, paths=paths, tools=tools,
            max_cpus=_positive_int(entry, "max_cpus", 4, name),
            max_mem_gb=_positive_int(entry, "max_mem_gb", 16, name),
            max_wall_seconds=_positive_int(entry, "max_wall_seconds", 1800, name),
            token=token,
            output_url_prefix=output_url_prefix,
            allow_insecure_http=bool(allow_insecure),
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
    token_hash = hashlib.sha256(site.token.encode("utf-8")).hexdigest()[:16] if site.token else ""
    key = (site.name, site.host, site.submit_host, site.driver, tuple(sorted(site.paths.items())), site.tools, token_hash)
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

        from .runner import TESComputeRunner
        from .tes_schema import build_tes_task

        gate = self.options.get("gate")
        if gate is None:
            raise ComputeError(f"site {self.site.name}: TES driver requires a verified manifest-backed SemanticGate")

        rcp_task = self.options.get("rcp_task")
        if rcp_task is None:
            raise ComputeError(f"site {self.site.name}: TES driver requires an original, verified RCP task document")

        if not self.site.host:
            raise ComputeError(f"site {self.site.name}: TES driver requires an explicit host URL")

        runner = TESComputeRunner(
            endpoint_url=self.site.host,
            bearer_token=self.site.token or "",
            gate=gate,
            allow_insecure_http=bool(self.site.allow_insecure_http or self.options.get("allow_insecure_http")),
        )

        output_prefix = (
            self.site.output_url_prefix
            or self.options.get("output_url_prefix")
        )
        if not output_prefix or not output_prefix.startswith(("http://", "https://", "file://")):
            raise ComputeError(f"site {self.site.name}: TES driver requires an explicit remote HTTP(S) or file:// output storage URL (output_url_prefix); object-store protocols like s3:// must be fetched via HTTP(S) gateways")
        if output_prefix.startswith("http://"):
            from urllib.parse import urlparse
            output_host = urlparse(output_prefix).hostname or ""
            if output_host not in {"localhost", "127.0.0.1", "::1"} and not (
                self.site.allow_insecure_http or self.options.get("allow_insecure_http")
            ):
                raise ComputeError(f"TES output destination '{output_prefix}' requires HTTPS unless allow_insecure_http is true")
        if output_prefix.startswith("file://"):
            local_dest = Path(output_prefix.removeprefix("file://")).resolve()
            allowed_roots = [fetch_to.resolve(), Path("/tmp").resolve()]
            if self.site.workdir:
                allowed_roots.append(Path(self.site.workdir).resolve())
            if not any(local_dest.is_relative_to(root) for root in allowed_roots):
                raise ComputeError(f"file:// output destination {local_dest} is outside permitted directories")

        default_image = "quay.io/biocontainers/bcftools:1.21--h8b25389_0" if job.workflow in {"genotype-export", "allele-frequency"} else "quay.io/vgteam/vg:v1.64.1"
        image = self.options.get("image", default_image)

        # Map inputs from job and datasets to declared container paths
        container_input_dir = "/container/input"
        container_work_dir = "/container/work"
        container_output_dir = "/container/output"

        # Build logical-to-container path mapping and storage map
        dataset_storage_map = {}
        path_replacements = {}
        if job.work_dir:
            path_replacements[job.work_dir] = container_work_dir

        used_input_names: dict[str, str] = {}
        def _allocate_container_input_path(src: str) -> str:
            clean = src.split("?", 1)[0].split("#", 1)[0]
            base_name = Path(clean).name or "input.dat"
            if base_name in used_input_names and used_input_names[base_name] != src:
                import hashlib
                token = hashlib.sha256(src.encode("utf-8")).hexdigest()[:8]
                stem = Path(base_name).stem
                suffix = Path(base_name).suffix
                base_name = f"{stem}_{token}{suffix}"
            used_input_names[base_name] = src
            return f"{container_input_dir}/{base_name}"

        for k, v in self.site.paths.items():
            dataset_storage_map[k] = v
            input_container_path = _allocate_container_input_path(v)
            path_replacements[v] = input_container_path

        if "usesDataset" in rcp_task and isinstance(rcp_task["usesDataset"], list):
            for ds in rcp_task["usesDataset"]:
                if isinstance(ds, dict) and ds.get("@id"):
                    ds_id = ds["@id"]
                    if ds.get("url"):
                        dataset_storage_map[ds_id] = ds["url"]
                        path_replacements[ds["url"]] = _allocate_container_input_path(ds["url"])
                    elif ds_id in self.site.paths:
                        dataset_storage_map[ds_id] = self.site.paths[ds_id]
                    else:
                        # Use the RCP type to choose the logical storage key.
                        types = ds.get("@type", [])
                        types = [types] if isinstance(types, str) else types
                        candidate_key = "vcf" if any(str(value).endswith(("RestrictedDataset", "IndividualGenotypeData")) for value in types) else "graph"
                        if candidate_key in self.site.paths:
                            dataset_storage_map[ds_id] = self.site.paths[candidate_key]
                elif isinstance(ds, str) and ds:
                    ds_id = ds
                    if ds_id in self.site.paths:
                        dataset_storage_map[ds_id] = self.site.paths[ds_id]
                    elif ds.startswith(("http://", "https://", "s3://", "file://", "/")):
                        dataset_storage_map[ds_id] = ds
                        path_replacements[ds] = _allocate_container_input_path(ds)
                    else:
                        candidate_key = "vcf" if any(k in ds.lower() for k in ("vcf", "restricted", "individual")) else "graph"
                        if candidate_key in self.site.paths:
                            dataset_storage_map[ds_id] = self.site.paths[candidate_key]

        # Map steps with stdout handling and translate paths to container mount paths
        # If the job has a single step and no complex output copy logic, use native command vectors directly;
        # otherwise compose discrete shell execution safely.
        commands = [f"mkdir -p {container_work_dir} {container_output_dir}"]
        single_direct_step = None
        fetched = _fetched_outputs(job)

        if len(job.steps) == 1 and not fetched:
            # Single step without intermediate workdir-to-output copies can run natively
            step = job.steps[0]
            remapped_argv = []
            for arg in step.argv:
                remapped_arg = arg
                for src_p, dst_p in path_replacements.items():
                    if remapped_arg == src_p:
                        remapped_arg = dst_p
                    elif remapped_arg.startswith(src_p.rstrip("/") + "/"):
                        remapped_arg = dst_p.rstrip("/") + "/" + remapped_arg[len(src_p.rstrip("/") + "/"):]
                remapped_argv.append(remapped_arg)
            if not step.stdout:
                single_direct_step = remapped_argv

        for step in job.steps:
            remapped_argv = []
            for arg in step.argv:
                remapped_arg = arg
                for src_p, dst_p in path_replacements.items():
                    if remapped_arg == src_p:
                        remapped_arg = dst_p
                    elif remapped_arg.startswith(src_p.rstrip("/") + "/"):
                        remapped_arg = dst_p.rstrip("/") + "/" + remapped_arg[len(src_p.rstrip("/") + "/"):]
                remapped_argv.append(remapped_arg)
            cmd = " ".join(shlex.quote(arg) for arg in remapped_argv)
            if step.stdout:
                stdout_path = step.stdout
                for src_p, dst_p in path_replacements.items():
                    if stdout_path == src_p:
                        stdout_path = dst_p
                    elif stdout_path.startswith(src_p.rstrip("/") + "/"):
                        stdout_path = dst_p.rstrip("/") + "/" + stdout_path[len(src_p.rstrip("/") + "/"):]
                cmd += f" > {shlex.quote(stdout_path)}"
            commands.append(cmd)

        # Ensure container output directory exists and copy outputs from /container/work to /container/output if needed
        if fetched:
            copy_cmds = [f"mkdir -p {container_output_dir}"]
            for out in fetched:
                out_name = Path(out["name"]).name
                copy_cmds.append(f"if [ -f {container_work_dir}/{shlex.quote(out_name)} ]; then cp {container_work_dir}/{shlex.quote(out_name)} {container_output_dir}/{shlex.quote(out_name)}; fi")
            commands.append(" && ".join(copy_cmds))

        combined_command = single_direct_step if single_direct_step is not None else ["sh", "-c", " && ".join(commands)]

        # Populate TES outputs from job.outputs with unique task namespace
        run_uuid = uuid.uuid4().hex[:12]
        output_dest_prefix = f"{output_prefix.rstrip('/')}/{run_uuid}"

        tes_outputs = []
        for out in fetched:
            out_name = out["name"]
            out_path = f"{container_output_dir}/{out_name}"
            out_url = f"{output_dest_prefix}/{out_name}"
            tes_outputs.append({
                "name": out_name,
                "path": out_path,
                "url": out_url,
            })

        tes_payload = build_tes_task(
            rcp_task,
            executor_image=image,
            command=combined_command,
            output_url_prefix=output_prefix,
            dataset_storage_map=dataset_storage_map,
            outputs=tes_outputs,
            resources=job.resources,
        )

        wall_time = job.resources.get("wall_seconds", self.site.max_wall_seconds)
        max_attempts = max(10, int(wall_time / 1.5))

        async def _run() -> str:
            task_id = await runner.dispatch(tes_payload, rcp_task=rcp_task)
            if hasattr(self, "on_dispatched") and callable(self.on_dispatched):
                self.on_dispatched()
            # Poll with adaptive backoff derived from wall time and consecutive checks
            attempts = 0
            current_delay = 1.0
            unknown_grace_count = 0
            while attempts < max_attempts:
                attempts += 1
                status = await runner.poll_status(task_id)
                if status == "UNKNOWN":
                    # Allow transient UNKNOWN (e.g. initial replica lag) up to 3 times
                    unknown_grace_count += 1
                    if unknown_grace_count > 3:
                        raise ComputeError(f"TES execution failed with state: {status}")
                elif status in {"COMPLETE", "SYSTEM_ERROR", "EXECUTOR_ERROR", "CANCELED", "PREEMPTED"}:
                    if status != "COMPLETE":
                        raise ComputeError(f"TES execution failed with state: {status}")
                    break
                else:
                    unknown_grace_count = 0

                await asyncio.sleep(current_delay)
                # Adaptive backoff: ramp from 1.0s to a maximum of 8.0s
                current_delay = min(8.0, current_delay * 1.5)
            else:
                try:
                    await runner.cancel(task_id)
                except ComputeError:
                    pass
                raise ComputeError(f"TES execution timed out polling task {task_id}")
            return task_id

        started_time = time.monotonic()
        async def _execute_and_close() -> str:
            try:
                return await _run()
            finally:
                await runner.aclose()

        try:
            import concurrent.futures
            # If an event loop is already running in this thread, execute in a separate worker thread
            has_running_loop = False
            try:
                loop = asyncio.get_running_loop()
                has_running_loop = loop.is_running()
            except RuntimeError:
                has_running_loop = False

            if has_running_loop:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    task_id = pool.submit(asyncio.run, _execute_and_close()).result()
            else:
                task_id = asyncio.run(_execute_and_close())
        except Exception as exc:
            if isinstance(exc, ComputeError):
                raise
            raise ComputeError(f"TES execution error: {exc}") from exc
        elapsed_seconds = round(time.monotonic() - started_time, 3)

        # Ensure declared outputs exist in fetch_to
        fetch_to.mkdir(parents=True, exist_ok=True)
        resolved_fetch_to = fetch_to.resolve()
        outputs: dict[str, Path] = {}
        for output in _fetched_outputs(job):
            # Guard against directory traversal attacks via output name
            safe_name = Path(output["name"]).name
            if safe_name != output["name"]:
                raise ComputeError(f"Invalid output name with path components: {output['name']!r}")
            target = (fetch_to / safe_name).resolve()
            if not target.is_relative_to(resolved_fetch_to):
                raise ComputeError(f"Output target path {target} escapes destination directory {fetch_to}")

            if not target.exists():
                src_path = Path(output["path"])
                if self.site.driver == "local" and src_path.exists():
                    shutil.copy2(src_path, target)
                else:
                    # Download remote output URL if available
                    out_url = f"{output_dest_prefix}/{safe_name}"
                    if out_url.startswith("file://"):
                        local_src = Path(out_url.removeprefix("file://")).resolve()
                        # Strict jail: file:// outputs must reside under allowed state/work directories
                        allowed_roots = [fetch_to.resolve(), Path("/tmp").resolve()]
                        if self.site.workdir:
                            allowed_roots.append(Path(self.site.workdir).resolve())
                        if not any(local_src.is_relative_to(root) for root in allowed_roots):
                            raise ComputeError(f"file:// output destination {local_src} is outside permitted directories")
                        if local_src.exists():
                            shutil.copy2(local_src, target)
                        else:
                            raise ComputeError(f"TES output {safe_name} was not produced at {local_src}")
                    elif out_url.startswith(("http://", "https://")):
                        try:
                            from urllib.parse import urlparse

                            import httpx

                            out_parsed = urlparse(out_url)
                            out_loopback = (out_parsed.hostname or "") in {"localhost", "127.0.0.1", "::1"}
                            allow_insecure = bool(self.site.allow_insecure_http or self.options.get("allow_insecure_http"))
                            if not out_loopback and out_parsed.scheme != "https" and not allow_insecure:
                                raise ComputeError(f"TES output download URL '{out_url}' requires HTTPS unless allow_insecure_http is true")

                            headers = {}
                            # Only attach TES bearer token if output destination is same-origin
                            if self.site.token and self.site.host:
                                site_parsed = urlparse(self.site.host)
                                if (site_parsed.scheme, site_parsed.netloc) == (out_parsed.scheme, out_parsed.netloc):
                                    headers["Authorization"] = f"Bearer {self.site.token}"

                            with httpx.Client(timeout=30.0) as client:
                                resp = client.get(out_url, headers=headers)
                                resp.raise_for_status()
                                target.write_bytes(resp.content)
                        except Exception as dl_err:
                            if isinstance(dl_err, ComputeError):
                                raise
                            raise ComputeError(f"Failed to download TES output from {out_url}: {dl_err}") from dl_err
                    else:
                        raise ComputeError(f"TES output {output['name']} was not produced or fetched into {target}")
            outputs[output["name"]] = target

        return JobResult(0, outputs, elapsed_seconds, task_id, [])
