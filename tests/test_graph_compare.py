import shutil

import pytest

from pangenome_town.tools import graph

needs_tools = pytest.mark.skipif(not (shutil.which("bcftools") and shutil.which("vg")), reason="bcftools and vg required")


@needs_tools
def test_compare_reports_population_differences(towns, tmp_path):
    ubar = towns["ubar"]
    tools = graph.GraphTools(ubar)
    result = tools.run("compare", graph.Region.parse("GRCh38:chr1:0-100", "GRCh38"), tmp_path / "out")
    assert result["kind"] == "compare"
    assert result["town_samples"] == ["ksa001", "ksa002"]
    assert result["other_sample_count"] == 1  # NA18940 (GRCh38 excluded as a reference)
    assert result["sites_compared"] == 3
    # chr1:14 C>A is carried only by ksa002: private to the town.
    private = [site for site in result["top_differentiated"] if site["pos"] == 14]
    assert private and private[0]["other_af"] == 0
    assert result["town_private_sites"] >= 1
    assert (tmp_path / "out" / "compare.json").exists()
