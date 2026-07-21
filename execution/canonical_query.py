from __future__ import annotations

import bootstrap  # noqa: F401

import re
from typing import Iterable, Sequence

from eimer.models import DependencyGraph
from eimer.query.query_spec import (
    ColumnSpec,
    MeasureSpec,
    QueryPredicateSpec,
    QuerySpec,
    VariableSpec,
    query_spec_to_match_recognize_sql,
)

from benchmark_types import CanonicalDependentConfig


def _fmt_number(value: float) -> str:
    return format(value, "g")


def normalize_sql_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def primary_type_predicate(alias: str, event_type: str) -> str:
    return f"{alias}.primary_type = '{event_type}'"


def spatial_lon_predicate(subject_alias: str, anchor_alias: str, window: float) -> str:
    w = _fmt_number(window)
    return (
        f"{subject_alias}.lon BETWEEN {anchor_alias}.lon - {w} "
        f"AND {anchor_alias}.lon + {w}"
    )


def spatial_lat_predicate(subject_alias: str, anchor_alias: str, window: float) -> str:
    w = _fmt_number(window)
    return (
        f"{subject_alias}.lat BETWEEN {anchor_alias}.lat - {w} "
        f"AND {anchor_alias}.lat + {w}"
    )


def dependent_b_predicate(config: CanonicalDependentConfig, b_alias: str = "B", r_alias: str = "R") -> str:
    return " AND ".join(
        [
            primary_type_predicate(b_alias, config.b_type),
            spatial_lon_predicate(b_alias, r_alias, config.b_lon_window),
            spatial_lat_predicate(b_alias, r_alias, config.b_lat_window),
        ]
    )


def dependent_m_predicate(config: CanonicalDependentConfig, m_alias: str = "M", r_alias: str = "R") -> str:
    return " AND ".join(
        [
            primary_type_predicate(m_alias, config.m_type),
            spatial_lon_predicate(m_alias, r_alias, config.m_lon_window),
            spatial_lat_predicate(m_alias, r_alias, config.m_lat_window),
        ]
    )


def canonical_match_recognize_sql(
    config: CanonicalDependentConfig,
    events_table: str = "events",
) -> str:
    return query_spec_to_match_recognize_sql(canonical_query_spec(config), events_table=events_table)


def canonical_query_spec(config: CanonicalDependentConfig | None = None) -> QuerySpec:
    config = config or CanonicalDependentConfig()
    return QuerySpec(
        name="canonical_r_b_m",
        variables=(
            VariableSpec(name="R", output_prefix="r", independent_predicate_id="ic_r_type"),
            VariableSpec(name="B", output_prefix="b", independent_predicate_id="ic_b_type"),
            VariableSpec(name="M", output_prefix="m", independent_predicate_id="ic_m_type"),
        ),
        independent_predicates=(
            QueryPredicateSpec(
                predicate_id="ic_r_type",
                kind="independent",
                variables=("R",),
                sql_template=primary_type_predicate("R", config.r_type),
                referenced_columns=("primary_type",),
                selectivity_key="R",
            ),
            QueryPredicateSpec(
                predicate_id="ic_b_type",
                kind="independent",
                variables=("B",),
                sql_template=primary_type_predicate("B", config.b_type),
                referenced_columns=("primary_type",),
                selectivity_key="B",
            ),
            QueryPredicateSpec(
                predicate_id="ic_m_type",
                kind="independent",
                variables=("M",),
                sql_template=primary_type_predicate("M", config.m_type),
                referenced_columns=("primary_type",),
                selectivity_key="M",
            ),
        ),
        dependent_predicates=(
            QueryPredicateSpec(
                predicate_id="dc_r_b_lon",
                kind="dependent",
                variables=("R", "B"),
                sql_template=spatial_lon_predicate("B", "R", config.b_lon_window),
                referenced_columns=("lon",),
                selectivity_key="R|B:lon",
            ),
            QueryPredicateSpec(
                predicate_id="dc_r_b_lat",
                kind="dependent",
                variables=("R", "B"),
                sql_template=spatial_lat_predicate("B", "R", config.b_lat_window),
                referenced_columns=("lat",),
                selectivity_key="R|B:lat",
            ),
            QueryPredicateSpec(
                predicate_id="dc_r_m_lon",
                kind="dependent",
                variables=("R", "M"),
                sql_template=spatial_lon_predicate("M", "R", config.m_lon_window),
                referenced_columns=("lon",),
                selectivity_key="R|M:lon",
            ),
            QueryPredicateSpec(
                predicate_id="dc_r_m_lat",
                kind="dependent",
                variables=("R", "M"),
                sql_template=spatial_lat_predicate("M", "R", config.m_lat_window),
                referenced_columns=("lat",),
                selectivity_key="R|M:lat",
            ),
        ),
        measures=(
            MeasureSpec("R.id", "r_id", "BIGINT"),
            MeasureSpec("R.time", "r_time", "TIMESTAMP(6)"),
            MeasureSpec("B.id", "b_id", "BIGINT"),
            MeasureSpec("B.time", "b_time", "TIMESTAMP(6)"),
            MeasureSpec("M.id", "m_id", "BIGINT"),
            MeasureSpec("M.time", "m_time", "TIMESTAMP(6)"),
        ),
        result_key_columns=("r_id", "r_time", "b_id", "b_time", "m_id", "m_time"),
        event_schema=tuple(ColumnSpec(name, sql_type) for name, sql_type in required_event_schema()),
        order_by=("time",),
        row_output="ONE ROW PER MATCH",
        after_match="AFTER MATCH SKIP TO NEXT ROW",
    )


