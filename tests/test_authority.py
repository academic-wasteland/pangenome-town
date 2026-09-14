import http.client
import json
import socket
import stat
import threading
from datetime import UTC, datetime, timedelta

import pytest

from pangenome_town.authority import AUTHORITY, issuer_iri, keys, registrar
from pangenome_town.authority import credentials as cred

NOW = datetime(2026, 9, 14, 9, 0, 0, tzinfo=UTC)
HOLDER = "https://orcid.org/0000-0001-8149-5890"
AUDIENCE = "https://w3id.org/academic-wasteland/ubar/"
DATASET = "https://w3id.org/academic-wasteland/ubar/datasets/ksa-individual-genotypes"
SCOPE = "https://w3id.org/academic-wasteland/pangenome-town/contract/AggregateFrequencyScope"


def task(identifier="urn:uuid:11111111-1111-1111-1111-111111111111", **extra):
    return {"@id": identifier, "@type": ["ResearchTask"], "taskType": "https://example.org/AlleleFrequencyTask", **extra}


@pytest.fixture
def world():
    council, irb, dac, holder = keys.generate(), keys.generate(), keys.generate(), keys.generate()
    ids = {"council": issuer_iri("ethics-council"), "irb": issuer_iri("wasteland-irb"), "dac": issuer_iri("ubar-dac")}
    acc_irb = cred.accreditation(accreditor=ids["council"], accreditor_key=council, subject_issuer=ids["irb"],
                                 subject_public_key=keys.public_key_text(irb), roles=["EthicsBoard"], now=NOW)
    acc_dac = cred.accreditation(accreditor=ids["council"], accreditor_key=council, subject_issuer=ids["dac"],
                                 subject_public_key=keys.public_key_text(dac), roles=["DataAccessCommittee"], now=NOW)
    subject = {"id": HOLDER, "holderKey": keys.public_key_text(holder), "dataset": DATASET, "scope": SCOPE}
    access = cred.issue(issuer=ids["dac"], issuer_key=dac, types="DataAccessAuthorization", subject=subject, now=NOW)
    ethics = cred.issue(issuer=ids["irb"], issuer_key=irb, types="EthicsApproval", subject={**subject, "protocol": "WIRB-1"}, now=NOW)
    return {"keys": {"council": council, "irb": irb, "dac": dac, "holder": holder}, "ids": ids,
            "accreditations": [acc_irb, acc_dac], "access": access, "ethics": ethics,
            "anchors": {ids["council"]: keys.public_key_text(council)}}


def presentation(world, the_task, *, credentials=None, accreditations=None, audience=AUDIENCE, now=NOW, holder_key=None):
    return cred.present(holder=HOLDER, holder_key=holder_key or world["keys"]["holder"],
                        credentials=credentials if credentials is not None else [world["access"], world["ethics"]],
                        accreditations=accreditations if accreditations is not None else world["accreditations"],
                        task=the_task, audience=audience, now=now)


def verify(world, doc, the_task, **options):
    options.setdefault("now", NOW + timedelta(minutes=1))
    return cred.verify_presentation(doc, the_task, audience=AUDIENCE, anchors=world["anchors"], **options)


def test_sign_verify_round_trip_and_tamper():
    key = keys.generate()
    signed = keys.sign({"b": 1, "a": "ü"}, key, "urn:x#key-1")
    assert keys.verify(signed, key.public_key())
    assert keys.verify({**signed, "b": 1}, key.public_key())
    assert not keys.verify({**signed, "b": 2}, key.public_key())
    assert not keys.verify({**signed, "proof": {**signed["proof"], "type": "Other"}}, key.public_key())
    assert not keys.verify({"a": 1}, key.public_key())
    assert not keys.verify({**signed, "proof": {**signed["proof"], "proofValue": "!!!"}}, key.public_key())
    assert not keys.verify("nope", key.public_key())
    assert keys.verify(signed, keys.parse_public(keys.public_key_text(key)))
    with pytest.raises(keys.AuthorityKeyError):
        keys.parse_public("rsa:abc")


