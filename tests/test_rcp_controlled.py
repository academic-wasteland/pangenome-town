"""Controlled-access tasks through the whole pipeline with a fixture reasoner (no km)."""

import re
import shutil

import pytest
from research_commons.km import Classification

from pangenome_town import config
from pangenome_town.authority import CRED, issuer_iri, keys
from pangenome_town.authority import credentials as creds
from pangenome_town.exchange import ExchangeLog
from pangenome_town.rcp import PG, RCP, a2a, authorization, contract, pipeline, town_iri
from pangenome_town.tools.graph import Region

needs_bcftools = pytest.mark.skipif(shutil.which("bcftools") is None, reason="bcftools missing")
TOWN = "https://w3id.org/academic-wasteland/ubar/"
AGG, IND = f"{PG}AggregateFrequencyScope", f"{PG}IndividualGenotypeScope"
SCOPE_TASKS = {
    "AggregateFrequency": {"AlleleFrequencyTask", "PopulationComparisonTask", "RegionVariantListingTask", "GraphSummaryTask"},
    "IndividualGenotype": {"IndividualGenotypeExportTask", "AlleleFrequencyTask", "PopulationComparisonTask", "RegionVariantListingTask", "GraphSummaryTask"},
}
HOLDER = "https://orcid.org/0000-0000-0000-0003"
YAMATAI = "https://w3id.org/academic-wasteland/yamatai/agents/townsfolk"
DATASET = f"{TOWN}datasets/ksa-individual-genotypes"
COUNCIL, DAC, IRB, ROGUE = (issuer_iri(slug) for slug in ("council", "ubar-dac", "irb", "rogue"))


class ControlledReasoner:
    """Decides the rendered contract for the scenarios below from the asserted facts, mirroring its axioms."""

    name = "controlled fixture"
    version = "0"

    def classify(self, ontology: str) -> Classification:
        lines = [line.strip() for line in ontology.strip().splitlines()]
        probe = lines[-2] if len(lines) > 2 else ""
        facts = ontology
        task_classes = set(re.findall(rf"ClassAssertion\(<{re.escape(PG)}(\w+Task)> <urn:uuid:", facts))
        controlled = bool(task_classes & {"AlleleFrequencyTask", "IndividualGenotypeExportTask"})
        basic_or_large = bool(task_classes - {"AlleleFrequencyTask", "IndividualGenotypeExportTask"})
        blocked = f"<{PG}BlockedRequester>" in facts
        reachable = f"ClassAssertion(<{TOWN}ReachableSite>" in facts
        nothing = f"ObjectAllValuesFrom(<{CRED}presents> owl:Nothing)" in facts
        presented = set(re.findall(rf"ObjectPropertyAssertion\(<{re.escape(CRED)}presents> <[^>]+> <([^>]+)>\)", facts))
        issued_by = dict(re.findall(rf"ObjectPropertyAssertion\(<{re.escape(CRED)}issuedBy> <([^>]+)> <([^>]+)>\)", facts))
        accredited = dict(re.findall(rf"ObjectPropertyAssertion\(<{re.escape(CRED)}accreditedBy> <([^>]+)> <([^>]+)>\)", facts))
        anchors = set(re.findall(rf"ClassAssertion\(<{re.escape(TOWN)}TrustAnchor> <([^>]+)>\)", facts))

        def under_anchor(issuer: str) -> bool:
            seen = set()
            while issuer and issuer not in seen:
                if issuer in anchors:
                    return True
                seen.add(issuer)
                issuer = accredited.get(issuer, "")
            return False

        def vetted(credential: str, kind: str) -> bool:
            return (f"ClassAssertion(<{CRED}VerifiedCredential> <{credential}>)" in facts and f"ClassAssertion(<{CRED}{kind}> <{credential}>)" in facts
                    and under_anchor(issued_by.get(credential, "")))

        def in_scope(scope: str) -> bool:
            return bool(task_classes & SCOPE_TASKS[scope])

        def covered(kind: str) -> bool:
            for credential in presented:
                for scope in SCOPE_TASKS:
                    if f"ClassAssertion(<{PG}Approves{scope}Scope> <{credential}>)" in facts and in_scope(scope) and vetted(credential, kind):
                        return True
            return False

        accepted = controlled and reachable and not blocked and covered("DataAccessAuthorization") and covered("EthicsApproval")
        rejected = blocked or (basic_or_large and f"ClassAssertion(<{RCP}RestrictedDataset>" in facts)
        permission = controlled and nothing
        consistent = True
        negative = re.fullmatch(r"ClassAssertion\(ObjectComplementOf\(<([^>]+)>\) <([^>]+)>\)", probe)
        positive = re.fullmatch(r"ClassAssertion\(<([^>]+)> <([^>]+)>\)", probe)
        match = negative or positive
        if match:
            cls, individual = match.groups()
            value = None
            if cls == f"{TOWN}AcceptedTask":
                value = accepted
            elif cls == f"{TOWN}RejectedTask":
                value = rejected
            elif cls == f"{TOWN}PermissionRequiredTask":
                value = permission
            elif cls == f"{TOWN}NoCapabilityTask":
                value = controlled and not reachable
            elif cls.startswith(PG) and cls.endswith("Scope"):
                value = in_scope(cls[len(PG):-len("Scope")])
                if positive and not value:
                    consistent = False
                value = value if negative else None
            elif cls in (f"{TOWN}VettedDataAccess", f"{TOWN}VettedEthicsApproval"):
                value = vetted(individual, "DataAccessAuthorization" if cls.endswith("DataAccess") else "EthicsApproval")
            elif cls == f"{PG}AggregateArtifact":
                value = f"ClassAssertion(<{PG}AggregateArtifact> <{individual}>)" in facts
                if positive and f"ClassAssertion(<{PG}IndividualLevelArtifact> <{individual}>)" in facts:
                    consistent = False
                value = value if negative else None
            elif cls == f"{RCP}ResearchArtifact":
                value = True if negative else None
            elif cls == f"{TOWN}ConformingContribution":
                value = "ResearchContribution" in facts and "ResearchArtifact" in facts and "Evidence" in facts
            if negative and value:
                consistent = False
            if positive and cls == f"{TOWN}AcceptedTask" and controlled and nothing:
                consistent = False
        return Classification(consistent, frozenset(), {}, 1)


