"""Render a town's RCP semantic contract (ontology + manifest) from the pack template.

The rendered files are committed in the town repository so their digests are
pinned; peers verify the bundle digest they advertise in their Agent Card.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research_commons.contracts import ContractManifest, bundle_digest, sha256_file

from ..config import TownConfig
from . import PG, RCP, town_iri

TEMPLATE = Path(__file__).resolve().parents[3] / "contract" / "pangenome.ofn.tmpl"
CONTRACT_VERSION = "0.1.0"
BASIC_TASKS = ("GraphSummaryTask", "RegionExtractionTask", "HaplotypePresenceTask", "RegionVariantListingTask", "GeneLookupTask")
LARGE_TASKS = ("ReadMappingVariantCallingTask", "WholeGraphDeconstructTask", "PopulationComparisonTask")
DATA_CLASSES = ("PangenomeGraph", "GenomicRegion", "GRCh38Region", "CHM13Region", "Gene", "ReadSet")
RCP_CLASSES = ("ResearchRequest", "ResearchTask", "ResearchContribution", "Claim", "Evidence", "Person", "Organization",
               "Agent", "Dataset", "PublicDataset", "RestrictedDataset", "ResearchArtifact")
RCP_PROPERTIES = ("partOfRequest", "requestedBy", "onBehalfOf", "producedBy", "addresses", "usesDataset", "hasOutput",
                  "hasClaim", "hasEvidence", "supports", "contradicts", "replicates", "critiques", "delegatesTo")


def graph_id(town: TownConfig) -> str:
    return str(town.extra.get("rcp", {}).get("graph_id") or "JaSaPaGe-v1")


def graph_iri(town: TownConfig) -> str:
    return f"{town_iri(town.name)}graphs/{graph_id(town)}"


def render_ontology(town: TownConfig) -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    return text.replace("{{TOWN_IRI}}", town_iri(town.name)).replace("{{GRAPH_ID}}", graph_id(town))


def render_manifest(town: TownConfig, ontology_path: Path) -> dict[str, Any]:
    base = town_iri(town.name)
    ontology_iri = f"{base}contract/ontology"
    file_digest = sha256_file(ontology_path)
    return {
        "id": f"{base}contract/{CONTRACT_VERSION}",
        "version": CONTRACT_VERSION,
        "bundleDigest": bundle_digest([(ontology_iri, file_digest)]),
        "ontologies": [{"id": ontology_iri, "path": ontology_path.name, "digest": file_digest}],
        "acceptedTaskClass": f"{base}AcceptedTask",
        "guaranteedOutputClass": f"{base}ConformingContribution",
        "rejectedTaskClass": f"{base}RejectedTask",
        "permissionRequiredClass": f"{base}PermissionRequiredTask",
        "assertionPolicy": {
            "allowedClasses": [
                *(f"{RCP}{name}" for name in RCP_CLASSES),
                *(f"{PG}{name}" for name in DATA_CLASSES),
                *(f"{PG}{name}" for name in BASIC_TASKS + LARGE_TASKS),
            ],
            "allowedObjectProperties": [f"{RCP}{name}" for name in RCP_PROPERTIES],
        },
        "limits": {"timeoutSeconds": 60, "maxMessageBytes": 1048576, "maxIndividuals": 2000},
    }


def render(town: TownConfig, out_dir: Path) -> ContractManifest:
    out_dir.mkdir(parents=True, exist_ok=True)
    ontology_path = out_dir / f"{town.name}.ofn"
    ontology_path.write_text(render_ontology(town), encoding="utf-8")
    manifest_path = out_dir / f"{town.name}.contract.json"
    manifest_path.write_text(json.dumps(render_manifest(town, ontology_path), indent=2) + "\n", encoding="utf-8")
    return ContractManifest.load(manifest_path)


def agent_card_extension(manifest: ContractManifest, manifest_url: str) -> dict[str, Any]:
    return {
        "extension": f"{RCP}a2a",
        "contracts": [
            {
                "manifest": manifest_url,
                "bundleDigest": manifest.bundle_digest,
                "acceptedTaskClass": manifest.data["acceptedTaskClass"],
                "guaranteedOutputClass": manifest.data["guaranteedOutputClass"],
            }
        ],
    }
