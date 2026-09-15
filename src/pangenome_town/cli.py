"""Command line entry point: `pangenome-town`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import config, envoy, mail, peers
from .exchange import Attachment, Envelope, EnvelopeError, ExchangeLog, sha256_file
from .tools import graph

TASK_CLASSES = {
    "summary": "GraphSummaryTask", "subgraph": "RegionExtractionTask", "haplotypes": "HaplotypePresenceTask",
    "variants": "RegionVariantListingTask", "deconstruct": "WholeGraphDeconstructTask", "compare": "PopulationComparisonTask",
    "allele-frequency": "AlleleFrequencyTask", "genotype-export": "IndividualGenotypeExportTask",
}
CONTROLLED_KIND_NAMES = {"allele-frequency", "genotype-export"}


def _print(value: Any) -> None:
    json.dump(value, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="pangenome-town", description="Operate one pangenome town.")
    root.add_argument("--town", type=Path, help="path to town.toml (default: $PT_TOWN_TOML or discovery)")
    commands = root.add_subparsers(dest="command", required=True)
    from .cli_delegation import add_parser
    add_parser(commands)
    resources = commands.add_parser("resources", help="published local result collections")
    resources.add_argument("--read", help="resource ID to read as a bounded base64 chunk")
    resources.add_argument("--offset", type=int, default=0)
    resources.add_argument("--sha256")

    serve = commands.add_parser("envoy", help="serve the town's envoy on a unix socket")
    serve.add_argument("--socket", help="socket path (default: $GC_SERVICE_SOCKET)")
    serve.add_argument("--no-deliver", action="store_true", help="log inbound envelopes without mailing the agent")

    send = commands.add_parser("send", help="send an envelope to a peer town")
    send.add_argument("--to", required=True, help="peer town name")
    send.add_argument("--kind", default="question", choices=["question", "answer", "notice"])
    send.add_argument("--text", required=True)
    send.add_argument("--region", help="assembly:chrom:start-end the question is about")
    send.add_argument("--reply-to", help="message id this envelope answers")
    send.add_argument("--attach", action="append", default=[], type=Path, help="file to attach (repeatable)")
    send.add_argument("--dry-run", action="store_true")

    query = commands.add_parser("query", help="run a deterministic query against the town's graph")
    query.add_argument("--kind", required=True, choices=list(graph.KINDS))
    query.add_argument("--region")
    query.add_argument("--out", type=Path, help="output directory (default: state dir)")

    answer = commands.add_parser("answer", help="run a query for a received question and send the answer back")
    answer.add_argument("--message", required=True, help="id of the question being answered")
    answer.add_argument("--kind", required=True, choices=list(graph.KINDS))
    answer.add_argument("--region", help="override the region named in the question")
    answer.add_argument("--text", required=True, help="the narrative answer to send")
    answer.add_argument("--out", type=Path)
    answer.add_argument("--dry-run", action="store_true")
    answer.add_argument("--again", action="store_true", help="send even if an answer to this message was already sent")

    inbox = commands.add_parser("inbox", help="list questions received but not yet answered or dispatched")
    inbox.add_argument("--check", action="store_true", help="exit 0 only when pending questions exist")
    inbox.add_argument("--mark-dispatched", metavar="MESSAGE_ID", help="record that an agent was dispatched")
    inbox.add_argument("--detail", default="", help="free-text detail stored with --mark-dispatched")

    messages = commands.add_parser("messages", help="show exchange log entries")
    messages.add_argument("--id", help="one message with its events and answers")
    messages.add_argument("--limit", type=int, default=20)
    messages.add_argument("--all-towns", action="store_true")

    commands.add_parser("town-info", help="describe this town")
    peer = commands.add_parser("peer-info", help="ask a peer town to describe itself")
    peer.add_argument("peer")

    commands.add_parser("doctor", help="check tools, data, and exchange log")

    rcp = commands.add_parser("rcp", help="Research Commons Protocol operations")
    rcp_commands = rcp.add_subparsers(dest="rcp_command", required=True)
    rcp_commands.add_parser("render-contract", help="render this town's semantic contract into <city>/contract/")
    rcp_commands.add_parser("agent-card", help="print this town's A2A Agent Card")
    task = rcp_commands.add_parser("task", help="author a ResearchTask addressed to a peer town (prints JSON-LD)")
    task.add_argument("--to", required=True, help="peer town name")
    task.add_argument("--kind", required=True, choices=list(TASK_CLASSES))
    task.add_argument("--region")
    task.add_argument("--on-behalf-of", help="principal IRI (ORCID, hop:// rig URI)")
    task.add_argument("--dataset", action="append", default=[], help="controlled dataset IRI (default for controlled kinds: the peer's restricted datasets)")
    submit = rcp_commands.add_parser("submit", help="send a ResearchTask to a peer town over A2A and print the task result")
    submit.add_argument("--to", required=True)
    submit.add_argument("--kind", required=True, choices=list(TASK_CLASSES))
    submit.add_argument("--region")
    submit.add_argument("--on-behalf-of")
    submit.add_argument("--task-file", type=Path, help="send this JSON-LD task instead of authoring one")
    submit.add_argument("--dataset", action="append", default=[], help="controlled dataset IRI (default for controlled kinds: the peer's restricted datasets)")
    submit.add_argument("--holder", help="present credentials held by this IRI (usually the same as --on-behalf-of)")
    submit.add_argument("--holder-slug", help="holder wallet name under ~/.gc/holders (default: derived from --holder)")
    submit.add_argument("--credential", action="append", default=[], type=Path, help="credential file to present (default: the whole wallet)")
    submit.add_argument("--registrar", help="registrar URL to fetch accreditations from (default: [trust].registrar)")
    submit.add_argument("--follow-referrals", action="store_true", help="resubmit once to a town the refusal refers to")
    get = rcp_commands.add_parser("get", help="fetch a task from a peer town (tasks/get)")
    get.add_argument("--to", required=True)
    get.add_argument("task_id")
    rcp_commands.add_parser("tasks", help="list this town's RCP tasks")

    ledger = commands.add_parser("commons", help="Wasteland commons: this town's reputation ledger")
    ledger_commands = ledger.add_subparsers(dest="commons_command", required=True)
    ledger_commands.add_parser("init", help="register this town as a rig in the commons")
    score = ledger_commands.add_parser("score", help="reputation standing of a handle or IRI (default: this town)")
    score.add_argument("handle", nargs="?")
    ledger_commands.add_parser("leaderboard", help="standing of every known handle")
    rows = ledger_commands.add_parser("rows", help="dump recent rows of a commons table")
    rows.add_argument("table", choices=["rigs", "wanted", "completions", "stamps"])
    rows.add_argument("--limit", type=int, default=20)
    stamp = ledger_commands.add_parser("stamp", help="stamp a peer's completion from a validation report (author = this town)")
    stamp.add_argument("--subject", required=True, help="handle of the contributing town")
    stamp.add_argument("--completion", required=True, help="completion id (c-...)")
    stamp.add_argument("--report", required=True, type=Path, help="semantic validation report JSON")

    authority = commands.add_parser("authority", help="Camelot: issuers, applications, human decisions, registrar service")
    authority.add_argument("--registry", help="registry directory (default: [authority].registry under the city)")
    authority.add_argument("--key-dir", help="issuer private keys (default: [authority].key_dir or ~/.gc/authority/<town>)")
    authority_commands = authority.add_subparsers(dest="authority_command", required=True)
    init_issuer = authority_commands.add_parser("init-issuer", help="create an issuer and its key (idempotent)")
    init_issuer.add_argument("slug")
    init_issuer.add_argument("--name", required=True)
    init_issuer.add_argument("--role", required=True, help="e.g. AccreditationCouncil, EthicsBoard, DataAccessCommittee")
    accredit = authority_commands.add_parser("accredit", help="an issuer accredits another issuer")
    accredit.add_argument("--by", required=True)
    accredit.add_argument("--subject", required=True)
    accredit.add_argument("--roles", nargs="+", required=True)
    accredit.add_argument("--valid-days", type=int, default=365)
    authority_commands.add_parser("issuers", help="list issuers with keys and accreditations")
    applications = authority_commands.add_parser("applications", help="list applications")
    applications.add_argument("--state", choices=["pending", "approved", "denied"])
    applications.add_argument("--check", action="store_true", help="exit 0 only when matching applications exist")
    show = authority_commands.add_parser("show", help="one application (app-...) or credential")
    show.add_argument("id")
    approve = authority_commands.add_parser("approve", help="HUMAN DECISION: issue the credential an application asks for")
    approve.add_argument("application")
    approve.add_argument("--valid-days", type=int, default=30)
    approve.add_argument("--decided-by", help="IRI of the deciding human (default: [authority].operator)")
    deny = authority_commands.add_parser("deny", help="HUMAN DECISION: deny an application")
    deny.add_argument("application")
    deny.add_argument("--reason", required=True)
    deny.add_argument("--decided-by")
    revoke = authority_commands.add_parser("revoke", help="HUMAN DECISION: revoke a credential")
    revoke.add_argument("credential")
    notify = authority_commands.add_parser("notify", help="mail the human operator about new pending applications")
    notify.add_argument("--dry-run", action="store_true")
    authority_commands.add_parser("agent-card", help="print Camelot's Agent Card")
    serve_authority = authority_commands.add_parser("serve", help="serve the registrar and Agent Card on a unix socket")
    serve_authority.add_argument("--socket")

    holder = commands.add_parser("holder", help="researcher keys and credential wallet")
    holder_commands = holder.add_subparsers(dest="holder_command", required=True)
    for name, help_text in (("new", "create a holder key"), ("wallet", "list stored credentials"), ("apply", "apply to Camelot for a credential"),
                            ("fetch", "store an approved credential in the wallet")):
        sub = holder_commands.add_parser(name, help=help_text)
        sub.add_argument("--holder", required=True, help="holder IRI, e.g. an ORCID")
        sub.add_argument("--slug")
        if name in {"apply", "fetch"}:
            sub.add_argument("--registrar", help="registrar URL (default: [trust].registrar)")
        if name == "apply":
            sub.add_argument("--type", required=True, choices=["DataAccessAuthorization", "EthicsApproval"])
            sub.add_argument("--issuer", required=True, help="issuer slug, e.g. ubar-dac")
            sub.add_argument("--scope", required=True, help="scope class IRI from the town's scope library")
            sub.add_argument("--dataset")
            sub.add_argument("--protocol")
            sub.add_argument("--purpose", required=True)
            sub.add_argument("--dry-run", action="store_true")
        if name == "fetch":
            sub.add_argument("application")

    compute = commands.add_parser("compute", help="compute sites and workflows (the rigger's tools)")
    compute_commands = compute.add_subparsers(dest="compute_command", required=True)
    compute_commands.add_parser("sites", help="sites, reachability, and which templates can run where")
    compute_plan = compute_commands.add_parser("plan", help="plan a workflow spec from a template")
    compute_plan.add_argument("template")
    compute_plan.add_argument("--region")
    compute_plan.add_argument("--site")
    compute_plan.add_argument("--threads", type=int, default=1)
    compute_validate = compute_commands.add_parser("validate", help="validate a workflow spec against its template and site")
    compute_validate.add_argument("spec")
    compute_pending = compute_commands.add_parser("pending", help="admitted compute tasks waiting for the rigger")
    compute_pending.add_argument("--check", action="store_true")
    compute_run = compute_commands.add_parser("run", help="re-check every gate for a waiting task and run it (optionally with a proposed spec)")
    compute_run.add_argument("--task", required=True)
    compute_run.add_argument("--workflow", help="workflow spec JSON proposed by the rigger (validated before use)")

    dashboard = commands.add_parser("dashboard", help="serve the operator dashboard on loopback")
    dashboard.add_argument("--towns", nargs="+", type=Path, help="town.toml files to watch (default: --town or $PT_TOWNS)")
    dashboard.add_argument("--port", type=int, default=8390)
    dashboard.add_argument("--bind", default="127.0.0.1")
    return root


def _town(arguments: argparse.Namespace) -> config.TownConfig:
    return config.load(arguments.town) if arguments.town else config.load_default()


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        return _dispatch(arguments)
    except (config.TownConfigError, EnvelopeError, graph.QueryError, mail.MailError, peers.PeerError, *_resource_errors()) as error:
        _print({"error": type(error).__name__, "detail": str(error)})
        return 2


def _resource_errors() -> tuple[type[Exception], ...]:
    from .authority.keys import AuthorityKeyError
    from .authority.registrar import RegistryError
    from .cli_resources import ResourceCommandError
    from .compute import ComputeError
    from .rcp.pipeline import PipelineError

    return (AuthorityKeyError, RegistryError, ResourceCommandError, ComputeError, PipelineError)


def _dispatch(arguments: argparse.Namespace) -> int:
    if arguments.command == "dashboard":
        from . import dashboard

        paths = arguments.towns or [Path(item) for item in os.environ.get("PT_TOWNS", "").split(":") if item]
        towns = [config.load(path) for path in paths] if paths else [_town(arguments)]
        dashboard.serve(towns, bind=arguments.bind, port=arguments.port)
        return 0
    town = _town(arguments)
    if arguments.command == "envoy":
        socket_path = arguments.socket or os.environ.get("GC_SERVICE_SOCKET")
        if not socket_path:
            raise config.TownConfigError("envoy needs --socket or GC_SERVICE_SOCKET")
        envoy.serve(town, socket_path, deliver=not arguments.no_deliver)
        return 0

    log = ExchangeLog(town.exchange_db)
    if arguments.command == "resources":
        from . import resources
        _print(resources.chunk(town, {"id": arguments.read, "offset": arguments.offset, "sha256": arguments.sha256})
               if arguments.read else {"resources": resources.listing(town)})
        return 0
    if arguments.command == "delegate":
        from .cli_delegation import command
        return command(arguments, town, log)
    if arguments.command == "send":
        body: dict[str, Any] = {"text": arguments.text}
        if arguments.region:
            body["region"] = str(graph.Region.parse(arguments.region, town.default_assembly))
        kind = "answer" if arguments.reply_to and arguments.kind == "question" else arguments.kind
        envelope = Envelope.new(
            kind, town.name, arguments.to, body,
            in_reply_to=arguments.reply_to,
            attachments=tuple(Attachment.from_file(path) for path in arguments.attach),
        )
        if arguments.dry_run:
            _print({"dry_run": True, "url": peers.envoy_url(town, town.peer_city(arguments.to)), "envelope": envelope.to_dict()})
            return 0
        result = peers.send(town, envelope, log)
        _print({"id": envelope.id, **result})
        return 0

    if arguments.command == "query":
        region = graph.Region.parse(arguments.region, town.default_assembly) if arguments.region else None
        out_dir = arguments.out or graph.default_out_dir(town, f"{arguments.kind}_{region or 'graph'}")
        result = graph.GraphTools(town).run(arguments.kind, region, out_dir)
        log.event(town.name, "query", None, {"kind": arguments.kind, "region": str(region) if region else None, "artifact": result["artifact"]})
        _print(result)
        return 0

    if arguments.command == "answer":
        question = log.envelope(arguments.message)
        if question is None:
            raise EnvelopeError(f"unknown message {arguments.message}")
        if question.recipient != town.name:
            raise EnvelopeError("that message was not addressed to this town")
        existing = log.answers(question.id)
        if existing and not arguments.again:
            _print({"already_answered": True, "answer_id": existing[0]["id"], "in_reply_to": question.id,
                    "hint": "the answer was already sent; pass --again only if you must send another"})
            return 0
        region_text = arguments.region or question.body.get("region")
        region = graph.Region.parse(str(region_text), town.default_assembly) if region_text else None
        out_dir = arguments.out or graph.default_out_dir(town, f"answer_{arguments.kind}_{region or 'graph'}")
        result = graph.GraphTools(town).run(arguments.kind, region, out_dir)
        log.event(town.name, "query", question.id, {"kind": arguments.kind, "region": str(region) if region else None, "artifact": result["artifact"]})
        attachments = [Attachment.from_file(Path(result["artifact"]))]
        for key in ("table", "gfa"):
            if result.get(key):
                attachments.append(Attachment.from_file(Path(result[key])))
        summary_keys = ("variant_count", "by_type", "per_sample_alt_sites", "distinct_threads", "haplotypes_traced", "town_samples_with_alt_in_region", "segments", "paths_total", "graph", "served_samples_path_fragments")
        body = {
            "text": arguments.text,
            "region": str(region) if region else None,
            "query_kind": arguments.kind,
            "result": {key: result[key] for key in summary_keys if key in result},
        }
        envelope = Envelope.new("answer", town.name, question.sender, body, in_reply_to=question.id, attachments=tuple(attachments))
        if arguments.dry_run:
            _print({"dry_run": True, "envelope": envelope.to_dict()})
            return 0
        sent = peers.send(town, envelope, log)
        log.set_status(question.id, "answered")
        log.event(town.name, "answered", question.id, {"answer_id": envelope.id})
        _print({"id": envelope.id, "in_reply_to": question.id, **sent})
        return 0

    if arguments.command == "inbox":
        if arguments.mark_dispatched:
            log.event(town.name, "dispatched", arguments.mark_dispatched, {"detail": arguments.detail})
            log.set_status(arguments.mark_dispatched, "dispatched")
        pending = log.pending_questions(town.name)
        if arguments.check:
            return 0 if pending else 1
        _print({"town": town.name, "pending": pending})
        return 0

    if arguments.command == "messages":
        if arguments.id:
            message = log.get(arguments.id)
            if message is None:
                raise EnvelopeError(f"unknown message {arguments.id}")
            message["events"] = log.events(arguments.id)
            message["answers"] = log.answers(arguments.id)
            _print(message)
        else:
            _print({"messages": log.list(limit=arguments.limit, town=None if arguments.all_towns else town.name)})
        return 0

    if arguments.command == "rcp":
        return _rcp(arguments, town, log)
    if arguments.command == "commons":
        return _commons(arguments, town, log)
    if arguments.command in {"authority", "holder", "compute"}:
        from . import cli_resources

        handler = {"authority": cli_resources.authority_command, "compute": cli_resources.compute_command}.get(arguments.command)
        return handler(arguments, town, log) if handler else cli_resources.holder_command(arguments, town)

    if arguments.command == "town-info":
        _print(envoy.EnvoyState(town, log, deliver=False).describe())
        return 0

    if arguments.command == "peer-info":
        _print(peers.town_info(town, arguments.peer))
        return 0

    if arguments.command == "doctor":
        problems = graph.environment_ok()
        report = {
            "town": town.name,
            "city_root": str(town.city_root),
            "graph": {"path": str(town.graph), "present": town.has_graph},
            "vcf": {"path": str(town.vcf), "present": town.has_vcf},
            "exchange_db": str(town.exchange_db),
            "peers": town.peers,
            "tools": graph.tool_versions(),
            "openrouter_key": bool(os.environ.get("OPENROUTER_API_KEY")),
            "problems": problems,
        }
        _print(report)
        return 1 if problems else 0
    raise config.TownConfigError(f"unknown command {arguments.command}")


if __name__ == "__main__":
    raise SystemExit(main())




def _rcp(arguments: argparse.Namespace, town: config.TownConfig, log: ExchangeLog) -> int:
    from .rcp import a2a, contract, pipeline

    if arguments.rcp_command == "render-contract":
        manifest = contract.render(town, town.city_root / "contract")
        _print({"manifest": str(manifest.path), "id": manifest.id, "bundleDigest": manifest.bundle_digest})
        return 0
    if arguments.rcp_command == "agent-card":
        node = pipeline.Node(town, log=log)
        _print(node.agent_card(f"{town.supervisor_url.rstrip('/')}/v0/city/{town.name}/svc/envoy"))
        return 0
    if arguments.rcp_command == "tasks":
        node = pipeline.Node(town, log=log)
        records = [pipeline.TaskRecord.load(d) for d in sorted(node.tasks_dir.iterdir()) if (d / "status.json").exists()]
        _print([{"id": r.id, "state": r.state, "message": r.message} for r in records])
        return 0
    if arguments.rcp_command == "get":
        payload = _a2a_post(town, arguments.to, {"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": arguments.task_id}})
        _print(payload)
        return 0 if "result" in payload else 1
    target = arguments.to
    payload: dict[str, Any] = {}
    for hop in range(2 if getattr(arguments, "follow_referrals", False) else 1):
        document, presentation = _author_task(arguments, town, target)
        if arguments.rcp_command == "task":
            _print(document if presentation is None else {"task": document, "presentation": presentation})
            return 0
        parts = [{"kind": "data", "data": document, "metadata": {"mediaType": a2a.TASK_PROFILE}}]
        if presentation is not None:
            parts.append({"kind": "data", "data": presentation, "metadata": {"mediaType": a2a.PRESENTATION_PROFILE}})
        request = {"jsonrpc": "2.0", "id": 1, "method": "message/send",
                   "params": {"message": {"role": "user", "messageId": document["@id"], "metadata": {"town": town.name}, "parts": parts}}}
        payload = _a2a_post(town, target, request)
        task_result = payload.get("result") or {}
        state = task_result.get("status", {}).get("state")
        refusal = (task_result.get("metadata") or {}).get("refusal") or {}
        log.event(town.name, "rcp_submitted", document["@id"], {"to": target, "state": state, "refusal": refusal, "presented": presentation is not None})
        if state == "completed":
            stamped = _stamp_peer_result(town, target, task_result)
            log.event(town.name, "rcp_stamped" if stamped.get("stamp") else "rcp_stamp_skipped", document["@id"], stamped)
            payload["ledger"] = stamped
        referrals = [item for item in refusal.get("referrals") or [] if item.get("town") and item["town"] != town.name]
        if hop == 0 and state == "input-required" and referrals and getattr(arguments, "follow_referrals", False):
            payload["referred_from"] = target
            target = referrals[0]["town"]
            log.event(town.name, "rcp_referral_followed", document["@id"], {"from": arguments.to, "to": target})
            continue
        break
    _print(payload)
    return 0 if "result" in payload else 1


def _author_task(arguments: argparse.Namespace, town: config.TownConfig, target: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    from .rcp import PG, authorization, contract, pipeline

    peer_town = config.load(_peer_town_toml(town, target))
    if arguments.rcp_command == "submit" and arguments.task_file:
        from research_commons.schema import load_json

        document = load_json(arguments.task_file)
    else:
        region = graph.Region.parse(arguments.region, peer_town.default_assembly) if arguments.region else None
        requester = f"https://w3id.org/academic-wasteland/{town.name}/agents/townsfolk"
        controlled = arguments.kind in CONTROLLED_KIND_NAMES
        dataset_iris = list(arguments.dataset) or ([item["iri"] for item in contract.restricted_datasets(peer_town)] if controlled else [])
        names = {item["iri"]: item["name"] for item in contract.restricted_datasets(peer_town)}
        document = pipeline.task_document(peer_town, f"{PG}{TASK_CLASSES[arguments.kind]}", requester=requester, region=region,
                                          on_behalf_of=arguments.on_behalf_of, include_graph=not controlled,
                                          datasets=[authorization.controlled_dataset_entity(value, names.get(value)) for value in dataset_iris])
    presentation = None
    if getattr(arguments, "holder", None):
        from .cli_resources import build_presentation

        registrar_url = arguments.registrar or (town.extra.get("trust") or {}).get("registrar")
        presentation = build_presentation(peer_town, document, holder=arguments.holder, slug=arguments.holder_slug,
                                          credential_files=list(arguments.credential), registrar_url=registrar_url)
    return document, presentation


def _a2a_post(town: config.TownConfig, target: str, request: dict[str, Any]) -> dict[str, Any]:
    import urllib.request

    url = peers.envoy_url(town, town.peer_city(target), "/a2a")
    data = json.dumps(request).encode("utf-8")
    http_request = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json", "X-GC-Request": "pangenome-town", "X-Town": town.name})
    with urllib.request.urlopen(http_request, timeout=900) as response:
        return json.loads(response.read().decode("utf-8"))


def _stamp_peer_result(town: config.TownConfig, peer: str, task_result: dict[str, Any]) -> dict[str, Any]:
    """Pay the peer in reputation: verify what came back and stamp its completion in the commons.

    Verification here is what the requester can check on its own: the contribution is a structurally
    valid ResearchContribution addressing our task, and the artifact digest it cites matches the file
    the peer wrote. The peer's own semantic report is carried inside the stamp for third parties.
    """
    from research_commons import wasteland as wl
    from research_commons.schema import StructuralValidationError, validate_message

    from .rcp import commons

    ledger = commons.Commons.for_town(town)
    if ledger is None:
        return {"skipped": "no commons configured"}
    contribution = report = None
    files: dict[str, str] = {}
    for artifact in task_result.get("artifacts") or []:
        for part in artifact.get("parts") or []:
            if part.get("kind") == "data" and artifact.get("name") == "contribution.jsonld":
                contribution = part["data"]
            elif part.get("kind") == "data" and artifact.get("name") == "semantic-validation-report.json":
                report = part["data"]
            elif part.get("kind") == "file":
                files[part["file"]["name"]] = part["file"].get("uri", "")
    if not isinstance(contribution, dict):
        return {"skipped": "no contribution returned"}
    problems: list[str] = []
    try:
        validate_message(contribution)
    except StructuralValidationError as error:
        problems.append(f"structure: {error}")
    for output in contribution.get("hasOutput") or []:
        path = files.get(output.get("name", ""))
        if path and Path(path).exists():
            actual = sha256_file(Path(path))
            if actual != output.get("digest"):
                problems.append(f"digest mismatch for {output.get('name')}")
        else:
            problems.append(f"artifact {output.get('name')} not readable here")
    peer_report = (report or {}).get("contribution") if isinstance(report, dict) else None
    if not isinstance(peer_report, dict) or "status" not in peer_report:
        return {"skipped": "peer returned no contribution report", "problems": problems}
    stamped_report = dict(peer_report)
    if problems:
        stamped_report["status"] = "invalid"
        stamped_report["checks"] = [*peer_report.get("checks", []), {"kind": "requester-verification", "status": "invalid", "durationMs": 0, "diagnostic": "; ".join(problems)}]
    else:
        stamped_report["checks"] = [*peer_report.get("checks", []), {"kind": "requester-verification", "status": "entailed", "durationMs": 0}]
    completion = wl.completion_id(contribution["@id"])
    subject = town.peer_city(peer)
    try:
        ledger.ensure_rig(town.name, display_name=f"{town.display} pangenome town", rig_type="agent")
        stamp = ledger.post_stamp(stamped_report, author=town.name, subject=subject, completion=completion)
    except (commons.CommonsError, ValueError) as error:
        return {"error": f"{type(error).__name__}: {error}", "problems": problems}
    return {"stamp": stamp, "completion": completion, "subject": subject, "status": stamped_report["status"], "problems": problems}


def _commons(arguments: argparse.Namespace, town: config.TownConfig, log: ExchangeLog) -> int:
    from .rcp import commons

    ledger = commons.Commons.for_town(town)
    if ledger is None:
        print("no commons found: run `wl create <org>/commons --local-only` or set [rcp].commons_dir in town.toml", file=sys.stderr)
        return 2
    if arguments.commons_command == "init":
        card = f"{town.supervisor_url.rstrip('/')}/v0/city/{town.name}/svc/envoy/.well-known/agent-card.json"
        ledger.ensure_rig(town.name, display_name=f"{town.display} pangenome town", hop_uri=card, rig_type="agent")
        _print({"commons": str(ledger.directory), "rig": town.name, "hop_uri": card})
        return 0
    if arguments.commons_command == "score":
        handle = commons.handle_for(arguments.handle) if arguments.handle else town.name
        _print(ledger.standing(handle).as_dict())
        return 0
    if arguments.commons_command == "leaderboard":
        _print(ledger.leaderboard())
        return 0
    if arguments.commons_command == "rows":
        _print(ledger.rows(arguments.table, arguments.limit))
        return 0
    if arguments.commons_command == "stamp":
        from research_commons.schema import load_json

        stamp = ledger.post_stamp(load_json(arguments.report), author=town.name, subject=arguments.subject, completion=arguments.completion)
        log.event(town.name, "rcp_stamped", arguments.completion, {"stamp": stamp, "subject": arguments.subject})
        _print({"stamp": stamp})
        return 0
    return 1


def _peer_town_toml(town: config.TownConfig, peer: str) -> Path:
    """Peer town.toml lives beside ours (same parent directory) in this deployment."""
    candidate = town.city_root.parent / town.peer_city(peer) / "town.toml"
    if not candidate.exists():
        raise config.TownConfigError(f"cannot find peer town.toml at {candidate}")
    return candidate
