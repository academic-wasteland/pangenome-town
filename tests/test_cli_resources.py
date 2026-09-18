import json
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from pangenome_town import cli, cli_resources, config
from pangenome_town.authority import AUTHORITY, registrar

PG = "https://w3id.org/academic-wasteland/pangenome-town/contract/"
HOLDER = "https://orcid.org/0000-0000-0000-0002"


@pytest.fixture
def camelot(tmp_path, monkeypatch):
    city = tmp_path / "camelot"
    city.mkdir()
    (city / "town.toml").write_text(f"""
[town]
name = "camelot"
display = "Camelot"
kind = "authority"

[town.peers]
ubar = "ubar"

[exchange]
db = "{tmp_path / 'exchange.db'}"
supervisor_url = "http://127.0.0.1:1"
state_dir = "{tmp_path / 'state'}"

[authority]
registry = "registry"
key_dir = "{tmp_path / 'keys'}"
operator = "https://orcid.org/0000-0000-0000-0001"
""", encoding="utf-8")
    monkeypatch.setattr(cli_resources, "HOLDERS_DIR", tmp_path / "holders")
    return city / "town.toml"


def run(capsys, *argv):
    code = cli.main([str(item) for item in argv])
    out = capsys.readouterr().out
    try:
        return code, json.loads(out)
    except json.JSONDecodeError:
        return code, out


@pytest.fixture
def registrar_url(camelot):
    registry = cli_resources.registry_for(config.load(camelot), SimpleNamespace(registry=None, key_dir=None))
    server = ThreadingHTTPServer(("127.0.0.1", 0), registrar.make_handler(registry))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


def _issuers(capsys, town):
    assert run(capsys, "--town", town, "authority", "init-issuer", "council", "--name", "Council", "--role", "AccreditationCouncil")[0] == 0
    assert run(capsys, "--town", town, "authority", "init-issuer", "dac", "--name", "DAC", "--role", "DataAccessCommittee")[0] == 0
    code, accreditation = run(capsys, "--town", town, "authority", "accredit", "--by", "council", "--subject", "dac", "--roles", "DataAccessCommittee")
    assert code == 0 and "Accreditation" in accreditation["type"]


def test_authority_town_needs_no_samples(camelot):
    town = config.load(camelot)
    assert town.kind == "authority" and town.samples == () and town.population == ""


def test_human_decision_flow_over_http(camelot, registrar_url, capsys, tmp_path):
    town = camelot
    _issuers(capsys, town)
    code, new = run(capsys, "--town", town, "holder", "new", "--holder", HOLDER)
    assert code == 0 and new["publicKey"].startswith("ed25519:")
    assert (tmp_path / "holders" / f"{new['slug']}.key").stat().st_mode & 0o777 == 0o600
    code, application = run(capsys, "--town", town, "holder", "apply", "--holder", HOLDER, "--registrar", registrar_url,
                            "--type", "DataAccessAuthorization", "--issuer", "dac", "--scope", f"{PG}AggregateFrequencyScope",
                            "--dataset", "https://w3id.org/academic-wasteland/ubar/datasets/ksa-individual-genotypes", "--purpose", "allele frequencies")
    assert code == 0 and application["state"] == "pending"
    assert run(capsys, "--town", town, "holder", "fetch", "--holder", HOLDER, "--registrar", registrar_url, application["id"])[0] == 1
    assert run(capsys, "--town", town, "authority", "applications", "--state", "pending", "--check")[0] == 0
    code, notified = run(capsys, "--town", town, "authority", "notify", "--dry-run")
    assert code == 0 and notified["notified"][0]["application"] == application["id"]
    code, issued = run(capsys, "--town", town, "authority", "approve", application["id"])
    assert code == 0 and issued["credentialSubject"]["id"] == HOLDER and issued["issuer"] == f"{AUTHORITY}issuers/dac"
    code, stored = run(capsys, "--town", town, "holder", "fetch", "--holder", HOLDER, "--registrar", registrar_url, application["id"])
    assert code == 0 and stored["id"] == issued["id"]
    code, wallet = run(capsys, "--town", town, "holder", "wallet", "--holder", HOLDER)
    assert [item["scope"] for item in wallet["credentials"]] == [f"{PG}AggregateFrequencyScope"]
    checker = registrar.http_status_checker({AUTHORITY: registrar_url})
    assert checker(issued) == "active"
    code, revoked = run(capsys, "--town", town, "authority", "revoke", issued["id"])
    assert code == 0 and revoked["status"] == "revoked" and checker(issued) == "revoked"
    assert run(capsys, "--town", town, "authority", "applications", "--state", "pending", "--check")[0] == 1


