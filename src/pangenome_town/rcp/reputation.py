"""Receiver-side trust facts about a requester.

Step 3b replaces the static lists with scores computed from the Wasteland
commons (stamps, trust levels). Until then a town declares who it trusts in
`town.toml`:

    [rcp]
    trusted_requesters = ["hop://lab@example.org/ubar/", "https://orcid.org/0000-..."]
    blocked_requesters = []

Every decision is returned as receiver assertions (class IRI, individual IRI)
so the semantic contract, not this module, decides what follows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import TownConfig
from . import PG

REPUTABLE = f"{PG}ReputableRequester"
UNVETTED = f"{PG}UnvettedRequester"
BLOCKED = f"{PG}BlockedRequester"


@dataclass(frozen=True)
class Verdict:
    requester: str
    standing: str  # reputable | unvetted | blocked
    reason: str
    score: float | None = None

    @property
    def class_iri(self) -> str:
        return {"reputable": REPUTABLE, "unvetted": UNVETTED, "blocked": BLOCKED}[self.standing]

    def assertions(self) -> list[tuple[str, str]]:
        return [(self.class_iri, self.requester)]

    def as_dict(self) -> dict[str, Any]:
        return {"requester": self.requester, "standing": self.standing, "class": self.class_iri, "reason": self.reason, "score": self.score}


def _lists(town: TownConfig) -> tuple[set[str], set[str]]:
    rcp = town.extra.get("rcp") or {}
    return set(rcp.get("trusted_requesters") or []), set(rcp.get("blocked_requesters") or [])


def judge(town: TownConfig, requester: str) -> Verdict:
    trusted, blocked = _lists(town)
    if requester in blocked:
        return Verdict(requester, "blocked", "listed in [rcp].blocked_requesters")
    if requester in trusted:
        return Verdict(requester, "reputable", "listed in [rcp].trusted_requesters")
    return Verdict(requester, "unvetted", "no commons standing known for this requester")
