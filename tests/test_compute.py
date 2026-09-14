import copy
import dataclasses
import os
import shlex
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from pangenome_town import config
from pangenome_town.compute import TEMPLATES, ComputeError, load_sites, plan, reachable, run_task, validate
from pangenome_town.compute import sites as sites_module
from pangenome_town.compute.runner import pick_site
from pangenome_town.compute.sites import LocalDriver, RenderedJob, Site, SshDriver, Step
from pangenome_town.compute.workflows import AGGREGATE, INDIVIDUAL, render
from pangenome_town.tools.graph import Region

needs_bcftools = pytest.mark.skipif(shutil.which("bcftools") is None, reason="bcftools missing")
needs_vg = pytest.mark.skipif(shutil.which("vg") is None, reason="vg missing")

REGION = Region("GRCh38", "chr1", 0, 27)
SAMPLES = ["ksa001", "ksa002"]


@pytest.fixture(autouse=True)
def fresh_reachability():
    sites_module.clear_reachability_cache()
    yield
    sites_module.clear_reachability_cache()


def town_with(towns, name, extra):
    path = towns[name].city_root / "town.toml"
    base = path.read_text(encoding="utf-8")
    path.write_text(base + extra, encoding="utf-8")
    try:
        return config.load(path)
    finally:
        path.write_text(base, encoding="utf-8")


def local_site(**changes):
    site = Site("workstation", "local", datasets=("vcf", "graph"), paths={"vcf": "/data/x.vcf.gz", "graph": "/data/g.gbz"}, tools=("vg", "bcftools"), max_mem_gb=64, max_wall_seconds=7200)
    return dataclasses.replace(site, **changes)


# Sites -------------------------------------------------------------------------------------------


def test_default_site_when_none_declared(towns):
    ubar = towns["ubar"]
    sites = load_sites(ubar)
    assert [site.name for site in sites] == ["workstation"]
    assert sites[0].driver == "local" and sites[0].datasets == ("graph", "vcf")
    if ubar.vcf is not None:
        assert sites[0].paths["vcf"] == str(ubar.vcf)
    assert sites[0].iri("ubar") == "https://w3id.org/academic-wasteland/ubar/sites/workstation"


def test_explicit_sites_are_loaded_with_local_defaults(towns, toy_data):
    extra = """
[[sites]]
name = "workstation"
driver = "local"
datasets = ["vcf"]
tools = ["bcftools"]
max_cpus = 2

[[sites]]
name = "ddbj"
driver = "ssh"
host = "ddbj"
workdir = "/home/asianhla/wasteland/ubar"
scheduler = "slurm"
enabled = false
datasets = ["graph", "vcf"]
paths = { graph = "/home/asianhla/data/JaSaPaGe/JaSaPaGe.gbz", vcf = "/home/asianhla/data/JaSaPaGe/JaSaPaGe.GRCh38.vcf.gz" }
"""
    town = town_with(towns, "ubar", extra)
    workstation, ddbj = load_sites(town)
    assert workstation.tools == ("bcftools",) and workstation.max_cpus == 2
    if town.vcf is not None:
        assert workstation.paths == {"vcf": str(town.vcf)}
    assert ddbj.driver == "ssh" and ddbj.scheduler == "slurm" and ddbj.enabled is False
    assert ddbj.paths["graph"].endswith("JaSaPaGe.gbz")


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ('[[sites]]\nname = "x"\ndriver = "docker"\n', "driver"),
        ('[[sites]]\nname = "x"\ndriver = "ssh"\nworkdir = "/w"\n', "host"),
        ('[[sites]]\nname = "x"\ndriver = "ssh"\nhost = "ddbj"\nworkdir = "relative"\n', "absolute workdir"),
        ('[[sites]]\nname = "x"\ndriver = "ssh"\nhost = "-oProxyCommand=evil"\nworkdir = "/w"\n', "host"),
        ('[[sites]]\nname = "Bad Name"\n', "site name"),
        ('[[sites]]\nname = "x"\nscheduler = "pbs"\n', "scheduler"),
        ('[[sites]]\nname = "x"\nmax_cpus = 0\n', "max_cpus"),
    ],
)
def test_bad_site_config_is_refused(towns, extra, message):
    town = town_with(towns, "ubar", "\n" + extra)
    with pytest.raises(ComputeError, match=message):
        load_sites(town)


