"""The Wasteland commons as this town's reputation ledger.

Wasteland (github.com/gastownhall/wasteland) is a shared Dolt database with
`rigs`, `wanted`, `completions`, and `stamps` tables. Towns write rows through
the `research_commons.wasteland` projection (task -> wanted, contribution ->
completion, validation report -> stamp) and read reputation back from stamps.

Minimal deployment: both towns on one workstation share one local commons
(created with `wl create <org>/commons --local-only`). Federation through Dolt
forks and pull requests is a later step; nothing here depends on it.

Reputation score of a handle (the plan's formula, 180-day half-life):

    score = sum over stamps on that handle of
            severity_weight * confidence * (2 * quality - 1) * exp(-age_days / 180)

with quality = valence.quality in [0, 1] (1.0 for an `entailed` report, 0.5 for
`unknown`, 0 otherwise), severity weights leaf 1, branch 2, root 3, and self
stamps excluded by the schema itself (`author <> subject`).
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from research_commons import wasteland as wl

from ..config import TownConfig

SEVERITY_WEIGHT = {"leaf": 1.0, "branch": 2.0, "root": 3.0}
HALF_LIFE_DAYS = 180.0
WL_CONFIG_DIR = Path.home() / ".config" / "wasteland" / "wastelands"


class CommonsError(RuntimeError):
    pass


def handle_for(iri: str) -> str:
    """Map a requester or agent IRI to a Wasteland rig handle.

    Town agents live under https://w3id.org/academic-wasteland/<town>/...; the
    town name is their handle. Anything else keeps its IRI as the handle so a
    person (ORCID) can accumulate stamps too.
    """
    prefix = "https://w3id.org/academic-wasteland/"
    if iri.startswith(prefix):
        rest = iri[len(prefix):]
        town = rest.split("/", 1)[0]
        if town and town not in {"pangenome-town"}:
            return town
    return iri[:255]


def default_commons_dir() -> Path | None:
    """The local directory of the (single) wasteland joined with `wl`."""
    if not WL_CONFIG_DIR.is_dir():
        return None
    for path in sorted(WL_CONFIG_DIR.glob("*/*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        local = data.get("local_dir")
        if isinstance(local, str) and Path(local).is_dir():
            return Path(local)
    return None


@dataclass(frozen=True)
class Standing:
    handle: str
    score: float
    stamps: int
    authors: int
    last_stamp: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"handle": self.handle, "score": round(self.score, 3), "stamps": self.stamps, "authors": self.authors, "last_stamp": self.last_stamp}


class Commons:
    """One Wasteland commons database, driven through the `dolt` CLI."""

    def __init__(self, directory: Path, *, dolt: str | None = None, commit: bool = True):
        self.directory = directory
        self.dolt = dolt or shutil.which("dolt") or "dolt"
        self.commit = commit
        if not (directory / ".dolt").is_dir():
            raise CommonsError(f"{directory} is not a Dolt database (run `wl create <org>/commons --local-only`)")

    @classmethod
    def for_town(cls, town: TownConfig) -> Commons | None:
        rcp = town.extra.get("rcp") or {}
        configured = rcp.get("commons_dir")
        if configured is not None and not configured:
            return None  # commons_dir = "" opts out of any ledger
        directory = Path(configured).expanduser() if configured else default_commons_dir()
        if directory is None:
            return None
        if not directory.is_absolute():
            directory = (town.city_root / directory).resolve()
        if not (directory / ".dolt").is_dir():
            return None
        return cls(directory)

    # Low-level -----------------------------------------------------------------------
    def query(self, sql: str) -> list[dict[str, Any]]:
        result = subprocess.run([self.dolt, "sql", "-q", sql, "-r", "json"], cwd=self.directory, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise CommonsError(f"dolt sql failed: {result.stderr.strip()[-500:]}")
        if not result.stdout.strip():
            return []
        payload = json.loads(result.stdout)
        return list(payload.get("rows") or [])

    def execute(self, statements: list[str], message: str) -> None:
        for statement in statements:
            result = subprocess.run([self.dolt, "sql", "-q", statement], cwd=self.directory, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                raise CommonsError(f"dolt sql failed: {result.stderr.strip()[-500:]}")
        if self.commit:
            subprocess.run([self.dolt, "add", "-A"], cwd=self.directory, capture_output=True, text=True, check=False)
            result = subprocess.run([self.dolt, "commit", "-m", message], cwd=self.directory, capture_output=True, text=True, check=False)
            if result.returncode != 0 and "nothing to commit" not in (result.stdout + result.stderr):
                raise CommonsError(f"dolt commit failed: {result.stderr.strip()[-500:]}")

    # Rows ----------------------------------------------------------------------------
    def ensure_rig(self, handle: str, *, display_name: str, hop_uri: str | None = None, rig_type: str = "agent", email: str | None = None) -> None:
        if self.query(f"SELECT handle FROM rigs WHERE handle = {wl._literal(handle)}"):
            return
        now = datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
        row = {
            "handle": handle, "display_name": display_name, "dolthub_org": None, "hop_uri": hop_uri, "owner_email": email,
            "gt_version": "gascity-1.4.1", "trust_level": 1, "registered_at": now, "last_seen": now, "rig_type": rig_type, "parent_rig": None,
        }
        columns = ", ".join(f"`{key}`" for key in row)
        values = ", ".join(wl._literal(value) for value in row.values())
        self.execute([f"INSERT INTO `rigs` ({columns}) VALUES ({values});"], f"wasteland: register rig {handle}")

    def post_task(self, task: dict[str, Any], *, posted_by: str, project: str = "pangenome") -> str:
        row = wl.task_to_wanted(task, posted_by=posted_by, project=project)
        self.execute([wl.insert_sql("wanted", row)], f"rcp: wanted {row['id']} posted by {posted_by}")
        return row["id"]

    def post_completion(self, contribution: dict[str, Any], *, completed_by: str, hop_uri: str | None = None) -> str:
        row = wl.contribution_to_completion(contribution, completed_by=completed_by, hop_uri=hop_uri)
        statements = [
            wl.insert_sql("completions", row),
            f"UPDATE `wanted` SET `status` = 'in_review', `claimed_by` = {wl._literal(completed_by)}, `updated_at` = NOW() WHERE `id` = {wl._literal(row['wanted_id'])} AND `status` IN ('open', 'claimed');",
        ]
        self.execute(statements, f"rcp: completion {row['id']} by {completed_by}")
        return row["id"]

    def post_stamp(self, report: dict[str, Any], *, author: str, subject: str, completion: str, hop_uri: str | None = None) -> str:
        row = wl.report_to_stamp(report, author=author, subject=subject, completion=completion, hop_uri=hop_uri)
        status = report["status"]
        statements = [wl.insert_sql("stamps", row)]
        if status == "entailed":
            statements.append(
                f"UPDATE `completions` SET `validated_by` = {wl._literal(author)}, `stamp_id` = {wl._literal(row['id'])}, `validated_at` = NOW() WHERE `id` = {wl._literal(completion)};"
            )
            statements.append(
                f"UPDATE `wanted` SET `status` = 'completed', `updated_at` = NOW() WHERE `id` = (SELECT `wanted_id` FROM `completions` WHERE `id` = {wl._literal(completion)});"
            )
        self.execute(statements, f"rcp: stamp {row['id']} by {author} on {subject} ({status})")
        return row["id"]

    # Reputation ----------------------------------------------------------------------
    def stamps_on(self, handle: str) -> list[dict[str, Any]]:
        return self.query(
            f"SELECT id, author, subject, valence, confidence, severity, context_id, message, created_at FROM stamps WHERE subject = {wl._literal(handle)} ORDER BY created_at"
        )

    def standing(self, handle: str, *, now: datetime | None = None) -> Standing:
        return standing_from_stamps(handle, self.stamps_on(handle), now=now)

    def leaderboard(self) -> list[dict[str, Any]]:
        handles = {row["handle"] for row in self.query("SELECT handle FROM rigs")}
        handles |= {row["subject"] for row in self.query("SELECT DISTINCT subject FROM stamps")}
        board = [self.standing(handle).as_dict() for handle in sorted(handles)]
        return sorted(board, key=lambda item: -item["score"])

    def rows(self, table: str, limit: int = 100) -> list[dict[str, Any]]:
        if table not in {"rigs", "wanted", "completions", "stamps"}:
            raise CommonsError(f"unknown table {table}")
        order = {"rigs": "registered_at", "wanted": "created_at", "completions": "completed_at", "stamps": "created_at"}[table]
        return self.query(f"SELECT * FROM `{table}` ORDER BY `{order}` DESC LIMIT {int(limit)}")


def standing_from_stamps(handle: str, stamps: list[dict[str, Any]], *, now: datetime | None = None) -> Standing:
    now = now or datetime.now(UTC)
    score = 0.0
    authors: set[str] = set()
    last: str | None = None
    for stamp in stamps:
        valence = stamp.get("valence")
        if isinstance(valence, str):
            try:
                valence = json.loads(valence)
            except json.JSONDecodeError:
                valence = {}
        quality = _quality(valence)
        confidence = float(stamp.get("confidence") or 1.0)
        weight = SEVERITY_WEIGHT.get(str(stamp.get("severity") or "leaf"), 1.0)
        age_days = _age_days(stamp.get("created_at"), now)
        score += weight * confidence * (2 * quality - 1) * math.exp(-age_days / HALF_LIFE_DAYS)
        if quality >= 0.5 and stamp.get("author"):
            authors.add(str(stamp["author"]))
        created = stamp.get("created_at")
        if isinstance(created, str) and (last is None or created > last):
            last = created
    return Standing(handle=handle, score=score, stamps=len(stamps), authors=len(authors), last_stamp=last)


def _quality(valence: Any) -> float:
    if isinstance(valence, dict):
        for key in ("quality", "semantic"):
            value = valence.get(key)
            if isinstance(value, int | float):
                return max(0.0, min(1.0, float(value)))
        # `wl accept --quality N` stores 1..5 ratings.
        rating = valence.get("quality_rating") or valence.get("rating")
        if isinstance(rating, int | float):
            return max(0.0, min(1.0, (float(rating) - 1) / 4))
    return 0.5


def _age_days(created: Any, now: datetime) -> float:
    if not isinstance(created, str) or not created:
        return 0.0
    text = created.replace("T", " ").replace("Z", "").split(".")[0]
    try:
        stamped = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return 0.0
    return max(0.0, (now - stamped).total_seconds() / 86400)
