"""CLI handlers for resources and credentials: Camelot (authority), holders (researchers), compute (riggers).

The CLI provides operator decisions (approve, deny, revoke); the protected local cockpit
also supports review and approval/denial. No peer-facing registrar endpoint can issue a credential. Holder keys live in ~/.gc/holders/<slug>.key and credentials in ~/.gc/holders/<slug>/credentials/.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import config
from .authority import credentials as creds
from .authority import keys, registrar
from .exchange import ExchangeLog

HOLDERS_DIR = Path.home() / ".gc" / "holders"


def _print(value: Any) -> None:
    json.dump(value, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False, default=str)
    sys.stdout.write("\n")


class ResourceCommandError(RuntimeError):
    pass


def http_json(url: str, body: Any | None = None, *, timeout: float = 30) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                     headers={"Content-Type": "application/json", "Accept": "application/json", "X-GC-Request": "pangenome-town"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as error:
        raise ResourceCommandError(f"{url} answered {error.code}: {error.read().decode('utf-8', 'replace')[:500]}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ResourceCommandError(f"could not reach {url}: {error}") from error


# Holders ---------------------------------------------------------------------------------------------
def holder_slug(holder_iri: str) -> str:
    tail = holder_iri.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9_.-]", "_", tail)[:64] or "holder"


def holder_paths(slug: str) -> tuple[Path, Path]:
    return HOLDERS_DIR / f"{slug}.key", HOLDERS_DIR / slug / "credentials"


def _registrar_url(town: config.TownConfig, explicit: str | None) -> str:
    url = explicit or (town.extra.get("trust") or {}).get("registrar")
    if not url:
        raise ResourceCommandError("no registrar URL: pass --registrar or set [trust].registrar in town.toml")
    return str(url).rstrip("/") + "/"


def holder_command(arguments: Any, town: config.TownConfig) -> int:
    slug = arguments.slug or holder_slug(arguments.holder)
    key_path, wallet = holder_paths(slug)
    command = arguments.holder_command
    if command == "new":
        key = keys.generate()
        keys.save_private(key, key_path)
        wallet.mkdir(parents=True, exist_ok=True)
        _print({"holder": arguments.holder, "slug": slug, "key_file": str(key_path), "publicKey": keys.public_key_text(key)})
        return 0
    if command == "wallet":
        items = []
        for path in sorted(wallet.glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            subject = document.get("credentialSubject") or {}
            items.append({"file": str(path), "id": document.get("id"), "type": document.get("type"), "issuer": document.get("issuer"),
                          "scope": subject.get("scope"), "dataset": subject.get("dataset"), "validUntil": document.get("validUntil")})
        _print({"holder": arguments.holder, "slug": slug, "credentials": items})
        return 0
    key = keys.load_private(key_path)
    url = _registrar_url(town, arguments.registrar)
    if command == "apply":
        subject = {name: value for name, value in (("dataset", arguments.dataset), ("scope", arguments.scope), ("protocol", arguments.protocol)) if value}
        if getattr(arguments, 'task', None):
            if not arguments.audience:
                raise ResourceCommandError('--task requires --audience')
            subject.update(taskDigest=creds.task_digest(json.loads(arguments.task.read_text())), audience=arguments.audience)
        body = {"holder": arguments.holder, "holder_key": keys.public_key_text(key), "credential_type": arguments.type,
                "issuer": arguments.issuer, "subject_fields": subject, "purpose": arguments.purpose}
        if arguments.dry_run:
            _print({"dry_run": True, "url": f"{url}v0/applications", "application": body})
            return 0
        _print(http_json(f"{url}v0/applications", body))
        return 0
    if command == "fetch":
        application = http_json(f"{url}v0/applications/{urllib.parse.quote(arguments.application)}")
        if application.get("state") != "approved":
            _print({"application": arguments.application, "state": application.get("state"), "reason": application.get("reason")})
            return 1
        token = str(application["credential"]).rstrip("/").rsplit("/", 1)[-1]
        document = http_json(f"{url}v0/credentials/{urllib.parse.quote(token)}")
        if (document.get("credentialSubject") or {}).get("holderKey") != keys.public_key_text(key):
            raise ResourceCommandError("the issued credential is bound to a different holder key; not stored")
        wallet.mkdir(parents=True, exist_ok=True)
        path = wallet / f"{token}.json"
        path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        _print({"stored": str(path), "id": document.get("id"), "type": document.get("type"), "validUntil": document.get("validUntil")})
        return 0
    raise ResourceCommandError(f"unknown holder command {command}")


def build_presentation(peer: config.TownConfig, task: dict[str, Any], *, holder: str, slug: str | None, credential_files: list[Path],
                       registrar_url: str | None, identity_tokens: list[dict] | None = None,
                       accreditation_files: list[Path] | None = None) -> dict[str, Any]:
    from .rcp import town_iri

    slug = slug or holder_slug(holder)
    key_path, wallet = holder_paths(slug)
    key = keys.load_private(key_path)
    files = credential_files or sorted(wallet.glob("*.json"))
    documents = [json.loads(Path(path).read_text(encoding="utf-8")) for path in files]
    if not documents and not identity_tokens:
        raise ResourceCommandError(f"no credentials in {wallet}; run `pangenome-town holder fetch` first")
    accreditations = registrar.http_accreditations(registrar_url) if registrar_url else []
    accreditations.extend(json.loads(p.read_text()) for p in accreditation_files or [])
    return creds.present(holder=holder, holder_key=key, credentials=documents, accreditations=accreditations, task=task,
                         audience=town_iri(peer.name), identity_tokens=identity_tokens)


# Authority (Camelot) -------------------------------------------------------------------------------
def registry_for(town: config.TownConfig, arguments: Any) -> registrar.Registry:
    settings = town.extra.get("authority") or {}
    root = Path(arguments.registry) if getattr(arguments, "registry", None) else town.city_root / str(settings.get("registry", "registry"))
    key_dir = Path(arguments.key_dir) if getattr(arguments, "key_dir", None) else Path(str(settings.get("key_dir", f"~/.gc/authority/{town.name}")))
    return registrar.Registry(root, key_dir.expanduser(), namespace=settings.get("namespace", registrar.AUTHORITY))


def _public_issuer(record: dict[str, Any]) -> dict[str, Any]:
    return {"id": record["id"], "slug": record.get("slug"), "name": record.get("name"), "role": record.get("role"), "publicKey": record.get("publicKey"),
            "accreditedBy": sorted({item.get("issuer") for item in record.get("accreditations") or []})}


def authority_card(town: config.TownConfig, registry: registrar.Registry, base_url: str) -> dict[str, Any]:
    base = base_url.rstrip("/")
    return {
        "name": town.display,
        "description": "Authority town of The Academic Wasteland: demo ethics board, data access committee, and accreditation council. "
                       "Applications are reviewed by residents; every decision is made by a human operator.",
        "url": base,
        "version": "0.1.0",
        "kind": "authority",
        "skills": [{"id": "credential-application", "name": "CredentialApplication",
                    "description": "POST /v0/applications with holder, holder_key, credential_type, issuer, subject_fields (dataset, scope, protocol), purpose",
                    "tags": ["credentials", "ethics", "data-access"]}],
        "issuers": [_public_issuer(record) for record in registry.issuers()],
        "endpoints": {"keys": f"{base}/v0/keys", "applications": f"{base}/v0/applications", "credential": f"{base}/v0/credentials/{{id}}",
                      "status": f"{base}/v0/status/{{id}}"},
        "iriBase": registry.namespace,
        "decisions": "human approval required (pangenome-town authority approve|deny|revoke)",
        "town": {"name": town.name, "kind": town.kind, "peers": sorted(town.peers)},
    }


def serve_authority(town: config.TownConfig, registry: registrar.Registry, socket_path: str, log: ExchangeLog | None) -> None:
    from http import HTTPStatus

    from .envoy import ThreadingUnixHTTPServer

    base_handler = registrar.make_handler(registry)
    base_url = os.environ.get("GC_SERVICE_PUBLIC_URL") or f"{town.supervisor_url.rstrip('/')}/v0/city/{town.name}/svc/envoy"

    class Handler(base_handler):  # type: ignore[misc, valid-type]
        server_version = "camelot-envoy/0.1"

        def do_GET(self) -> None:
            route = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            if route == "/.well-known/agent-card.json":
                self._json(HTTPStatus.OK, authority_card(town, registry, base_url))
            elif route == "/v0/town":
                self._json(HTTPStatus.OK, {"name": town.name, "display": town.display, "kind": town.kind, "peers": sorted(town.peers),
                                           "pending_applications": len(registry.applications("pending"))})
            else:
                super().do_GET()

        def do_POST(self) -> None:
            before = {item["id"] for item in registry.applications()}
            super().do_POST()
            if log is not None:
                for item in registry.applications():
                    if item["id"] not in before:
                        log.event(town.name, "application_received", item["id"], {"holder": item["holder"], "type": item["credential_type"], "issuer": item["issuer"]})

    try:
        os.remove(socket_path)
    except FileNotFoundError:
        pass
    with ThreadingUnixHTTPServer(socket_path, Handler) as server:
        if log is not None:
            log.event(town.name, "envoy_started", None, {"socket": socket_path, "kind": "authority"})
        sys.stderr.write(f"[envoy {town.name}] authority registrar listening on {socket_path}\n")
        server.serve_forever()


def review_commands(town: config.TownConfig, application: dict[str, Any]) -> dict[str, str]:
    """Commands work outside the town directory and never invent review evidence."""
    path = town.config_path or town.city_root / "town.toml"
    prefix = shlex.join(["pangenome-town", "--town", str(path.resolve()), "authority"])
    app_id = shlex.quote(application["id"])
    approve = f"{prefix} approve {app_id}"
    if application.get("relay_sender"):
        approve += ' --evidence-ref "${EVIDENCE_REF:?Set EVIDENCE_REF to your private review reference first}"'
    return {
        "approve": approve,
        "deny": f'{prefix} deny {app_id} --reason "${{DENIAL_REASON:?Set DENIAL_REASON first}}"',
    }


def notify_pending(town: config.TownConfig, registry: registrar.Registry, *, dry_run: bool = False) -> list[dict[str, Any]]:
    """Mail the human operator once per new pending application."""
    state_path = town.state_dir / "authority" / "notified.json"
    seen = set(json.loads(state_path.read_text(encoding="utf-8"))) if state_path.exists() else set()
    sent = []
    for application in registry.applications("pending"):
        if application["id"] in seen:
            continue
        commands = review_commands(town, application)
        subject = f"{town.display}: {application['credential_type']} application {application['id']}"
        body = (f"Holder: {application['holder']}\nIssuer: {application['issuer']}\nFields: {json.dumps(application['subject_fields'])}\n"
                f"Purpose: {application['purpose']}\n\nReview the evidence before approving. For a relay application, "
                f"set EVIDENCE_REF to the private review record first.\n\nDecide with:\n  {commands['approve']}\n"
                f"  {commands['deny']}\n")
        if not dry_run:
            subprocess.run(["gc", "mail", "send", "human", "-s", subject, "-m", body], cwd=town.city_root, capture_output=True, text=True, check=False, timeout=60)
            seen.add(application["id"])
        sent.append({"application": application["id"], "subject": subject})
    if not dry_run:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(sorted(seen)) + "\n", encoding="utf-8")
    return sent


def authority_command(arguments: Any, town: config.TownConfig, log: ExchangeLog) -> int:
    registry = registry_for(town, arguments)
    command = arguments.authority_command
    operator = str((town.extra.get("authority") or {}).get("operator") or os.environ.get("USER") or "operator")
    if command == "init-issuer":
        _print(_public_issuer(registry.init_issuer(arguments.slug, arguments.name, arguments.role)))
    elif command == "accredit":
        _print(registry.accredit(arguments.by, arguments.subject, list(arguments.roles), valid_days=arguments.valid_days,
                                 types=arguments.types, scopes=arguments.scopes, datasets=arguments.datasets,
                                 delegation_depth=arguments.delegation_depth))
    elif command == "issuers":
        _print([_public_issuer(record) for record in registry.issuers()])
    elif command == "applications":
        applications = registry.applications(arguments.state)
        if arguments.check:
            return 0 if applications else 1
        _print(applications)
    elif command == "show":
        if arguments.id.startswith("app-"):
            _print(registry.application(arguments.id))
        else:
            _print({"credential": registry.credential(arguments.id), "status": registry.status(arguments.id)})
    elif command == "approve":
        document = registry.approve(arguments.application, valid_days=arguments.valid_days, decided_by=arguments.decided_by or operator, evidence_ref=arguments.evidence_ref)
        log.event(town.name, "credential_issued", document["id"], {"application": arguments.application, "type": document["type"], "holder": document["credentialSubject"]["id"]})
        _print(document)
    elif command == "deny":
        record = registry.deny(arguments.application, reason=arguments.reason, decided_by=arguments.decided_by or operator)
        log.event(town.name, "application_denied", arguments.application, {"reason": arguments.reason})
        _print(record)
    elif command == "revoke":
        registry.revoke(arguments.credential)
        log.event(town.name, "credential_revoked", arguments.credential, {})
        _print({"revoked": arguments.credential, "status": registry.status(arguments.credential)})
    elif command == "notify":
        _print({"notified": notify_pending(town, registry, dry_run=arguments.dry_run)})
    elif command == "agent-card":
        _print(authority_card(town, registry, f"{town.supervisor_url.rstrip('/')}/v0/city/{town.name}/svc/envoy"))
    elif command == "serve":
        socket_path = arguments.socket or os.environ.get("GC_SERVICE_SOCKET")
        if not socket_path:
            raise ResourceCommandError("serve needs --socket or GC_SERVICE_SOCKET")
        serve_authority(town, registry, socket_path, log)
    else:
        raise ResourceCommandError(f"unknown authority command {command}")
    return 0


# Compute (riggers) ---------------------------------------------------------------------------------
def compute_command(arguments: Any, town: config.TownConfig, log: ExchangeLog) -> int:
    from .compute import TEMPLATES, load_sites, plan, validate
    from .compute.runner import pick_site
    from .rcp import capability, pipeline
    from .tools.graph import Region

    command = arguments.compute_command
    if command == "sites":
        _, sites = capability.site_facts(town)
        _print({"sites": sites, "templates": {name: pick_site(town, name)[0].name if pick_site(town, name)[0] else None for name in TEMPLATES}})
        return 0
    if command in {"plan", "validate"}:
        sites = {site.name: site for site in load_sites(town)}
        if command == "plan":
            region = Region.parse(arguments.region, town.default_assembly) if arguments.region else None
            site = sites.get(arguments.site) if arguments.site else pick_site(town, arguments.template)[0]
            if site is None:
                raise ResourceCommandError(f"no usable site for {arguments.template} (see `pangenome-town compute sites`)")
            spec = plan(arguments.template, region=region, site=site, samples=list(town.samples), threads=arguments.threads, town=town)
        else:
            spec = json.loads(Path(arguments.spec).read_text(encoding="utf-8"))
            site = sites.get(str(spec.get("site")))
            if site is None:
                raise ResourceCommandError(f"spec names unknown site {spec.get('site')!r}")
        problems = validate(spec, site, samples=list(town.samples))
        _print({"spec": spec, "site": site.name, "problems": problems})
        return 0 if not problems else 1
    node = pipeline.Node(town, log=log)
    if command == "pending":
        pending = []
        for directory in sorted(node.tasks_dir.iterdir()):
            if not (directory / "status.json").exists():
                continue
            record = pipeline.TaskRecord.load(directory)
            task = json.loads((directory / "task.jsonld").read_text(encoding="utf-8"))
            if record.state == "working" and str(task.get("taskType")) in pipeline.COMPUTE_KINDS:
                pending.append({"id": record.id, "template": pipeline.COMPUTE_KINDS[str(task.get("taskType"))], "message": record.message})
        if arguments.check:
            return 0 if pending else 1
        _print({"town": town.name, "pending": pending})
        return 0
    if command == "run":
        spec = json.loads(Path(arguments.workflow).read_text(encoding="utf-8")) if arguments.workflow else None
        record = node.resume(arguments.task, spec=spec)
        _print({"id": record.id, "state": record.state, "message": record.message, "gates": record.gates, "refusal": record.refusal,
                "artifacts": [item["name"] for item in record.artifacts]})
        return 0 if record.state == "completed" else 1
    raise ResourceCommandError(f"unknown compute command {command}")
