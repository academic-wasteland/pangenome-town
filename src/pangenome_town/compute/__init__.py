"""Compute for a pangenome town: sites it can reach, workflow templates, a validator, and a runner.

A town exercises capabilities (an SSH alias, a local toolchain); it never hands them out. Workflows are
rendered from a fixed template library and re-validated before every execution, so anything an agent
proposes is data that the validator either accepts or refuses.
"""

from __future__ import annotations


class ComputeError(RuntimeError):
    """A site, workflow, or job could not be used; the message says why."""


class SemanticPolicyError(ComputeError):
    """ADR 0001: Raised when semantic gating fails under SROIQ reasoning."""


from .runner import (
    ComputeRunner,
    ComputeState,
    SemanticGate,
    TESComputeRunner,
    run_task,
)
from .sites import Site, load_sites, reachable
from .workflows import TEMPLATES, plan, validate

__all__ = [
    "TEMPLATES",
    "ComputeError",
    "ComputeRunner",
    "ComputeState",
    "SemanticGate",
    "SemanticPolicyError",
    "Site",
    "TESComputeRunner",
    "load_sites",
    "plan",
    "reachable",
    "run_task",
    "validate",
]
