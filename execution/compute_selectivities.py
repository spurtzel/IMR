from __future__ import annotations

import argparse
import json
import math
import re
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

import bootstrap  # noqa: F401

from eimer.models import ConditionType, DependencyGraph, canonical_variable_pair
from eimer.pipeline import build_from_sql
from eimer.query.query_spec import query_spec_to_dependency_graph
from eimer.sql.sql_render import render_expression_for_base_scan
from eimer.plans.composition import classify_condition_type

from trino_client import TrinoClient, TrinoClientError, TrinoConnectionConfig
from query_registry import CANONICAL_QUERY_SPEC_NAME
from query_spec_source import load_query_spec_from_args
from sql_generator import DEFAULT_WORKLOAD_PROFILE, DEFAULT_WORKLOAD_SEED, WORKLOAD_PROFILES

# Stats-only selectivity estimator behind the ,,b_unified'' mode.
from mode_b_estimator import (
    Predicate,
    TableStats,
    classify_column_types as _classify_column_types,
    estimate as _unified_estimate,
    gather_stats as _unified_gather_stats,
)


CANONICAL_TYPE_COLUMNS = frozenset({"primary_type", "etype"})
DEFAULT_EVENTS_TABLE = "events"
BASE_ALIAS = "b"
SCALAR_ALIAS = "value"

# Predicate-shape matchers. The selectivity step classifies each dependent condition by
# parsing its rendered SQL text; one regex per shape we know how to price.
_FLOAT_RE = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?"

# ,,var.col = <literal>'': a qualified column equals a string or numeric literal.
_QUALIFIED_LITERAL_EQ_RE = re.compile(
    rf"(?is)^\s*(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*(?P<col>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<literal>'[^']*'|{_FLOAT_RE})\s*$"
)
# ,,<literal> = var.col'': the same equality with the operands reversed.
_LITERAL_QUALIFIED_EQ_RE = re.compile(
    rf"(?is)^\s*(?P<literal>'[^']*'|{_FLOAT_RE})\s*=\s*(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*(?P<col>[A-Za-z_][A-Za-z0-9_]*)\s*$"
)
# ,,left.col = right.col'': an equi-join between two qualified columns.
_QUALIFIED_EQ_RE = re.compile(
    r"(?is)^\s*(?P<left_var>[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*(?P<left_col>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"(?P<right_var>[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*(?P<right_col>[A-Za-z_][A-Za-z0-9_]*)\s*$"
)
# ,,subj.col BETWEEN anchor.col - d AND anchor.col + d'': a symmetric band of half-width d
# around an anchor column (one shared ,,delta''; the asymmetric form is _OFFSET_BETWEEN_RE).
_SPATIAL_BETWEEN_RE = re.compile(
    rf"(?is)^\s*(?P<subject_var>[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*(?P<subject_col>[A-Za-z_][A-Za-z0-9_]*)\s+BETWEEN\s+"
    rf"(?P<anchor_var>[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*(?P<anchor_col>[A-Za-z_][A-Za-z0-9_]*)\s*-\s*(?P<delta>{_FLOAT_RE})\s+AND\s+"
    rf"(?P=anchor_var)\s*\.\s*(?P=anchor_col)\s*\+\s*(?P=delta)\s*$"
)
# a SQL ,,INTERVAL 'value' unit'' literal (captures the value and the unit token).
_INTERVAL_LITERAL_RE = re.compile(
    r"(?is)INTERVAL\s*'(?P<value>[^']+)'\s*(?P<unit>[A-Za-z]+)"
)
# ,,later.ts - anchor.ts <= INTERVAL 'v' unit'': a temporal window as a timestamp difference.
_SUBTRACTION_WINDOW_RE = re.compile(
    r"(?is)\b(?P<later>[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*(?:time|ts)\s*"
    r"-\s*(?P<anchor>[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*(?:time|ts)\s*"
    r"<=?\s*INTERVAL\s*'(?P<value>[^']+)'\s*(?P<unit>[A-Za-z]+)"
)
# ,,later.ts BETWEEN anchor.ts AND anchor.ts + INTERVAL 'v' unit'': the BETWEEN form of the
# same temporal window.
_BETWEEN_WINDOW_RE = re.compile(
    r"(?is)\b(?P<later>[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*(?:time|ts)\s+BETWEEN\s+"
    r"(?P<anchor_left>[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*(?:time|ts)\s+AND\s+"
    r"(?P<anchor_right>[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*(?:time|ts)\s*\+\s*"
    r"INTERVAL\s*'(?P<value>[^']+)'\s*(?P<unit>[A-Za-z]+)"
)
# the symmetric window form:
#   abs(date_diff('millisecond', A.ts, B.ts)) <= 200
_ABS_DATE_DIFF_WINDOW_RE = re.compile(
    r"(?is)\babs\s*\(\s*date_diff\s*\(\s*'(?P<unit>[A-Za-z]+)'\s*,\s*"
    r"(?P<left>[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*(?:time|ts)\s*,\s*"
    r"(?P<right>[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*(?:time|ts)\s*\)\s*\)\s*"
    rf"<=?\s*(?P<value>{_FLOAT_RE})"
)
_INTERVAL_SECONDS = {
    "SECOND": 1.0,
    "MINUTE": 60.0,
    "HOUR": 3600.0,
    "DAY": 86400.0,
    "WEEK": 604800.0,
}


@dataclass(frozen=True)
class IndependentLiteralEquality:
    variable: str
    column: str
    literal_sql: str


@dataclass(frozen=True)
class QualifiedEquality:
    left_var: str
    left_col: str
    right_var: str
    right_col: str


@dataclass(frozen=True)
class SpatialBetween:
    subject_var: str
    subject_col: str
    anchor_var: str
    anchor_col: str
    delta: float


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compute selectivities.json for cost-model validation. "
            "Mode c directly measures dependent selectivities with O(N^2) pair-count queries and may be slow."
        )
    )
    parser.add_argument("--canonical-query-sql", default=None)
    parser.add_argument("--query-spec", default=CANONICAL_QUERY_SPEC_NAME)
    parser.add_argument("--query-spec-file", type=Path, default=None)
    # b_unified (stats-only estimator) is the DEFAULT selectivity source;
    # c is the O(N^2) ground-truth oracle, never a selection input.
    parser.add_argument("--mode", default="b_unified", choices=("c", "b_unified"))
    parser.add_argument("--events-table", default=DEFAULT_EVENTS_TABLE)
    parser.add_argument("--external-manifest-fingerprint", default=None)
    parser.add_argument("--rho", type=float, default=None, help="event-time density (events/second) recorded into the payload")
    parser.add_argument("--output", required=True)
    parser.add_argument("--trino-server")
    parser.add_argument("--trino-catalog")
    parser.add_argument("--trino-schema")
    parser.add_argument("--trino-user")
    parser.add_argument("--workload-profile", choices=WORKLOAD_PROFILES, default=None)
    parser.add_argument("--workload-seed", type=int, default=None)
    parser.add_argument("--total-events", type=int, default=None)
    parser.add_argument("--initial-history", type=int, default=None)
    parser.add_argument("--updates", type=int, default=None)
    parser.add_argument("--update-size", type=int, default=None)
    parser.add_argument("--scale", type=float, default=None)
    parser.add_argument("--sel-r", type=float, default=None)
    parser.add_argument("--sel-b", type=float, default=None)
    parser.add_argument("--sel-m", type=float, default=None)
    return parser.parse_args(argv)


