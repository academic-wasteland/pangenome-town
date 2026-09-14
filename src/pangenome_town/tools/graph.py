"""Deterministic pangenome queries over the town's graph and variant call set.

Every query returns a JSON-serialisable dict with a `provenance` block: the
exact argv of every external command, tool versions, input digests (cheap
size+mtime fingerprint for multi-gigabyte inputs), and wall time. Region
strings are validated before touching any tool and are only ever passed as
argv elements, never through a shell.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import TownConfig

KINDS = ("summary", "haplotypes", "variants", "subgraph")
REGION_RE = re.compile(r"^(?:(?P<assembly>[A-Za-z0-9_.]+):)?(?P<chrom>chr(?:[0-9]{1,2}|[XYM]|MT)):(?P<start>[0-9]{1,9})-(?P<end>[0-9]{1,9})$")
MAX_REGION_SPAN = 5_000_000
DEFAULT_TIMEOUT = 600


class QueryError(ValueError):
    pass


@dataclass(frozen=True)
class Region:
    assembly: str
    chrom: str
    start: int
    end: int

    @classmethod
    def parse(cls, text: str, default_assembly: str) -> Region:
        match = REGION_RE.match(text.replace(",", "").strip())
        if not match:
            raise QueryError(f"region {text!r} must look like GRCh38:chr6:31000000-31050000")
        start, end = int(match["start"]), int(match["end"])
        if start >= end:
            raise QueryError("region start must be smaller than end")
        if end - start > MAX_REGION_SPAN:
            raise QueryError(f"region span exceeds {MAX_REGION_SPAN:,} bp")
        return cls(assembly=match["assembly"] or default_assembly, chrom=match["chrom"], start=start, end=end)

    def __str__(self) -> str:
        return f"{self.assembly}:{self.chrom}:{self.start}-{self.end}"

    @property
    def span(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class Call:
    called: bool
    alt: bool

    @classmethod
    def parse(cls, genotype: str) -> Call:
        alleles = [allele for allele in re.split(r"[/|]", genotype.strip()) if allele != ""]
        called = any(allele != "." for allele in alleles)
        return cls(called=called, alt=any(allele not in {"0", "."} for allele in alleles))


def _fingerprint(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    stat = path.stat()
    return {"path": str(path), "bytes": stat.st_size, "mtime": int(stat.st_mtime)}


def _which(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise QueryError(f"required tool {name!r} not found on PATH")
    return found


def _version(name: str) -> str:
    try:
        out = subprocess.run([name, "--version"], capture_output=True, text=True, timeout=30, check=False)
        text = (out.stdout or out.stderr).strip().splitlines()
        return text[0] if text else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


class Provenance:
    def __init__(self, town: TownConfig, kind: str, region: Region | None):
        self.started = time.time()
        self.data: dict[str, Any] = {
            "town": town.name,
            "kind": kind,
            "region": str(region) if region else None,
            "samples": list(town.samples),
            "inputs": {"graph": _fingerprint(town.graph), "vcf": _fingerprint(town.vcf)},
            "commands": [],
            "versions": {},
        }

    def run(self, argv: list[str], *, timeout: int = DEFAULT_TIMEOUT, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        tool = Path(argv[0]).name
        if tool not in self.data["versions"]:
            self.data["versions"][tool] = _version(argv[0])
        started = time.time()
        result = subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout, check=False)
        self.data["commands"].append(
            {"argv": argv, "returncode": result.returncode, "seconds": round(time.time() - started, 3),
             "stderr_tail": result.stderr.decode("utf-8", "replace")[-2000:]}
        )
        if result.returncode != 0:
            raise QueryError(f"{tool} failed ({result.returncode}): {result.stderr.decode('utf-8', 'replace')[-500:]}")
        return result

    def finish(self) -> dict[str, Any]:
        self.data["wall_seconds"] = round(time.time() - self.started, 3)
        return self.data


def _sample_of(path_name: str) -> str:
    # GBZ / PanSN path names look like sample#haplotype#contig[:range]; references look like GRCh38#0#chr6.
    return path_name.split("#", 1)[0]


class GraphTools:
    def __init__(self, town: TownConfig):
        self.town = town

    def _require_graph(self) -> Path:
        if not self.town.has_graph:
            raise QueryError(f"graph not present at {self.town.graph}")
        return self.town.graph  # type: ignore[return-value]

    def _require_vcf(self) -> Path:
        if not self.town.has_vcf:
            raise QueryError(f"VCF not present at {self.town.vcf}")
        return self.town.vcf  # type: ignore[return-value]

    def summary(self) -> dict[str, Any]:
        graph = self._require_graph()
        prov = Provenance(self.town, "summary", None)
        vg = _which("vg")
        stats = prov.run([vg, "stats", "-l", "-z", str(graph)]).stdout.decode()
        numbers: dict[str, int] = {}
        for line in stats.splitlines():
            parts = line.split(":")
            if len(parts) == 2 and parts[1].strip().isdigit():
                numbers[parts[0].strip()] = int(parts[1].strip())
        paths = prov.run([vg, "paths", "-x", str(graph), "-L"]).stdout.decode().splitlines()
        per_sample: dict[str, int] = {}
        for name in paths:
            per_sample[_sample_of(name)] = per_sample.get(_sample_of(name), 0) + 1
        served = {sample: per_sample.get(sample, 0) for sample in self.town.samples}
        return {
            "kind": "summary",
            "graph": numbers,
            "paths_total": len(paths),
            "path_names_by_sample_total": len(per_sample),
            "served_samples": served,
            "reference_paths": [name for name in paths if _sample_of(name) in self.town.reference_paths][:200],
            "provenance": prov.finish(),
        }

    def _chunk(self, prov: Provenance, region: Region, out_dir: Path, *, trace: bool = False) -> tuple[Path, Path | None]:
        """Extract the region subgraph; with trace=True also collect haplotype thread frequencies."""
        graph = self._require_graph()
        vg = _which("vg")
        path_name = self.town.reference_path(region.assembly, region.chrom)
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = f"chunk_{region.chrom}_{region.start}_{region.end}"
        chunk = out_dir / f"{stem}.vg"
        argv = [vg, "chunk", "-x", str(graph), "-p", f"{path_name}:{region.start}-{max(region.end - 1, region.start)}", "-c", "0", "-b", str(out_dir / stem)]
        if trace:
            argv.append("-T")
        result = prov.run(argv)
        chunk.write_bytes(result.stdout)
        annotate = next(iter(out_dir.glob(f"{stem}*annotate.txt")), None) if trace else None
        return chunk, annotate

    def haplotypes(self, region: Region, out_dir: Path) -> dict[str, Any]:
        """Local haplotype diversity: distinct haplotype threads through the region and their frequencies.

        Thread frequencies count every haplotype in the graph, not only the town's samples; per-sample
        presence for the town's samples comes from the VCF when it is available.
        """
        prov = Provenance(self.town, "haplotypes", region)
        _, annotate = self._chunk(prov, region, out_dir, trace=True)
        threads: list[dict[str, Any]] = []
        reference_in_region = False
        if annotate is not None:
            for line in annotate.read_text(encoding="utf-8").splitlines():
                name, _, count = line.partition("\t")
                if not count.strip().isdigit():
                    continue
                if name.startswith("thread_"):
                    threads.append({"thread": name, "haplotypes": int(count)})
                else:
                    reference_in_region = True
        threads.sort(key=lambda item: -item["haplotypes"])
        total = sum(item["haplotypes"] for item in threads)
        samples_genotyped: list[str] = []
        samples_with_alt: list[str] = []
        if self.town.has_vcf:
            try:
                genotypes = self._genotypes(prov, region)
                samples_genotyped = [sample for sample, calls in genotypes.items() if any(call.called for call in calls)]
                samples_with_alt = [sample for sample, calls in genotypes.items() if any(call.alt for call in calls)]
            except QueryError as error:
                prov.data["vcf_error"] = str(error)
        return {
            "kind": "haplotypes",
            "region": str(region),
            "reference_path": self.town.reference_path(region.assembly, region.chrom),
            "reference_in_region": reference_in_region,
            "distinct_threads": len(threads),
            "haplotypes_traced": total,
            "threads": threads[:50],
            "town_samples_genotyped_in_region": samples_genotyped,
            "town_samples_with_alt_in_region": samples_with_alt,
            "note": "thread frequencies cover every haplotype in the graph; sample lists cover this town's samples via the VCF",
            "provenance": prov.finish(),
        }

    def _genotypes(self, prov: Provenance, region: Region) -> dict[str, list[Call]]:
        vcf = self._require_vcf()
        bcftools = _which("bcftools")
        header_samples = prov.run([bcftools, "query", "-l", str(vcf)]).stdout.decode().split()
        samples = [sample for sample in self.town.samples if sample in header_samples]
        if not samples:
            raise QueryError("none of the town's samples are present in the VCF")
        contigs = prov.run([bcftools, "index", "-s", str(vcf)]).stdout.decode()
        contig_names = [line.split("\t")[0] for line in contigs.splitlines()]
        candidates = [region.chrom, f"{region.assembly}#0#{region.chrom}", region.chrom.removeprefix("chr")]
        contig = next((name for name in candidates if name in contig_names), None)
        if contig is None:
            raise QueryError(f"no VCF contig matches {region.chrom}; contigs start with {contig_names[:5]}")
        window = f"{contig}:{region.start + 1}-{region.end}"
        subset = prov.run([bcftools, "view", "-r", window, "-s", ",".join(samples), "--min-ac", "1:nref", "-Ou", str(vcf)]).stdout
        query = prov.run([bcftools, "query", "-f", "%CHROM\t%POS\t%REF\t%ALT\t%AC\t%AN[\t%GT]\n", "-"], stdin=subset).stdout.decode()
        rows = [line.split("\t") for line in query.splitlines() if line]
        prov.data["vcf_contig"] = contig
        prov.data["vcf_rows"] = rows
        prov.data["vcf_samples"] = samples
        return {sample: [Call.parse(row[6 + index]) for row in rows] for index, sample in enumerate(samples)}

    def subgraph(self, region: Region, out_dir: Path) -> dict[str, Any]:
        prov = Provenance(self.town, "subgraph", region)
        chunk, _ = self._chunk(prov, region, out_dir)
        vg = _which("vg")
        gfa_path = out_dir / f"subgraph_{region.chrom}_{region.start}_{region.end}.gfa"
        gfa = prov.run([vg, "view", str(chunk)]).stdout
        gfa_path.write_bytes(gfa)
        counts = {"S": 0, "L": 0, "P": 0, "W": 0}
        for line in gfa.splitlines():
            key = line[:1].decode()
            if key in counts:
                counts[key] += 1
        return {
            "kind": "subgraph",
            "region": str(region),
            "gfa": str(gfa_path),
            "gfa_bytes": len(gfa),
            "segments": counts["S"],
            "links": counts["L"],
            "paths": counts["P"] + counts["W"],
            "provenance": prov.finish(),
        }

    def variants(self, region: Region, out_dir: Path) -> dict[str, Any]:
        prov = Provenance(self.town, "variants", region)
        genotypes = self._genotypes(prov, region)
        rows: list[list[str]] = prov.data.pop("vcf_rows")
        samples: list[str] = prov.data.pop("vcf_samples")
        out_dir.mkdir(parents=True, exist_ok=True)
        table_path = out_dir / f"variants_{region.chrom}_{region.start}_{region.end}.tsv"
        header = ["CHROM", "POS", "REF", "ALT", "AC", "AN", *samples]
        table_path.write_text("\n".join(["\t".join(header), *("\t".join(row) for row in rows)]) + "\n", encoding="utf-8")
        by_type = {"snv": 0, "indel": 0, "sv_50bp_plus": 0}
        for row in rows:
            ref, alts = row[2], row[3].split(",")
            longest = max(len(ref), *(len(alt) for alt in alts))
            if len(ref) == 1 and all(len(alt) == 1 for alt in alts):
                by_type["snv"] += 1
            elif longest >= 50:
                by_type["sv_50bp_plus"] += 1
            else:
                by_type["indel"] += 1
        per_sample_alt = {sample: sum(1 for call in calls if call.alt) for sample, calls in genotypes.items()}
        return {
            "kind": "variants",
            "region": str(region),
            "vcf_contig": prov.data.get("vcf_contig"),
            "samples": samples,
            "variant_count": len(rows),
            "by_type": by_type,
            "per_sample_alt_sites": per_sample_alt,
            "table": str(table_path),
            "provenance": prov.finish(),
        }

    def run(self, kind: str, region: Region | None, out_dir: Path) -> dict[str, Any]:
        if kind not in KINDS:
            raise QueryError(f"kind must be one of {KINDS}")
        if kind == "summary":
            result = self.summary()
        else:
            if region is None:
                raise QueryError(f"{kind} needs --region")
            result = getattr(self, kind)(region, out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{kind}.json"
        out_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        result["artifact"] = str(out_path)
        return result


def default_out_dir(town: TownConfig, label: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", label)[:80]
    path = town.state_dir / "queries" / f"{int(time.time())}_{safe}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def tool_versions() -> dict[str, str]:
    return {name: (_version(path) if (path := shutil.which(name)) else "missing") for name in ("vg", "bcftools", "gc", "bd", "dolt")}


def environment_ok() -> list[str]:
    problems = []
    for name in ("vg", "bcftools"):
        if not shutil.which(name):
            problems.append(f"{name} not on PATH ({os.environ.get('PATH', '')[:80]}…)")
    return problems