@pytest.fixture
def setup(towns, tmp_path):
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

[compute]
dispatch = "inline"

[[sites]]
name = "workstation"
driver = "local"
datasets = ["graph", "vcf", "ksa-individual-genotypes"]
paths = {{ "ksa-individual-genotypes" = "{ubar.vcf}" }}
tools = ["bcftools"]
""", encoding="utf-8")
    ubar = config.load(path)
    contract.render(ubar, ubar.city_root / "contract")
    log = ExchangeLog(towns["db"])
    hk = keys.public_key_text(holder)
    accreditations = [creds.accreditation(accreditor=COUNCIL, accreditor_key=council, subject_issuer=issuer, subject_public_key=keys.public_key_text(key), roles=["x"])
                      for issuer, key in ((DAC, dac), (IRB, irb))]

    def credential(kind, scope=AGG, issuer=None, key=None):
        if kind == "DataAccessAuthorization":
            return creds.issue(issuer=issuer or DAC, issuer_key=key or dac, types=kind, subject={"id": HOLDER, "holderKey": hk, "dataset": DATASET, "scope": scope})
        return creds.issue(issuer=IRB, issuer_key=irb, types=kind, subject={"id": HOLDER, "holderKey": hk, "protocol": "P1", "scope": scope})

    def task(kind="AlleleFrequencyTask", restricted=True):
        return pipeline.task_document(ubar, f"{PG}{kind}", requester=YAMATAI, on_behalf_of=HOLDER, include_graph=not restricted,
                                      region=Region.parse("GRCh38:chr1:0-27", "GRCh38"),
                                      datasets=[authorization.controlled_dataset_entity(DATASET)] if restricted else [])

    def present(document, credentials, accreditation_list=None, for_task=None):
        return creds.present(holder=HOLDER, holder_key=holder, credentials=credentials, task=for_task or document,
                             accreditations=accreditations if accreditation_list is None else accreditation_list, audience=town_iri("ubar"))

    def node(**options):
        options.setdefault("status_checker", lambda _: "active")
        return pipeline.Node(ubar, reasoner=ControlledReasoner(), log=log, **options)

    yield {"town": ubar, "credential": credential, "task": task, "present": present, "node": node, "rogue": rogue}
    log.close()


def send(node, document, presentation=None):
    parts = [{"kind": "data", "data": document, "metadata": {"mediaType": a2a.TASK_PROFILE}}]
    if presentation is not None:
        parts.append({"kind": "data", "data": presentation, "metadata": {"mediaType": a2a.PRESENTATION_PROFILE}})
    request = {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": {"role": "user", "messageId": "m", "metadata": {"town": "yamatai"}, "parts": parts}}}
    return a2a.handle(node, request)["result"]


def both(setup, scope=AGG):
    return [setup["credential"]("DataAccessAuthorization", scope), setup["credential"]("EthicsApproval", scope)]


def test_without_credentials_the_authority_gate_asks_for_both(setup):
    result = send(setup["node"](), setup["task"]())
    assert result["status"]["state"] == "input-required", result["status"]
    refusal = result["metadata"]["refusal"]
    assert refusal["gate"] == "authority" and refusal["reason"] == "missing-credential"
    assert [need["type"] for need in refusal["needs"]] == ["DataAccessAuthorization", "EthicsApproval"]
    assert result["metadata"]["gates"]["authority"] == "pending"


@needs_bcftools
def test_covered_allele_frequency_runs_and_releases_only_aggregate_output(setup):
    document = setup["task"]()
    result = send(setup["node"](), document, setup["present"](document, both(setup)))
    assert result["status"]["state"] == "completed", result["status"]
    gates = result["metadata"]["gates"]
    assert gates["authority"] == "pass" and gates["capability"] == "pass" and gates["admissibility"] == "pass"
    names = {artifact["name"] for artifact in result["artifacts"]}
    assert "allele_frequencies.tsv" in names and "genotypes.tsv" not in names
    contribution = next(artifact for artifact in result["artifacts"] if artifact["name"] == "contribution.jsonld")["parts"][0]["data"]
    assert all(f"{PG}AggregateArtifact" in output["@type"] for output in contribution["hasOutput"])
    assert contribution["hasClaim"][0]["subject"] == DATASET
    credentials = result["metadata"]["authority"]["credentials"]
    assert all(item["checks"]["covers"] == "entailed" for item in credentials)


def test_export_under_an_aggregate_scope_is_rejected_as_out_of_scope(setup):
    document = setup["task"]("IndividualGenotypeExportTask")
    result = send(setup["node"](), document, setup["present"](document, both(setup)))
    assert result["status"]["state"] == "rejected", result["status"]
    assert result["metadata"]["refusal"]["reason"] == "out-of-scope"


@needs_bcftools
def test_export_under_an_individual_genotype_scope_releases_genotypes(setup):
    document = setup["task"]("IndividualGenotypeExportTask")
    result = send(setup["node"](), document, setup["present"](document, both(setup, IND)))
    assert result["status"]["state"] == "completed", result["status"]
    assert "genotypes.tsv" in {artifact["name"] for artifact in result["artifacts"]}


def test_unaccredited_issuer_leaves_the_task_waiting(setup):
    document = setup["task"]()
    rogue_dac = setup["credential"]("DataAccessAuthorization", issuer=ROGUE, key=setup["rogue"])
    presentation = setup["present"](document, [rogue_dac, setup["credential"]("EthicsApproval")])
    result = send(setup["node"](directory={ROGUE: keys.public_key_text(setup["rogue"])}), document, presentation)
    assert result["status"]["state"] == "input-required"
    refusal = result["metadata"]["refusal"]
    assert refusal["reason"] == "credential-not-accepted" and [need["type"] for need in refusal["needs"]] == ["DataAccessAuthorization"]
    rogue_entry = next(item for item in refusal["credentials"] if item["issuer"] == ROGUE)
    assert "issuer is not under one of this town's trust anchors" in rogue_entry["problems"]


def test_replayed_presentation_is_refused(setup):
    document, other = setup["task"](), setup["task"]()
    result = send(setup["node"](), document, setup["present"](document, both(setup), for_task=other))
    assert result["status"]["state"] == "input-required"
    assert result["metadata"]["refusal"]["reason"] in {"presentation-invalid", "missing-credential"}
    assert any("another task" in problem for problem in result["metadata"]["authority"]["problems"])


def test_basic_task_on_restricted_data_is_rejected(setup):
    document = setup["task"]("RegionVariantListingTask")
    document["usesDataset"].insert(0, {"@id": contract.graph_iri(setup["town"]), "@type": ["PublicDataset", f"{PG}PangenomeGraph"]})
    result = send(setup["node"](), document)
    assert result["status"]["state"] == "rejected" and result["metadata"]["refusal"]["reason"] == "restricted-dataset"


def test_no_reachable_site_refers_only_to_a_peer_holding_the_data(setup):
    can_run = {"url": "http://peer/a2a", "compute": {"templates": {"allele-frequency": "workstation"}}}
    holds_data = dict(can_run, trust={"restricted_datasets": [{"iri": DATASET}]})
    for card, expected in ((can_run, []), (holds_data, ["yamatai"])):
        node = setup["node"](reachable_fn=lambda site: (False, "down for maintenance"),
                             referral_fetch=lambda url, card=card: card if "/city/yamatai/" in url else None)
        document = setup["task"]()
        result = send(node, document, setup["present"](document, both(setup)))
        assert result["status"]["state"] == "input-required"
        refusal = result["metadata"]["refusal"]
        assert refusal["gate"] == "capability" and [item["town"] for item in refusal["referrals"]] == expected


@needs_bcftools
def test_release_is_withheld_when_outputs_exceed_the_scope(setup, monkeypatch):
    from pangenome_town.compute import runner

    original = runner.run_task

    def mislabelled(*args, **kwargs):
        result = original(*args, **kwargs)
        for output in result["outputs"]:
            output["class"] = f"{PG}IndividualLevelArtifact"
        return result

    monkeypatch.setattr(runner, "run_task", mislabelled)
    document = setup["task"]()
    result = send(setup["node"](), document, setup["present"](document, both(setup)))
    assert result["status"]["state"] == "failed", result["status"]
    assert result["metadata"]["refusal"]["reason"] == "output-exceeds-scope"
    assert not result["artifacts"] or all(artifact["name"] == "semantic-validation-report.json" for artifact in result["artifacts"])
