"""Workflow template library, planner, validator, and renderer.

A workflow spec is a JSON document an agent may inspect and propose. It is only ever executed after
`validate` accepts it against the template of the same name and the target site. The spec may change
resources, threads, and region; everything else (tools, arguments, placeholders, output paths and
classes, release flags) must equal the template. Validation happens on placeholder tokens before any
rendering, and every argument ends up in an argv list, never a shell string, except through the ssh
driver, which quotes each token.
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from typing import Any

from ..tools.graph import QueryError, Region
from . import ComputeError
from .sites import RenderedJob, Site, Step

PG = "https://w3id.org/academic-wasteland/pangenome-town/contract/"
AGGREGATE = f"{PG}AggregateArtifact"
INDIVIDUAL = f"{PG}IndividualLevelArtifact"
ARTIFACT_CLASSES = (AGGREGATE, INDIVIDUAL)
PLACEHOLDER_RE = re.compile(r"\{([a-z_]+(?::[a-z0-9_-]+)?)\}")
SIMPLE_PLACEHOLDERS = frozenset({"region", "vg_region", "vg_path", "reference", "samples", "work", "threads"})
FORBIDDEN_CHARACTERS = frozenset(";|&$`<>\n")
SAMPLE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SPEC_VERSION = 1
GENOTYPE_FORMAT = "%CHROM\\t%POS\\t%REF\\t%ALT[\\t%GT]\\n"


@dataclass(frozen=True)
class Template:
    name: str
    task_class: str
    steps: tuple[dict[str, Any], ...]
    outputs: tuple[dict[str, Any], ...]
    datasets: tuple[str, ...]
    tools: tuple[str, ...]
    default_resources: dict[str, int]
    max_span: int
    seconds_per_mb: float
    postprocess: str | None = None
    base_seconds: int = 30
    description: str = ""

    def estimate_seconds(self, region: Region | None) -> int:
        span = region.span if region is not None else self.max_span
        return math.ceil(self.base_seconds + self.seconds_per_mb * span / 1_000_000)


_GENOTYPE_STEPS: tuple[dict[str, Any], ...] = (
    {"id": "subset", "argv": ["bcftools", "view", "-r", "{region}", "-s", "{samples}", "-Ob", "-o", "{work}/subset.bcf", "{data:vcf}"], "stdout": None},
    {"id": "query", "argv": ["bcftools", "query", "-H", "-f", GENOTYPE_FORMAT, "{work}/subset.bcf"], "stdout": "{work}/genotypes.tsv"},
)

TEMPLATES: dict[str, Template] = {
    "genotype-export": Template(
        name="genotype-export",
        task_class=f"{PG}IndividualGenotypeExportTask",
        steps=_GENOTYPE_STEPS,
        outputs=({"name": "genotypes.tsv", "path": "{work}/genotypes.tsv", "class": INDIVIDUAL, "release": True, "stage": "job"},),
        datasets=("vcf",),
        tools=("bcftools",),
        default_resources={"cpus": 1, "mem_gb": 2, "wall_seconds": 600},
        max_span=5_000_000,
        seconds_per_mb=60,
        description="Per-sample genotypes of the town's samples in one region (individual-level output).",
    ),
    "allele-frequency": Template(
        name="allele-frequency",
        task_class=f"{PG}AlleleFrequencyTask",
        steps=_GENOTYPE_STEPS,
        outputs=(
            {"name": "genotypes.tsv", "path": "{work}/genotypes.tsv", "class": INDIVIDUAL, "release": False, "stage": "job"},
            {"name": "allele_frequencies.tsv", "path": "{work}/allele_frequencies.tsv", "class": AGGREGATE, "release": True, "stage": "postprocess"},
        ),
        datasets=("vcf",),
        tools=("bcftools",),
        default_resources={"cpus": 1, "mem_gb": 2, "wall_seconds": 600},
        max_span=5_000_000,
        seconds_per_mb=60,
        postprocess="aggregate_genotypes",
        description="Per-site allele counts over the town's samples; genotypes are an intermediate that never leaves the site.",
    ),
    "deconstruct-region": Template(
        name="deconstruct-region",
        task_class=f"{PG}WholeGraphDeconstructTask",
        steps=(
            # vg 1.68 cannot extract a region with its GBWT haplotypes, so the reference contig (all of its
            # subpath fragments, hence -P and -C) is deconstructed and the region is cut out afterwards.
            {"id": "deconstruct", "argv": ["vg", "deconstruct", "-P", "{vg_path}", "-C", "-a", "-t", "{threads}", "{data:graph}"], "stdout": "{work}/contig.vcf"},
            {"id": "cut", "argv": ["bcftools", "view", "-t", "{region}", "-o", "{work}/deconstruct.vcf", "{work}/contig.vcf"], "stdout": None},
        ),
        outputs=({"name": "deconstruct.vcf", "path": "{work}/deconstruct.vcf", "class": INDIVIDUAL, "release": True, "stage": "job"},),
        datasets=("graph",),
        tools=("vg", "bcftools"),
        # Measured on JaSaPaGe.gbz (3.5 GB, 67 samples): deconstructing even GRCh38#0#chrM held more than
        # 30 GB resident memory and ran longer than 8 minutes, because the whole graph is loaded and decomposed.
        # A site must grant at least this envelope; the default workstation limits refuse it on purpose.
        default_resources={"cpus": 4, "mem_gb": 48, "wall_seconds": 7200},
        max_span=1_000_000,
        seconds_per_mb=120,
        base_seconds=3600,
        description="Graph variants (snarls, all levels) against the reference in one region, with per-haplotype genotypes.",
    ),
}


def plan(template_name: str, *, region: Region | None, site: Site, samples: list[str], threads: int = 1, town: Any = None) -> dict[str, Any]:
    """A spec for this template on this site with default resources widened to the span estimate."""
    template = TEMPLATES.get(template_name)
    if template is None:
        raise ComputeError(f"unknown workflow template {template_name!r}; known: {sorted(TEMPLATES)}")
    resources = dict(template.default_resources)
    resources["cpus"] = max(resources["cpus"], int(threads))
    resources["wall_seconds"] = max(resources["wall_seconds"], template.estimate_seconds(region))
    return {
        "workflow": template.name,
        "version": SPEC_VERSION,
        "site": site.name,
        "region": str(region) if region is not None else None,
        "threads": int(threads),
        "resources": resources,
        "steps": copy.deepcopy(list(template.steps)),
        "outputs": copy.deepcopy(list(template.outputs)),
        "datasets": list(template.datasets),
    }


def _placeholders(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        found.update(PLACEHOLDER_RE.findall(value))
    elif isinstance(value, dict):
        for item in value.values():
            found |= _placeholders(item)
    elif isinstance(value, list | tuple):
        for item in value:
            found |= _placeholders(item)
    return found


def _token_problems(token: str, where: str, template: Template) -> list[str]:
    problems: list[str] = []
    bad = sorted({character for character in token if character in FORBIDDEN_CHARACTERS})
    if bad:
        problems.append(f"{where}: token {token!r} contains forbidden characters {''.join(bad)!r}")
    if token.startswith("/"):
        problems.append(f"{where}: token {token!r} is an absolute path; name data with a {{data:<key>}} placeholder")
    if ".." in token:
        problems.append(f"{where}: token {token!r} contains '..'")
    for name in PLACEHOLDER_RE.findall(token):
        if name.startswith("data:"):
            key = name.split(":", 1)[1]
            if key not in template.datasets:
                problems.append(f"{where}: placeholder {{{name}}} names a dataset the template does not use")
        elif name not in SIMPLE_PLACEHOLDERS:
            problems.append(f"{where}: unknown placeholder {{{name}}}")
    if "{" in PLACEHOLDER_RE.sub("", token) or "}" in PLACEHOLDER_RE.sub("", token):
        problems.append(f"{where}: token {token!r} has a malformed placeholder")
    return problems


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def validate(spec: Any, site: Site, *, samples: list[str]) -> list[str]:
    """Every reason this spec must not run on this site; an empty list means it may run."""
    if not isinstance(spec, dict):
        return ["workflow spec must be a JSON object"]
    template = TEMPLATES.get(spec.get("workflow"))  # type: ignore[arg-type]
    if template is None:
        return [f"unknown workflow {spec.get('workflow')!r}; known: {sorted(TEMPLATES)}"]
    problems: list[str] = []
    if spec.get("version") != SPEC_VERSION:
        problems.append(f"spec version must be {SPEC_VERSION}")
    if spec.get("site") != site.name:
        problems.append(f"spec targets site {spec.get('site')!r} but is being validated for site {site.name!r}")
    if not site.enabled:
        problems.append(f"site {site.name} is disabled")

    steps = spec.get("steps")
    if not isinstance(steps, list) or len(steps) != len(template.steps):
        problems.append(f"steps must be the {len(template.steps)} steps of template {template.name}")
    else:
        for given, expected in zip(steps, template.steps, strict=True):
            where = f"step {expected['id']}"
            argv = given.get("argv") if isinstance(given, dict) else None
            if not isinstance(argv, list) or not argv or not all(isinstance(token, str) for token in argv):
                problems.append(f"{where}: argv must be a non-empty list of strings")
                continue
            if given.get("id") != expected["id"]:
                problems.append(f"{where}: step id {given.get('id')!r} differs from the template")
            if argv[0] != expected["argv"][0]:
                problems.append(f"{where}: tool {argv[0]!r} differs from template tool {expected['argv'][0]!r}")
            if argv[0] not in site.tools:
                problems.append(f"{where}: tool {argv[0]!r} is not allowed on site {site.name}")
            stdout = given.get("stdout")
            if _placeholders([argv, stdout]) != _placeholders([expected["argv"], expected["stdout"]]):
                problems.append(f"{where}: placeholder set differs from the template")
            if argv != expected["argv"]:
                problems.append(f"{where}: arguments differ from the template (only resources, threads, and region may change)")
            for token in argv:
                problems.extend(_token_problems(token, where, template))
            if stdout != expected["stdout"]:
                problems.append(f"{where}: stdout differs from the template")
            if stdout is not None:
                if not isinstance(stdout, str) or not stdout.startswith("{work}/"):
                    problems.append(f"{where}: stdout {stdout!r} must start with {{work}}/")
                else:
                    problems.extend(_token_problems(stdout, where, template))

    outputs = spec.get("outputs")
    if not isinstance(outputs, list) or len(outputs) != len(template.outputs):
        problems.append(f"outputs must be the {len(template.outputs)} outputs of template {template.name}")
    else:
        for given, expected in zip(outputs, template.outputs, strict=True):
            where = f"output {expected['name']}"
            if not isinstance(given, dict):
                problems.append(f"{where}: must be an object")
                continue
            path = given.get("path")
            if not isinstance(path, str) or not path.startswith("{work}/"):
                problems.append(f"{where}: path {path!r} must start with {{work}}/")
            elif _token_problems(path, where, template):
                problems.extend(_token_problems(path, where, template))
            if given.get("class") not in ARTIFACT_CLASSES:
                problems.append(f"{where}: class {given.get('class')!r} is not an artifact class of the contract")
            elif given.get("class") != expected["class"]:
                problems.append(f"{where}: class {given.get('class')!r} differs from the template class {expected['class']!r}")
            for key in ("name", "path", "release", "stage"):
                if given.get(key) != expected.get(key):
                    problems.append(f"{where}: {key} differs from the template")

    for key in template.datasets:
        if key not in site.datasets:
            problems.append(f"dataset {key} does not reside on site {site.name}")
        elif key not in site.paths:
            problems.append(f"site {site.name} has no path for dataset {key}")

    region: Region | None = None
    region_text = spec.get("region")
    if not isinstance(region_text, str) or not region_text:
        problems.append("region is required")
    else:
        try:
            region = Region.parse(region_text, "GRCh38")
        except QueryError as error:
            problems.append(f"region: {error}")
        else:
            if region.span > template.max_span:
                problems.append(f"region span {region.span:,} bp exceeds the {template.name} limit of {template.max_span:,} bp")

    resources = spec.get("resources")
    cpus = mem_gb = wall = None
    if not isinstance(resources, dict):
        problems.append("resources must be an object with cpus, mem_gb, wall_seconds")
    else:
        cpus, mem_gb, wall = _int(resources.get("cpus")), _int(resources.get("mem_gb")), _int(resources.get("wall_seconds"))
        for label, value, limit in (("cpus", cpus, site.max_cpus), ("mem_gb", mem_gb, site.max_mem_gb), ("wall_seconds", wall, site.max_wall_seconds)):
            if value is None or value <= 0:
                problems.append(f"resources.{label} must be a positive integer")
            elif value > limit:
                problems.append(f"resources.{label} = {value} exceeds the site {site.name} limit of {limit}")
        estimate = template.estimate_seconds(region) if region is not None else None
        if estimate is not None and wall is not None and wall > 0 and estimate > wall:
            problems.append(f"estimated wall time {estimate}s exceeds the requested wall_seconds of {wall}s")
        if estimate is not None and estimate > site.max_wall_seconds:
            problems.append(f"estimated wall time {estimate}s exceeds the site {site.name} limit of {site.max_wall_seconds}s")

    threads = _int(spec.get("threads"))
    if threads is None or threads <= 0:
        problems.append("threads must be a positive integer")
    elif threads > site.max_cpus or (cpus is not None and threads > cpus):
        problems.append(f"threads = {threads} exceeds the cpus granted to this job")

    if not samples:
        problems.append("the town has no samples to select")
    for sample in samples:
        if not SAMPLE_RE.match(sample):
            problems.append(f"sample name {sample!r} must match {SAMPLE_RE.pattern}")
    return problems


def render(spec: dict[str, Any], site: Site, *, work_dir: str, samples: list[str], reference_path_template: str) -> RenderedJob:
    """Substitute placeholders. Call only with a spec that `validate` accepted for this site."""
    region = Region.parse(str(spec["region"]), "GRCh38")
    vg_path = reference_path_template.format(assembly=region.assembly, chrom=region.chrom)
    values = {
        "region": f"{region.chrom}:{region.start + 1}-{region.end}",
        "vg_region": f"{vg_path}:{region.start}-{max(region.end - 1, region.start)}",
        "vg_path": vg_path,
        "reference": region.assembly,
        "samples": ",".join(samples),
        "work": work_dir.rstrip("/"),
        "threads": str(spec["threads"]),
    }

    def substitute(token: str) -> str:
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name.startswith("data:"):
                key = name.split(":", 1)[1]
                if key not in site.paths:
                    raise ComputeError(f"site {site.name} has no path for dataset {key}")
                return site.paths[key]
            return values[name]

        return PLACEHOLDER_RE.sub(replace, token)

    steps = [Step(id=step["id"], argv=[substitute(token) for token in step["argv"]], stdout=substitute(step["stdout"]) if step.get("stdout") else None) for step in spec["steps"]]
    outputs = [{**output, "path": substitute(output["path"])} for output in spec["outputs"]]
    resources = {key: int(spec["resources"][key]) for key in ("cpus", "mem_gb", "wall_seconds")}
    return RenderedJob(workflow=str(spec["workflow"]), steps=steps, work_dir=values["work"], outputs=outputs, resources=resources)
