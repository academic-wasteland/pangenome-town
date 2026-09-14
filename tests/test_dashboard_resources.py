import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from conftest import write_town

from pangenome_town import config, dashboard
from pangenome_town.authority import registrar

PG = "https://w3id.org/academic-wasteland/pangenome-town/contract/"
HOLDER = "https://orcid.org/0000-0000-0000-0002"


@pytest.fixture
def mixed_towns(tmp_path, toy_data):
    db = tmp_path / "exchange.db"
    ubar_path = write_town(tmp_path, "ubar", peers={"camelot": "camelot"}, samples=["ksa001", "ksa002"],
                           graph=toy_data["gbz"], vcf=toy_data["vcf"], db=db)
    ubar_path.write_text(ubar_path.read_text() + """
[[sites]]
name = "workstation"
driver = "local"
datasets = ["vcf"]
tools = ["bcftools"]

[[sites]]
name = "ddbj"
driver = "ssh"
host = "ddbj"
workdir = "/srv/wasteland"
enabled = false
datasets = ["graph", "vcf"]
""", encoding="utf-8")
    keys_dir = tmp_path / "keys"
    camelot = tmp_path / "camelot"
    camelot.mkdir()
    (camelot / "town.toml").write_text(f"""
[town]
name = "camelot"
display = "Camelot"
kind = "authority"

[town.peers]
ubar = "ubar"

[exchange]
db = "{db}"
supervisor_url = "http://127.0.0.1:1"
state_dir = "{tmp_path / 'camelot-state'}"

[authority]
registry = "registry"
key_dir = "{keys_dir}"
""", encoding="utf-8")
    registry = registrar.Registry(camelot / "registry", keys_dir)
    registry.init_issuer("council", "Council", "AccreditationCouncil")
    registry.init_issuer("dac", "DAC", "DataAccessCommittee")
    registry.accredit("council", "dac", ["DataAccessCommittee"])
    from pangenome_town.authority import keys

    holder_key = keys.public_key_text(keys.generate())
    first = registry.apply(holder=HOLDER, holder_key_text=holder_key, credential_type="DataAccessAuthorization", issuer_slug="dac",
                           subject_fields={"scope": f"{PG}AggregateFrequencyScope"}, purpose="x" * 500)
    second = registry.apply(holder=HOLDER, holder_key_text=holder_key, credential_type="EthicsApproval", issuer_slug="dac",
                            subject_fields={"scope": f"{PG}AggregateFrequencyScope"}, purpose="pending one")
    issued = registry.approve(first["id"], decided_by="https://orcid.org/0000-0000-0000-0001")
    registry.revoke(issued["id"])
    state = dashboard.DashboardState([config.load(ubar_path), config.load(camelot / "town.toml")])
    return {"state": state, "keys": keys_dir, "issued": issued, "pending": second}


def test_towns_view_handles_authority_town(mixed_towns):
    view = {town["name"]: town for town in mixed_towns["state"].towns_view()}
    assert view["camelot"]["kind"] == "authority"
    assert view["camelot"]["authority"] == {"issuers": 2, "pending_applications": 1}
    assert view["ubar"]["kind"] == "pangenome" and view["ubar"]["authority"] is None


def test_authority_api_shapes_without_secrets(mixed_towns):
    payload = mixed_towns["state"].authority()
    (camelot,) = payload["authorities"]
    assert {item["slug"] for item in camelot["issuers"]} == {"council", "dac"}
    dac = next(item for item in camelot["issuers"] if item["slug"] == "dac")
    assert set(dac) == {"id", "slug", "name", "role", "publicKey", "accreditedBy"} and dac["accreditedBy"][0].endswith("/issuers/council")
    states = {item["state"] for item in camelot["applications"]}
    assert states == {"approved", "pending"}
    assert all(len(item["purpose"]) <= 200 for item in camelot["applications"])
    credential = next(item for item in camelot["credentials"] if item["id"] == mixed_towns["issued"]["id"])
    assert credential["id"] == mixed_towns["issued"]["id"] and credential["status"] == "revoked"
    assert credential["type"] == ["DataAccessAuthorization"] and credential["holder"] == HOLDER
    text = json.dumps(payload)
    assert str(mixed_towns["keys"]) not in text and ".key" not in text and "holder_key" not in text


def test_sites_api_reports_reachability_and_templates(mixed_towns):
    payload = mixed_towns["state"].sites()
    (ubar,) = payload["towns"]
    sites = {item["site"]: item for item in ubar["sites"]}
    assert sites["ddbj"]["reachable"] is False and sites["ddbj"]["enabled"] is False
    assert set(sites["workstation"]) >= {"driver", "reachable", "detail", "datasets", "limits"}
    assert set(ubar["templates"]) == {"genotype-export", "allele-frequency", "deconstruct-region"}
    assert ubar["templates"]["deconstruct-region"] is None
    assert mixed_towns["state"].sites() == payload  # cached


def test_http_routes_over_loopback(mixed_towns):
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(mixed_towns["state"]))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        for route, key in (("/api/authority", "authorities"), ("/api/sites", "towns")):
            with urllib.request.urlopen(base + route, timeout=30) as response:
                assert key in json.loads(response.read())
        with urllib.request.urlopen(base + "/", timeout=30) as response:
            html = response.read().decode()
        assert "loadAuthority" in html and "gateChips" in html and "—" not in html
    finally:
        server.shutdown()
