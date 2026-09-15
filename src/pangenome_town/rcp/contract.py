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

CRED = "https://w3id.org/academic-wasteland/credentials/v0.1/"

TEMPLATE = Path(__file__).with_name("pangenome.ofn.tmpl")
if not TEMPLATE.is_file():  # source/editable checkout; wheels carry the template alongside this module
    TEMPLATE = Path(__file__).resolve().parents[3] / "contract" / "pangenome.ofn.tmpl"
CONTRACT_VERSION = "0.1.0"
BASIC_TASKS = ("GraphSummaryTask", "RegionExtractionTask", "HaplotypePresenceTask", "RegionVariantListingTask", "GeneLookupTask")
LARGE_TASKS = ("ReadMappingVariantCallingTask", "WholeGraphDeconstructTask", "PopulationComparisonTask")
CONTROLLED_TASKS = ("AlleleFrequencyTask", "IndividualGenotypeExportTask")
DATA_CLASSES = ("PangenomeGraph", "GenomicRegion", "GRCh38Region", "CHM13Region", "Gene", "ReadSet", "IndividualGenotypeData")
ARTIFACT_CLASSES = ("AggregateArtifact", "IndividualLevelArtifact")
# Scope library: a committee approves one of these task classes (the credential is asserted into `approves`); every
# released output must be an instance of `release`.
SCOPES = {
    f"{PG}AggregateFrequencyScope": {"approves": f"{PG}ApprovesAggregateFrequencyScope", "release": f"{PG}AggregateArtifact",
                                     "label": "Aggregate allele-frequency research (no individual-level output)"},
    f"{PG}IndividualGenotypeScope": {"approves": f"{PG}ApprovesIndividualGenotypeScope", "release": f"{RCP}ResearchArtifact",
                                     "label": "Individual-level genotype research"},
}
RCP_CLASSES = ("ResearchRequest", "ResearchTask", "ResearchContribution", "Claim", "Evidence", "Person", "Organization",
               "Agent", "Dataset", "PublicDataset", "RestrictedDataset", "ResearchArtifact")
RCP_PROPERTIES = ("partOfRequest", "requestedBy", "onBehalfOf", "producedBy", "addresses", "usesDataset", "hasOutput",
                  "hasClaim", "hasEvidence", "supports", "contradicts", "replicates", "critiques", "delegatesTo")


def graph_id(town: TownConfig) -> str:
    return str(town.extra.get("rcp", {}).get("graph_id") or "JaSaPaGe-v1")


def graph_iri(town: TownConfig) -> str:
    return f"{town_iri(town.name)}graphs/{graph_id(town)}"


def trust_anchors(town: TownConfig) -> dict[str, str]:
    """Trust anchors of this town: issuer IRI -> pinned public key text."""
    anchors = (town.extra.get("trust") or {}).get("anchors") or {}
    return {str(key): str(value) for key, value in anchors.items()}


def restricted_datasets(town: TownConfig) -> list[dict[str, str]]:
    """Controlled-access datasets this town serves ([[restricted_datasets]] with key, iri, name, source)."""
    items = []
    for entry in town.extra.get("restricted_datasets") or []:
        if not isinstance(entry, dict) or not entry.get("key") or not entry.get("iri"):
            raise ValueError("[[restricted_datasets]] entries need key and iri")
        items.append({"key": str(entry["key"]), "iri": str(entry["iri"]), "name": str(entry.get("name") or entry["key"]),
                      "source": str(entry.get("source") or "vcf")})
    return items


def site_iri(town: TownConfig, name: str) -> str:
    return f"{town_iri(town.name)}sites/{name}"


def site_holdings(town: TownConfig) -> dict[str, tuple[str, ...]]:
    """Site name -> dataset keys held there, read from [[sites]] (default: one local workstation with graph and vcf)."""
    sites = town.extra.get("sites")
    if not sites:
        return {"workstation": ("graph", "vcf")}
    return {str(site["name"]): tuple(str(key) for key in site.get("datasets") or ()) for site in sites}


def dataset_keys(town: TownConfig) -> dict[str, str]:
    """Dataset individual IRI -> the site dataset key whose residence decides where it can be used."""
    keys = {graph_iri(town): "graph"}
    for dataset in restricted_datasets(town):
        keys[dataset["iri"]] = dataset["key"]
    return keys


def town_axioms(town: TownConfig) -> list[str]:
    """Static, town-specific facts rendered into the contract: anchors, served restricted data, sites, residence."""
    from research_commons import axioms as ax

    lines: list[str] = []
    for anchor in sorted(trust_anchors(town)):
        lines.append(f"Declaration(NamedIndividual({ax.iri(anchor)}))")
        lines.append(ax.class_assertion(f"{town_iri(town.name)}TrustAnchor", anchor))
    for dataset in restricted_datasets(town):
        lines.append(f"Declaration(NamedIndividual({ax.iri(dataset['iri'])}))")
        lines.append(ax.class_assertion(f"{town_iri(town.name)}ServedRestrictedDataset", dataset["iri"]))
    holdings = site_holdings(town)
    for name in sorted(holdings):
        lines.append(f"Declaration(NamedIndividual({ax.iri(site_iri(town, name))}))")
        lines.append(ax.class_assertion(f"{PG}ComputeSite", site_iri(town, name)))
    for dataset, key in dataset_keys(town).items():
        sites = [site_iri(town, name) for name in sorted(holdings) if key in holdings[name]]
        for site in sites:
            lines.append(ax.property_assertion(f"{PG}residesOn", dataset, site))
        lines.append(ax.class_assertion(ax.only(f"{PG}residesOn", ax.one_of(*sites)), dataset))
    if len(holdings) > 1:
        lines.append(f"DifferentIndividuals({' '.join(ax.iri(site_iri(town, name)) for name in sorted(holdings))})")
    return lines


def render_ontology(town: TownConfig) -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    block = "\n".join(f" {line}" for line in town_axioms(town))
    return (text.replace("{{TOWN_AXIOMS}}", block).replace("{{TOWN_IRI}}", town_iri(town.name))
            .replace("{{GRAPH_ID}}", graph_id(town)))


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
                *(f"{PG}{name}" for name in BASIC_TASKS + LARGE_TASKS + CONTROLLED_TASKS),
                *(f"{PG}{name}" for name in ARTIFACT_CLASSES),
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
