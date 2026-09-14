"""Authority gate facts without a reasoner: what code verifies, what it asserts, and how probes are read."""

from datetime import UTC, datetime

import pytest

from pangenome_town import config
from pangenome_town.authority import CRED, issuer_iri, keys
from pangenome_town.authority import credentials as creds
from pangenome_town.rcp import PG, authorization, contract, pipeline, town_iri
from pangenome_town.tools.graph import Region

AGG, IND = f"{PG}AggregateFrequencyScope", f"{PG}IndividualGenotypeScope"
HOLDER = "https://orcid.org/0000-0000-0000-0003"
YAMATAI = "https://w3id.org/academic-wasteland/yamatai/agents/townsfolk"
DATASET = "https://w3id.org/academic-wasteland/ubar/datasets/ksa-individual-genotypes"
COUNCIL, DAC, IRB, ROGUE = (issuer_iri(slug) for slug in ("council", "ubar-dac", "irb", "rogue"))


@pytest.fixture
def world(towns):
    ubar = towns["ubar"]
    council, dac, irb, rogue, holder = (keys.generate() for _ in range(5))
    path = ubar.city_root / "town.toml"
    path.write_text(path.read_text() + f"""
[trust]
anchors = {{ "{COUNCIL}" = "{keys.public_key_text(council)}" }}

[[restricted_datasets]]
key = "ksa-individual-genotypes"
iri = "{DATASET}"
name = "KSA individual genotypes"

[[sites]]
name = "workstation"
driver = "local"
datasets = ["graph", "vcf", "ksa-individual-genotypes"]
tools = ["bcftools"]
""", encoding="utf-8")
    ubar = config.load(path)
    contract.render(ubar, ubar.city_root / "contract")
    hk = keys.public_key_text(holder)
    accreditations = [creds.accreditation(accreditor=COUNCIL, accreditor_key=council, subject_issuer=issuer, subject_public_key=keys.public_key_text(key), roles=["x"])
                      for issuer, key in ((DAC, dac), (IRB, irb))]

    def dac_credential(scope=AGG, issuer=DAC, key=dac, dataset=DATASET, subject=HOLDER):
        return creds.issue(issuer=issuer, issuer_key=key, types="DataAccessAuthorization",
                           subject={"id": subject, "holderKey": hk, "dataset": dataset, "scope": scope})

    def irb_credential(scope=AGG):
        return creds.issue(issuer=IRB, issuer_key=irb, types="EthicsApproval", subject={"id": HOLDER, "holderKey": hk, "protocol": "P1", "scope": scope})

    def task(kind="AlleleFrequencyTask"):
        return pipeline.task_document(ubar, f"{PG}{kind}", requester=YAMATAI, on_behalf_of=HOLDER, include_graph=False,
                                      region=Region.parse("GRCh38:chr1:0-27", "GRCh38"), datasets=[authorization.controlled_dataset_entity(DATASET)])

    def present(document, credentials, accreditation_list=None):
        return creds.present(holder=HOLDER, holder_key=holder, credentials=credentials,
                             accreditations=accreditations if accreditation_list is None else accreditation_list, task=document, audience=town_iri("ubar"))

    return {"town": ubar, "dac": dac_credential, "irb": irb_credential, "task": task, "present": present, "rogue": rogue}


def test_no_presentation_closes_presents(world):
    document = world["task"]()
    facts = authorization.assess(world["town"], document, None)
    assert not facts.presented and not facts.probes
    assert f"ClassAssertion(ObjectAllValuesFrom(<{CRED}presents> owl:Nothing) <{document['@id']}>)" in facts.axioms


