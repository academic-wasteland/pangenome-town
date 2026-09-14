import shutil
import subprocess
from pathlib import Path

import pytest

from pangenome_town import config

TOY_GFA = """H\tVN:Z:1.1\tRS:Z:GRCh38
S\t1\tACGTACGTAC
S\t2\tGG
S\t3\tTT
S\t4\tCCCCAAAA
S\t5\tGATTACA
W\tGRCh38\t0\tchr1\t0\t27\t>1>2>4>5
W\tksa001\t1\tchr1\t0\t27\t>1>3>4>5
W\tksa001\t2\tchr1\t0\t27\t>1>2>4>5
W\tksa002\t1\tchr1\t0\t19\t>1>2>4
W\tNA18940\t1\tchr1\t0\t27\t>1>3>4>5
"""

TOY_VCF = """##fileformat=VCFv4.2
##contig=<ID=chr1,length=27>
##INFO=<ID=AC,Number=A,Type=Integer,Description="alt count">
##INFO=<ID=AN,Number=1,Type=Integer,Description="allele number">
##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tGRCh38\tksa001\tksa002\tNA18940
chr1\t11\t.\tGG\tTT\t60\tPASS\tAC=2;AN=8\tGT\t0|0\t1|0\t0|0\t1|0
chr1\t14\t.\tC\tA\t60\tPASS\tAC=1;AN=8\tGT\t0|0\t0|0\t1|0\t0|0
chr1\t20\t.\tG\tGATTACAGATTACAGATTACAGATTACAGATTACAGATTACAGATTACAGATTACAGA\t60\tPASS\tAC=1;AN=8\tGT\t0|0\t0|0\t0|0\t0|1
"""


def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


@pytest.fixture(scope="session")
def toy_data(tmp_path_factory) -> dict:
    root = tmp_path_factory.mktemp("toy")
    gfa = root / "toy.gfa"
    gfa.write_text(TOY_GFA, encoding="utf-8")
    vcf_text = root / "toy.vcf"
    vcf_text.write_text(TOY_VCF, encoding="utf-8")
    data = {"root": root, "gfa": gfa, "gbz": None, "vcf": None}
    if _have("vg"):
        gbz = root / "toy.gbz"
        subprocess.run(["vg", "gbwt", "-G", str(gfa), "--gbz-format", "-g", str(gbz)], check=True, capture_output=True)
        data["gbz"] = gbz
    if _have("bcftools"):
        vcf = root / "toy.vcf.gz"
        subprocess.run(["bcftools", "view", "-Oz", "-o", str(vcf), str(vcf_text)], check=True, capture_output=True)
        subprocess.run(["bcftools", "index", "-t", str(vcf)], check=True, capture_output=True)
        data["vcf"] = vcf
    return data


def write_town(root: Path, name: str, *, peers: dict[str, str], samples: list[str], graph: Path | None, vcf: Path | None, db: Path) -> Path:
    city = root / name
    city.mkdir(parents=True, exist_ok=True)
    peers_toml = "\n".join(f'{key} = "{value}"' for key, value in peers.items())
    text = f"""
[town]
name = "{name}"
display = "{name.title()}"
population = "TEST"
samples = {samples!r}
rig = "{name}-rig"

[town.peers]
{peers_toml}

[data]
graph = "{graph or ''}"
vcf = "{vcf or ''}"
reference_paths = ["GRCh38"]
default_assembly = "GRCh38"

[exchange]
db = "{db}"
supervisor_url = "http://127.0.0.1:1"

[rcp]
commons_dir = ""
"""
    path = city / "town.toml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def towns(tmp_path, toy_data):
    db = tmp_path / "exchange.db"
    ubar = write_town(tmp_path, "ubar", peers={"yamatai": "yamatai"}, samples=["ksa001", "ksa002"], graph=toy_data["gbz"], vcf=toy_data["vcf"], db=db)
    yamatai = write_town(tmp_path, "yamatai", peers={"ubar": "ubar"}, samples=["NA18940"], graph=toy_data["gbz"], vcf=toy_data["vcf"], db=db)
    return {"ubar": config.load(ubar), "yamatai": config.load(yamatai), "db": db}