def test_key_file_permissions_and_no_overwrite(tmp_path):
    key = keys.generate()
    path = tmp_path / "vault" / "issuer.key"
    keys.save_private(key, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert keys.public_key_text(keys.load_private(path)) == keys.public_key_text(key)
    with pytest.raises(keys.AuthorityKeyError):
        keys.save_private(keys.generate(), path)
    keys.save_private(keys.generate(), path, overwrite=True)


def test_happy_path_anchor_accredits_dac_and_irb(world):
    the_task = task()
    result = verify(world, presentation(world, the_task), the_task)
    assert result.ok, result.as_dict()
    assert not result.problems
    assert {c.types[1] for c in result.verified_credentials} == {"DataAccessAuthorization", "EthicsApproval"}
    assert set(result.accreditation_edges) == {(world["ids"]["irb"], world["ids"]["council"]), (world["ids"]["dac"], world["ids"]["council"])}
    assert result.key_sources[world["ids"]["dac"]] == "accreditation"
    assert result.key_sources[world["ids"]["council"]] == "anchor"
    access = next(c for c in result.credentials if "DataAccessAuthorization" in c.types)
    assert access.dataset == DATASET and access.scope == SCOPE and access.holder == HOLDER and access.status == "unchecked"
    json.dumps(result.as_dict())


def test_directory_issuer_verifies_signature_but_earns_no_edge(world):
    rogue = keys.generate()
    rogue_id = issuer_iri("rogue-dac")
    subject = {"id": HOLDER, "holderKey": keys.public_key_text(world["keys"]["holder"]), "dataset": DATASET, "scope": SCOPE}
    rogue_credential = cred.issue(issuer=rogue_id, issuer_key=rogue, types="DataAccessAuthorization", subject=subject, now=NOW)
    the_task = task()
    doc = presentation(world, the_task, credentials=[rogue_credential], accreditations=[])
    without = verify(world, doc, the_task)
    assert not without.ok and without.credentials[0].problems == ["issuer key unknown"]
    result = verify(world, doc, the_task, directory={rogue_id: keys.public_key_text(rogue)})
    assert result.ok and result.credentials[0].verified
    assert result.accreditation_edges == [] and result.key_sources[rogue_id] == "directory"


def test_expired_and_not_yet_valid(world):
    the_task = task()
    later = NOW + timedelta(days=31)
    result = verify(world, presentation(world, the_task, now=later), the_task, now=later)
    access = next(c for c in result.credentials if "DataAccessAuthorization" in c.types)
    assert not result.ok and any(p.startswith("expired at") for p in access.problems)
    early = NOW - timedelta(days=1)
    result = verify(world, presentation(world, the_task, now=early), the_task, now=early)
    assert not result.ok and any(p.startswith("not yet valid") for c in result.credentials for p in c.problems)
    assert any("accreditation" in p and "not yet valid" in p for p in result.problems)


def test_revocation_through_registry(tmp_path, world):
    registry = registrar.Registry(tmp_path / "public", tmp_path / "keys")
    registry.init_issuer("ubar-dac", "Ubar DAC (demo)", "DataAccessCommittee")
    holder_key = keys.public_key_text(world["keys"]["holder"])
    app = registry.apply(holder=HOLDER, holder_key_text=holder_key, credential_type="DataAccessAuthorization",
                         issuer_slug="ubar-dac", subject_fields={"dataset": DATASET, "scope": SCOPE}, purpose="allele frequencies")
    issued = registry.approve(app["id"], decided_by="operator")
    directory = registry.directory()
    the_task = task()
    now = datetime.now(UTC)
    checker = registrar.status_checker_for(registry)
    doc = presentation(world, the_task, credentials=[issued], accreditations=[], now=now)
    ok = cred.verify_presentation(doc, the_task, audience=AUDIENCE, anchors={}, directory=directory, status_checker=checker, now=now)
    assert ok.ok and ok.credentials[0].status == "active"
    registry.revoke(issued["id"])
    revoked = cred.verify_presentation(doc, the_task, audience=AUDIENCE, anchors={}, directory=directory, status_checker=checker, now=now)
    assert not revoked.ok and revoked.credentials[0].status == "revoked" and "revoked" in revoked.credentials[0].problems


def test_unknown_status_required_versus_best_effort(world):
    the_task = task()
    doc = presentation(world, the_task)
    required = verify(world, doc, the_task, status_checker=lambda _: "unknown")
    assert not required.ok and "status unknown (registrar unreachable)" in required.credentials[0].problems
    best = verify(world, doc, the_task, status_checker=lambda _: "unknown", revocation="best-effort")
    assert best.ok and best.credentials[0].status == "unknown"

    def broken(_):
        raise RuntimeError("boom")

    assert not verify(world, doc, the_task, status_checker=broken).ok


def test_replay_edit_audience_staleness(world):
    the_task = task()
    doc = presentation(world, the_task)
    other = task("urn:uuid:22222222-2222-2222-2222-222222222222")
    replay = verify(world, doc, other)
    assert not replay.ok and "presentation is bound to another task" in replay.problems
    assert all(c.problems == ["presentation invalid"] for c in replay.credentials)
    edited = dict(the_task, taskType="https://example.org/IndividualGenotypeExportTask")
    assert "task digest mismatch" in verify(world, doc, edited).problems
    assert "audience mismatch" in cred.verify_presentation(doc, the_task, audience="https://w3id.org/academic-wasteland/yamatai/", anchors=world["anchors"], now=NOW).problems
    stale = verify(world, doc, the_task, now=NOW + timedelta(hours=1))
    assert not stale.ok and "presentation too old" in stale.problems
    future = verify(world, doc, the_task, now=NOW - timedelta(minutes=5))
    assert "presentation created in the future" in future.problems
    forged = {**doc, "nonce": "different"}
    assert "presentation signature invalid" in verify(world, forged, the_task).problems


def test_wrong_holder_key_and_other_holder(world):
    the_task = task()
    thief = keys.generate()
    stolen = verify(world, presentation(world, the_task, holder_key=thief), the_task)
    assert not stolen.ok and all("holder key mismatch" in c.problems for c in stolen.credentials)
    subject = {"id": "https://orcid.org/0000-0000-0000-0000", "holderKey": keys.public_key_text(world["keys"]["holder"]), "dataset": DATASET, "scope": SCOPE}
    foreign = cred.issue(issuer=world["ids"]["dac"], issuer_key=world["keys"]["dac"], types="DataAccessAuthorization", subject=subject, now=NOW)
    result = verify(world, presentation(world, the_task, credentials=[foreign]), the_task)
    assert not result.ok and "holder mismatch" in result.credentials[0].problems


def test_accreditation_is_not_a_holder_credential(world):
    the_task = task()
    result = verify(world, presentation(world, the_task, credentials=[world["accreditations"][0]]), the_task)
    assert not result.ok and "not a holder credential type" in result.credentials[0].problems


def test_key_conflict_between_directory_and_anchor(world):
    the_task = task()
    imposter = keys.public_key_text(keys.generate())
    result = verify(world, presentation(world, the_task), the_task, directory={world["ids"]["council"]: imposter})
    assert f"key conflict for {world['ids']['council']}" in result.problems
    assert result.accreditation_edges == [] and not result.ok


def test_conflicting_accreditation_key_is_not_an_edge(world):
    the_task = task()
    wrong = cred.accreditation(accreditor=world["ids"]["council"], accreditor_key=world["keys"]["council"], subject_issuer=world["ids"]["dac"],
                               subject_public_key=keys.public_key_text(keys.generate()), roles=["DataAccessCommittee"], now=NOW)
    result = verify(world, presentation(world, the_task, credentials=[world["access"]], accreditations=[world["accreditations"][1], wrong]), the_task)
    assert f"key conflict for {world['ids']['dac']}" in result.problems
    assert result.accreditation_edges == [(world["ids"]["dac"], world["ids"]["council"])]


def test_accreditation_chain_depth(world):
    board, board_id = keys.generate(), issuer_iri("regional-board")
    dac, dac_id = keys.generate(), issuer_iri("deep-dac")
    council_to_board = cred.accreditation(accreditor=world["ids"]["council"], accreditor_key=world["keys"]["council"], subject_issuer=board_id,
                                          subject_public_key=keys.public_key_text(board), roles=["Accreditor"], now=NOW)
    board_to_dac = cred.accreditation(accreditor=board_id, accreditor_key=board, subject_issuer=dac_id,
                                      subject_public_key=keys.public_key_text(dac), roles=["DataAccessCommittee"], now=NOW)
    subject = {"id": HOLDER, "holderKey": keys.public_key_text(world["keys"]["holder"]), "dataset": DATASET, "scope": SCOPE}
    deep = cred.issue(issuer=dac_id, issuer_key=dac, types="DataAccessAuthorization", subject=subject, now=NOW)
    the_task = task()
    doc = presentation(world, the_task, credentials=[deep], accreditations=[board_to_dac, council_to_board])
    result = verify(world, doc, the_task)
    assert result.ok and set(result.accreditation_edges) == {(board_id, world["ids"]["council"]), (dac_id, board_id)}
    shallow = verify(world, doc, the_task, max_depth=1)
    assert not shallow.ok and shallow.credentials[0].problems == ["issuer key unknown"]


@pytest.mark.parametrize("junk", [None, 42, "x", {}, {"type": "Presentation"}, {"type": "Presentation", "holderKey": "ed25519:AAAA", "credentials": [1, None], "accreditations": "no", "created": "yesterday"}])
def test_malformed_presentations_do_not_raise(world, junk):
    result = cred.verify_presentation(junk, task(), audience=AUDIENCE, anchors=world["anchors"], now=NOW)
    assert not result.ok and result.problems
    assert cred.verify_presentation(junk, "not a task", audience=AUDIENCE, anchors={"x": "garbage"}, now=NOW).ok is False


def test_registry_lifecycle(tmp_path):
    registry = registrar.Registry(tmp_path / "public", tmp_path / "keys")
    first = registry.init_issuer("ethics-council", "Wasteland Ethics Council (demo)", "TrustAnchor")
    again = registry.init_issuer("ethics-council", "renamed", "TrustAnchor")
    assert first == again and stat.S_IMODE((tmp_path / "keys" / "ethics-council.key").stat().st_mode) == 0o600
    registry.init_issuer("ubar-dac", "Ubar DAC (demo)", "DataAccessCommittee")
    registry.init_issuer("wasteland-irb", "Wasteland IRB (demo)", "EthicsBoard")
    registry.accredit("ethics-council", "ubar-dac", ["DataAccessCommittee"])
    assert [doc["issuer"] for doc in registry.accreditations_for(issuer_iri("ubar-dac"))] == [issuer_iri("ethics-council")]
    assert set(registry.directory()) == {issuer_iri(s) for s in ("ethics-council", "ubar-dac", "wasteland-irb")}
    with pytest.raises(registrar.RegistryError):
        registry.accredit("ubar-dac", "ubar-dac", [])
    holder = keys.generate()
    common = {"holder": HOLDER, "holder_key_text": keys.public_key_text(holder), "subject_fields": {"dataset": DATASET, "scope": SCOPE}, "purpose": "study"}
    app = registry.apply(credential_type="DataAccessAuthorization", issuer_slug="ubar-dac", **common)
    other = registry.apply(credential_type="EthicsApproval", issuer_slug="wasteland-irb", **common)
    assert {a["id"] for a in registry.applications("pending")} == {app["id"], other["id"]}
    issued = registry.approve(app["id"], decided_by="operator")
    assert registry.application(app["id"])["state"] == "approved" and registry.credential(issued["id"]) == issued
    assert issued["credentialSubject"]["holderKey"] == keys.public_key_text(holder)
    denied = registry.deny(other["id"], reason="protocol missing", decided_by="operator")
    assert denied["state"] == "denied" and registry.applications("pending") == []
    with pytest.raises(registrar.RegistryError):
        registry.approve(app["id"], decided_by="operator")
    for bad in ({"credential_type": "Accreditation", "issuer_slug": "ubar-dac"}, {"credential_type": "DataAccessAuthorization", "issuer_slug": "nobody"}):
        with pytest.raises(registrar.RegistryError):
            registry.apply(**common, **bad)
    with pytest.raises(registrar.RegistryError):
        registry.apply(**{**common, "holder_key_text": "nope"}, credential_type="EthicsApproval", issuer_slug="wasteland-irb")
    with pytest.raises(registrar.RegistryError):
        registry.application("../etc")
    assert registry.status(issued["id"]) == "active" and registry.status(f"{AUTHORITY}credentials/00000000-0000-0000-0000-000000000000") == "unknown"


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost")
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.unix_path)