def required_event_schema() -> tuple[tuple[str, str], ...]:
    return (
        ("id", "BIGINT"),
        ("time", "TIMESTAMP(6)"),
        ("ts", "TIMESTAMP(6)"),
        ("primary_type", "VARCHAR"),
        ("etype", "VARCHAR"),
        ("lon", "DOUBLE"),
        ("lat", "DOUBLE"),
    )


def _contains_fragment(expressions: Iterable[str], fragment: str) -> bool:
    normalized_fragment = normalize_sql_text(fragment)
    for expression in expressions:
        if normalized_fragment in normalize_sql_text(expression):
            return True
    return False


def validate_dependency_graph_matches_canonical(
    dep_graph: DependencyGraph,
    config: CanonicalDependentConfig,
) -> None:
    if list(dep_graph.variables) != ["R", "B", "M"]:
        raise ValueError(
            "Canonical benchmark expects variables ['R', 'B', 'M'], "
            f"got {dep_graph.variables}"
        )

    independent = dep_graph.independent_conditions
    independent_checks = (
        ("R", primary_type_predicate("R", config.r_type)),
        ("B", primary_type_predicate("B", config.b_type)),
        ("M", primary_type_predicate("M", config.m_type)),
    )

    for variable, fragment in independent_checks:
        if not _contains_fragment(independent.get(variable, []), fragment):
            raise ValueError(
                f"Missing canonical independent condition for {variable}: {fragment}"
            )

    edge_conditions: dict[frozenset[str], list[str]] = {}
    for edge in dep_graph.edges:
        pair = frozenset({edge.var_left, edge.var_right})
        edge_conditions.setdefault(pair, []).extend(edge.dependent_conditions)

    rb_conditions = edge_conditions.get(frozenset({"R", "B"}), [])
    rm_conditions = edge_conditions.get(frozenset({"R", "M"}), [])

    rb_expected = (
        spatial_lon_predicate("B", "R", config.b_lon_window),
        spatial_lat_predicate("B", "R", config.b_lat_window),
    )
    for fragment in rb_expected:
        if not _contains_fragment(rb_conditions, fragment):
            raise ValueError(f"Missing canonical R-B dependent condition: {fragment}")

    rm_expected = (
        spatial_lon_predicate("M", "R", config.m_lon_window),
        spatial_lat_predicate("M", "R", config.m_lat_window),
    )
    for fragment in rm_expected:
        if not _contains_fragment(rm_conditions, fragment):
            raise ValueError(f"Missing canonical R-M dependent condition: {fragment}")


def canonical_validation_fragments(config: CanonicalDependentConfig) -> tuple[str, ...]:
    return (
        primary_type_predicate("R", config.r_type),
        primary_type_predicate("B", config.b_type),
        primary_type_predicate("M", config.m_type),
        spatial_lon_predicate("B", "R", config.b_lon_window),
        spatial_lat_predicate("B", "R", config.b_lat_window),
        spatial_lon_predicate("M", "R", config.m_lon_window),
        spatial_lat_predicate("M", "R", config.m_lat_window),
    )
