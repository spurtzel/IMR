from __future__ import annotations

import bootstrap  # noqa: F401

from dataclasses import dataclass
from typing import Any, Dict, Mapping

from eimer.models import DependencyGraph


@dataclass(frozen=True)
class CanonicalDependentConfig:
    r_type: str = "ROBBERY"
    b_type: str = "BATTERY"
    m_type: str = "MOTOR VEHICLE THEFT"
    b_lon_window: float = 0.05
    b_lat_window: float = 0.02
    m_lon_window: float = 0.05
    m_lat_window: float = 0.02

    def as_dict(self) -> Dict[str, Any]:
        return {
            "r_type": self.r_type,
            "b_type": self.b_type,
            "m_type": self.m_type,
            "b_lon_window": self.b_lon_window,
            "b_lat_window": self.b_lat_window,
            "m_lon_window": self.m_lon_window,
            "m_lat_window": self.m_lat_window,
        }


@dataclass(frozen=True)
class BenchmarkStrategy:
    strategy_id: str
    origin: str
    description: str
    cache_schemas: tuple[str, ...]
    cache_updates: tuple[str, ...]
    compose_sql: str
    path_a_sql: str
    path_b_sql: str
    cache_statement_op_ids: tuple[str, ...] = ()
    compose_op_id: str | None = None
    post_filter_op_id: str | None = None


@dataclass
class StrategyCatalogBundle:
    config: CanonicalDependentConfig
    query_spec_name: str
    dep_graph: DependencyGraph
    legacy_strategies: Dict[str, BenchmarkStrategy]
    n_strategies: Dict[str, BenchmarkStrategy]
    n_summaries: Dict[str, Dict[str, Any]]
    only_with_nc_subpattern: bool
    query_spec_source: str = "registry"
    query_spec_fingerprint: str | None = None
    query_spec_payload: Mapping[str, Any] | None = None

    @property
    def all_strategies(self) -> Dict[str, BenchmarkStrategy]:
        merged: Dict[str, BenchmarkStrategy] = {}
        for sid in sorted(self.legacy_strategies.keys()):
            merged[sid] = self.legacy_strategies[sid]
        for sid in sorted(self.n_strategies.keys(), key=lambda s: int(s[1:])):
            merged[sid] = self.n_strategies[sid]
        return merged

    def require_strategy(self, strategy_id: str) -> BenchmarkStrategy:
        try:
            return self.all_strategies[strategy_id]
        except KeyError as exc:
            known = ", ".join(self.all_strategies.keys())
            raise ValueError(f"Unknown strategy id {strategy_id!r}. Known: {known}") from exc

    def to_catalog_payload(self) -> Dict[str, Any]:
        pattern_parts: list[str] = []
        for idx, variable in enumerate(self.dep_graph.variables):
            if idx:
                pattern_parts.append("Z*")
            pattern_parts.append(variable)
        payload: Dict[str, Any] = {
            "query_spec": self.query_spec_name,
            "query_spec_source": self.query_spec_source,
            "query_spec_fingerprint": self.query_spec_fingerprint,
            "query_spec_payload": dict(self.query_spec_payload or {}),
            "pattern": " ".join(pattern_parts),
            "only_with_nc_subpattern": self.only_with_nc_subpattern,
            "config": self.config.as_dict(),
            "legacy_strategy_count": len(self.legacy_strategies),
            "n_strategy_count": len(self.n_strategies),
            "variable_count": len(self.dep_graph.variables),
            "variables": list(self.dep_graph.variables),
            "strategies": [],
        }

        for sid in sorted(self.legacy_strategies.keys()):
            strategy = self.legacy_strategies[sid]
            payload["strategies"].append(
                {
                    "id": sid,
                    "origin": strategy.origin,
                    "description": strategy.description,
                }
            )

        for sid in sorted(self.n_strategies.keys(), key=lambda s: int(s[1:])):
            strategy = self.n_strategies[sid]
            summary = self.n_summaries[sid]
            payload["strategies"].append(
                {
                    "id": sid,
                    "origin": strategy.origin,
                    "description": strategy.description,
                    "summary": summary,
                }
            )

        return payload


def merge_selected_strategies(
    bundle: StrategyCatalogBundle,
    strategy_ids: list[str],
) -> list[BenchmarkStrategy]:
    selected: list[BenchmarkStrategy] = []
    for strategy_id in strategy_ids:
        selected.append(bundle.require_strategy(strategy_id))
    return selected


def dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item in seen:
            continue
        ordered.append(item)
        seen.add(item)
    return ordered
