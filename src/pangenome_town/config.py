"""Town configuration loaded from a city's town.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SUPERVISOR_URL = "http://127.0.0.1:8372"
DEFAULT_AGENT = "pangenome-town.townsfolk"  # pack-qualified agent name inside the rig


class TownConfigError(ValueError):
    pass


@dataclass(frozen=True)
class TownConfig:
    name: str
    display: str
    population: str
    samples: tuple[str, ...]
    peers: dict[str, str]
    city_root: Path
    rig: str
    agent: str
    graph: Path | None
    vcf: Path | None
    reference_paths: tuple[str, ...]
    reference_path_template: str
    default_assembly: str
    exchange_db: Path
    supervisor_url: str
    state_dir: Path
    citation: str = ""
    extra: dict = field(default_factory=dict)
    kind: str = "pangenome"  # "pangenome" towns serve a graph; "authority" towns (Camelot) issue credentials

    @property
    def mail_recipient(self) -> str:
        return f"{self.rig}/{self.agent}"

    def peer_city(self, peer: str) -> str:
        try:
            return self.peers[peer]
        except KeyError as error:
            raise TownConfigError(f"unknown peer {peer!r}; known peers: {sorted(self.peers)}") from error

    def reference_path(self, assembly: str, chrom: str) -> str:
        return self.reference_path_template.format(assembly=assembly, chrom=chrom)

    @property
    def has_graph(self) -> bool:
        return self.graph is not None and self.graph.exists()

    @property
    def has_vcf(self) -> bool:
        return self.vcf is not None and self.vcf.exists()


def _path(base: Path, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(os.path.expanduser(value))
    return path if path.is_absolute() else (base / path).resolve()


def load(path: Path | str) -> TownConfig:
    path = Path(path).resolve()
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    town = data.get("town")
    if not isinstance(town, dict):
        raise TownConfigError(f"{path}: missing [town] table")
    base = path.parent
    kind = str(town.get("kind") or "pangenome")
    if kind not in {"pangenome", "authority"}:
        raise TownConfigError(f"{path}: [town].kind must be 'pangenome' or 'authority'")
    for key in ("name", "population", "samples") if kind == "pangenome" else ("name",):
        if key not in town:
            raise TownConfigError(f"{path}: [town].{key} is required")
    name = str(town["name"])
    if not name.isidentifier():
        raise TownConfigError(f"{path}: town name {name!r} must be an identifier")
    samples = tuple(str(sample) for sample in town.get("samples") or ())
    peers = {str(key): str(value) for key, value in (town.get("peers") or {}).items()}
    data_table = data.get("data") or {}
    exchange = data.get("exchange") or {}
    state_dir = _path(base, exchange.get("state_dir")) or (base / ".gc" / "town")
    return TownConfig(
        name=name,
        display=str(town.get("display") or name),
        population=str(town.get("population") or ""),
        samples=samples,
        peers=peers,
        city_root=_path(base, town.get("city_root")) or base,
        rig=str(town.get("rig") or f"{name}-rig"),
        agent=str(town.get("agent") or DEFAULT_AGENT),
        graph=_path(base, data_table.get("graph")),
        vcf=_path(base, data_table.get("vcf")),
        reference_paths=tuple(str(item) for item in data_table.get("reference_paths") or ("GRCh38",)),
        reference_path_template=str(data_table.get("reference_path_template") or "{assembly}#0#{chrom}"),
        default_assembly=str(data_table.get("default_assembly") or "GRCh38"),
        exchange_db=_path(base, exchange.get("db")) or (base.parent / "data" / "exchange.db"),
        supervisor_url=str(exchange.get("supervisor_url") or DEFAULT_SUPERVISOR_URL),
        state_dir=state_dir,
        citation=str(town.get("citation") or ""),
        extra={key: value for key, value in data.items() if key not in {"town", "data", "exchange"}},
        kind=kind,
    )


def locate() -> Path:
    """Find town.toml from PT_TOWN_TOML, a Gas City service state root, or the working directory."""
    explicit = os.environ.get("PT_TOWN_TOML")
    if explicit:
        return Path(explicit)
    for variable in ("GC_CITY_PATH", "GC_CITY_ROOT"):
        city = os.environ.get(variable)
        if city and (Path(city) / "town.toml").exists():
            return Path(city) / "town.toml"
    state_root = os.environ.get("GC_SERVICE_STATE_ROOT")
    if state_root:
        # <city>/.gc/services/<name> -> <city>
        candidate = Path(state_root).resolve().parents[2] / "town.toml"
        if candidate.exists():
            return candidate
    for directory in (Path.cwd(), *Path.cwd().parents):
        candidate = directory / "town.toml"
        if candidate.exists():
            return candidate
    raise TownConfigError("no town.toml found; set PT_TOWN_TOML")


def load_default() -> TownConfig:
    return load(locate())
