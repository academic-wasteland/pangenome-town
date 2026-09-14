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
import re
import secrets
import shutil
import sqlite3
import subprocess
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
AGENTSVIEW_URL = (os.environ.get("PT_AGENTSVIEW_URL") or "http://127.0.0.1:8080").rstrip("/")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,119}$")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
MAX_POST_BYTES = 65536
AUTHORITY_EVENTS = {"application_received", "credential_issued", "credential_revoked", "application_denied"}


class _LockedLog:
    """Serialize every call into the shared SQLite connection: the dashboard serves requests on many threads."""

    def __init__(self, log: ExchangeLog, lock: threading.RLock):
        self._log = log
        self._lock = lock

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._log, name)
        if not callable(attribute):
            return attribute

        def locked(*args: Any, **kwargs: Any) -> Any:
            with self._lock:
                return attribute(*args, **kwargs)

        return locked


class ActionError(ValueError):
    """A cockpit action was malformed or not allowed."""


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not NAME_RE.match(value):
        raise ActionError(f"{label} must be a session id, alias, agent, or 'human' (letters, digits, _ . : / @ -)")
    return value


def _text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ActionError(f"{label} must be non-empty text of at most {limit} characters")
    return value


def _get_json(url: str, timeout: float) -> Any:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "null")
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return None