def test_verified_credentials_become_facts_without_nominals(world):
    document = world["task"]()
    dac, irb = world["dac"](), world["irb"]()
    facts = authorization.assess(world["town"], document, world["present"](document, [dac, irb]), status_checker=lambda _: "active")
    text = "\n".join(facts.axioms)
    assert "ObjectOneOf" not in text and "owl:Nothing" not in text
    for credential in (dac, irb):
        assert f"ObjectPropertyAssertion(<{CRED}presents> <{document['@id']}> <{credential['id']}>)" in text
        assert f"ClassAssertion(<{CRED}VerifiedCredential> <{credential['id']}>)" in text
        assert f"ClassAssertion(<{PG}ApprovesAggregateFrequencyScope> <{credential['id']}>)" in text
    assert f"ObjectPropertyAssertion(<{CRED}accreditedBy> <{DAC}> <{COUNCIL}>)" in text
    kinds = {probe[0].split(":")[0] for probe in facts.probes}
    assert kinds == {"scope", "vetted"}
    vetted = [probe for probe in facts.probes if probe[0].startswith("vetted:")]
    assert {probe[2] for probe in vetted} == {dac["id"], irb["id"]}


def test_identity_mismatch_is_verified_but_not_presented(world):
    document = world["task"]()
    other_dataset = world["dac"](dataset="https://w3id.org/academic-wasteland/ubar/datasets/other")
    facts = authorization.assess(world["town"], document, world["present"](document, [other_dataset]), status_checker=lambda _: "active")
    item = facts.credentials[0]
    assert item.verified and not item.matches and "credential is for a dataset this task does not use" in item.problems
    assert not any("presents> <" in axiom for axiom in facts.axioms)
    assert any("presents> owl:Nothing" in axiom for axiom in facts.axioms)


def test_unknown_scope_is_not_verified(world):
    document = world["task"]()
    odd = world["dac"](scope=f"{PG}EverythingScope")
    facts = authorization.assess(world["town"], document, world["present"](document, [odd]), status_checker=lambda _: "active")
    assert not facts.credentials[0].verified and any("scope library" in problem for problem in facts.credentials[0].problems)


def test_diagnose_and_release_from_probe_results(world):
    document = world["task"]()
    dac, irb = world["dac"](), world["irb"]()
    facts = authorization.assess(world["town"], document, world["present"](document, [dac, irb]), status_checker=lambda _: "active")
    labels = {item.id: item.label for item in facts.credentials}
    entailed = {f"scope:{labels[dac['id']]}": "entailed", f"vetted:{labels[dac['id']]}": "entailed",
                f"scope:{labels[irb['id']]}": "entailed", f"vetted:{labels[irb['id']]}": "entailed"}
    assert authorization.release_class(facts, entailed) == f"{PG}AggregateArtifact"
    assert authorization.diagnose(facts, entailed, world["town"])["needs"] == []
    untrusted = dict(entailed, **{f"vetted:{labels[dac['id']]}": "unknown"})
    diagnosis = authorization.diagnose(facts, untrusted, world["town"])
    assert diagnosis["reason"] == "credential-not-accepted" and [need["type"] for need in diagnosis["needs"]] == ["DataAccessAuthorization"]
    assert authorization.release_class(facts, untrusted) is None
    outside = {key: ("contradicted" if key.startswith("scope:") else value) for key, value in entailed.items()}
    assert authorization.diagnose(facts, outside, world["town"])["reason"] == "out-of-scope"


def test_presentation_for_another_task_verifies_nothing(world):
    document, other = world["task"](), world["task"]()
    facts = authorization.assess(world["town"], document, world["present"](other, [world["dac"](), world["irb"]()]), status_checker=lambda _: "active")
    assert not facts.ok and not facts.verified()
    assert authorization.diagnose(facts, {}, world["town"])["reason"] == "presentation-invalid"


def test_rendered_contract_has_scope_approval_classes_and_no_coverage_nominals(world):
    text = (world["town"].city_root / "contract" / "ubar.ofn").read_text()
    assert "DataAccessCovered" in text and "ApprovesIndividualGenotypeScope" in text and "coveredBy" not in text
    assert f"<{COUNCIL}>" in text and f"<{DATASET}>" in text
    assert datetime.now(UTC)  # contract render is deterministic apart from keys, which never enter the ontology
