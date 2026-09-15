"""Envelopes exchanged between towns and the shared SQLite exchange log.

The log is the audit trail the dashboard reads. Every envelope a town sends or
receives is recorded once (keyed by envelope id) and every processing step is
an event row. The `rcp` column is reserved for the JSON-LD message and its
validation report once the towns speak the Research Commons Protocol.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

KINDS = ("question", "answer", "notice")
MAX_BODY_BYTES = 1_048_576
SCHEMA_VERSION = 1


class EnvelopeError(ValueError):
    pass


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _town_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.isidentifier():
        raise EnvelopeError(f"{label} must be a town identifier")
    return value


@dataclass(frozen=True)
class Attachment:
    name: str
    sha256: str
    path: str | None = None
    media_type: str = "application/json"
    bytes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "sha256": self.sha256, "media_type": self.media_type}
        if self.path is not None:
            data["path"] = self.path
        if self.bytes is not None:
            data["bytes"] = self.bytes
        return data

    @classmethod
    def from_dict(cls, data: Any) -> Attachment:
        if not isinstance(data, dict) or not isinstance(data.get("name"), str) or not isinstance(data.get("sha256"), str):
            raise EnvelopeError("attachment needs string name and sha256")
        if "/" in data["name"] or data["name"].startswith("."):
            raise EnvelopeError("attachment name must be a plain file name")
        return cls(
            name=data["name"],
            sha256=data["sha256"],
            path=data.get("path") if isinstance(data.get("path"), str) else None,
            media_type=str(data.get("media_type") or "application/json"),
            bytes=data.get("bytes") if isinstance(data.get("bytes"), int) else None,
        )

    @classmethod
    def from_file(cls, path: Path, media_type: str | None = None) -> Attachment:
        guessed = media_type or ("application/json" if path.suffix == ".json" else "text/plain")
        return cls(name=path.name, sha256=sha256_file(path), path=str(path.resolve()), media_type=guessed, bytes=path.stat().st_size)


@dataclass(frozen=True)
class Envelope:
    id: str
    kind: str
    sender: str
    recipient: str
    created: str
    body: dict[str, Any]
    in_reply_to: str | None = None
    attachments: tuple[Attachment, ...] = field(default_factory=tuple)
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def new(
        cls,
        kind: str,
        sender: str,
        recipient: str,
        body: dict[str, Any],
        *,
        in_reply_to: str | None = None,
        attachments: tuple[Attachment, ...] = (),
    ) -> Envelope:
        envelope = cls(
            id=f"urn:uuid:{uuid.uuid4()}",
            kind=kind,
            sender=sender,
            recipient=recipient,
            created=now_iso(),
            body=dict(body),
            in_reply_to=in_reply_to,
            attachments=tuple(attachments),
        )
        envelope.validate()
        return envelope

    def validate(self) -> None:
        if self.kind not in KINDS:
            raise EnvelopeError(f"kind must be one of {KINDS}")
        if not self.id.startswith("urn:uuid:"):
            raise EnvelopeError("id must be a urn:uuid")
        _town_name(self.sender, "from")
        _town_name(self.recipient, "to")
        if self.sender == self.recipient:
            raise EnvelopeError("from and to must differ")
        if not isinstance(self.body, dict):
            raise EnvelopeError("body must be an object")
        if self.kind == "answer" and not self.in_reply_to:
            raise EnvelopeError("answer requires in_reply_to")
        if self.in_reply_to is not None and not str(self.in_reply_to).startswith("urn:uuid:"):
            raise EnvelopeError("in_reply_to must be a urn:uuid")
        text = self.body.get("text")
        if text is not None and not isinstance(text, str):
            raise EnvelopeError("body.text must be a string")
        if len(canonical(self.body).encode("utf-8")) > MAX_BODY_BYTES:
            raise EnvelopeError("body exceeds size limit")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "kind": self.kind,
            "from": self.sender,
            "to": self.recipient,
            "in_reply_to": self.in_reply_to,
            "created": self.created,
            "body": self.body,
            "attachments": [attachment.to_dict() for attachment in self.attachments],
        }

    @classmethod
    def from_dict(cls, data: Any) -> Envelope:
        if not isinstance(data, dict):
            raise EnvelopeError("envelope must be a JSON object")
        try:
            envelope = cls(
                id=str(data["id"]),
                kind=str(data["kind"]),
                sender=data["from"],
                recipient=data["to"],
                created=str(data.get("created") or now_iso()),
                body=data.get("body") if isinstance(data.get("body"), dict) else {},
                in_reply_to=data.get("in_reply_to"),
                attachments=tuple(Attachment.from_dict(item) for item in data.get("attachments") or []),
                schema_version=int(data.get("schema_version") or SCHEMA_VERSION),
            )
        except KeyError as error:
            raise EnvelopeError(f"envelope is missing {error.args[0]}") from error
        envelope.validate()
        return envelope

    @property
    def digest(self) -> str:
        return sha256_text(canonical(self.to_dict()))

    @property
    def text(self) -> str:
        return str(self.body.get("text") or "")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    in_reply_to TEXT,
    created TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    body TEXT NOT NULL,
    attachments TEXT NOT NULL,
    digest TEXT NOT NULL,
    status TEXT NOT NULL,
    rcp TEXT
);
CREATE INDEX IF NOT EXISTS messages_reply ON messages(in_reply_to);
CREATE INDEX IF NOT EXISTS messages_created ON messages(created);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    town TEXT NOT NULL,
    message_id TEXT,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_message ON events(message_id);
"""


