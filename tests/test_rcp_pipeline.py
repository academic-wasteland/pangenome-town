import json
import shutil

import pytest
from research_commons.km import Classification

from pangenome_town.exchange import ExchangeLog
from pangenome_town.rcp import PG, a2a, contract, pipeline
from pangenome_town.tools.graph import Region

needs_tools = pytest.mark.skipif(shutil.which("vg") is None or shutil.which("bcftools") is None, reason="vg/bcftools missing")


class ContractReasoner:
    """Decides the toy contract by pattern: a task is admissible when it uses the served graph and, for large tasks,
    the requester is asserted reputable; the served-graph restriction is what the town's ontology encodes."""

    name = "toy reasoner"
    version = "0"

    def classify(self, ontology: str) -> Classification:
        town = "https://w3id.org/academic-wasteland/ubar/"
        large = any(f"ClassAssertion(<{PG}{name}>" in ontology for name in ("WholeGraphDeconstructTask", "PopulationComparisonTask", "ReadMappingVariantCallingTask"))
        reputable = f"ClassAssertion(<{PG}ReputableRequester>" in ontology
        blocked = f"ClassAssertion(<{PG}BlockedRequester>" in ontology
        uses_graph = f"<{town}graphs/JaSaPaGe-v1>" in ontology and "ObjectPropertyAssertion(<https://w3id.org/research-commons/v0.1/usesDataset>" in ontology
        consistent = True
        if f"ObjectComplementOf(<{town}AcceptedTask>)" in ontology:
            admissible = uses_graph and not blocked and (reputable if large else True)
            consistent = not admissible
        elif f"ObjectComplementOf(<{town}RejectedTask>)" in ontology:
            consistent = not blocked
        elif f"ObjectComplementOf(<{town}PermissionRequiredTask>)" in ontology:
            consistent = not (large and f"ClassAssertion(<{PG}UnvettedRequester>" in ontology)
        elif f"ObjectComplementOf(<{town}ConformingContribution>)" in ontology:
            consistent = not ("ResearchContribution" in ontology and "ResearchArtifact" in ontology and "Evidence" in ontology)
        return Classification(consistent, frozenset(), {}, 1)


@pytest.fixture
def node(towns, tmp_path):
    ubar = towns["ubar"]
    contract.render(ubar, ubar.city_root / "contract")
    ubar.city_root.joinpath("town.toml").write_text(
        ubar.city_root.joinpath("town.toml").read_text() + '\n[rcp]\ntrusted_requesters = ["https://orcid.org/0000-0001-8149-5890"]\nblocked_requesters = ["https://evil.example/agent"]\n',
        encoding="utf-8",
    )
    from pangenome_town import config

    ubar = config.load(ubar.city_root / "town.toml")
    log = ExchangeLog(towns["db"])
    yield pipeline.Node(ubar, reasoner=ContractReasoner(), log=log), ubar, log
    log.close()


def _send(node, document, hint="yamatai"):
    request = {"jsonrpc": "2.0", "id": 7, "method": "message/send", "params": {"message": {"role": "user", "messageId": "m1", "metadata": {"town": hint},
               "parts": [{"kind": "data", "data": document, "metadata": {"mediaType": a2a.TASK_PROFILE}}]}}}
    return a2a.handle(node, request)


def test_agent_card_advertises_contract(node):
    node, _, _ = node
    card = node.agent_card("http://127.0.0.1:8372/v0/city/ubar/svc/envoy")
    extension = card["capabilities"]["extensions"][0]
    assert extension["uri"] == "https://w3id.org/research-commons/v0.1/a2a"
    assert extension["params"]["contracts"][0]["bundleDigest"] == node.manifest.bundle_digest
    assert card["url"].endswith("/a2a")


@needs_tools
def test_basic_task_completes_inline_with_contribution(node):
    node, ubar, log = node
    region = Region.parse("GRCh38:chr1:0-27", "GRCh38")
    document = pipeline.task_document(ubar, f"{PG}RegionVariantListingTask", requester="https://w3id.org/academic-wasteland/yamatai/agents/townsfolk", region=region)
    response = _send(node, document)
    task = response["result"]
    assert task["status"]["state"] == "completed", task["status"]
    artifacts = {a["name"]: a for a in task["artifacts"]}
    contribution = artifacts["contribution.jsonld"]["parts"][0]["data"]
    assert contribution["addresses"] == document["@id"]
    claims = {c["predicate"].rsplit("/", 1)[1]: c["object"] for c in contribution["hasClaim"]}
    assert claims["variantSiteCount"] == 2 and claims["snvCount"] == 1
    assert contribution["hasClaim"][0]["subject"].endswith("/regions/GRCh38/chr1/0-27")
    assert task["metadata"]["verdict"]["standing"] == "unvetted"
    fetched = a2a.handle(node, {"jsonrpc": "2.0", "id": 8, "method": "tasks/get", "params": {"id": task["id"]}})
    assert fetched["result"]["status"]["state"] == "completed"
    mirrored = log.get(task["id"])
    assert mirrored and mirrored["rcp"]["state"] == "completed" and mirrored["from"] == "yamatai"


def test_large_task_needs_reputation_or_approval(node):
    node, ubar, _ = node
    unvetted = pipeline.task_document(ubar, f"{PG}WholeGraphDeconstructTask", requester="https://w3id.org/academic-wasteland/yamatai/agents/townsfolk")
    state = _send(node, unvetted)["result"]["status"]
    assert state["state"] == "input-required" and "approval" in state["message"]["parts"][0]["text"]
    trusted = pipeline.task_document(ubar, f"{PG}WholeGraphDeconstructTask", requester="https://w3id.org/academic-wasteland/yamatai/agents/townsfolk", on_behalf_of="https://orcid.org/0000-0001-8149-5890")
    task = _send(node, trusted)["result"]
    assert task["status"]["state"] == "working" and task["metadata"]["verdict"]["standing"] == "reputable"


def test_blocked_and_malformed_tasks_are_rejected(node):
    node, ubar, _ = node
    blocked = pipeline.task_document(ubar, f"{PG}GraphSummaryTask", requester="https://evil.example/agent")
    assert _send(node, blocked)["result"]["status"]["state"] == "rejected"
    broken = pipeline.task_document(ubar, f"{PG}GraphSummaryTask", requester="https://w3id.org/academic-wasteland/yamatai/agents/townsfolk")
    del broken["requestedBy"]
    task = _send(node, broken)["result"]
    assert task["status"]["state"] == "rejected" and "structural" in task["status"]["message"]["parts"][0]["text"]
    assert a2a.handle(node, {"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "urn:uuid:nope"}})["error"]["code"] == a2a.TASK_NOT_FOUND
    assert a2a.handle(node, {"jsonrpc": "2.0", "id": 1, "method": "message/stream", "params": {}})["error"]["code"] == a2a.UNSUPPORTED_OPERATION


def test_without_reasoner_nothing_executes(towns):
    ubar = towns["ubar"]
    contract.render(ubar, ubar.city_root / "contract")
    node = pipeline.Node(ubar, reasoner=None)
    node.validator = None
    document = pipeline.task_document(ubar, f"{PG}GraphSummaryTask", requester="https://w3id.org/academic-wasteland/yamatai/agents/townsfolk")
    task = _send(node, document)["result"]
    assert task["status"]["state"] == "failed" and task["metadata"]["verdict"]["standing"] == "unvetted"
    assert json.loads(json.dumps(task))["artifacts"][0]["name"] == "semantic-validation-report.json"