def _normalize_interval_unit(raw_unit: str) -> str | None:
    unit = raw_unit.strip().upper()
    if unit.endswith("S"):
        unit = unit[:-1]
    if unit not in _INTERVAL_SECONDS:
        return None
    return unit


def parse_interval_literal_seconds(expression: str) -> float | None:
    match = _INTERVAL_LITERAL_RE.search(expression)
    if match is None:
        return None
    unit = _normalize_interval_unit(match.group("unit"))
    if unit is None:
        return None
    try:
        value = float(match.group("value").strip())
    except ValueError:
        return None
    return value * _INTERVAL_SECONDS[unit]


def parse_independent_literal_equality(expression: str, variable: str) -> IndependentLiteralEquality | None:
    match = _QUALIFIED_LITERAL_EQ_RE.match(expression)
    if match is None:
        match = _LITERAL_QUALIFIED_EQ_RE.match(expression)
    if match is None:
        return None
    if match.group("var") != variable:
        return None
    return IndependentLiteralEquality(
        variable=variable,
        column=match.group("col"),
        literal_sql=match.group("literal").strip(),
    )


def parse_qualified_column_equality(expression: str) -> QualifiedEquality | None:
    match = _QUALIFIED_EQ_RE.match(expression)
    if match is None:
        return None
    if match.group("left_col") != match.group("right_col"):
        return None
    return QualifiedEquality(
        left_var=match.group("left_var"),
        left_col=match.group("left_col"),
        right_var=match.group("right_var"),
        right_col=match.group("right_col"),
    )


def parse_spatial_between(expression: str) -> SpatialBetween | None:
    match = _SPATIAL_BETWEEN_RE.match(expression)
    if match is None:
        return None
    return SpatialBetween(
        subject_var=match.group("subject_var"),
        subject_col=match.group("subject_col"),
        anchor_var=match.group("anchor_var"),
        anchor_col=match.group("anchor_col"),
        delta=float(match.group("delta")),
    )


@dataclass(frozen=True)
class OffsetBetween:
    subject_var: str
    subject_col: str
    anchor_var: str
    anchor_col: str
    lo_offset: float
    hi_offset: float