def test_reachable_disabled_and_local(tmp_path):
    data = tmp_path / "x.vcf.gz"
    data.write_text("", encoding="utf-8")
    assert reachable(Site("off", "local", enabled=False)) == (False, "disabled in town.toml")
    ok, detail = reachable(Site("here", "local", datasets=("vcf",), paths={"vcf": str(data)}, tools=("sh",)))
    assert ok, detail
    ok, detail = reachable(Site("there", "local", datasets=("vcf",), paths={"vcf": str(tmp_path / "missing")}, tools=("definitely-not-a-tool-xyz",)))
    assert not ok and "definitely-not-a-tool-xyz" in detail and "vcf" in detail


def test_reachable_ssh_uses_batch_mode_and_caches():
    calls = []

    def runner(argv, **_):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    site = Site("remote", "ssh", host="ddbj-test", workdir="/w")
    assert reachable(site, runner=runner) == (True, "ssh ddbj-test answered")
    assert reachable(site, runner=runner)[0]
    assert calls == [["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "ddbj-test", "true"]]


# Validation --------------------------------------------------------------------------------------


def problems_after(mutate, *, template="genotype-export", site=None, samples=SAMPLES, region=REGION):
    site = site or local_site()
    spec = plan(template, region=region, site=site, samples=samples)
    changed = copy.deepcopy(spec)
    mutate(changed)
    return validate(changed, site, samples=samples)


def test_planned_specs_validate():
    for name in TEMPLATES:
        site = local_site()
        spec = plan(name, region=REGION, site=site, samples=SAMPLES)
        assert validate(spec, site, samples=SAMPLES) == [], name


def _set_step_token(index, position, value):
    def mutate(spec):
        spec["steps"][index]["argv"][position] = value
    return mutate


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_set_step_token(0, 3, "chr1:1-27;rm -rf ~"), "forbidden characters"),
        (_set_step_token(0, 9, "/etc/passwd"), "absolute path"),
        (_set_step_token(0, 0, "sh"), "differs from template tool"),
        (_set_step_token(0, 0, "sh"), "not allowed on site"),
        (_set_step_token(0, 5, "{home}"), "unknown placeholder"),
        (_set_step_token(0, 9, "{data:graph}"), "dataset the template does not use"),
        (_set_step_token(0, 8, "{work}/../subset.bcf"), "'..'"),
        (_set_step_token(1, 4, "%CHROM\\n"), "arguments differ"),
        (lambda spec: spec["outputs"][0].update(path="genotypes.tsv"), "must start with {work}/"),
        (lambda spec: spec["outputs"][0].update({"class": AGGREGATE}), "differs from the template class"),
        (lambda spec: spec["outputs"][0].update({"class": "https://example.org/Anything"}), "not an artifact class"),
        (lambda spec: spec["outputs"][0].update(release=False), "release differs"),
        (lambda spec: spec["steps"][1].update(stdout="/tmp/out.tsv"), "must start with {work}/"),
        (lambda spec: spec["resources"].update(cpus=64), "cpus = 64 exceeds"),
        (lambda spec: spec["resources"].update(mem_gb=512), "mem_gb = 512 exceeds"),
        (lambda spec: spec["resources"].update(wall_seconds=10**6), "wall_seconds = 1000000 exceeds"),
        (lambda spec: spec["resources"].update(wall_seconds=5), "estimated wall time"),
        (lambda spec: spec.update(threads=16), "threads = 16 exceeds"),
        (lambda spec: spec.update(region=None), "region is required"),
        (lambda spec: spec.update(region="chr1;1-2"), "region:"),
        (lambda spec: spec.update(site="ddbj"), "targets site"),
        (lambda spec: spec.update(workflow="rm-everything"), "unknown workflow"),
        (lambda spec: spec["steps"].pop(), "steps must be"),
    ],
)
def test_validator_refuses(mutate, message):
    problems = problems_after(mutate)
    assert any(message in problem for problem in problems), problems


