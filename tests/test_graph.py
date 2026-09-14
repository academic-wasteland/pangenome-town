import shutil

import pytest

from pangenome_town.tools import graph

needs_vg = pytest.mark.skipif(shutil.which("vg") is None, reason="vg not installed")
needs_bcftools = pytest.mark.skipif(shutil.which("bcftools") is None, reason="bcftools not installed")


def test_region_parse():
    region = graph.Region.parse("chr6:31,000,000-31,050,000", "GRCh38")
    assert (region.assembly, region.chrom, region.start, region.end) == ("GRCh38", "chr6", 31000000, 31050000)
    assert str(graph.Region.parse("CHM13:chrX:5-10", "GRCh38")) == "CHM13:chrX:5-10"
    for bad in ("chr6", "chr6:10-5", "chr6:0-9000000", "chr6:1-2; rm -rf /", "6:1-2"):
        with pytest.raises(graph.QueryError):
            graph.Region.parse(bad, "GRCh38")


@needs_vg
def test_summary_and_haplotypes_on_toy_graph(towns, tmp_path):
    tools = graph.GraphTools(towns["ubar"])
    summary = tools.run("summary", None, tmp_path / "s")
    assert summary["served_samples_path_fragments"] == {"ksa001": 2, "ksa002": 1}
    assert summary["graph"].get("nodes") == 5 and summary["cached"] is False
    assert tools.summary()["cached"] is True
    assert summary["provenance"]["commands"][0]["argv"][1] == "stats"
    region = graph.Region.parse("GRCh38:chr1:0-20", "GRCh38")
    haplotypes = tools.run("haplotypes", region, tmp_path / "h")
    assert haplotypes["reference_in_region"] is True
    assert haplotypes["distinct_threads"] == 2 and haplotypes["haplotypes_traced"] == 5
    assert haplotypes["town_samples_genotyped_in_region"] == ["ksa001", "ksa002"]
    assert haplotypes["town_samples_with_alt_in_region"] == ["ksa001", "ksa002"]
    subgraph = tools.run("subgraph", region, tmp_path / "g")
    assert subgraph["segments"] >= 3 and subgraph["gfa"].endswith(".gfa")


@needs_bcftools
def test_variants_on_toy_vcf(towns, tmp_path):
    tools = graph.GraphTools(towns["ubar"])
    region = graph.Region.parse("GRCh38:chr1:0-27", "GRCh38")
    result = tools.run("variants", region, tmp_path / "v")
    assert result["samples"] == ["ksa001", "ksa002"]
    assert result["variant_count"] == 2
    assert result["by_type"] == {"snv": 1, "indel": 1, "sv_50bp_plus": 0}
    assert result["per_sample_alt_sites"] == {"ksa001": 1, "ksa002": 1}
    yamatai = graph.GraphTools(towns["yamatai"]).run("variants", region, tmp_path / "y")
    assert yamatai["variant_count"] == 2 and yamatai["by_type"]["sv_50bp_plus"] == 1