# ,,subj.col BETWEEN anchor.col [+/- lo] AND anchor.col [+/- hi]'': asymmetric cross-column
# band. Generalizes _SPATIAL_BETWEEN_RE; the two offsets are INDEPENDENT and either bound may
# be absent (offset 0), so ,,B.id BETWEEN A.id AND A.id + 500'' matches with lo=0, hi=500.
# Same anchor var/col on both bounds.
_OFFSET_BETWEEN_RE = re.compile(
    rf"(?is)^\s*(?P<subject_var>[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*(?P<subject_col>[A-Za-z_][A-Za-z0-9_]*)\s+BETWEEN\s+"
    rf"(?P<anchor_var>[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*(?P<anchor_col>[A-Za-z_][A-Za-z0-9_]*)"
    rf"(?:\s*(?P<lo_sign>[+-])\s*(?P<lo_mag>{_FLOAT_RE}))?\s+AND\s+"
    rf"(?P=anchor_var)\s*\.\s*(?P=anchor_col)"
    rf"(?:\s*(?P<hi_sign>[+-])\s*(?P<hi_mag>{_FLOAT_RE}))?\s*$"
)


def _signed_offset(sign, mag) -> float:
    """(sign, magnitude) match groups -> signed float; absent magnitude -> 0.0."""
    if mag is None:
        return 0.0
    value = float(mag)
    return -value if sign == "-" else value


def parse_offset_between(expression: str) -> "OffsetBetween | None":
    """Parse ,,b.col BETWEEN a.col+lo AND a.col+hi'' into an OffsetBetween, or None.

    Offsets are independent and either may be absent (offset 0).
    """
    match = _OFFSET_BETWEEN_RE.match(expression)
    if match is None:
        return None
    return OffsetBetween(
        subject_var=match.group("subject_var"),
        subject_col=match.group("subject_col"),
        anchor_var=match.group("anchor_var"),
        anchor_col=match.group("anchor_col"),
        lo_offset=_signed_offset(match.group("lo_sign"), match.group("lo_mag")),
        hi_offset=_signed_offset(match.group("hi_sign"), match.group("hi_mag")),
    )


_DATE_DIFF_UNIT_SECONDS = {
    "MILLISECOND": 1e-3,
    "SECOND": 1.0,
    "MINUTE": 60.0,
    "HOUR": 3600.0,
    "DAY": 86400.0,
}


def parse_temporal_window_seconds(expression: str) -> float | None:
    match = _SUBTRACTION_WINDOW_RE.search(expression)
    if match is not None:
        return parse_interval_literal_seconds(match.group(0))
    match = _BETWEEN_WINDOW_RE.search(expression)
    if match is not None and match.group("anchor_left") == match.group("anchor_right"):
        return parse_interval_literal_seconds(match.group(0))
    match = _ABS_DATE_DIFF_WINDOW_RE.search(expression)
    if match is not None:
        factor = _DATE_DIFF_UNIT_SECONDS.get(match.group("unit").upper())
        if factor is not None:
            return float(match.group("value")) * factor
    return None


def serialize_edge_key(pair: tuple[str, str]) -> str:
    return f"{pair[0]}|{pair[1]}"


def _warn(message: str) -> None:
    warnings.warn(message, RuntimeWarning, stacklevel=2)


def _base_where_clause(dep_graph: DependencyGraph, variable: str, alias: str = BASE_ALIAS) -> str:
    conditions = [
        render_expression_for_base_scan(predicate.text, variable, alias)
        for predicate in dep_graph.vertices[variable].independent_predicates
    ]
    if not conditions:
        return "TRUE"
    return " AND ".join(conditions)


def _scalar_query(sql_expression: str, *, table: str = DEFAULT_EVENTS_TABLE, where: str | None = None) -> str:
    lines = [f"SELECT {sql_expression} AS {SCALAR_ALIAS}", f"FROM {table} AS {BASE_ALIAS}"]
    if where:
        lines.append(f"WHERE {where}")
    return "\n".join(lines)


def _count_sql(*, table: str = DEFAULT_EVENTS_TABLE, where: str | None = None) -> str:
    return _scalar_query("COUNT(*)", table=table, where=where)


def _qualified_where_clause(dep_graph: DependencyGraph, variable: str) -> str:
    alias = variable.lower()
    conditions = [
        _rewrite_variable_alias_case(predicate.text, (variable,))
        for predicate in dep_graph.vertices[variable].independent_predicates
    ]
    if not conditions:
        return "TRUE"
    return " AND ".join(conditions).replace(f"{variable}.", f"{alias}.")


def _filtered_count_sql(dep_graph: DependencyGraph, variable: str, *, table: str = DEFAULT_EVENTS_TABLE) -> str:
    return _count_sql(table=table, where=_base_where_clause(dep_graph, variable))