class ExchangeLog:
    """SQLite-backed log shared by every town on this host."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._connection.executescript(_SCHEMA)

    def close(self) -> None:
        self._connection.close()

    def record(self, envelope: Envelope, *, town: str, direction: str, status: str) -> bool:
        """Insert the envelope if unseen; always log a direction event. Returns True when newly inserted."""
        assert direction in {"sent", "received"}
        data = envelope.to_dict()
        cursor = self._connection.execute(
            "INSERT OR IGNORE INTO messages (id, kind, sender, recipient, in_reply_to, created, first_seen, body,"
            " attachments, digest, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                envelope.id, envelope.kind, envelope.sender, envelope.recipient, envelope.in_reply_to,
                envelope.created, now_iso(), canonical(data["body"]), canonical(data["attachments"]),
                envelope.digest, status,
            ),
        )
        inserted = cursor.rowcount == 1
        if not inserted:
            self.set_status(envelope.id, status)
        self.event(town, direction, envelope.id, {"status": status, "digest": envelope.digest})
        return inserted

    def set_status(self, message_id: str, status: str) -> None:
        self._connection.execute("UPDATE messages SET status = ? WHERE id = ?", (status, message_id))

    def set_rcp(self, message_id: str, rcp: dict[str, Any]) -> None:
        self._connection.execute("UPDATE messages SET rcp = ? WHERE id = ?", (canonical(rcp), message_id))

    def event(self, town: str, kind: str, message_id: str | None = None, detail: dict[str, Any] | None = None) -> int:
        cursor = self._connection.execute(
            "INSERT INTO events (ts, town, message_id, kind, detail) VALUES (?, ?, ?, ?, ?)",
            (now_iso(), town, message_id, kind, canonical(detail or {})),
        )
        return int(cursor.lastrowid)

    def get(self, message_id: str) -> dict[str, Any] | None:
        row = self._connection.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return _message_row(row) if row else None

    def envelope(self, message_id: str) -> Envelope | None:
        row = self.get(message_id)
        return Envelope.from_dict(row["envelope"]) if row else None

    def list(self, *, limit: int = 50, town: str | None = None, since_seq: int | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        if town:
            clauses.append("(sender = ? OR recipient = ?)")
            params.extend([town, town])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM messages {where} ORDER BY created DESC, rowid DESC LIMIT ?", (*params, limit)
        ).fetchall()
        return [_message_row(row) for row in rows]

    def events(self, message_id: str | None = None, *, limit: int = 200, after: int = 0) -> list[dict[str, Any]]:
        if message_id:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE message_id = ? AND seq > ? ORDER BY seq LIMIT ?", (message_id, after, limit)
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (after, limit)
            ).fetchall()
        return [_event_row(row) for row in rows]

    def answers(self, message_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM messages WHERE in_reply_to = ? AND kind = 'answer' ORDER BY created", (message_id,)
        ).fetchall()
        return [_message_row(row) for row in rows]

    def recent_events(self, limit: int = 300, message_id: str | None = None) -> list[dict[str, Any]]:
        """Return the newest window, in sequence order (not the oldest page)."""
        where = "WHERE message_id = ?" if message_id else ""
        params = (message_id, limit) if message_id else (limit,)
        rows = self._connection.execute(
            f"SELECT * FROM events {where} ORDER BY seq DESC LIMIT ?", params
        ).fetchall()
        return [_event_row(row) for row in reversed(rows)]

    def pending_questions(self, town: str) -> list[dict[str, Any]]:
        """Questions addressed to `town` that were received but not yet dispatched to an agent."""
        rows = self._connection.execute(
            "SELECT m.* FROM messages m WHERE m.recipient = ? AND m.kind = 'question'"
            " AND (m.status IS NULL OR m.status NOT LIKE 'rcp-%')"  # RCP tasks are handled by the pipeline, not by an agent
            " AND EXISTS (SELECT 1 FROM events e WHERE e.message_id = m.id AND e.town = ? AND e.kind = 'received')"
            " AND NOT EXISTS (SELECT 1 FROM events e WHERE e.message_id = m.id AND e.town = ? AND e.kind = 'dispatched')"
            " AND NOT EXISTS (SELECT 1 FROM messages a WHERE a.in_reply_to = m.id AND a.kind = 'answer')"
            " ORDER BY m.created",
            (town, town, town),
        ).fetchall()
        return [_message_row(row) for row in rows]


def _message_row(row: sqlite3.Row) -> dict[str, Any]:
    body = json.loads(row["body"])
    attachments = json.loads(row["attachments"])
    return {
        "id": row["id"],
        "kind": row["kind"],
        "from": row["sender"],
        "to": row["recipient"],
        "in_reply_to": row["in_reply_to"],
        "created": row["created"],
        "first_seen": row["first_seen"],
        "digest": row["digest"],
        "status": row["status"],
        "rcp": json.loads(row["rcp"]) if row["rcp"] else None,
        "envelope": {
            "schema_version": SCHEMA_VERSION,
            "id": row["id"],
            "kind": row["kind"],
            "from": row["sender"],
            "to": row["recipient"],
            "in_reply_to": row["in_reply_to"],
            "created": row["created"],
            "body": body,
            "attachments": attachments,
        },
    }


def _event_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "seq": row["seq"],
        "ts": row["ts"],
        "town": row["town"],
        "message_id": row["message_id"],
        "kind": row["kind"],
        "detail": json.loads(row["detail"]),
    }
