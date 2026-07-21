"""experiment.yaml / environment.yaml schema: defines config shape only.

Scalar values only; list values are grid axes expanded by runner/grid.py.
Construction is strict: unknown keys are rejected by name. Value legality and
cross-field rules live in validate.py. JSON loads via stdlib; YAML needs PyYAML.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """An invalid configuration; the message names the violated constraint."""


def _check_keys(section: str, data: dict, allowed: set[str]) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"{section}: unknown key(s) {sorted(unknown)} (allowed: {sorted(allowed)})")


def _require(section: str, data: dict, key: str) -> Any:
    if key not in data:
        raise ConfigError(f"{section}: required key '{key}' is missing")
    return data[key]


# --------------------------------------------------------------------------- #
# experiment.yaml
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class KleeneConfig:
    """Kleene shape: ,,count'' Kleene variables with ,,quantifier'', each preceded by a
    ,,gap'' wildcard (greedy Z* or reluctant Z*?)."""

    count: int
    quantifier: str          # PLUS | RELUCTANT_PLUS
    gap: str                 # reluctant (Z*?) | greedy (Z*)

    @classmethod
    def from_dict(cls, data: dict) -> "KleeneConfig":
        _check_keys("query.kleene", data, {"count", "quantifier", "gap"})
        return cls(count=int(_require("query.kleene", data, "count")),
                   quantifier=str(_require("query.kleene", data, "quantifier")),
                   gap=str(_require("query.kleene", data, "gap")))


@dataclass(frozen=True)
class QueryConfig:
    """-> campaign_spec_gen."""

    pattern_length: int
    topology: str            # chain | star | cycle | clique | ... (validate.py)
    predicate_class: str = "spatial"
    kleene: KleeneConfig | None = None
    measures: tuple[str, ...] = ()   # e.g. ("COUNT(K1.id) AS k1_count",); empty = per-var id+time

    @classmethod
    def from_dict(cls, data: dict) -> "QueryConfig":
        _check_keys("query", data, {"pattern_length", "topology", "predicate_class",
                                    "kleene", "measures"})
        kleene = KleeneConfig.from_dict(data["kleene"]) if data.get("kleene") else None
        return cls(pattern_length=int(_require("query", data, "pattern_length")),
                   topology=str(_require("query", data, "topology")),
                   predicate_class=str(data.get("predicate_class", "spatial")),
                   kleene=kleene,
                   measures=tuple(data.get("measures", ())))


@dataclass(frozen=True)
class BurstConfig:
    rows: int
    period: int
    multiplier: float

    @classmethod
    def from_dict(cls, data: dict) -> "BurstConfig":
        _check_keys("data.burst", data, {"rows", "period", "multiplier"})
        return cls(rows=int(_require("data.burst", data, "rows")),
                   period=int(_require("data.burst", data, "period")),
                   multiplier=float(_require("data.burst", data, "multiplier")))


@dataclass(frozen=True)
class DataConfig:
    """-> datagen. ,,stream'' is the batch_sizes form
    [initial_table_size, batch1, batch2, ...]; total_rows = sum.
    ,,independent_selectivity'' is a PER-TYPE upper bound on presence (does NOT sum to
    1; the sum <= 1 constraint and feasibility floor live in validate.py). Its keys ARE
    the type set."""

    stream: tuple[int, ...]
    independent_selectivity: dict[str, float]
    seed: int | None = None          # None = random; the ACTUAL seed used is recorded
    rho: float = 1000.0
    cluster_count: int = 4
    burst: BurstConfig | None = None
    # optional band geometry override [lon, lat]. None = the canonical half-widths
    # (0.05, 0.02). Expressed once and derived into BOTH the query band literal and the
    # datagen BandTarget so the coupling invariant (query hw == generation hw) holds.
    # Everyday configs should omit it.
    band_half_widths: tuple[float, float] | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "DataConfig":
        _check_keys("data", data, {"stream", "independent_selectivity", "seed", "rho",
                                   "cluster_count", "burst", "band_half_widths"})
        burst = BurstConfig.from_dict(data["burst"]) if data.get("burst") else None
        indep = _require("data", data, "independent_selectivity")
        if not isinstance(indep, dict):
            raise ConfigError("data.independent_selectivity: must be a {type: fraction} map")
        return cls(stream=tuple(int(v) for v in _require("data", data, "stream")),
                   independent_selectivity={str(k): float(v) for k, v in indep.items()},
                   seed=None if data.get("seed") is None else int(data["seed"]),
                   rho=float(data.get("rho", 1000.0)),
                   cluster_count=int(data.get("cluster_count", 4)),
                   burst=burst,
                   band_half_widths=(None if data.get("band_half_widths") is None
                                     else tuple(float(v) for v in data["band_half_widths"])))


@dataclass(frozen=True)
class ExecutionConfig:
    """-> pipeline (neither generator)."""

    selectivity_mode: str = "b_unified"      # b_unified | c: ONLY these two
    join_order: str = "bushy"                # canonical | dp | bushy
    selector: str = "clique_grow"            # clique_grow | edge_cover (the sweep's cover selector)
    strategy_set: str | None = None
    caps: dict[str, int] = field(default_factory=dict)
    regime: str = "dependent_predicates_in_on"
    # query schedule: at which batches the cumulative result is QUERIED.
    # all = every batch (the default); everyNth (N in {2,4,8}) = every Nth batch + always
    # the final; last = only the final batch. trino/memory require 'all' (validator).
    # Mapping: eimer.query.query_schedule.frequency_batches.
    query_frequency: str = "all"             # all | every2nd | every4th | every8th | last

    @classmethod
    def from_dict(cls, data: dict) -> "ExecutionConfig":
        _check_keys("execution", data, {"selectivity_mode", "join_order",
                                        "selector", "strategy_set", "caps", "regime", "query_frequency"})
        return cls(selectivity_mode=str(data.get("selectivity_mode", "b_unified")),
                   join_order=str(data.get("join_order", "bushy")),
                   selector=str(data.get("selector", "clique_grow")),
                   strategy_set=data.get("strategy_set"),
                   caps={str(k): int(v) for k, v in (data.get("caps") or {}).items()},
                   regime=str(data.get("regime", "dependent_predicates_in_on")),
                   query_frequency=str(data.get("query_frequency", "all")))


@dataclass(frozen=True)
class OutputConfig:
    """Capture-everything cheap by default; the expensive captures are opt-in flags
    (with_qep, with_correctness; selectivity_mode 'c' gates the third)."""

    with_qep: bool = False
    with_correctness: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "OutputConfig":
        _check_keys("output", data, {"with_qep", "with_correctness"})
        return cls(with_qep=bool(data.get("with_qep", False)),
                   with_correctness=bool(data.get("with_correctness", False)))


@dataclass(frozen=True)
class ExperimentConfig:
    query: QueryConfig
    data: DataConfig
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    # band tightness: expressed once, derived at materialization into BOTH the query
    # band literal AND the datagen band-sigma. None = natural sigma (no calibration
    # target).
    band_tightness: float | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "ExperimentConfig":
        _check_keys("experiment", data, {"query", "data", "execution", "output",
                                         "band_tightness"})
        return cls(query=QueryConfig.from_dict(_require("experiment", data, "query")),
                   data=DataConfig.from_dict(_require("experiment", data, "data")),
                   execution=ExecutionConfig.from_dict(data.get("execution") or {}),
                   output=OutputConfig.from_dict(data.get("output") or {}),
                   band_tightness=None if data.get("band_tightness") is None
                   else float(data["band_tightness"]))


# --------------------------------------------------------------------------- #
# environment.yaml
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ParallelismConfig:
    mode: str = "serial"     # serial (the only supported mode)
    containers: int = 1

    @classmethod
    def from_dict(cls, data: dict) -> "ParallelismConfig":
        _check_keys("environment.parallelism", data, {"mode", "containers"})
        return cls(mode=str(data.get("mode", "serial")),
                   containers=int(data.get("containers", 1)))


@dataclass(frozen=True)
class EnvironmentConfig:
    """,,backend'' is REQUIRED with NO default:
    a config that does not name its backend fails loud here, before any run.
    ,,memory'' is only ever an EXPLICITLY named value, never a fallback."""

    backend: str             # trino | memory: REQUIRED, no default
    connection: dict[str, str] = field(default_factory=dict)
    parallelism: ParallelismConfig = field(default_factory=ParallelismConfig)

    @classmethod
    def from_dict(cls, data: dict) -> "EnvironmentConfig":
        _check_keys("environment", data, {"backend", "connection", "parallelism"})
        backend = data.get("backend")
        if not backend:
            raise ConfigError(
                "environment.backend is REQUIRED and has no default: name the backend "
                "explicitly (trino | memory); there is no silent fallback")
        return cls(backend=str(backend),
                   connection={str(k): str(v) for k, v in (data.get("connection") or {}).items()},
                   parallelism=ParallelismConfig.from_dict(data.get("parallelism") or {}))


# --------------------------------------------------------------------------- #
# File loading (JSON stdlib; YAML behind the optional-PyYAML seam)
# --------------------------------------------------------------------------- #

def load_config_dict(path: Path) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    if str(path).endswith((".yaml", ".yml")):
        try:
            import yaml
        except ImportError as exc:
            raise ConfigError(
                f"{path}: YAML configs need PyYAML, which is not installed (and not yet a "
                "pinned project dependency: a Stage-1 review item). Install it, or use "
                "the JSON form of the same structure.") from exc
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def load_experiment(path: Path) -> ExperimentConfig:
    return ExperimentConfig.from_dict(load_config_dict(path))


def load_environment(path: Path) -> EnvironmentConfig:
    return EnvironmentConfig.from_dict(load_config_dict(path))