class DashboardState:
    def __init__(self, towns: list[config.TownConfig]):
        if not towns:
            raise config.TownConfigError("dashboard needs at least one town.toml")
        self.towns = {town.name: town for town in towns}
        self.supervisor_url = towns[0].supervisor_url.rstrip("/")
        self.lock = threading.RLock()
        self.log = _LockedLog(ExchangeLog(towns[0].exchange_db), self.lock)
        self._sites_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._agentsview_cache: tuple[float, dict[str, Any]] | None = None
        self.token = secrets.token_urlsafe(24)
        self.runner = subprocess.run
        self.agentsview_url = AGENTSVIEW_URL

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
        message["agentsview"] = []
        if town and window:
            start, end = window
            for item in self.agentsview().get("sessions") or []:
                if item["town"] != town.name:
                    continue
                began, ended = _epoch(str(item.get("started_at") or "")), _epoch(str(item.get("ended_at") or item.get("started_at") or ""))
                if began and began <= end + 60 and (ended or began) >= start - 60:
                    message["agentsview"].append(item)
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

    # Step 4 views: interfaces, residents, trust, decisions ------------------------------------
    def interfaces(self, dashboard_url: str) -> dict[str, Any]:
        """Every place a human or peer talks to the towns, with a live health check."""
        items: list[dict[str, Any]] = [
            {"name": "Cockpit (this dashboard)", "kind": "dashboard", "url": dashboard_url, "ok": True},
        ]
        cities = self.supervisor("/v0/cities", timeout=3)
        items.append({"name": "Gas City supervisor and gc dashboard", "kind": "supervisor", "url": f"{self.supervisor_url}/",
                      "ok": isinstance(cities, dict) and "error" not in cities})
        for town in self.towns.values():
            base = f"{self.supervisor_url}/v0/city/{town.name}/svc/envoy"
            health = self.supervisor(f"/v0/city/{town.name}/svc/envoy/healthz", timeout=3)
            ok = isinstance(health, dict) and bool(health.get("ok"))
            items.append({"name": f"{town.display} envoy", "kind": f"{town.kind} envoy", "town": town.name, "url": f"{base}/", "ok": ok})
            items.append({"name": f"{town.display} Agent Card", "kind": "agent card", "town": town.name, "url": f"{base}/.well-known/agent-card.json", "ok": ok})
            if town.kind == "authority":
                for label, path in (("registrar keys", "/v0/keys"), ("applications", "/v0/applications"), ("credential status", "/v0/status/{id}")):
                    items.append({"name": f"{town.display} {label}", "kind": "registrar", "town": town.name, "url": f"{base}{path}", "ok": ok})
            else:
                items.append({"name": f"{town.display} A2A (Research Commons Protocol)", "kind": "a2a", "town": town.name, "url": f"{base}/a2a", "ok": ok,
                              "method": "POST"})
        try:
            from .rcp import commons

            first = next(iter(self.towns.values()))
            ledger = commons.Commons.for_town(first)
            items.append({"name": "Wasteland commons (Dolt database)", "kind": "commons", "url": str(ledger.directory) if ledger else None, "ok": ledger is not None})
        except Exception as error:  # noqa: BLE001
            items.append({"name": "Wasteland commons (Dolt database)", "kind": "commons", "url": None, "ok": False, "detail": str(error)})
        return {"interfaces": items}

    def residents(self) -> dict[str, Any]:
        """City-scoped agents of every town (not pack plumbing), their model, and session state."""
        result = []
        for town in self.towns.values():
            agents = self.supervisor(f"/v0/city/{town.name}/agents", timeout=3)
            sessions = self.supervisor(f"/v0/city/{town.name}/sessions", timeout=3)
            session_items = sessions.get("items") if isinstance(sessions, dict) else []
            by_template: dict[str, list[dict[str, Any]]] = {}
            for item in session_items or []:
                by_template.setdefault(str(item.get("template") or "").split("/")[-1], []).append(item)
            people = []
            for agent in (agents.get("items") if isinstance(agents, dict) else None) or []:
                name = str(agent.get("name") or "")
                if agent.get("pack") in {"bd", "core"}:
                    continue
                short = name.split(".")[-1].split("/")[-1]
                description = str(agent.get("description") or "")
                people.append({
                    "name": name, "short": short, "provider": agent.get("provider"),
                    "model": {"qwen": "Qwen3.8 27B on unimatrix01 (local vLLM)", "glm": "GLM 5.3 Flash via OpenRouter"}.get(str(agent.get("provider")), agent.get("provider")),
                    "title": description.split(":")[0].split(". ")[0][:120], "description": description[:400],
                    "running": agent.get("running"), "state": agent.get("state"),
                    "sessions": [{k: item.get(k) for k in ("id", "alias", "state", "reason", "created_at")} for item in by_template.get(short, [])],
                })
            result.append({"town": town.name, "display": town.display, "kind": town.kind, "residents": people,
                           "error": agents.get("error") if isinstance(agents, dict) else None})
        return {"towns": result}

    def trust(self) -> dict[str, Any]:
        """What each pangenome town trusts, serves under control, and approves by scope."""
        from .rcp import authorization, contract

        result = []
        for town in self.towns.values():
            if town.kind != "pangenome":
                continue
            try:
                settings = authorization.settings(town)
                result.append({
                    "town": town.name, "display": town.display,
                    "anchors": sorted(settings["anchors"]), "registrar": settings["registrar"], "revocation": settings["revocation"],
                    "restricted_datasets": contract.restricted_datasets(town),
                    "required_credentials": list(authorization.REQUIRED_TYPES),
                    "scopes": [{"scope": key, **value} for key, value in contract.SCOPES.items()],
                    "reputation": {k: (town.extra.get("rcp") or {}).get(k) for k in ("reputation_threshold", "reputation_min_authors", "trusted_requesters", "blocked_requesters")},
                })
            except Exception as error:  # noqa: BLE001
                result.append({"town": town.name, "display": town.display, "error": f"{type(error).__name__}: {error}"})
        return {"towns": result}

    def decisions(self) -> dict[str, Any]:
        """Pending applications joined with residents' recommendations, plus recent authority events."""
        import re as _re

        result = []
        authority_events = {"application_received", "credential_issued", "credential_revoked", "application_denied"}
        events = [event for event in self.log.events(None, limit=500) if event.get("kind") in authority_events][-30:]
        for town in self.towns.values():
            if town.kind != "authority":
                continue
            mail = self.supervisor(f"/v0/city/{town.name}/mail", timeout=3)
            recommendations: dict[str, list[dict[str, Any]]] = {}
            for item in (mail.get("items") if isinstance(mail, dict) else None) or []:
                subject = str(item.get("subject") or "")
                match = _re.match(r"Recommendation (app-[0-9a-f]{8}): *(\w+)", subject)
                if match:
                    recommendations.setdefault(match.group(1), []).append({
                        "from": item.get("from"), "verdict": match.group(2), "subject": subject,
                        "body": str(item.get("body") or "")[:1200], "created_at": item.get("created_at"), "read": item.get("read")})
            try:
                registry = self._registry(town)
                applications = registry.applications()
            except Exception as error:  # noqa: BLE001
                result.append({"town": town.name, "error": f"{type(error).__name__}: {error}"})
                continue
            pending = []
            for app in applications:
                if app.get("state") != "pending":
                    continue
                pending.append({key: app.get(key) for key in ("id", "credential_type", "issuer", "holder", "created", "subject_fields")}
                               | {"purpose": str(app.get("purpose") or "")[:400], "recommendations": recommendations.get(app["id"], []),
                                  "commands": {"approve": f"pangenome-town --town {town.city_root}/town.toml authority approve {app['id']}",
                                               "deny": f"pangenome-town --town {town.city_root}/town.toml authority deny {app['id']} --reason \"...\""}})
            result.append({"town": town.name, "display": town.display, "pending": pending,
                           "recommendations_for_decided": {k: v for k, v in recommendations.items() if k not in {p["id"] for p in pending}}})
        return {"authorities": result, "events": list(reversed(events))}

    # Cockpit actions: mail and sessions through the gc CLI ------------------------------------
    def _town(self, name: Any) -> config.TownConfig:
        town = self.towns.get(str(name))
        if town is None:
            raise ActionError(f"unknown town {name!r}")
        return town

    def _gc(self, town: config.TownConfig, subcommand: list[str], flags: list[str], positionals: list[str], *, timeout: float = 60) -> dict[str, Any]:
        gc = os.environ.get("PT_GC_BIN") or shutil.which("gc") or str(Path.home() / ".local/bin/gc")
        argv = [gc, *subcommand, "--city", str(town.city_root), *flags, "--", *positionals]
        env = {key: value for key, value in os.environ.items() if key != "OPENROUTER_API_KEY"}
        try:
            result = self.runner(argv, capture_output=True, text=True, timeout=timeout, env=env, check=False)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"gc {' '.join(subcommand)} timed out after {timeout:.0f}s"}
        parsed: Any = None
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    pass
        return {"ok": result.returncode == 0, "returncode": result.returncode, "json": parsed,
                "stdout": (result.stdout or "").strip()[-4000:], "stderr": (result.stderr or "").strip()[-2000:]}

    def _record_action(self, town: config.TownConfig, action: str, detail: dict[str, Any]) -> None:
        self.log.event(town.name, "cockpit_action", None, {"action": action, **detail})

    def mail_list(self, town_name: Any) -> dict[str, Any]:
        town = self._town(town_name)
        data = self.supervisor(f"/v0/city/{town.name}/mail?status=all", timeout=4)
        items = data.get("items") if isinstance(data, dict) else None
        messages = sorted(items or [], key=lambda item: str(item.get("created_at") or ""), reverse=True)[:200]
        return {"town": town.name, "messages": messages, "error": data.get("error") if isinstance(data, dict) and items is None else None}

    def act(self, action: str, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ActionError("body must be a JSON object")
        town = self._town(payload.get("town"))
        if action == "mail/send":
            to = _name(payload.get("to"), "to")
            subject = _text(payload.get("subject"), "subject", 200)
            body = _text(payload.get("body"), "body", 8000)
            flags = ["--from", "human", "--to", to, "-s", subject, "-m", body, "--json"] + (["--notify"] if payload.get("notify", True) else [])
            result = self._gc(town, ["mail", "send"], flags, [])
            self._record_action(town, "mail_send", {"to": to, "subject": subject[:120], "ok": result["ok"]})
        elif action == "mail/reply":
            message = _name(payload.get("id"), "id")
            body = _text(payload.get("body"), "body", 8000)
            result = self._gc(town, ["mail", "reply"], ["-m", body, "--json", "--notify"], [message])
            self._record_action(town, "mail_reply", {"id": message, "ok": result["ok"]})
        elif action == "mail/mark":
            message = _name(payload.get("id"), "id")
            result = self._gc(town, ["mail", "mark-unread" if payload.get("unread") else "mark-read"], [], [message])
        elif action == "sessions/new":
            template = _name(payload.get("template"), "template")
            alias = payload.get("alias")
            flags = ["--no-attach", "--json"] + (["--alias", _name(alias, "alias")] if alias else [])
            result = self._gc(town, ["session", "new"], flags, [template], timeout=90)
            self._record_action(town, "session_new", {"template": template, "alias": alias, "ok": result["ok"]})
        elif action == "sessions/wake":
            session = _name(payload.get("session"), "session")
            result = self._gc(town, ["session", "wake"], [], [session])
            self._record_action(town, "session_wake", {"session": session, "ok": result["ok"]})
        elif action == "sessions/nudge":
            session = _name(payload.get("session"), "session")
            message = _text(payload.get("message"), "message", 4000)
            result = self._gc(town, ["session", "nudge"], [], [session, message])
            self._record_action(town, "session_nudge", {"session": session, "ok": result["ok"]})
        else:
            raise ActionError(f"unknown action {action}")
        return result

    def peek(self, town_name: Any, session: Any, lines: int = 80) -> dict[str, Any]:
        town = self._town(town_name)
        return self._gc(town, ["session", "peek"], ["--lines", str(max(10, min(int(lines), 400)))], [_name(session, "session")], timeout=30)

    # AgentsView and the live monitor ----------------------------------------------------------
    def agentsview(self, limit: int = 80) -> dict[str, Any]:
        now = time.time()
        if self._agentsview_cache and now - self._agentsview_cache[0] < 60:
            return self._agentsview_cache[1]
        roots = {name: str(town.city_root) for name, town in self.towns.items()}
        found: list[dict[str, Any]] = []
        cursor = None
        available = False
        for _ in range(8):
            # Town residents run opencode ACP sessions, which AgentsView classifies as one-shot and hides by default.
            url = f"{self.agentsview_url}/api/v1/sessions?limit=200&include_one_shot=true" + (f"&cursor={urllib.parse.quote(str(cursor))}" if cursor else "")
            data = _get_json(url, 3)
            if not isinstance(data, dict) or "sessions" not in data:
                break
            available = True
            for item in data["sessions"]:
                cwd = str(item.get("cwd") or "")
                town = next((name for name, root in roots.items() if cwd == root or cwd.startswith(root + "/")), None)
                if town is None and item.get("project") in roots:
                    town = item["project"]
                if town:
                    found.append({"town": town, "url": f"{self.agentsview_url}/sessions/{urllib.parse.quote(str(item.get('id')))}",
                                  **{key: item.get(key) for key in ("id", "project", "agent", "first_message", "started_at", "ended_at",
                                                                    "message_count", "health_grade", "outcome", "cwd")}})
            cursor = data.get("next_cursor")
            if not cursor:
                break
        found.sort(key=lambda item: str(item.get("started_at") or ""), reverse=True)
        payload = {"available": available, "url": self.agentsview_url, "sessions": found[:limit]}
        self._agentsview_cache = (now, payload)
        return payload

    def monitor(self) -> dict[str, Any]:
        messages = []
        for message in self.log.list(limit=400):
            rcp = message.get("rcp") or {}
            refusal = rcp.get("refusal") or {}
            body = message["envelope"]["body"]
            messages.append({
                "id": message["id"], "from": message["from"], "to": message["to"], "kind": message["kind"], "created": message["created"],
                "status": message["status"], "rcp": bool(rcp), "state": rcp.get("state"), "task": str(body.get("rcp_task_type") or "").rsplit("/", 1)[-1],
                "gates": rcp.get("gates"), "refusal_gate": refusal.get("gate"), "refusal_reason": refusal.get("reason"),
                "region": body.get("region"), "text": str(body.get("text") or "")[:180],
            })
        events = [event for event in self.log.events(None, limit=1000)
                  if event.get("kind") in AUTHORITY_EVENTS | {"cockpit_action", "rcp_stamped"}]
        nodes = [{"id": name, "display": town.display, "kind": town.kind} for name, town in self.towns.items()]
        sites = []
        for entry in self.sites()["towns"]:
            for site in entry.get("sites") or []:
                sites.append({"town": entry["town"], "site": site.get("site"), "reachable": site.get("reachable"), "driver": site.get("driver")})
        return {"now": time.time(), "nodes": nodes, "sites": sites, "messages": messages, "events": events[-300:]}

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
                if not self._loopback():
                    return
                if route == "/":
                    page = HTML_PATH.read_text(encoding="utf-8").replace("__COCKPIT_TOKEN__", state.token).replace("__AGENTSVIEW_URL__", state.agentsview_url)
                    self._send(HTTPStatus.OK, page.encode("utf-8"), "text/html; charset=utf-8")
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
                elif route == "/api/interfaces":
                    host = self.headers.get("Host") or "127.0.0.1:8390"
                    self._json(HTTPStatus.OK, state.interfaces(f"http://{host}/"))
                elif route == "/api/residents":
                    self._json(HTTPStatus.OK, state.residents())
                elif route == "/api/trust":
                    self._json(HTTPStatus.OK, state.trust())
                elif route == "/api/decisions":
                    self._json(HTTPStatus.OK, state.decisions())
                elif route == "/api/mail":
                    self._json(HTTPStatus.OK, state.mail_list(query.get("town", [""])[0]))
                elif route == "/api/sessions/peek":
                    self._json(HTTPStatus.OK, state.peek(query.get("town", [""])[0], query.get("session", [""])[0], int(query.get("lines", ["80"])[0])))
                elif route == "/api/agentsview":
                    self._json(HTTPStatus.OK, state.agentsview())
                elif route == "/api/monitor":
                    self._json(HTTPStatus.OK, state.monitor())
                elif route == "/api/events":
                    after = int(query.get("after", ["0"])[0])
                    self._json(HTTPStatus.OK, {"events": state.events_since(after)})
                elif route == "/api/events/stream":
                    self._stream(int(query.get("after", ["0"])[0]))
                elif route == "/healthz":
                    self._json(HTTPStatus.OK, {"ok": True})
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "no such route"})
            except ActionError as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except BrokenPipeError:
                pass
            except Exception as error:  # noqa: BLE001
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": type(error).__name__, "detail": str(error)})

        def _loopback(self) -> bool:
            host = (self.headers.get("Host") or "127.0.0.1").rsplit(":", 1)[0] if not (self.headers.get("Host") or "").startswith("[") else "[::1]"
            origin = self.headers.get("Origin")
            if host not in LOOPBACK_HOSTS or (origin and urllib.parse.urlparse(origin).hostname not in LOOPBACK_HOSTS):
                self._json(HTTPStatus.FORBIDDEN, {"error": "the cockpit answers loopback requests only"})
                return False
            return True

        def do_POST(self) -> None:
            route = urllib.parse.urlparse(self.path).path.rstrip("/")
            if not self._loopback():
                return
            if not secrets.compare_digest(self.headers.get("X-Cockpit-Token") or "", state.token):
                self._json(HTTPStatus.FORBIDDEN, {"error": "missing or wrong cockpit token (reload the page)"})
                return
            if not route.startswith("/api/"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "no such route"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_POST_BYTES:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body missing or too large"})
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                result = state.act(route[len("/api/"):], payload)
                self._json(HTTPStatus.OK, result)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "body is not JSON"})
            except ActionError as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
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
