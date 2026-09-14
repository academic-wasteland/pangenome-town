"""The registrar: a file-backed store for demo issuers, applications, and credentials.

Public material (issuer keys, accreditations, credentials, revocations) lives
under `root`; private issuer keys live under `key_dir` with mode 0600 and never
leave it. The HTTP surface is read-only apart from submitting applications:
approving, denying, and revoking are human decisions made through the CLI.
"""

from __future__ import annotations

import json
import os
import re
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from . import AUTHORITY, CREDENTIAL_TYPES, issuer_iri
from . import credentials as credmod
from . import keys as keymod

MAX_APPLICATION_BYTES = 64 * 1024
SLUG = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
APP_ID = re.compile(r"^app-[0-9a-f]{8}$")
TOKEN = re.compile(r"^[0-9a-f-]{36}$")
APPLICATION_FIELDS = {"holder", "holder_key", "credential_type", "issuer", "subject_fields", "purpose"}


class RegistryError(ValueError):
    pass


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _token(credential_id: str) -> str:
    return credential_id.rstrip("/").rsplit("/", 1)[-1]


class Registry:
    def __init__(self, root: Path, key_dir: Path):
        self.root = Path(root)
        self.key_dir = Path(key_dir)
        for sub in ("issuers", "applications", "credentials"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # Issuers -----------------------------------------------------------------------
    def _issuer_path(self, slug: str) -> Path:
        if not isinstance(slug, str) or not SLUG.match(slug):
            raise RegistryError(f"invalid issuer slug {slug!r}")
        return self.root / "issuers" / f"{slug}.json"

    def _key(self, slug: str):
        return keymod.load_private(self.key_dir / f"{slug}.key")

    def init_issuer(self, slug: str, name: str, role: str) -> dict[str, Any]:
        path = self._issuer_path(slug)
        if path.exists():
            return _read(path)
        key_path = self.key_dir / f"{slug}.key"
        key = keymod.load_private(key_path) if key_path.exists() else keymod.generate()
        if not key_path.exists():
            keymod.save_private(key, key_path)
        record = {"id": issuer_iri(slug), "slug": slug, "name": name, "role": role,
                  "publicKey": keymod.public_key_text(key), "accreditations": []}
        _write(path, record)
        return record

    def issuer(self, slug: str) -> dict[str, Any] | None:
        path = self._issuer_path(slug)
        return _read(path) if path.exists() else None

    def issuers(self) -> list[dict[str, Any]]:
        return [_read(path) for path in sorted((self.root / "issuers").glob("*.json"))]

    def _slug_for(self, iri: str) -> str | None:
        for record in self.issuers():
            if record["id"] == iri:
                return record["slug"]
        return None

    def directory(self) -> dict[str, str]:
        return {record["id"]: record["publicKey"] for record in self.issuers()}

    def accredit(self, accreditor_slug: str, subject_slug: str, roles: list[str], valid_days: int = 365) -> dict[str, Any]:
        accreditor = self.issuer(accreditor_slug)
        subject = self.issuer(subject_slug)
        if accreditor is None or subject is None:
            raise RegistryError("both issuers must exist before accreditation")
        if accreditor_slug == subject_slug:
            raise RegistryError("an issuer cannot accredit itself")
        document = credmod.accreditation(accreditor=accreditor["id"], accreditor_key=self._key(accreditor_slug),
                                         subject_issuer=subject["id"], subject_public_key=subject["publicKey"],
                                         roles=roles, valid_days=valid_days)
        subject["accreditations"].append(document)
        _write(self._issuer_path(subject_slug), subject)
        _write(self.root / "credentials" / f"{_token(document['id'])}.json", document)
        return document

    def accreditations_for(self, iri: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        pending = [iri]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            slug = self._slug_for(current)
            record = self.issuer(slug) if slug else None
            for document in (record or {}).get("accreditations", []):
                found.append(document)
                pending.append(document.get("issuer"))
        return found

    # Applications ------------------------------------------------------------------
    def _app_path(self, app_id: str) -> Path:
        if not isinstance(app_id, str) or not APP_ID.match(app_id):
            raise RegistryError(f"invalid application id {app_id!r}")
        return self.root / "applications" / f"{app_id}.json"

    def apply(self, *, holder: str, holder_key_text: str, credential_type: str, issuer_slug: str,
              subject_fields: dict[str, Any], purpose: str) -> dict[str, Any]:
        if credential_type not in CREDENTIAL_TYPES or credential_type == "Accreditation":
            raise RegistryError(f"cannot apply for credential type {credential_type!r}")
        if self.issuer(issuer_slug) is None:
            raise RegistryError(f"unknown issuer {issuer_slug!r}")
        if not isinstance(holder, str) or not urllib.parse.urlparse(holder).scheme:
            raise RegistryError("holder must be an absolute IRI")
        try:
            keymod.parse_public(holder_key_text)
        except keymod.AuthorityKeyError as error:
            raise RegistryError(f"holder key: {error}") from error
        if not isinstance(subject_fields, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in subject_fields.items()):
            raise RegistryError("subject_fields must map strings to strings")
        if {"id", "holderKey"} & set(subject_fields):
            raise RegistryError("subject_fields may not set id or holderKey")
        if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > 2000:
            raise RegistryError("purpose must be a non-empty string of at most 2000 characters")
        app_id = f"app-{uuid.uuid4().hex[:8]}"
        record = {"id": app_id, "state": "pending", "holder": holder, "holder_key": holder_key_text,
                  "credential_type": credential_type, "issuer": issuer_slug, "subject_fields": dict(subject_fields),
                  "purpose": purpose, "created": _now()}
        _write(self._app_path(app_id), record)
        return record

    def application(self, app_id: str) -> dict[str, Any] | None:
        path = self._app_path(app_id)
        return _read(path) if path.exists() else None

    def applications(self, state: str | None = None) -> list[dict[str, Any]]:
        records = [_read(path) for path in sorted((self.root / "applications").glob("app-*.json"))]
        return [record for record in records if state is None or record["state"] == state]

    def approve(self, app_id: str, *, valid_days: int = 30, decided_by: str) -> dict[str, Any]:
        record = self.application(app_id)
        if record is None:
            raise RegistryError(f"unknown application {app_id}")
        if record["state"] != "pending":
            raise RegistryError(f"application {app_id} is {record['state']}, not pending")
        issuer = self.issuer(record["issuer"])
        if issuer is None:
            raise RegistryError(f"issuer {record['issuer']} disappeared")
        subject = {**record["subject_fields"], "id": record["holder"], "holderKey": record["holder_key"], "purpose": record["purpose"]}
        document = credmod.issue(issuer=issuer["id"], issuer_key=self._key(record["issuer"]), types=record["credential_type"],
                                 subject=subject, valid_days=valid_days)
        _write(self.root / "credentials" / f"{_token(document['id'])}.json", document)
        record.update({"state": "approved", "credential": document["id"], "decided_by": decided_by, "decided": _now()})
        _write(self._app_path(app_id), record)
        return document

    def deny(self, app_id: str, *, reason: str, decided_by: str) -> dict[str, Any]:
        record = self.application(app_id)
        if record is None:
            raise RegistryError(f"unknown application {app_id}")
        if record["state"] != "pending":
            raise RegistryError(f"application {app_id} is {record['state']}, not pending")
        record.update({"state": "denied", "reason": reason, "decided_by": decided_by, "decided": _now()})
        _write(self._app_path(app_id), record)
        return record

    # Credentials -------------------------------------------------------------------
    def _revoked(self) -> list[str]:
        path = self.root / "revoked.json"
        return list(_read(path)) if path.exists() else []

    def credential(self, credential_id: str) -> dict[str, Any] | None:
        token = _token(credential_id)
        if not TOKEN.match(token):
            return None
        path = self.root / "credentials" / f"{token}.json"
        return _read(path) if path.exists() else None

    def revoke(self, credential_id: str) -> None:
        document = self.credential(credential_id)
        if document is None:
            raise RegistryError(f"unknown credential {credential_id}")
        revoked = self._revoked()
        if document["id"] not in revoked:
            revoked.append(document["id"])
            _write(self.root / "revoked.json", revoked)

    def status(self, credential_id: str) -> str:
        document = self.credential(credential_id)
        if document is None:
            return "unknown"
        return "revoked" if document["id"] in self._revoked() else "active"


def status_checker_for(registry: Registry) -> Callable[[dict], str]:
    def check(document: dict) -> str:
        status = document.get("credentialStatus") if isinstance(document, dict) else None
        reference = status.get("id") if isinstance(status, dict) else None
        reference = reference or (document.get("id") if isinstance(document, dict) else None)
        return registry.status(reference) if isinstance(reference, str) else "unknown"

    return check


def http_status_checker(base_urls: dict[str, str], timeout: float = 5) -> Callable[[dict], str]:
    def check(document: dict) -> str:
        try:
            reference = document["credentialStatus"]["id"]
            for prefix, base in base_urls.items():
                if reference.startswith(prefix):
                    token = _token(reference)
                    if not TOKEN.match(token):
                        return "unknown"
                    url = f"{base.rstrip('/')}/v0/status/{token}"
                    request = urllib.request.Request(url, headers={"Accept": "application/json", "X-GC-Request": "pangenome-town"})
                    with urllib.request.urlopen(request, timeout=timeout) as response:
                        status = json.loads(response.read().decode("utf-8")).get("status")
                    return status if status in {"active", "revoked", "unknown"} else "unknown"
        except Exception:  # noqa: BLE001 - unreachable or malformed status is unknown
            return "unknown"
        return "unknown"

    return check


def _get_json(url: str, timeout: float) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "X-GC-Request": "pangenome-town"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def http_directory(url: str, timeout: float = 5) -> dict[str, str]:
    try:
        payload = _get_json(f"{url.rstrip('/')}/v0/keys", timeout)
        return {item["id"]: item["publicKey"] for item in payload.get("issuers", []) if isinstance(item.get("id"), str) and isinstance(item.get("publicKey"), str)}
    except Exception:  # noqa: BLE001
        return {}


def http_accreditations(url: str, timeout: float = 5) -> list[dict[str, Any]]:
    try:
        payload = _get_json(f"{url.rstrip('/')}/v0/keys", timeout)
        return [doc for item in payload.get("issuers", []) for doc in item.get("accreditations", []) if isinstance(doc, dict)]
    except Exception:  # noqa: BLE001
        return []


def _validate_application(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RegistryError("application must be a JSON object")
    unknown = set(payload) - APPLICATION_FIELDS
    if unknown:
        raise RegistryError(f"unknown fields: {sorted(unknown)}")
    missing = {"holder", "holder_key", "credential_type", "issuer", "purpose"} - set(payload)
    if missing:
        raise RegistryError(f"missing fields: {sorted(missing)}")
    for name in ("holder", "holder_key", "credential_type", "issuer", "purpose"):
        if not isinstance(payload[name], str):
            raise RegistryError(f"{name} must be a string")
    return payload


def make_handler(registry: Registry) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "pangenome-town-registrar/0.1"

        def log_message(self, format: str, *args: Any) -> None:
            sys.stderr.write(f"[registrar] {self.command} {self.path} {format % args}\n")

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            route = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            try:
                if route == "/healthz":
                    self._json(HTTPStatus.OK, {"ok": True, "service": "registrar"})
                elif route == "/v0/keys":
                    issuers = [{key: record[key] for key in ("id", "name", "role", "publicKey", "accreditations")} for record in registry.issuers()]
                    self._json(HTTPStatus.OK, {"issuers": issuers, "base": AUTHORITY})
                elif route.startswith("/v0/credentials/"):
                    document = registry.credential(urllib.parse.unquote(route[len("/v0/credentials/"):]))
                    self._json(HTTPStatus.OK, document) if document else self._json(HTTPStatus.NOT_FOUND, {"error": "unknown credential"})
                elif route.startswith("/v0/status/"):
                    token = urllib.parse.unquote(route[len("/v0/status/"):])
                    if not TOKEN.match(token):
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "malformed credential id"})
                    else:
                        self._json(HTTPStatus.OK, {"id": f"{AUTHORITY}credentials/{token}", "status": registry.status(token)})
                elif route == "/v0/applications":
                    self._json(HTTPStatus.OK, {"applications": registry.applications()})
                elif route.startswith("/v0/applications/"):
                    try:
                        record = registry.application(urllib.parse.unquote(route[len("/v0/applications/"):]))
                    except RegistryError as error:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                        return
                    self._json(HTTPStatus.OK, record) if record else self._json(HTTPStatus.NOT_FOUND, {"error": "unknown application"})
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "no such route"})
            except Exception as error:  # noqa: BLE001
                traceback.print_exc()
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": type(error).__name__, "detail": str(error)})

        def do_POST(self) -> None:
            route = urllib.parse.urlparse(self.path).path.rstrip("/")
            if route != "/v0/applications":
                self._json(HTTPStatus.NOT_FOUND, {"error": "no such route"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_APPLICATION_BYTES:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body missing or too large"})
                return
            try:
                payload = _validate_application(json.loads(self.rfile.read(length).decode("utf-8")))
                record = registry.apply(holder=payload["holder"], holder_key_text=payload["holder_key"],
                                        credential_type=payload["credential_type"], issuer_slug=payload["issuer"],
                                        subject_fields=payload.get("subject_fields") or {}, purpose=payload["purpose"])
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "body is not JSON", "detail": str(error)})
                return
            except RegistryError as error:
                self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})
                return
            self._json(HTTPStatus.CREATED, record)

    return Handler


def serve(registry: Registry, socket_path: str) -> None:
    from ..envoy import ThreadingUnixHTTPServer

    try:
        os.remove(socket_path)
    except FileNotFoundError:
        pass
    with ThreadingUnixHTTPServer(socket_path, make_handler(registry)) as server:
        sys.stderr.write(f"[registrar] listening on {socket_path}\n")
        server.serve_forever()