def _filtered_ndv_sql(
    dep_graph: DependencyGraph,
    variable: str,
    column: str,
    *,
    table: str = DEFAULT_EVENTS_TABLE,
) -> str:
    return _scalar_query(
        f"approx_distinct({BASE_ALIAS}.{column})",
        table=table,
        where=_base_where_clause(dep_graph, variable),
    )


def _sanitize_alias(prefix: str, name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", f"{prefix}_{name}")


def _rewrite_variable_alias_case(expression: str, variables: Sequence[str]) -> str:
    rewritten = expression
    for variable in sorted(set(variables), key=len, reverse=True):
        lowered = variable.lower()
        rewritten = re.sub(
            rf"(?i)\b{re.escape(variable)}\s*\.",
            f"{lowered}.",
            rewritten,
        )
    return rewritten


def _mode_c_pair_count_sql(
    dep_graph: DependencyGraph,
    left_var: str,
    right_var: str,
    dependent_predicates: Sequence[str] = (),
    *,
    table: str = DEFAULT_EVENTS_TABLE,
) -> str:
    left_alias = left_var.lower()
    right_alias = right_var.lower()
    if left_alias == right_alias:
        raise ValueError(f"Mode c requires distinct aliases, got collision for variables {left_var!r} and {right_var!r}")

    clauses = [
        _qualified_where_clause(dep_graph, left_var),
        _qualified_where_clause(dep_graph, right_var),
    ]
    clauses.extend(_rewrite_variable_alias_case(predicate, (left_var, right_var)) for predicate in dependent_predicates)
    return "\n".join(
        [
            f"SELECT COUNT(*) AS {SCALAR_ALIAS}",
            f"FROM {table} AS {left_alias}, {table} AS {right_alias}",
            "WHERE " + "\n  AND ".join(clauses),
        ]
    )


def _is_temporal_ordering_predicate(expression: str, left_var: str, right_var: str) -> bool:
    left = re.escape(left_var.lower())
    right = re.escape(right_var.lower())
    # pure temporal ordering ,,X.ts < Y.ts'' between the two variables (either direction).
    pattern = re.compile(
        rf"(?is)^\s*\(?\s*(?:{left}|{right})\s*\.\s*(?:time|ts)\s*<\s*(?:{left}|{right})\s*\.\s*(?:time|ts)\s*\)?\s*$"
    )
    return pattern.match(_rewrite_variable_alias_case(expression, (left_var, right_var))) is not None


def _row_to_mapping(columns: Sequence[str], rows: Sequence[Sequence[object]]) -> dict[str, object]:
    if not rows:
        raise ValueError("Expected query to return a row, but it returned none")
    if len(rows) != 1:
        raise ValueError(f"Expected scalar query to return one row, got {len(rows)}")
    row = rows[0]
    return {str(column): row[idx] for idx, column in enumerate(columns)}


def _scalar_number(client: TrinoClient, sql: str, *, label: str) -> float:
    columns, rows = client.execute(sql)
    mapping = _row_to_mapping(columns, rows)
    value = mapping.get(SCALAR_ALIAS)
    if value is None:
        raise ValueError(f"{label} returned NULL")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} returned non-numeric value {value!r}") from exc
    if not math.isfinite(numeric):
        raise ValueError(f"{label} returned non-finite value {numeric!r}")
    return numeric


def _count_value(client: TrinoClient, sql: str, *, label: str) -> int:
    return int(math.floor(_scalar_number(client, sql, label=label)))


def _compute_mode_b_independent(
    dep_graph: DependencyGraph,
    total_rows: int,
    client: TrinoClient,
    *,
    table: str = DEFAULT_EVENTS_TABLE,
) -> dict[str, float]:
    if total_rows == 0:
        raise ValueError("events table is empty; populate it before computing selectivities")

    independent: dict[str, float] = {}
    for variable in dep_graph.variables:
        filtered = _count_value(
            client,
            _filtered_count_sql(dep_graph, variable, table=table),
            label=f"filtered count for {variable}",
        )
        independent[variable] = min(1.0, filtered / total_rows)
    return independent


def _filtered_ndv(
    dep_graph: DependencyGraph,
    variable: str,
    column: str,
    client: TrinoClient,
    *,
    table: str = DEFAULT_EVENTS_TABLE,
) -> float:
    return _scalar_number(
        client,
        _filtered_ndv_sql(dep_graph, variable, column, table=table),
        label=f"filtered NDV for {variable}.{column}",
    )


# --------------------------------------------------------------------------- #
# mode b_unified: the stats-only selectivity producer. Gathers coarse per-column stats
# ({low, high, ndv, nulls} in one aggregate SELECT) and prices every predicate by a closed
# form, emitting the {kind:constant} / {kind:window} payload the cost model reads.
#
# Predicate conventions (consistent with mode c so the cost model does not double-count):
#   * pure ts-ordering ,,X.ts < Y.ts''  -> sigma 1.0 (ordering cardinality is priced
#                                          structurally by the cost model).
#   * real INTERVAL / date_diff window  -> deferred {kind:window, w}.
#   * everything else (equality, band, offset band, cross-column inequality) -> a constant.
# Independent (family-A) atoms are priced from the same stats, combined by independence.
# --------------------------------------------------------------------------- #

