"""Gate 4 (capability): which compute sites this town can use now, as receiver facts, plus referrals.

A site is reachable when it is enabled and its driver health check passes (see
`compute.sites.reachable`). Reachability enters the reasoner as
`town:ReachableSite(s)` or its complement, so the contract decides data
locality: large and controlled tasks need a dataset residing on a reachable
site. Tool allowlists and resource limits stay in code (the workflow validator)
and are enforced again by the operating system and the scheduler.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from research_commons import axioms as ax

from ..config import TownConfig
from . import contract, town_iri

TASK_TEMPLATES = {
    "https://w3id.org/academic-wasteland/pangenome-town/contract/WholeGraphDeconstructTask": "deconstruct-region",
    "https://w3id.org/academic-wasteland/pangenome-town/contract/AlleleFrequencyTask": "allele-frequency",
    "https://w3id.org/academic-wasteland/pangenome-town/contract/IndividualGenotypeExportTask": "genotype-export",
}


def site_facts(town: TownConfig, *, reachable_fn: Callable[[Any], tuple[bool, str]] | None = None) -> tuple[list[str], list[dict[str, Any]]]:
    from ..compute import load_sites, reachable

    check = reachable_fn or reachable
    axioms: list[str] = []
    report: list[dict[str, Any]] = []
    reachable_class = f"{town_iri(town.name)}ReachableSite"
    for site in load_sites(town):
        ok, detail = check(site)
        iri = contract.site_iri(town, site.name)
        axioms.append(ax.class_assertion(reachable_class if ok else ax.complement(reachable_class), iri))
        report.append({"site": site.name, "iri": iri, "driver": site.driver, "scheduler": site.scheduler, "enabled": site.enabled,
                       "reachable": ok, "detail": detail, "datasets": list(site.datasets), "tools": list(site.tools),
                       "limits": {"cpus": site.max_cpus, "mem_gb": site.max_mem_gb, "wall_seconds": site.max_wall_seconds}})
    return axioms, report


def capabilities(town: TownConfig) -> dict[str, Any]:
    """Templates this town can run now and on which site (advertised in the Agent Card)."""
    from ..compute import TEMPLATES
    from ..compute.runner import pick_site

    templates: dict[str, Any] = {}
    for name in TEMPLATES:
        site, _ = pick_site(town, name)
        templates[name] = site.name if site else None
    _, sites = site_facts(town)
    return {"templates": templates, "sites": sites}


def _fetch_card(url: str, timeout: float) -> dict[str, Any] | None:
    request = urllib.request.Request(url, headers={"X-GC-Request": "pangenome-town", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None


def referrals(town: TownConfig, template_name: str, *, datasets: list[str] | None = None,
              fetch: Callable[[str], dict[str, Any] | None] | None = None, timeout: float = 3.0) -> list[dict[str, Any]]:
    """Peers that can run `template_name` on a reachable site now AND hold the data the task needs.

    A controlled dataset is specific to the town that serves it, so a peer qualifies only if its Agent Card lists
    the same restricted dataset IRI. Otherwise the peer must serve the same graph (same graph id).
    """
    served_restricted = {item["iri"] for item in contract.restricted_datasets(town)}
    wanted_restricted = {item for item in datasets or [] if item in served_restricted}
    graph = contract.graph_id(town)
    found = []
    for peer, city in sorted(town.peers.items()):
        url = f"{town.supervisor_url.rstrip('/')}/v0/city/{city}/svc/envoy/.well-known/agent-card.json"
        card = (fetch or (lambda value: _fetch_card(value, timeout)))(url) or {}
        site = (card.get("compute") or {}).get("templates", {}).get(template_name)
        if not site:
            continue
        if wanted_restricted:
            offered = {item.get("iri") for item in (card.get("trust") or {}).get("restricted_datasets") or [] if isinstance(item, dict)}
            if not wanted_restricted <= offered:
                continue
        elif (card.get("town") or {}).get("graph") != graph:
            continue
        found.append({"town": peer, "site": site, "agent_card": url, "a2a": card.get("url")})
    return found