def test_validator_refuses_site_and_sample_problems():
    assert any("disabled" in p for p in problems_after(lambda spec: None, site=local_site(enabled=False)))
    no_graph = local_site(datasets=("vcf",), paths={"vcf": "/data/x.vcf.gz"})
    assert any("dataset graph does not reside" in p for p in problems_after(lambda spec: None, template="deconstruct-region", site=no_graph))
    assert any("sample name" in p for p in problems_after(lambda spec: None, samples=["ksa001;x"]))
    too_long = Region("GRCh38", "chr6", 0, 2_000_000)
    assert any("exceeds the deconstruct-region limit" in p for p in problems_after(lambda spec: None, template="deconstruct-region", region=too_long))
    small_site = local_site(max_wall_seconds=100)
    assert any("exceeds the site workstation limit of 100s" in p for p in problems_after(lambda spec: None, template="deconstruct-region", site=small_site))
    default_limits = Site("workstation", "local", datasets=("graph",), paths={"graph": "/data/g.gbz"})
    refused = problems_after(lambda spec: None, template="deconstruct-region", site=default_limits)
    assert any("mem_gb = 48 exceeds" in p for p in refused) and any("wall_seconds = 7200 exceeds" in p for p in refused)


def test_render_substitutes_placeholders_with_one_based_region():
    site = local_site()
    spec = plan("genotype-export", region=Region("GRCh38", "chr6", 29940000, 29990000), site=site, samples=SAMPLES)
    job = render(spec, site, work_dir="/w/", samples=SAMPLES, reference_path_template="{assembly}#0#{chrom}")
    assert job.steps[0].argv == ["bcftools", "view", "-r", "chr6:29940001-29990000", "-s", "ksa001,ksa002", "-Ob", "-o", "/w/subset.bcf", "/data/x.vcf.gz"]
    assert job.steps[1].stdout == "/w/genotypes.tsv" and job.outputs[0]["path"] == "/w/genotypes.tsv"
    decon = render(plan("deconstruct-region", region=REGION, site=site, samples=SAMPLES, threads=2), site, work_dir="/w", samples=SAMPLES, reference_path_template="{assembly}#0#{chrom}")
    assert decon.steps[0].argv == ["vg", "deconstruct", "-P", "GRCh38#0#chr1", "-C", "-a", "-t", "2", "/data/g.gbz"]
    assert decon.steps[1].argv[:3] == ["bcftools", "view", "-t"] and decon.steps[1].argv[3] == "chr1:1-27"
    assert decon.resources == {"cpus": 4, "mem_gb": 48, "wall_seconds": 7200}


# Local execution ---------------------------------------------------------------------------------


def _vcf_town(towns, toy_data, name="ubar"):
    return town_with(towns, name, f"""
[[sites]]
name = "workstation"
driver = "local"
datasets = ["vcf"]
tools = ["bcftools"]
paths = {{ vcf = "{toy_data['vcf']}" }}

[[sites]]
name = "ddbj"
driver = "ssh"
host = "ddbj"
workdir = "/home/asianhla/wasteland"
enabled = false
datasets = ["graph", "vcf"]
""")