def test_apply_dry_run_and_bad_application(camelot, registrar_url, capsys):
    _issuers(capsys, camelot)
    run(capsys, "--town", camelot, "holder", "new", "--holder", HOLDER)
    code, dry = run(capsys, "--town", camelot, "holder", "apply", "--holder", HOLDER, "--registrar", registrar_url, "--type", "EthicsApproval",
                    "--issuer", "dac", "--scope", f"{PG}AggregateFrequencyScope", "--purpose", "p", "--dry-run")
    assert code == 0 and dry["dry_run"] and dry["application"]["holder_key"].startswith("ed25519:")
    code, error = run(capsys, "--town", camelot, "holder", "apply", "--holder", HOLDER, "--registrar", registrar_url, "--type", "EthicsApproval",
                      "--issuer", "nobody", "--scope", f"{PG}AggregateFrequencyScope", "--purpose", "p")
    assert code == 2 and "unknown issuer" in error["detail"]


def test_authority_card_publishes_keys_not_secrets(camelot, capsys, tmp_path):
    _issuers(capsys, camelot)
    code, card = run(capsys, "--town", camelot, "authority", "agent-card")
    assert code == 0 and card["kind"] == "authority"
    text = json.dumps(card)
    assert all(issuer["publicKey"].startswith("ed25519:") for issuer in card["issuers"])
    assert str(tmp_path / "keys") not in text and ".key" not in text
    dac = next(issuer for issuer in card["issuers"] if issuer["slug"] == "dac")
    assert dac["accreditedBy"] == [f"{AUTHORITY}issuers/council"]


def test_approve_twice_is_refused(camelot, registrar_url, capsys):
    _issuers(capsys, camelot)
    run(capsys, "--town", camelot, "holder", "new", "--holder", HOLDER)
    _, application = run(capsys, "--town", camelot, "holder", "apply", "--holder", HOLDER, "--registrar", registrar_url, "--type", "EthicsApproval",
                         "--issuer", "dac", "--scope", f"{PG}AggregateFrequencyScope", "--purpose", "p")
    assert run(capsys, "--town", camelot, "authority", "approve", application["id"])[0] == 0
    code, error = run(capsys, "--town", camelot, "authority", "approve", application["id"])
    assert code == 2 and "not pending" in error["detail"]


def test_notified_approval_works_from_elsewhere_and_requires_review(camelot, capsys, tmp_path, monkeypatch):
    import os
    import shlex
    import subprocess
    import sys
    # A configuration need not live inside city_root; preserve the actual loaded file.
    separate = tmp_path / 'separate config'
    separate.mkdir()
    special = separate / 'authority config.toml'
    special.write_text(camelot.read_text().replace('[town]', f'[town]\ncity_root = "{camelot.parent}"').replace('registry = "registry"', f'registry = "{camelot.parent / "registry"}"'))
    _issuers(capsys, special)
    town = config.load(special)
    registry = cli_resources.registry_for(town, SimpleNamespace(registry=None, key_dir=None))
    from pangenome_town.authority import keys
    key = keys.generate()
    application = registry.apply(holder=HOLDER, holder_key_text=keys.public_key_text(key), credential_type='Qualification',
                                 issuer_slug='dac', subject_fields={'qualification': 'SyntheticRelayTestOnly'}, purpose='Synthetic test only')
    application['relay_sender'] = 'zerzura'
    registry._app_path(application['id']).write_text(json.dumps(application))
    sent = []
    monkeypatch.setattr(cli_resources.subprocess, 'run', lambda args, **kwargs: sent.append(args))
    cli_resources.notify_pending(town, registry)
    body = sent[0][sent[0].index('-m') + 1]
    command = cli_resources.review_commands(town, application)['approve']
    assert command in body and shlex.quote(str(special)) in command
    from pangenome_town.dashboard import DashboardState
    state = DashboardState([town])
    monkeypatch.setattr(state, 'mail_list', lambda name: {'messages': []})
    assert state.decisions()['authorities'][0]['pending'][0]['commands']['approve'] == command
    monkeypatch.undo()
    # Invoke the real CLI in a subprocess, from an unrelated directory.
    command = command.replace('pangenome-town ', shlex.quote(sys.executable)+' -m pangenome_town.cli ', 1)
    env = dict(os.environ)
    env.pop('PT_TOWN_TOML', None); env.pop('EVIDENCE_REF', None)
    refused = subprocess.run(['bash', '-c', command], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert refused.returncode != 0 and 'Set EVIDENCE_REF' in refused.stderr
    assert registry.application(application['id'])['state'] == 'pending'
    env['EVIDENCE_REF'] = 'private-review:synthetic-test'
    approved = subprocess.run(['bash', '-c', command], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert approved.returncode == 0, approved.stderr + approved.stdout
    assert registry.application(application['id'])['state'] == 'approved'