# Independent family-A atoms: ,,var.col OP literal'' and ,,var.col BETWEEN lit AND lit''.
_INDEP_CMP_RE = re.compile(
    rf"(?is)^\s*(?P<var>[A-Za-z_]\w*)\s*\.\s*(?P<col>[A-Za-z_]\w*)\s*"
    rf"(?P<op><=|>=|<>|!=|<|>|=)\s*(?P<lit>'[^']*'|true|false|{_FLOAT_RE})\s*$"
)
# ,,var.col BETWEEN lo AND hi'': a single-column numeric range (family-A independent).
_INDEP_BETWEEN_RE = re.compile(
    rf"(?is)^\s*(?P<var>[A-Za-z_]\w*)\s*\.\s*(?P<col>[A-Za-z_]\w*)\s+BETWEEN\s+"
    rf"(?P<lo>{_FLOAT_RE})\s+AND\s+(?P<hi>{_FLOAT_RE})\s*$"
)
# Dependent cross-column comparison ,,var.col OP var.col'' (OP excludes '=', which is the
# EQUALITY path; the band path uses BETWEEN). Longest operators first.
_DEP_CMP_RE = re.compile(
    r"(?is)^\s*(?P<lvar>[A-Za-z_]\w*)\s*\.\s*(?P<lcol>[A-Za-z_]\w*)\s*"
    r"(?P<op><=|>=|<>|!=|<|>)\s*(?P<rvar>[A-Za-z_]\w*)\s*\.\s*(?P<rcol>[A-Za-z_]\w*)\s*$"
)
_INDEP_KIND = {"=": "eq", "<>": "neq", "!=": "neq", "<": "lt", "<=": "le", ">": "gt", ">=": "ge"}
_DEP_KIND = {"<": "lt", "<=": "le", ">": "gt", ">=": "ge", "<>": "neq", "!=": "neq"}

# Column-agnostic abs(date_diff('unit', X, Y)) <= value, the column NAMES do not affect the
# window width, so (unlike the shared {time,ts}-restricted regexes) this works on any schema.
_ABS_DATE_DIFF_VALUE_RE = re.compile(
    rf"(?is)date_diff\(\s*'(?P<unit>[A-Za-z]+)'\s*,[^)]*\)\s*\)?\s*<=?\s*(?P<value>{_FLOAT_RE})"
)


def _b_unified_window_seconds(text: str) -> "float | None":
    """Temporal-window half-width in seconds, column-name-agnostic (None if not a window).

    The INTERVAL / abs(date_diff(...)) bound does not depend on the timestamp column's name,
    so real-world tables (real_time/event_time) are handled. INTERVAL/date_diff is the window
    signal: a column-bounded BETWEEN without INTERVAL is an offset band, not a window.
    """
    low = text.lower()
    if "interval" not in low and "date_diff" not in low:
        return None
    fast = parse_temporal_window_seconds(text)  # canonical {time,ts} forms
    if fast is not None:
        return fast
    dd = _ABS_DATE_DIFF_VALUE_RE.search(text)
    if dd is not None:
        factor = _DATE_DIFF_UNIT_SECONDS.get(dd.group("unit").upper())
        if factor is not None:
            return float(dd.group("value")) * factor
    iv = _INTERVAL_LITERAL_RE.search(text)
    if iv is not None:
        return parse_interval_literal_seconds(iv.group(0))
    return None


def _b_unified_is_ordering(text: str, timestamp_columns) -> bool:
    """,,X.c < Y.c'' with BOTH columns TIMESTAMP-typed -> pure temporal ordering (sigma 1.0).

    Data-driven via ,,timestamp_columns'', so ,,A.real_time < B.real_time'' is recognized, not
    only the synthetic {time,ts}. Only strict ,,<'' is ordering; ,,<=''/,,>=''/,,>'' on a
    timestamp are value inequalities.
    """
    m = _DEP_CMP_RE.match(text)
    if m is None or m.group("op") != "<":
        return False
    return m.group("lcol") in timestamp_columns and m.group("rcol") in timestamp_columns


