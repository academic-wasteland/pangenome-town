"""Plan, validate, execute, and package one compute job for a town.

The runner never trusts a workflow it is handed: a spec passed in by an agent is re-validated against the
template library and the chosen site before anything is rendered, and a spec it plans itself goes through
the same validator. Only outputs the template marks `release = true` leave the work area; intermediates
(for example individual genotypes that feed an aggregate) are deleted after postprocessing.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from ..config import TownConfig
from ..exchange import sha256_file
from ..tools.graph import Call, Region, _version
from . import ComputeError
from .sites import RenderedJob, Site, Step, driver_for, load_sites, reachable
from .workflows import TEMPLATES, plan, render, validate

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
) -> dict[str, Any]:
    template = TEMPLATES.get(template_name)
    if template is None:
        raise ComputeError(f"unknown workflow template {template_name!r}")
    if site is None:
        site, diagnostics = pick_site(town, template_name, extra_datasets)
        if site is None:
            reasons = "; ".join(f"{item['site']}: {item['detail']}" for item in diagnostics) or "no sites declared"
            raise ComputeError(f"no reachable site holds {', '.join((*template.datasets, *extra_datasets))} ({reasons})")
    samples = list(town.samples)
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
