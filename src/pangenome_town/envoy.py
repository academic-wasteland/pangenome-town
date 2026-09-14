"""The envoy: each town's HTTP face, served on a unix socket managed by Gas City.

Routes (all relative to the service mount /v0/city/<city>/svc/envoy):
  GET  /healthz              liveness
  GET  /v0/town              public description of this town
  POST /v0/messages          receive an envelope from a peer town
  GET  /v0/messages          recent envelopes involving this town
  GET  /v0/messages/<id>     one envelope with its events and answers
"""

from __future__ import annotations

import json
import os
import socketserver
import sys
import threading
import traceback
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any

from . import mail
from .config import TownConfig
from .exchange import MAX_BODY_BYTES, Envelope, EnvelopeError, ExchangeLog

MAX_REQUEST_BYTES = MAX_BODY_BYTES + 65536


class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class EnvoyState:
    def __init__(self, town: TownConfig, log: ExchangeLog, *, deliver: bool = True, node: Any | None = None):
        self.town = town
        self.log = log
        self.deliver = deliver
        self.lock = threading.Lock()
        self._node = node
        self._node_error: str | None = None

    @property
    def node(self) -> Any | None:
        """The RCP node, created lazily once a contract has been rendered for this town."""
        if self._node is None and self._node_error is None:
            try:
                from .rcp.pipeline import Node

                self._node = Node(self.town, log=self.log)
            except Exception as error:  # noqa: BLE001
                self._node_error = f"{type(error).__name__}: {error}"
        return self._node

    def public_base_url(self) -> str:
        return os.environ.get("GC_SERVICE_PUBLIC_URL") or f"{self.town.supervisor_url.rstrip('/')}/v0/city/{self.town.name}/svc/envoy"

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.town.name,
            "display": self.town.display,
            "population": self.town.population,
            "samples": list(self.town.samples),
            "peers": sorted(self.town.peers),
            "graph_present": self.town.has_graph,
            "vcf_present": self.town.has_vcf,
            "reference_paths": list(self.town.reference_paths),
            "citation": self.town.citation,
            "protocol": {"envelope_schema_version": 1, "rcp": "available" if self.node is not None else f"unavailable ({self._node_error})"},
        }

    def receive(self, payload: Any) -> tuple[int, dict[str, Any]]:
        try:
            envelope = Envelope.from_dict(payload)
        except EnvelopeError as error:
            self.log.event(self.town.name, "rejected", None, {"reason": str(error)})
            return HTTPStatus.BAD_REQUEST, {"error": "invalid envelope", "detail": str(error)}
        if envelope.recipient != self.town.name:
            self.log.event(self.town.name, "rejected", envelope.id, {"reason": "wrong recipient", "to": envelope.recipient})
            return HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "envelope is not addressed to this town", "to": envelope.recipient}
        if envelope.sender not in self.town.peers:
            self.log.event(self.town.name, "rejected", envelope.id, {"reason": "unknown peer", "from": envelope.sender})
            return HTTPStatus.FORBIDDEN, {"error": "unknown peer town", "from": envelope.sender}
        with self.lock:
            inserted = self.log.record(envelope, town=self.town.name, direction="received", status="received")
        if not inserted and self.log.events(envelope.id) and any(
            event["kind"] == "delivered" for event in self.log.events(envelope.id)
        ):
            return HTTPStatus.OK, {"id": envelope.id, "status": "duplicate"}
        delivery: dict[str, Any] = {"skipped": True, "reason": "delivery disabled"}
        if self.deliver:
            # Mail only reaches an agent that already has a session; otherwise the
            # inbox-dispatch order starts one. Either way the envelope is logged.
            try:
                delivery = mail.send(self.town, envelope)
                self.log.event(self.town.name, "delivered", envelope.id, {"mail": delivery})
                self.log.set_status(envelope.id, "delivered")
            except mail.MailError as error:
                delivery = {"skipped": True, "reason": str(error)[:500]}
                self.log.event(self.town.name, "mail_skipped", envelope.id, {"reason": str(error)[:500]})
        return HTTPStatus.ACCEPTED, {"id": envelope.id, "status": "received", "delivery": delivery}