def _adapt_independent_predicate(text: str) -> "Predicate | None":
    """,,var.col OP literal'' / ,,var.col BETWEEN lit AND lit'' -> family-A Predicate, or None."""
    between = _INDEP_BETWEEN_RE.match(text)
    if between is not None:
        return Predicate(kind="between", family="A", col=between.group("col"),
                         lo=float(between.group("lo")), hi=float(between.group("hi")))
    match = _INDEP_CMP_RE.match(text)
    if match is None:
        return None
    kind = _INDEP_KIND.get(match.group("op"))
    if kind is None:
        return None
    raw = match.group("lit")
    if raw.startswith("'"):
        literal = raw[1:-1]
    elif raw.lower() in ("true", "false"):
        literal = raw.lower() == "true"  # python bool; mode_b prices via ndv (2) / nf
    else:
        literal = float(raw)
    return Predicate(kind=kind, family="A", col=match.group("col"), literal=literal)


def _adapt_dependent_predicate(text: str) -> "Predicate | None":
    """Cross-row predicate text -> family-B Predicate, or None.

    Callers MUST have already peeled off pure ts-ordering and real INTERVAL/date_diff
    windows; this handles equality, symmetric/offset bands, and cross-column inequality
    (incl. ts ,,>=''/,,<=''/,,>'' that classify_condition_type lumps under TEMPORAL_WINDOW).
    """
    classification = classify_condition_type(text)
    if classification == ConditionType.EQUALITY:
        parsed = parse_qualified_column_equality(text)
        if parsed is None:
            return None
        return Predicate(kind="eq", family="B", col=parsed.left_col, other=parsed.right_col)
    if classification == ConditionType.RANGE_SPATIAL:
        band = parse_spatial_between(text)
        if band is not None:
            return Predicate(kind="band", family="B", col=band.subject_col,
                             other=band.subject_col, delta=band.delta)
        offset = parse_offset_between(text)
        if offset is not None:
            return Predicate(kind="offset_band", family="B", col=offset.subject_col,
                             other=offset.subject_col, lo_offset=offset.lo_offset,
                             hi_offset=offset.hi_offset)
        # Not a parseable band -> FALL THROUGH to the col-col inequality path: a
        # spatial-named cross-column inequality (,,A.lon < B.lon'') is bucketed RANGE_SPATIAL
        # by classify_condition_type (the LAT/LON/DISTANCE token check) but is really an
        # inequality, not a band.
    # OTHER / RANGE_SPATIAL-non-band, or a ts-comparison that is neither ,,<''-ordering nor a
    # parseable window: cross-column inequality.
    match = _DEP_CMP_RE.match(text)
    if match is None:
        return None
    kind = _DEP_KIND.get(match.group("op"))
    if kind is None:
        return None
    return Predicate(kind=kind, family="B", col=match.group("lcol"), other=match.group("rcol"))


def _b_unified_stat_plan(dep_graph: DependencyGraph, timestamp_columns) -> tuple[list[str], set[str], set[str]]:
    """Parse predicates once -> (all_columns, eq_role_columns, hist_role_columns).

    * all_columns       : every physical column the estimator needs BASE stats for.
    * eq_role_columns   : columns in an equality / not-equal predicate; a top-K frequency
                           sketch helps IF the column is categorical.
    * hist_role_columns : columns in a family-A range (lt/le/gt/ge/between) or a dependent
                           band / offset_band; an equi-depth histogram helps IF the column is
                           numeric. Family-B col-col inequality is EXCLUDED (the estimator
                           uses the analytic low/high form, so a histogram there would be
                           gathered but unused).

    Raises on any in-scope predicate the adapter cannot parse, so coverage gaps surface
    rather than silently producing a wrong selectivity.
    """
    all_cols: set[str] = set()
    eq_cols: set[str] = set()
    hist_cols: set[str] = set()
    for variable in dep_graph.variables:
        for predicate in dep_graph.vertices[variable].independent_predicates:
            parsed = _adapt_independent_predicate(predicate.text)
            if parsed is None:
                raise ValueError(f"b_unified: unsupported independent predicate: {predicate.text!r}")
            all_cols.add(parsed.col)
            if parsed.kind in ("eq", "neq"):
                eq_cols.add(parsed.col)
            elif parsed.kind in ("lt", "le", "gt", "ge", "between"):
                hist_cols.add(parsed.col)
    for edge in dep_graph.edges:
        for predicate in edge.dependent_predicates:
            if _b_unified_is_ordering(predicate.text, timestamp_columns):
                continue
            if _b_unified_window_seconds(predicate.text) is not None:
                continue
            parsed = _adapt_dependent_predicate(predicate.text)
            if parsed is None:
                raise ValueError(f"b_unified: unsupported dependent predicate: {predicate.text!r}")
            all_cols.add(parsed.col)
            if parsed.other is not None:
                all_cols.add(parsed.other)
            if parsed.kind in ("eq", "neq"):
                eq_cols.add(parsed.col)
                if parsed.other is not None:
                    eq_cols.add(parsed.other)
            elif parsed.kind in ("band", "offset_band"):
                hist_cols.add(parsed.col)
    return sorted(all_cols), eq_cols, hist_cols