@needs_bcftools
def test_genotype_export_runs_locally(towns, toy_data, tmp_path):
    town = _vcf_town(towns, toy_data)
    out = tmp_path / "export"
    result = run_task(town, template_name="genotype-export", region=REGION, out_dir=out)
    assert result["site"] == "workstation" and result["variant_count"] == 3 and result["sample_count"] == 2
    assert [(o["name"], o["class"]) for o in result["outputs"]] == [("genotypes.tsv", INDIVIDUAL)]
    table = Path(result["outputs"][0]["path"])
    assert table.parent == out and result["outputs"][0]["digest"].startswith("sha256:")
    header = table.read_text(encoding="utf-8").splitlines()[0]
    assert "ksa001" in header and "ksa002" in header and "NA18940" not in header
    assert not (out / "work").exists() and not list(out.rglob("subset.bcf"))
    assert Path(result["artifact"]).exists() and len(result["provenance"]["commands"]) == 2


@needs_bcftools
def test_allele_frequency_releases_only_aggregates(towns, toy_data, tmp_path):
    town = _vcf_town(towns, toy_data)
    out = tmp_path / "af"
    result = run_task(town, template_name="allele-frequency", region=REGION, out_dir=out)
    assert [(o["name"], o["class"]) for o in result["outputs"]] == [("allele_frequencies.tsv", AGGREGATE)]
    assert not list(out.rglob("genotypes.tsv")) and not (out / "work").exists()
    lines = Path(result["outputs"][0]["path"]).read_text(encoding="utf-8").splitlines()
    assert lines[0].split("\t") == ["CHROM", "POS", "REF", "ALT", "allele_count", "allele_number", "alt_frequency"]
    assert "ksa" not in "\n".join(lines)
    rows = {line.split("\t")[1]: line.split("\t")[4:6] for line in lines[1:]}
    assert rows == {"11": ["1", "4"], "14": ["1", "4"], "20": ["0", "4"]}
    assert result["variant_count"] == 3 and result["sample_count"] == 2 and result["mean_alt_frequency"] == pytest.approx(0.166667)


def test_pick_site_explains_why_sites_are_not_used(towns, toy_data):
    town = _vcf_town(towns, toy_data)
    site, diagnostics = pick_site(town, "deconstruct-region")
    assert site is None
    assert {d["site"]: d["detail"] for d in diagnostics} == {"workstation": "does not hold graph", "ddbj": "disabled in town.toml"}
    with pytest.raises(ComputeError, match="no reachable site holds graph"):
        run_task(town, template_name="deconstruct-region", region=REGION, out_dir=Path("/nonexistent/never-created"))


def test_run_task_revalidates_a_proposed_spec(towns, toy_data, tmp_path):
    town = _vcf_town(towns, toy_data)
    site = load_sites(town)[0]
    spec = plan("genotype-export", region=REGION, site=site, samples=list(town.samples))
    spec["outputs"][0]["class"] = AGGREGATE
    with pytest.raises(ComputeError, match="workflow rejected: .*differs from the template class"):
        run_task(town, template_name="genotype-export", region=REGION, out_dir=tmp_path / "x", site=site, spec=spec)
    assert not (tmp_path / "x").exists()


def test_local_driver_enforces_wall_time_and_reports_failures(tmp_path):
    slow = RenderedJob("t", [Step("sleep", ["sleep", "5"])], str(tmp_path / "w"), [], {"cpus": 1, "mem_gb": 1, "wall_seconds": 1})
    with pytest.raises(ComputeError, match="wall-time limit of 1s"):
        LocalDriver().run(slow, fetch_to=tmp_path)
    failing = RenderedJob("t", [Step("fail", ["sh", "-c", "echo broken >&2; exit 3"])], str(tmp_path / "w"), [], {"cpus": 1, "mem_gb": 1, "wall_seconds": 30})
    with pytest.raises(ComputeError, match="exit code 3: broken") as caught:
        LocalDriver().run(failing, fetch_to=tmp_path)
    assert caught.value.log[0]["returncode"] == 3


