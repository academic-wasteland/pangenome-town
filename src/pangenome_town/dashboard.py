"""Operator dashboard: watch towns talk, drill into every exchange.

Stdlib only. Serves one HTML page plus a JSON API on loopback and streams
exchange-log events over SSE. Reads:

- the shared exchange log (SQLite) written by every town's envoy and CLI;
- the Gas City supervisor API (cities, sessions, agents) on 127.0.0.1:8372;
- opencode's local database for the agent transcript and token cost of each
  session, correlated with an exchange by rig directory and time window.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import config
from .exchange import ExchangeLog

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8390
OPENCODE_DB = Path(os.environ.get("PT_OPENCODE_DB") or Path.home() / ".local/share/opencode/opencode.db")
HTML_PATH = Path(__file__).with_name("dashboard.html")


class DashboardState:
    def __init__(self, towns: list[config.TownConfig]):
        if not towns:
            raise config.TownConfigError("dashboard needs at least one town.toml")
        self.towns = {town.name: town for town in towns}
        self.supervisor_url = towns[0].supervisor_url.rstrip("/")
        self.log = ExchangeLog(towns[0].exchange_db)
        self.lock = threading.Lock()
        self._sites_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    # Supervisor proxy -----------------------------------------------------------------
    def supervisor(self, path: str, timeout: float = 5.0) -> Any:
        request = urllib.request.Request(f"{self.supervisor_url}{path}", headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8") or "null")
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            return {"error": str(error)}

    def towns_view(self) -> list[dict[str, Any]]:
        cities = self.supervisor("/v0/cities")
        running = {item["name"]: item.get("running") for item in (cities.get("items") or [])} if isinstance(cities, dict) else {}
        result = []
        for name, town in self.towns.items():
            sessions = self.supervisor(f"/v0/city/{name}/sessions")
            session_items = sessions.get("items") if isinstance(sessions, dict) else None
            health = self.supervisor(f"/v0/city/{name}/svc/envoy/healthz")
            summary = _read_json(town.state_dir / "summary.json") if town.kind == "pangenome" else None
            authority = self._authority_counts(town) if town.kind == "authority" else None
            result.append({
                "kind": town.kind,
                "authority": authority,
                "name": name,
                "display": town.display,
                "population": town.population,
                "samples": list(town.samples),
                "peers": sorted(town.peers),
                "city_running": running.get(name),
                "envoy_ok": bool(isinstance(health, dict) and health.get("ok")),
                "graph_present": town.has_graph,
                "vcf_present": town.has_vcf,
                "graph_summary": {k: summary.get(k) for k in ("graph", "paths_total", "samples_in_graph")} if summary else None,
                "sessions": [
                    {k: item.get(k) for k in ("id", "template", "state", "provider", "created_at", "last_active", "running")}
                    for item in (session_items or [])
                ],
                "citation": town.citation,
                "costs": opencode_costs_for_dir(town.city_root / "rig"),
            })
        return result

    # Exchanges ------------------------------------------------------------------------
    def exchanges(self, limit: int = 100) -> list[dict[str, Any]]:
        messages = self.log.list(limit=limit)
        by_id = {message["id"]: message for message in messages}
        for message in messages:
            events = self.log.events(message["id"])
            message["events"] = events
            message["answers"] = [answer["id"] for answer in self.log.answers(message["id"])]
            message["timeline"] = _timeline(message, events, self.log)
        # Group answers under their question for the two-lane view.
        return [dict(message, thread_root=(message["in_reply_to"] if message["in_reply_to"] in by_id else message["id"])) for message in messages]

    def exchange(self, message_id: str) -> dict[str, Any] | None:
        message = self.log.get(message_id)
        if message is None:
            return None
        events = self.log.events(message_id)
        message["events"] = events
        message["answers"] = self.log.answers(message_id)
        message["timeline"] = _timeline(message, events, self.log)
        message["artifacts"] = _artifacts(message)
        for answer in message["answers"]:
            answer["artifacts"] = _artifacts(answer)
            answer["events"] = self.log.events(answer["id"])
        window = _agent_window(events, message["answers"])
        town = self.towns.get(message["to"])
        message["agent_activity"] = opencode_activity(town.city_root / "rig", *window) if town and window else []
        return message

    def ledger(self) -> dict[str, Any]:
        """Wasteland commons view: standings plus recent wanted/completion/stamp rows."""
        from .rcp import commons

        ledger = None
        for town in self.towns.values():
            try:
                ledger = commons.Commons.for_town(town)
            except commons.CommonsError:
                ledger = None
            if ledger is not None:
                break
        if ledger is None:
            return {"available": False}
        try:
            board = ledger.leaderboard()
            stamps = ledger.rows("stamps", 50)
            wanted = ledger.rows("wanted", 50)
            completions = ledger.rows("completions", 50)
        except commons.CommonsError as error:
            return {"available": False, "error": str(error)}
        for row in stamps:
            if isinstance(row.get("valence"), str):
                try:
                    row["valence"] = json.loads(row["valence"])
                except json.JSONDecodeError:
                    pass
        for row in wanted:
            row["description"] = None  # the JSON-LD task is large; the exchange detail shows it
        for row in completions:
            row["evidence"] = None
        return {"available": True, "commons": str(ledger.directory), "leaderboard": board, "stamps": stamps, "wanted": wanted, "completions": completions}

    # Authority towns and compute sites ---------------------------------------------------
    def _registry(self, town: config.TownConfig) -> Any:
        from .cli_resources import registry_for

        return registry_for(town, SimpleNamespace(registry=None, key_dir=None))

    def _authority_counts(self, town: config.TownConfig) -> dict[str, Any]:
        try:
            registry = self._registry(town)
            return {"issuers": len(registry.issuers()), "pending_applications": len(registry.applications("pending"))}
        except Exception as error:  # noqa: BLE001
            return {"error": f"{type(error).__name__}: {error}"}

    def authority(self) -> dict[str, Any]:
        result = []
        for town in self.towns.values():
            if town.kind != "authority":
                continue
            try:
                registry = self._registry(town)
                issuers = [{"id": item.get("id"), "slug": item.get("slug"), "name": item.get("name"), "role": item.get("role"),
                            "publicKey": item.get("publicKey"),
                            "accreditedBy": sorted({acc.get("issuer") for acc in item.get("accreditations") or [] if acc.get("issuer")})}
                           for item in registry.issuers()]
                applications = [{key: item.get(key) for key in ("id", "state", "credential_type", "issuer", "holder", "created", "decided", "decided_by", "reason")}
                                | {"purpose": str(item.get("purpose") or "")[:200]} for item in registry.applications()]
                credentials = []
                for path in sorted((Path(registry.root) / "credentials").glob("*.json")):
                    document = _read_json(path) or {}
                    subject = document.get("credentialSubject") or {}
                    types = document.get("type") or []
                    credentials.append({"id": document.get("id"), "type": [t for t in types if t != "VerifiableCredential"],
                                        "issuer": document.get("issuer"), "holder": subject.get("id"), "scope": subject.get("scope"),
                                        "dataset": subject.get("dataset"), "validFrom": document.get("validFrom"),
                                        "validUntil": document.get("validUntil"),
                                        "status": registry.status(document["id"]) if document.get("id") else "unknown"})
                result.append({"town": town.name, "display": town.display, "issuers": issuers, "applications": applications, "credentials": credentials})
            except Exception as error:  # noqa: BLE001
                result.append({"town": town.name, "display": town.display, "error": f"{type(error).__name__}: {error}"})
        return {"authorities": result}

    def sites(self) -> dict[str, Any]:
        from .compute import TEMPLATES
        from .compute.runner import pick_site
        from .rcp import capability

        result = []
        now = time.time()
        for town in self.towns.values():
            if town.kind != "pangenome":
                continue
            cached = self._sites_cache.get(town.name)
            if cached and now - cached[0] < 60:
                result.append(cached[1])
                continue
            try:
                _, report = capability.site_facts(town)
                templates = {}
                for name in TEMPLATES:
                    site, _ = pick_site(town, name)
                    templates[name] = site.name if site else None
                entry = {"town": town.name, "display": town.display, "sites": report, "templates": templates}
            except Exception as error:  # noqa: BLE001
                entry = {"town": town.name, "display": town.display, "error": f"{type(error).__name__}: {error}"}
            self._sites_cache[town.name] = (now, entry)
            result.append(entry)
        return {"towns": result}

    def events_since(self, after: int, limit: int = 200) -> list[dict[str, Any]]:
        return self.log.events(None, limit=limit, after=after)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _timeline(message: dict[str, Any], events: list[dict[str, Any]], log: ExchangeLog) -> list[dict[str, Any]]:
    steps = [{"ts": message["created"], "town": message["from"], "kind": "created", "detail": {}}]
    steps.extend({"ts": e["ts"], "town": e["town"], "kind": e["kind"], "detail": e["detail"]} for e in events)
    for answer in log.answers(message["id"]):
        steps.append({"ts": answer["created"], "town": answer["from"], "kind": "answer", "detail": {"id": answer["id"]}})
    steps.sort(key=lambda step: step["ts"])
    return steps


def _artifacts(message: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for attachment in message["envelope"].get("attachments") or []:
        item = dict(attachment)
        path = attachment.get("path")
        if path and Path(path).exists() and Path(path).stat().st_size < 2_000_000:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            if attachment.get("media_type") == "application/json":
                try:
                    item["content"] = json.loads(text)
                except json.JSONDecodeError:
                    item["text"] = text[:20000]
            else:
                item["text"] = text[:20000]
        result.append(item)
    return result


def _agent_window(events: list[dict[str, Any]], answers: list[dict[str, Any]]) -> tuple[float, float] | None:
    dispatched = [e["ts"] for e in events if e["kind"] == "dispatched"]
    starts = dispatched or [e["ts"] for e in events if e["kind"] == "received"]
    if not starts:
        return None
    start = _epoch(min(starts)) - 30
    ends = [a["created"] for a in answers]
    end = _epoch(max(ends)) + 120 if ends else time.time()
    return start, end


def _epoch(iso: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(iso).timestamp()


# opencode transcript and cost -------------------------------------------------------------
def _opencode() -> sqlite3.Connection | None:
    if not OPENCODE_DB.exists():
        return None
    connection = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def opencode_costs_for_dir(directory: Path) -> dict[str, Any]:
    connection = _opencode()
    if connection is None:
        return {"available": False}
    try:
        rows = connection.execute(
            "SELECT m.data FROM message m JOIN session s ON s.id = m.session_id WHERE s.directory = ?", (str(directory),)
        ).fetchall()
    except sqlite3.Error as error:
        return {"available": False, "error": str(error)}
    finally:
        connection.close()
    cost = 0.0
    tokens = {"input": 0, "output": 0, "reasoning": 0}
    calls = 0
    for row in rows:
        try:
            data = json.loads(row["data"])
        except json.JSONDecodeError:
            continue
        if data.get("role") != "assistant":
            continue
        calls += 1
        cost += float(data.get("cost") or 0)
        for key in tokens:
            tokens[key] += int((data.get("tokens") or {}).get(key) or 0)
    return {"available": True, "usd": round(cost, 4), "tokens": tokens, "assistant_turns": calls}


def opencode_activity(directory: Path, start: float, end: float) -> list[dict[str, Any]]:
    connection = _opencode()
    if connection is None:
        return []
    try:
        rows = connection.execute(
            "SELECT p.data, p.time_created, m.session_id FROM part p JOIN message m ON m.id = p.message_id"
            " JOIN session s ON s.id = m.session_id WHERE s.directory = ? AND p.time_created BETWEEN ? AND ?"
            " ORDER BY p.time_created",
            (str(directory), int(start * 1000), int(end * 1000)),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    activity = []
    for row in rows:
        try:
            part = json.loads(row["data"])
        except json.JSONDecodeError:
            continue
        kind = part.get("type")
        entry: dict[str, Any] = {"ts": row["time_created"] / 1000, "session": row["session_id"], "type": kind}
        if kind == "text":
            entry["text"] = (part.get("text") or "")[:4000]
        elif kind == "tool":
            state = part.get("state") or {}
            entry["tool"] = part.get("tool")
            entry["input"] = state.get("input")
            entry["output"] = (state.get("output") or "")[:4000]
            entry["status"] = state.get("status")
        elif kind == "step-finish":
            entry["tokens"] = part.get("tokens")
            entry["cost"] = part.get("cost")
        else:
            continue
        activity.append(entry)
    return activity


# HTTP ----------------------------------------------------------------------------------
def make_handler(state: DashboardState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "pangenome-town-dashboard/0.1"

        def log_message(self, format: str, *args: Any) -> None:
            if os.environ.get("PT_DASHBOARD_LOG"):
                sys.stderr.write(f"[dashboard] {format % args}\n")

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            self._send(status, json.dumps(payload, indent=None, sort_keys=False, default=str).encode("utf-8"), "application/json")

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            route = parsed.path.rstrip("/") or "/"
            query = urllib.parse.parse_qs(parsed.query)
            try:
                if route == "/":
                    self._send(HTTPStatus.OK, HTML_PATH.read_bytes(), "text/html; charset=utf-8")
                elif route == "/api/towns":
                    self._json(HTTPStatus.OK, {"towns": state.towns_view(), "supervisor": state.supervisor_url})
                elif route == "/api/exchanges":
                    limit = min(int(query.get("limit", ["100"])[0]), 500)
                    self._json(HTTPStatus.OK, {"exchanges": state.exchanges(limit)})
                elif route.startswith("/api/exchanges/"):
                    message = state.exchange(urllib.parse.unquote(route[len("/api/exchanges/"):]))
                    self._json(HTTPStatus.OK if message else HTTPStatus.NOT_FOUND, message or {"error": "unknown exchange"})
                elif route == "/api/ledger":
                    self._json(HTTPStatus.OK, state.ledger())
                elif route == "/api/authority":
                    self._json(HTTPStatus.OK, state.authority())
                elif route == "/api/sites":
                    self._json(HTTPStatus.OK, state.sites())
                elif route == "/api/events":
                    after = int(query.get("after", ["0"])[0])
                    self._json(HTTPStatus.OK, {"events": state.events_since(after)})
                elif route == "/api/events/stream":
                    self._stream(int(query.get("after", ["0"])[0]))
                elif route == "/healthz":
                    self._json(HTTPStatus.OK, {"ok": True})
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "no such route"})
            except BrokenPipeError:
                pass
            except Exception as error:  # noqa: BLE001
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": type(error).__name__, "detail": str(error)})

        def _stream(self, after: int) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            cursor = after
            while True:
                events = state.events_since(cursor)
                for event in events:
                    cursor = max(cursor, event["seq"])
                    payload = json.dumps(event, default=str)
                    self.wfile.write(f"id: {event['seq']}\nevent: exchange\ndata: {payload}\n\n".encode())
                if not events:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                time.sleep(2)

    return Handler


def serve(towns: list[config.TownConfig], *, bind: str = DEFAULT_BIND, port: int = DEFAULT_PORT) -> None:
    if bind not in {"127.0.0.1", "localhost", "::1"}:
        raise config.TownConfigError("the dashboard binds to loopback only; put a reverse proxy in front for remote access")
    state = DashboardState(towns)
    server = ThreadingHTTPServer((bind, port), make_handler(state))
    server.daemon_threads = True
    sys.stderr.write(f"[dashboard] http://{bind}:{port}/  towns={sorted(state.towns)}\n")
    try:
        server.serve_forever()
    finally:
        server.server_close()