def _compute_b_unified_independent(dep_graph: DependencyGraph, stats: TableStats) -> dict[str, float]:
    independent: dict[str, float] = {}
    for variable in dep_graph.variables:
        selectivity = 1.0
        for predicate in dep_graph.vertices[variable].independent_predicates:
            parsed = _adapt_independent_predicate(predicate.text)  # validated in the column pass
            value = _unified_estimate(parsed, stats, profile="auto")
            if value != value:  # NaN -> unknown; conservatively no filtering for this conjunct
                continue
            selectivity *= value
        independent[variable] = max(0.0, min(1.0, selectivity))
    return independent


def _b_unified_edge_components(dep_graph: DependencyGraph, edge, stats: TableStats,
                               timestamp_columns) -> list[dict[str, float | str]]:
    components: list[dict[str, float | str]] = []
    for predicate in edge.dependent_predicates:
        text = predicate.text
        if _b_unified_is_ordering(text, timestamp_columns):
            components.append({"kind": "constant", "value": 1.0})
            continue
        seconds = _b_unified_window_seconds(text)
        if seconds is not None:
            components.append({"kind": "window", "w": seconds})
            continue
        parsed = _adapt_dependent_predicate(text)
        if parsed is None:
            raise ValueError(f"b_unified: unsupported dependent predicate: {text!r}")
        value = _unified_estimate(parsed, stats, profile="auto")
        if value != value:  # NaN
            raise ValueError(f"b_unified: estimate is unknown (NaN) for dependent predicate: {text!r}")
        components.append({"kind": "constant", "value": float(value)})
    return components


def _compute_b_unified_payload(
    dep_graph: DependencyGraph,
    client: TrinoClient,
    *,
    table: str = DEFAULT_EVENTS_TABLE,
    workload_metadata: Mapping[str, object] | None = None,
) -> dict[str, object]:
    total_rows = _count_value(client, _count_sql(table=table), label="global source row count")
    if total_rows == 0:
        raise ValueError("events table is empty; populate it before computing selectivities")

    # Data-driven timestamp set (the table may name its TIMESTAMP column anything:
    # real_time, event_time, so ordering detection must not be restricted to {time,ts}).
    _str_cols, _num_cols, ts_cols = _classify_column_types(client, table)
    columns, eq_cols, hist_cols = _b_unified_stat_plan(dep_graph, ts_cols)
    if columns:
        # gather the enriched catalog (top-K frequency for categorical equality columns,
        # equi-depth histograms for numeric range/band columns); operators on a column that
        # lacks a sketch fall back to the analytic form.
        stats = _unified_gather_stats(columns, client, table, enriched=True,
                                      eq_columns=eq_cols, hist_columns=hist_cols)
    else:
        stats = TableStats(row_count=total_rows, columns={})

    independent = _compute_b_unified_independent(dep_graph, stats)
    dependent: dict[str, list[dict[str, float | str]]] = {}
    for edge in dep_graph.edges:
        key = canonical_variable_pair(edge.var_left, edge.var_right, dep_graph.positions)
        dependent[serialize_edge_key(key)] = _b_unified_edge_components(dep_graph, edge, stats, ts_cols)

    payload: dict[str, object] = {
        "mode": "b_unified",
        "computed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source_events_rowcount": total_rows,
        "independent": independent,
        "dependent": dependent,
    }
    if workload_metadata:
        payload.update(workload_metadata)
        payload["workload_config"] = dict(workload_metadata)
    return payload


def _compute_mode_c_dependent(
    dep_graph: DependencyGraph,
    client: TrinoClient,
    *,
    table: str = DEFAULT_EVENTS_TABLE,
) -> dict[str, list[dict[str, float | str]]]:
    dependent: dict[str, list[dict[str, float | str]]] = {}

    for edge in dep_graph.edges:
        key = canonical_variable_pair(edge.var_left, edge.var_right, dep_graph.positions)
        serialized = serialize_edge_key(key)

        ordering_predicates: list[str] = []
        measured_predicates: list[str] = []
        for predicate in edge.dependent_predicates:
            if _is_temporal_ordering_predicate(predicate.text, edge.var_left, edge.var_right):
                ordering_predicates.append(predicate.text)
            else:
                measured_predicates.append(predicate.text)

        if ordering_predicates and measured_predicates:
            raise ValueError(
                f"Edge {serialized} includes temporal ordering predicates in dependent_predicates "
                f"alongside measured predicates: {ordering_predicates!r}"
            )

        if ordering_predicates and not measured_predicates:
            _warn(
                f"Edge {serialized} contains only temporal ordering predicates in dependent_predicates; "
                "emitting sigma=1.0 for mode c."
            )
            dependent[serialized] = [{"kind": "constant", "value": 1.0}]
            continue

        total_pairs = _count_value(
            client,
            _mode_c_pair_count_sql(dep_graph, edge.var_left, edge.var_right, table=table),
            label=f"mode c total pairs for edge {serialized}",
        )
        if total_pairs == 0:
            raise ValueError(
                f"Mode c total_pairs is zero for edge {serialized}; events table may be empty or an endpoint "
                "independent predicate may match no rows."
            )

        matching_pairs = _count_value(
            client,
            _mode_c_pair_count_sql(dep_graph, edge.var_left, edge.var_right, measured_predicates, table=table),
            label=f"mode c matching pairs for edge {serialized}",
        )
        dependent[serialized] = [{"kind": "constant", "value": matching_pairs / total_pairs}]

    return dependent