def _call(path, method, route, body=None):
    connection = _UnixConnection(str(path))
    payload = json.dumps(body).encode() if body is not None else None
    connection.request(method, route, body=payload, headers={"Content-Type": "application/json"} if payload else {})
    response = connection.getresponse()
    data = json.loads(response.read().decode())
    connection.close()
    return response.status, data


def test_registrar_http_over_unix_socket(tmp_path):
    from pangenome_town.envoy import ThreadingUnixHTTPServer

    registry = registrar.Registry(tmp_path / "public", tmp_path / "keys")
    registry.init_issuer("ethics-council", "Council (demo)", "TrustAnchor")
    registry.init_issuer("ubar-dac", "Ubar DAC (demo)", "DataAccessCommittee")
    registry.accredit("ethics-council", "ubar-dac", ["DataAccessCommittee"])
    sock = tmp_path / "registrar.sock"
    server = ThreadingUnixHTTPServer(str(sock), registrar.make_handler(registry))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert _call(sock, "GET", "/healthz")[1]["ok"] is True
        status, body = _call(sock, "GET", "/v0/keys")
        assert status == 200 and {i["id"] for i in body["issuers"]} == {issuer_iri("ethics-council"), issuer_iri("ubar-dac")}
        dac = next(i for i in body["issuers"] if i["id"] == issuer_iri("ubar-dac"))
        assert dac["accreditations"][0]["issuer"] == issuer_iri("ethics-council")
        application = {"holder": HOLDER, "holder_key": keys.public_key_text(keys.generate()), "credential_type": "DataAccessAuthorization",
                       "issuer": "ubar-dac", "subject_fields": {"dataset": DATASET, "scope": SCOPE}, "purpose": "allele frequencies"}
        status, created = _call(sock, "POST", "/v0/applications", application)
        assert status == 201 and created["state"] == "pending"
        assert _call(sock, "GET", f"/v0/applications/{created['id']}")[1]["id"] == created["id"]
        assert _call(sock, "POST", "/v0/applications", {**application, "issuer": "nobody"})[0] == 422
        assert _call(sock, "POST", "/v0/applications", {**application, "extra": 1})[0] == 422
        assert _call(sock, "POST", "/v0/applications", {**application, "holder": 5})[0] == 422
        assert _call(sock, "POST", "/v0/approve", application)[0] == 404
        issued = registry.approve(created["id"], decided_by="operator")
        token = issued["id"].rsplit("/", 1)[1]
        assert _call(sock, "GET", f"/v0/status/{token}")[1]["status"] == "active"
        assert _call(sock, "GET", f"/v0/credentials/{token}")[1]["id"] == issued["id"]
        registry.revoke(issued["id"])
        assert _call(sock, "GET", f"/v0/status/{token}")[1]["status"] == "revoked"
        assert _call(sock, "GET", "/v0/status/not-a-token")[0] == 400
    finally:
        server.shutdown()
        server.server_close()