@needs_vg
@needs_bcftools
def test_deconstruct_region_on_the_toy_graph(towns, toy_data, tmp_path):
    if toy_data["gbz"] is None:
        pytest.skip("toy gbz not built")
    town = towns["ubar"]
    site = Site("workstation", "local", datasets=("graph",), paths={"graph": str(toy_data["gbz"])}, tools=("vg", "bcftools"),
                max_mem_gb=64, max_wall_seconds=7200)
    result = run_task(town, template_name="deconstruct-region", region=REGION, out_dir=tmp_path / "decon", site=site)
    assert result["variant_count"] == 1
    assert [(o["name"], o["class"]) for o in result["outputs"]] == [("deconstruct.vcf", INDIVIDUAL)]
    vcf = Path(result["outputs"][0]["path"]).read_text(encoding="utf-8")
    assert "\nchr1\t11\t" in vcf and not list((tmp_path / "decon").rglob("contig.vcf"))


# SSH driver against stand-in executables ---------------------------------------------------------

FAKE_SSH = """#!/bin/sh
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
host="$1"; shift
printf '%s\\n' "ssh $host $*" >> "$FAKE_LOG"
exec sh -c "$*"
"""
FAKE_SCP = """#!/bin/sh
while [ "$#" -gt 2 ]; do shift; done
printf '%s\\n' "scp $1 $2" >> "$FAKE_LOG"
cp "${1#*:}" "$2"
"""
FAKE_TIMEOUT = """#!/bin/sh
shift
cp "$2" "$FAKE_LOG.jobsh"
exec "$@"
"""
FAKE_SBATCH = """#!/bin/sh
printf '%s\\n' "sbatch $*" >> "$FAKE_LOG"
for last; do :; done
cp "$last" "$FAKE_LOG.jobsh"
sh "$last" > /dev/null 2>&1
echo 4242
"""
FAKE_SACCT = """#!/bin/sh
printf '%s\\n' "sacct $*" >> "$FAKE_LOG"
echo "${FAKE_STATE:-COMPLETED}"
"""
FAKE_SCANCEL = """#!/bin/sh
printf '%s\\n' "scancel $*" >> "$FAKE_LOG"
"""


