import pytest

from pangenome_town.rcp import PG, contract, iri
from pangenome_town.tools.graph import Region


def test_contract_renders_and_verifies(towns, tmp_path):
    manifest = contract.render(towns["ubar"], tmp_path / "contract")
    assert manifest.id == "https://w3id.org/academic-wasteland/ubar/contract/0.1.0"
    assert manifest.bundle_digest.startswith("sha256:")
    allowed = set(manifest.data["assertionPolicy"]["allowedClasses"])
    for forbidden in ("ReputableRequester", "UnvettedRequester", "BlockedRequester", "BasicQueryTask", "LargeAnalysisTask"):
        assert f"{PG}{forbidden}" not in allowed
    for decision in ("AcceptedTask", "RejectedTask", "PermissionRequiredTask", "ConformingContribution"):
        assert f"https://w3id.org/academic-wasteland/ubar/{decision}" not in allowed
    assert f"{PG}RegionVariantListingTask" in allowed and f"{PG}GRCh38Region" in allowed
    text = (tmp_path / "contract" / "ubar.ofn").read_text()
    assert "Import(" not in text and "{{" not in text
    assert "town:graphs/JaSaPaGe-v1" in text
    card = contract.agent_card_extension(manifest, "https://example.org/ubar/contract.json")
    assert card["contracts"][0]["bundleDigest"] == manifest.bundle_digest


def test_region_iri_round_trip():
    region = Region.parse("GRCh38:chr6:29940000-29950000", "GRCh38")
    ref = iri.region_iri("ubar", region)
    assert ref.iri == "https://w3id.org/academic-wasteland/ubar/regions/GRCh38/chr6/29940000-29950000"
    assert ref.class_iri.endswith("GRCh38Region")
    back = iri.parse_region_iri(ref.iri)
    assert back.region == region and back.town == "ubar"
    assert ref.entity()["@type"] == ref.class_iri
    for bad in (
        "https://w3id.org/academic-wasteland/ubar/regions/GRCh38/chr6/10-5",
        "https://w3id.org/academic-wasteland/ubar/regions/hg19/chr6/1-2",
        "https://evil.example/regions/GRCh38/chr6/1-2",
    ):
        with pytest.raises(iri.IriError):
            iri.parse_region_iri(bad)


def test_gene_iri():
    assert iri.gene_iri("yamatai", "GRCh38", "AMY1A").endswith("/genes/GRCh38/AMY1A")
    with pytest.raises(iri.IriError):
        iri.gene_iri("yamatai", "GRCh38", "AMY1A; rm -rf")
    assert iri.parse_gene_iri("https://w3id.org/academic-wasteland/yamatai/genes/GRCh38/HLA-A") == ("yamatai", "GRCh38", "HLA-A")
