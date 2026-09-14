"""Build a ResearchContribution from a deterministic query result."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from research_commons.constants import CONTEXT_IRI

from ..config import TownConfig
from ..exchange import sha256_file
from . import PG, town_iri

DATA_LICENSE = "https://creativecommons.org/publicdomain/zero/1.0/"
ARTIFACT_LICENSE = "https://creativecommons.org/licenses/by/4.0/"
QUANTITIES = {
    "variants": [("variant_count", "variantSiteCount"), ("by_type.snv", "snvCount"), ("by_type.indel", "indelCount"), ("by_type.sv_50bp_plus", "structuralVariantCount")],
    "haplotypes": [("distinct_threads", "distinctHaplotypeCount"), ("haplotypes_traced", "tracedHaplotypeCount")],
    "subgraph": [("segments", "segmentCount"), ("links", "linkCount"), ("paths", "pathCount")],
    "summary": [("paths_total", "pathCount"), ("graph.nodes", "nodeCount"), ("graph.edges", "edgeCount")],
}


def _dig(result: dict[str, Any], dotted: str) -> Any:
    value: Any = result
    for key in dotted.split("."):
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def agent_iri(town: TownConfig) -> str:
    return f"{town_iri(town.name)}agents/townsfolk"


def build(town: TownConfig, task: dict[str, Any], result: dict[str, Any], subject_iri: str, *, statement: str | None = None) -> dict[str, Any]:
    kind = result["kind"]
    artifact_path = Path(result["artifact"])
    artifact_id = f"urn:uuid:{uuid.uuid4()}"
    evidence_id = f"urn:uuid:{uuid.uuid4()}"
    digest = sha256_file(artifact_path)
    claims = []
    for dotted, predicate in QUANTITIES.get(kind, []):
        value = _dig(result, dotted)
        if isinstance(value, int | float):
            claims.append({
                "@id": f"urn:uuid:{uuid.uuid4()}",
                "@type": "Claim",
                "statement": statement or f"{predicate} of {subject_iri} among {len(town.samples)} {town.population} samples is {value}",
                "subject": subject_iri,
                "predicate": f"{PG}{predicate}",
                "object": value,
                "hasEvidence": [evidence_id],
            })
    if not claims:
        claims.append({
            "@id": f"urn:uuid:{uuid.uuid4()}",
            "@type": "Claim",
            "statement": statement or f"{kind} query completed for {subject_iri}",
            "subject": subject_iri,
            "predicate": f"{PG}queryCompleted",
            "object": kind,
            "hasEvidence": [evidence_id],
        })
    return {
        "@context": CONTEXT_IRI,
        "@id": f"urn:uuid:{uuid.uuid4()}",
        "@type": "ResearchContribution",
        "semanticContract": task["semanticContract"],
        "ontologyProfile": task["ontologyProfile"],
        "addresses": task["@id"],
        "producedBy": {"@id": agent_iri(town), "@type": "Agent", "name": f"{town.display} townsfolk"},
        "generatedAtTime": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "hasOutput": [
            {"@id": artifact_id, "@type": "ResearchArtifact", "name": artifact_path.name, "license": ARTIFACT_LICENSE, "digest": digest}
        ],
        "hasClaim": claims,
        "hasEvidence": [
            {"@id": evidence_id, "@type": "Evidence", "name": f"{kind} query artifact with provenance", "digest": digest, "license": ARTIFACT_LICENSE}
        ],
    }


def write(contribution: dict[str, Any], path: Path) -> None:
    path.write_text(json.dumps(contribution, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
