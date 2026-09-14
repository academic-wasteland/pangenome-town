"""IRI scheme for pangenome task parameters carried as RCP individuals.

RCP messages are closed: a region, gene, or read set must be an IRI-identified
individual typed by a contract class. These IRIs are identifiers only; they
are parsed, never dereferenced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..tools.graph import MAX_REGION_SPAN, Region
from . import PG, town_iri

ASSEMBLIES = {"GRCh38": "GRCh38Region", "CHM13": "CHM13Region"}
_REGION = re.compile(r"^https://w3id\.org/academic-wasteland/(?P<town>[a-z][a-z0-9_]*)/regions/(?P<assembly>GRCh38|CHM13)/(?P<chrom>chr(?:[0-9]{1,2}|[XYM]|MT))/(?P<start>[0-9]{1,9})-(?P<end>[0-9]{1,9})$")
_GENE = re.compile(r"^https://w3id\.org/academic-wasteland/(?P<town>[a-z][a-z0-9_]*)/genes/(?P<assembly>GRCh38|CHM13)/(?P<symbol>[A-Za-z0-9][A-Za-z0-9._-]{0,40})$")


class IriError(ValueError):
    pass


@dataclass(frozen=True)
class RegionIri:
    town: str
    region: Region

    @property
    def iri(self) -> str:
        r = self.region
        return f"{town_iri(self.town)}regions/{r.assembly}/{r.chrom}/{r.start}-{r.end}"

    @property
    def class_iri(self) -> str:
        return f"{PG}{ASSEMBLIES[self.region.assembly]}"

    def entity(self) -> dict:
        return {"@id": self.iri, "@type": ["Dataset", self.class_iri], "name": str(self.region)}


def region_iri(town: str, region: Region) -> RegionIri:
    if region.assembly not in ASSEMBLIES:
        raise IriError(f"assembly {region.assembly!r} has no region class (known: {sorted(ASSEMBLIES)})")
    return RegionIri(town, region)


def parse_region_iri(iri: str) -> RegionIri:
    match = _REGION.match(iri)
    if not match:
        raise IriError(f"not a pangenome-town region IRI: {iri!r}")
    start, end = int(match["start"]), int(match["end"])
    if start >= end or end - start > MAX_REGION_SPAN:
        raise IriError("region IRI has an invalid or oversized span")
    return RegionIri(match["town"], Region(match["assembly"], match["chrom"], start, end))


def gene_iri(town: str, assembly: str, symbol: str) -> str:
    iri = f"{town_iri(town)}genes/{assembly}/{symbol}"
    if not _GENE.match(iri):
        raise IriError(f"gene symbol {symbol!r} or assembly {assembly!r} not allowed")
    return iri


def parse_gene_iri(iri: str) -> tuple[str, str, str]:
    match = _GENE.match(iri)
    if not match:
        raise IriError(f"not a pangenome-town gene IRI: {iri!r}")
    return match["town"], match["assembly"], match["symbol"]
