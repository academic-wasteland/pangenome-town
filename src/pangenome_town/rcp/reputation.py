"""Receiver-side trust facts about a requester.

Standing comes from the Wasteland commons: stamps that other rigs put on the
requester's handle (see `commons.standing_from_stamps`). `town.toml` adds two
short-circuits and the thresholds:

    [rcp]
    blocked_requesters = []          # always BlockedRequester
    trusted_requesters = []          # always ReputableRequester (bootstrap)
    reputation_threshold = 1.0       # commons score needed for ReputableRequester
    reputation_min_authors = 1       # distinct stampers needed

Every decision is returned as receiver assertions (class IRI, individual IRI)
so the semantic contract, not this module, decides what follows: a
LargeAnalysisTask by an UnvettedRequester is a PermissionRequiredTask, by a
BlockedRequester a RejectedTask, by a ReputableRequester an AcceptedLargeTask.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import TownConfig
from . import PG, commons

REPUTABLE = f"{PG}ReputableRequester"
UNVETTED = f"{PG}UnvettedRequester"
BLOCKED = f"{PG}BlockedRequester"


@dataclass(frozen=True)
class Verdict:
    requester: str
    standing: str  # reputable | unvetted | blocked
    reason: str
    score: float | None = None
    handle: str | None = None
    detail: dict[str, Any] | None = None

    @property
    def class_iri(self) -> str:
        return {"reputable": REPUTABLE, "unvetted": UNVETTED, "blocked": BLOCKED}[self.standing]

    def assertions(self) -> list[tuple[str, str]]:
        return [(self.class_iri, self.requester)]

    def as_dict(self) -> dict[str, Any]:
        return {
            "requester": self.requester, "standing": self.standing, "class": self.class_iri, "reason": self.reason,
            "score": None if self.score is None else round(self.score, 3), "handle": self.handle, "detail": self.detail,
        }


def _settings(town: TownConfig) -> dict[str, Any]:
    rcp = town.extra.get("rcp") or {}
    return {
        "trusted": set(rcp.get("trusted_requesters") or []),
        "blocked": set(rcp.get("blocked_requesters") or []),
        "threshold": float(rcp.get("reputation_threshold", 1.0)),
        "min_authors": int(rcp.get("reputation_min_authors", 1)),
    }


def judge(town: TownConfig, requester: str, *, ledger: commons.Commons | None = None) -> Verdict:
    settings = _settings(town)
    handle = commons.handle_for(requester)
    if requester in settings["blocked"] or handle in settings["blocked"]:
        return Verdict(requester, "blocked", "listed in [rcp].blocked_requesters", handle=handle)
    if requester in settings["trusted"] or handle in settings["trusted"]:
        return Verdict(requester, "reputable", "listed in [rcp].trusted_requesters", handle=handle)
    if ledger is None:
        try:
            ledger = commons.Commons.for_town(town)
        except commons.CommonsError:
            ledger = None
    if ledger is None:
        return Verdict(requester, "unvetted", "no commons configured; standing unknown", handle=handle)
    try:
        standing = ledger.standing(handle)
    except commons.CommonsError as error:
        return Verdict(requester, "unvetted", f"commons unavailable: {error}", handle=handle)
    detail = standing.as_dict()
    if round(standing.score, 3) >= settings["threshold"] and standing.authors >= settings["min_authors"]:
        return Verdict(
            requester, "reputable",
            f"commons score {standing.score:.2f} >= {settings['threshold']} from {standing.authors} stamper(s)",
            score=standing.score, handle=handle, detail=detail,
        )
    return Verdict(
        requester, "unvetted",
        f"commons score {standing.score:.2f} < {settings['threshold']} or fewer than {settings['min_authors']} stamper(s) ({standing.authors})",
        score=standing.score, handle=handle, detail=detail,
    )