def make_handler(state: EnvoyState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "pangenome-town-envoy/0.1"

        def log_message(self, format: str, *args: Any) -> None:
            sys.stderr.write(f"[envoy {state.town.name}] {self.command} {self.path} {format % args}\n")

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            route = parsed.path.rstrip("/") or "/"
            query = urllib.parse.parse_qs(parsed.query)
            try:
                if route == "/healthz":
                    self._json(HTTPStatus.OK, {"ok": True, "town": state.town.name})
                elif route == "/v0/town":
                    self._json(HTTPStatus.OK, state.describe())
                elif route == "/.well-known/agent-card.json":
                    node = state.node
                    if node is None:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "this town has no RCP contract yet", "detail": state._node_error})
                    else:
                        self._json(HTTPStatus.OK, node.agent_card(state.public_base_url()))
                elif route.startswith("/v0/contract/"):
                    name = urllib.parse.unquote(route[len("/v0/contract/"):])
                    path = (state.town.city_root / "contract" / name).resolve()
                    if "/" in name or not path.is_relative_to((state.town.city_root / "contract").resolve()) or not path.is_file():
                        self._json(HTTPStatus.NOT_FOUND, {"error": "no such contract file"})
                    else:
                        body = path.read_bytes()
                        self.send_response(HTTPStatus.OK)
                        self.send_header("Content-Type", "application/json" if name.endswith(".json") else "text/plain; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                elif route == "/v0/messages":
                    limit = min(int(query.get("limit", ["50"])[0]), 500)
                    self._json(HTTPStatus.OK, {"messages": state.log.list(limit=limit, town=state.town.name)})
                elif route.startswith("/v0/messages/"):
                    message_id = urllib.parse.unquote(route[len("/v0/messages/"):])
                    message = state.log.get(message_id)
                    if not message:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "unknown message"})
                    else:
                        message["events"] = state.log.events(message_id)
                        message["answers"] = state.log.answers(message_id)
                        self._json(HTTPStatus.OK, message)
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "no such route"})
            except Exception as error:  # noqa: BLE001
                traceback.print_exc()
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": type(error).__name__, "detail": str(error)})

        def do_POST(self) -> None:
            route = urllib.parse.urlparse(self.path).path.rstrip("/")
            if route not in {"/v0/messages", "/a2a"}:
                self._json(HTTPStatus.NOT_FOUND, {"error": "no such route"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_REQUEST_BYTES:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body missing or too large"})
                return
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "body is not JSON", "detail": str(error)})
                return
            if route == "/a2a":
                from .rcp import a2a

                node = state.node
                if node is None:
                    self._json(HTTPStatus.OK, a2a.error(payload.get("id") if isinstance(payload, dict) else None, a2a.INTERNAL, "this town has no RCP contract yet", state._node_error))
                    return
                try:
                    self._json(HTTPStatus.OK, a2a.handle(node, payload, requester_hint=self.headers.get("X-Town")))
                except Exception as error:  # noqa: BLE001
                    traceback.print_exc()
                    self._json(HTTPStatus.OK, a2a.error(payload.get("id") if isinstance(payload, dict) else None, a2a.INTERNAL, f"{type(error).__name__}: {error}"))
                return
            try:
                status, body = state.receive(payload)
            except Exception as error:  # noqa: BLE001
                traceback.print_exc()
                status, body = HTTPStatus.INTERNAL_SERVER_ERROR, {"error": type(error).__name__, "detail": str(error)}
            self._json(status, body)

    return Handler


def serve(town: TownConfig, socket_path: str, *, log: ExchangeLog | None = None, deliver: bool = True) -> None:
    log = log or ExchangeLog(town.exchange_db)
    state = EnvoyState(town, log, deliver=deliver)
    try:
        os.remove(socket_path)
    except FileNotFoundError:
        pass
    with ThreadingUnixHTTPServer(socket_path, make_handler(state)) as server:
        log.event(town.name, "envoy_started", None, {"socket": socket_path})
        sys.stderr.write(f"[envoy {town.name}] listening on {socket_path}\n")
        try:
            server.serve_forever()
        finally:
            log.event(town.name, "envoy_stopped", None, {"socket": socket_path})