@pytest.fixture
def fake_remote(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, text in {"ssh": FAKE_SSH, "scp": FAKE_SCP, "timeout": FAKE_TIMEOUT, "sbatch": FAKE_SBATCH, "sacct": FAKE_SACCT, "scancel": FAKE_SCANCEL}.items():
        path = bin_dir / name
        path.write_text(text, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log = tmp_path / "remote.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    remote = tmp_path / "remote host"
    remote.mkdir()
    return {"log": log, "remote": remote}


def _echo_job(work):
    return RenderedJob(
        "echo",
        [Step("write", ["printf", "%s\\n", "hello world; not a command"], stdout=f"{work}/out file.txt")],
        work,
        [{"name": "out file.txt", "path": f"{work}/out file.txt", "class": AGGREGATE, "release": True, "stage": "job"}],
        {"cpus": 1, "mem_gb": 1, "wall_seconds": 600},
    )


@pytest.mark.parametrize("scheduler", ["none", "slurm"])
def test_ssh_driver_uploads_quoted_script_runs_and_fetches(fake_remote, tmp_path, scheduler):
    work = str(fake_remote["remote"] / "job-1")
    site = Site("ddbj-test", "ssh", host="ddbj-test", workdir=str(fake_remote["remote"]), scheduler=scheduler)
    driver = SshDriver(site, poll_seconds=0)
    job = _echo_job(work)
    result = driver.run(job, fetch_to=tmp_path / "fetched")
    fetched = result.outputs["out file.txt"]
    assert fetched.read_text(encoding="utf-8") == "hello world; not a command\n"
    uploaded = Path(f"{fake_remote['log']}.jobsh").read_text(encoding="utf-8")
    assert uploaded == driver.script(job)
    assert shlex.join(["printf", "%s\\n", "hello world; not a command"]) + " > " + shlex.quote(f"{work}/out file.txt") in uploaded
    log = fake_remote["log"].read_text(encoding="utf-8")
    assert "cat > " in log and f"scp ddbj-test:{work}/out file.txt" in log
    if scheduler == "slurm":
        assert result.backend_id == "4242"
        assert "sbatch --parsable --time=00:10:00 --cpus-per-task=1 --mem=1G" in log and "sacct -j 4242 -n -X -o State" in log
    else:
        assert result.backend_id is None and "timeout 600 sh" in log
    assert not Path(work).exists()


def test_ssh_driver_reports_failed_slurm_jobs(fake_remote, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_STATE", "OUT_OF_MEMORY")
    site = Site("ddbj-test", "ssh", host="ddbj-test", workdir=str(fake_remote["remote"]), scheduler="slurm")
    with pytest.raises(ComputeError, match="ended in state OUT_OF_MEMORY"):
        SshDriver(site, poll_seconds=0).run(_echo_job(str(fake_remote["remote"] / "job-2")), fetch_to=tmp_path / "fetched")


@needs_bcftools
def test_allele_frequency_through_the_ssh_driver(fake_remote, towns, toy_data, tmp_path):
    town = towns["ubar"]
    site = Site("ddbj-test", "ssh", host="ddbj-test", workdir=str(fake_remote["remote"]), scheduler="slurm",
                datasets=("vcf",), paths={"vcf": str(toy_data["vcf"])}, tools=("bcftools",))
    out = tmp_path / "ssh-af"
    result = run_task(town, template_name="allele-frequency", region=REGION, out_dir=out, site=site, driver=SshDriver(site, poll_seconds=0))
    assert result["site"] == "ddbj-test" and result["provenance"]["backend_id"] == "4242"
    assert [o["name"] for o in result["outputs"]] == ["allele_frequencies.tsv"] and result["variant_count"] == 3
    assert not list(out.rglob("genotypes.tsv")) and not (out / "fetched").exists()
    assert list(fake_remote["remote"].iterdir()) == []


def test_ssh_driver_hops_to_a_submit_host(fake_remote, tmp_path):
    work = str(fake_remote["remote"] / "job-3")
    site = Site("ddbj-test", "ssh", host="gw-test", submit_host="a001", partition="epyc", workdir=str(fake_remote["remote"]), scheduler="slurm")
    driver = SshDriver(site, poll_seconds=0)
    result = driver.run(_echo_job(work), fetch_to=tmp_path / "fetched")
    assert result.outputs["out file.txt"].read_text(encoding="utf-8") == "hello world; not a command\n"
    log = fake_remote["log"].read_text(encoding="utf-8")
    assert "ssh gw-test ssh -o BatchMode=yes a001" in log and "\nssh a001 " in "\n" + log
    assert "--partition=epyc" in log and "sacct -j 4242" in log
    assert "scp " not in log and f"cat {shlex.quote(work + '/out file.txt')}" in log
    assert all(item["argv"][3] == "gw-test" and item["argv"][4].startswith("ssh -o BatchMode=yes a001 ") for item in result.log if item["argv"][0] == "ssh")
    assert not Path(work).exists()


def test_submit_host_config_is_validated(towns):
    from pangenome_town import config

    town = towns["yamatai"]
    path = town.city_root / "town.toml"
    base = path.read_text()
    path.write_text(base + '\n[[sites]]\nname = "ddbj"\ndriver = "ssh"\nhost = "ddbj"\nsubmit_host = "a001"\npartition = "epyc"\nworkdir = "/home/x/wasteland"\nscheduler = "slurm"\n', encoding="utf-8")
    site = load_sites(config.load(path))[0]
    assert (site.submit_host, site.partition) == ("a001", "epyc")
    for bad in ('submit_host = "a001; rm -rf /"', 'partition = "epyc --wrap=id"'):
        path.write_text(base + f'\n[[sites]]\nname = "ddbj"\ndriver = "ssh"\nhost = "ddbj"\n{bad}\nworkdir = "/home/x/wasteland"\n', encoding="utf-8")
        with pytest.raises(ComputeError):
            load_sites(config.load(path))
    path.write_text(base + '\n[[sites]]\nname = "box"\ndriver = "local"\nsubmit_host = "a001"\n', encoding="utf-8")
    with pytest.raises(ComputeError, match="submit_host needs the ssh driver"):
        load_sites(config.load(path))


def test_reachable_checks_the_submit_host_through_the_gateway():
    from pangenome_town.compute.sites import clear_reachability_cache

    clear_reachability_cache()
    seen = []

    def runner(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    site = Site("ddbj", "ssh", host="ddbj", submit_host="a001", workdir="/home/x", scheduler="slurm")
    ok, detail = reachable(site, runner=runner, cache_seconds=0)
    assert ok and "ddbj then a001" in detail
    assert seen[0][-2] == "ddbj" and seen[0][-1] == "ssh -o BatchMode=yes -o ConnectTimeout=10 a001 true"


@needs_bcftools
def test_remote_allele_frequency_keeps_genotypes_on_the_site(fake_remote, towns, toy_data, tmp_path):
    town = towns["ubar"]
    remote_site = Site("ddbj-test", "ssh", host="gw-test", submit_host="a001", workdir=str(fake_remote["remote"]), scheduler="slurm",
                       datasets=("vcf",), paths={"vcf": str(toy_data["vcf"])}, tools=("bcftools",))
    local_site = Site("box", "local", datasets=("vcf",), paths={"vcf": str(toy_data["vcf"])}, tools=("bcftools",))
    remote = run_task(town, template_name="allele-frequency", region=REGION, out_dir=tmp_path / "remote", site=remote_site,
                      driver=SshDriver(remote_site, poll_seconds=0))
    local = run_task(town, template_name="allele-frequency", region=REGION, out_dir=tmp_path / "local", site=local_site)
    assert Path(remote["outputs"][0]["path"]).read_text(encoding="utf-8") == Path(local["outputs"][0]["path"]).read_text(encoding="utf-8")
    assert remote["variant_count"] == local["variant_count"] and remote["mean_alt_frequency"] == local["mean_alt_frequency"]
    assert [step["step"] for step in remote["provenance"]["commands"] if step["step"].startswith("fetch:")] == ["fetch:allele_frequencies.tsv"]
    script = Path(f"{fake_remote['log']}.jobsh").read_text(encoding="utf-8")
    assert "awk -F" in script and "rm -f" in script and "genotypes.tsv" in script
    assert not list(tmp_path.rglob("genotypes.tsv"))


def test_pick_site_requires_the_controlled_dataset_the_task_uses(towns, toy_data):
    from pangenome_town.compute.sites import clear_reachability_cache

    clear_reachability_cache()
    town = towns["yamatai"]
    path = town.city_root / "town.toml"
    path.write_text(path.read_text() + f"""
[[sites]]
name = "workstation"
driver = "local"
datasets = ["vcf"]
paths = {{ vcf = "{toy_data['vcf']}" }}
tools = ["bcftools"]

[[sites]]
name = "cluster"
driver = "local"
datasets = ["vcf", "jpt-individual-genotypes"]
paths = {{ vcf = "{toy_data['vcf']}", "jpt-individual-genotypes" = "{toy_data['vcf']}" }}
tools = ["bcftools"]
""", encoding="utf-8")
    town = config.load(path)
    assert pick_site(town, "allele-frequency")[0].name == "workstation"
    site, diagnostics = pick_site(town, "allele-frequency", ("jpt-individual-genotypes",))
    assert site.name == "cluster"
    assert any(item["site"] == "workstation" and "does not hold jpt-individual-genotypes" in item["detail"] for item in diagnostics)
