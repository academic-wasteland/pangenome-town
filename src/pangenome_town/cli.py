"""Command line entry point: `pangenome-town`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import config, envoy, mail, peers
from .exchange import Attachment, Envelope, EnvelopeError, ExchangeLog
from .tools import graph


def _print(value: Any) -> None:
    json.dump(value, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="pangenome-town", description="Operate one pangenome town.")
    root.add_argument("--town", type=Path, help="path to town.toml (default: $PT_TOWN_TOML or discovery)")
    commands = root.add_subparsers(dest="command", required=True)

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
    task.add_argument("--kind", required=True, choices=["summary", "subgraph", "haplotypes", "variants", "deconstruct", "compare"])
    task.add_argument("--region")
    task.add_argument("--on-behalf-of", help="principal IRI (ORCID, hop:// rig URI)")
    submit = rcp_commands.add_parser("submit", help="send a ResearchTask to a peer town over A2A and print the task result")
    submit.add_argument("--to", required=True)
    submit.add_argument("--kind", required=True, choices=["summary", "subgraph", "haplotypes", "variants", "deconstruct", "compare"])
    submit.add_argument("--region")
    submit.add_argument("--on-behalf-of")
    submit.add_argument("--task-file", type=Path, help="send this JSON-LD task instead of authoring one")
    get = rcp_commands.add_parser("get", help="fetch a task from a peer town (tasks/get)")
    get.add_argument("--to", required=True)
    get.add_argument("task_id")
    rcp_commands.add_parser("tasks", help="list this town's RCP tasks")

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
    except (config.TownConfigError, EnvelopeError, graph.QueryError, mail.MailError, peers.PeerError) as error:
        _print({"error": type(error).__name__, "detail": str(error)})
        return 2


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


TASK_CLASSES = {
    "summary": "GraphSummaryTask", "subgraph": "RegionExtractionTask", "haplotypes": "HaplotypePresenceTask",
    "variants": "RegionVariantListingTask", "deconstruct": "WholeGraphDeconstructTask", "compare": "PopulationComparisonTask",
}


def _rcp(arguments: argparse.Namespace, town: config.TownConfig, log: ExchangeLog) -> int:
    from .rcp import PG, a2a, contract, pipeline

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
    authoring = arguments.rcp_command == "task" or (arguments.rcp_command == "submit" and not arguments.task_file)
    if authoring:
        peer_town = config.load(_peer_town_toml(town, arguments.to))
        region = graph.Region.parse(arguments.region, peer_town.default_assembly) if arguments.region else None
        requester = f"https://w3id.org/academic-wasteland/{town.name}/agents/townsfolk"
        document = pipeline.task_document(peer_town, f"{PG}{TASK_CLASSES[arguments.kind]}", requester=requester, region=region, on_behalf_of=arguments.on_behalf_of)
        if arguments.rcp_command == "task":
            _print(document)
            return 0
    elif arguments.rcp_command == "submit":
        from research_commons.schema import load_json

        document = load_json(arguments.task_file)
    else:
        document = {"@id": arguments.task_id}
    request = {"jsonrpc": "2.0", "id": 1, "method": "message/send" if arguments.rcp_command == "submit" else "tasks/get",
               "params": {"message": {"role": "user", "messageId": document["@id"], "metadata": {"town": town.name},
                                      "parts": [{"kind": "data", "data": document, "metadata": {"mediaType": a2a.TASK_PROFILE}}]}}
               if arguments.rcp_command == "submit" else {"id": arguments.task_id}}
    url = peers.envoy_url(town, town.peer_city(arguments.to), "/a2a")
    import urllib.request

    data = json.dumps(request).encode("utf-8")
    http_request = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json", "X-GC-Request": "pangenome-town", "X-Town": town.name})
    with urllib.request.urlopen(http_request, timeout=600) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if arguments.rcp_command == "submit":
        log.event(town.name, "rcp_submitted", document["@id"], {"to": arguments.to, "state": (payload.get("result") or {}).get("status", {}).get("state"), "url": url})
    _print(payload)
    return 0 if "result" in payload else 1


def _peer_town_toml(town: config.TownConfig, peer: str) -> Path:
    """Peer town.toml lives beside ours (same parent directory) in this deployment."""
    candidate = town.city_root.parent / town.peer_city(peer) / "town.toml"
    if not candidate.exists():
        raise config.TownConfigError(f"cannot find peer town.toml at {candidate}")
    return candidate