def compute_selectivities_payload(
    dep_graph: DependencyGraph,
    *,
    mode: str,
    client: TrinoClient,
    workload_metadata: Mapping[str, object] | None = None,
    table: str = DEFAULT_EVENTS_TABLE,
) -> dict[str, object]:
    if mode == "b_unified":
        return _compute_b_unified_payload(
            dep_graph, client, table=table, workload_metadata=workload_metadata
        )
    if mode != "c":
        raise ValueError(f"Unknown mode {mode!r}; use b_unified (deployable estimator) "
                         "or c (O(N^2) ground truth)")
    total_rows = _count_value(client, _count_sql(table=table), label="global source row count")
    independent = _compute_mode_b_independent(dep_graph, total_rows, client, table=table)
    dependent = _compute_mode_c_dependent(dep_graph, client, table=table)

    payload: dict[str, object] = {
        "mode": mode,
        "computed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source_events_rowcount": total_rows,
        "independent": independent,
        "dependent": dependent,
    }
    if workload_metadata:
        payload.update(workload_metadata)
        payload["workload_config"] = dict(workload_metadata)
    return payload


def workload_metadata_from_args(args: argparse.Namespace) -> dict[str, object]:
    metadata: dict[str, object] = {}
    if args.workload_profile is not None:
        metadata["workload_profile"] = args.workload_profile
    if args.workload_seed is not None:
        metadata["workload_seed"] = args.workload_seed
    if args.total_events is not None:
        metadata["total_events"] = args.total_events
    for attr in ("initial_history", "updates", "update_size", "scale", "sel_r", "sel_b", "sel_m"):
        value = getattr(args, attr)
        if value is not None:
            metadata[attr] = value
    if getattr(args, "external_manifest_fingerprint", None):
        metadata["external_manifest_fingerprint"] = args.external_manifest_fingerprint
    if getattr(args, "rho", None) is not None:
        metadata["rho"] = args.rho
    if getattr(args, "events_table", DEFAULT_EVENTS_TABLE) != DEFAULT_EVENTS_TABLE:
        metadata["events_table"] = args.events_table
    query_spec_name = getattr(args, "effective_query_spec_name", args.query_spec)
    metadata["query_spec"] = query_spec_name
    if getattr(args, "query_spec_source", None):
        metadata["query_spec_source"] = args.query_spec_source
    if getattr(args, "query_spec_fingerprint", None):
        metadata["query_spec_fingerprint"] = args.query_spec_fingerprint
    if getattr(args, "query_spec_file", None):
        metadata["query_spec_file"] = str(args.query_spec_file)
    for attr in ("trino_catalog", "trino_schema", "trino_server"):
        value = getattr(args, attr)
        if value is not None:
            metadata[attr] = value
    if args.workload_profile is None and args.workload_seed is None:
        return metadata
    metadata.setdefault("workload_profile", args.workload_profile or DEFAULT_WORKLOAD_PROFILE)
    metadata.setdefault("workload_seed", args.workload_seed if args.workload_seed is not None else DEFAULT_WORKLOAD_SEED)
    return metadata


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    output_path = Path(args.output)

    if args.canonical_query_sql:
        dep_graph, _ = build_from_sql(sql_file=Path(args.canonical_query_sql))
        args.effective_query_spec_name = args.query_spec
        args.query_spec_source = "sql"
        args.query_spec_fingerprint = None
    else:
        loaded_query_spec = load_query_spec_from_args(args)
        args.effective_query_spec_name = loaded_query_spec.spec.name
        args.query_spec_source = loaded_query_spec.source
        args.query_spec_fingerprint = loaded_query_spec.fingerprint
        dep_graph = query_spec_to_dependency_graph(loaded_query_spec.spec)
    config = TrinoConnectionConfig.from_env(
        server=args.trino_server,
        catalog=args.trino_catalog,
        schema=args.trino_schema,
        user=args.trino_user,
    )
    client = TrinoClient(config)

    try:
        payload = compute_selectivities_payload(
            dep_graph,
            mode=args.mode,
            client=client,
            workload_metadata=workload_metadata_from_args(args),
            table=args.events_table,
        )
    except (TrinoClientError, ValueError) as exc:
        raise SystemExit(f"Failed to compute selectivities: {exc}") from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
