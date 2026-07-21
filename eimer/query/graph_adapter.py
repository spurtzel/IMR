"""adapters that build a dependency graph from sql text, a file, or json, plus the
schema-min referenced-column collection."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List

from eimer.query.dependency_graph import build_dependency_graph, compile_sql_to_dependency_graph
from eimer.models import DependencyEdge, DependencyGraph, DependencyPredicate, DependencyVertex, WildcardGap
from eimer.query.parser import compile_sql_file, compile_sql_text


class DependencyGraphBuildError(ValueError):
    pass


def _normalize_source_variables(raw: Any) -> List[str]:
    """coerce a raw source-variables field (str or list) to a list of strings."""
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return []


def _predicate_from_text(text: str, source_variable: str = "") -> DependencyPredicate:
    """a bare dependent predicate carrying only its text."""
    return DependencyPredicate(
        text=text,
        operator="UNKNOWN",
        referenced_variables=[],
        source_variable=source_variable,
        kind="DEPENDENT",
    )


def _from_current_dependency_graph_json(payload: Dict[str, Any]) -> DependencyGraph:
    """load a DependencyGraph from the current full json schema (explicit positions/gaps)."""
    variables = list(payload.get("variables", []))
    positions = payload.get("positions") or {var: idx + 1 for idx, var in enumerate(variables)}
    quantifiers = payload.get("quantifiers") or {var: "ONE" for var in variables}

    wildcard_gaps = [
        WildcardGap(
            left_variable=str(item["left_variable"]),
            right_variable=str(item["right_variable"]),
            wildcard=str(item["wildcard"]),
            mode=str(item["mode"]),
        )
        for item in payload.get("wildcard_gaps", [])
    ]

    vertices: Dict[str, DependencyVertex] = {}
    raw_vertices = payload.get("vertices", {})
    for var in variables:
        raw_vertex = raw_vertices.get(var, {})
        preds = [
            DependencyPredicate(
                text=str(item.get("text", "")),
                operator=str(item.get("operator", "UNKNOWN")),
                referenced_variables=[str(v) for v in item.get("referenced_variables", [])],
                source_variable=str(item.get("source_variable", var)),
                kind=str(item.get("kind", "INDEPENDENT")),
            )
            for item in raw_vertex.get("independent_predicates", [])
        ]
        vertices[var] = DependencyVertex(
            variable=var,
            position=int(raw_vertex.get("position", positions[var])),
            quantifier=str(raw_vertex.get("quantifier", quantifiers[var])),
            independent_predicates=preds,
        )

    edges: List[DependencyEdge] = []
    for raw_edge in payload.get("edges", []):
        preds = [
            DependencyPredicate(
                text=str(item.get("text", "")),
                operator=str(item.get("operator", "UNKNOWN")),
                referenced_variables=[str(v) for v in item.get("referenced_variables", [])],
                source_variable=str(item.get("source_variable", "")),
                kind=str(item.get("kind", "DEPENDENT")),
            )
            for item in raw_edge.get("dependent_predicates", [])
        ]
        edges.append(
            DependencyEdge(
                var_left=str(raw_edge["var_left"]),
                var_right=str(raw_edge["var_right"]),
                span=(int(raw_edge["span"][0]), int(raw_edge["span"][1])),
                dependent_predicates=preds,
                source_variables=[str(item) for item in raw_edge.get("source_variables", [])],
            )
        )

    return DependencyGraph(
        variables=variables,
        positions={str(k): int(v) for k, v in positions.items()},
        quantifiers={str(k): str(v) for k, v in quantifiers.items()},
        wildcard_gaps=wildcard_gaps,
        vertices=vertices,
        edges=edges,
        warnings=list(payload.get("warnings", [])),
    )


def _from_simplified_dependency_graph_json(payload: Dict[str, Any]) -> DependencyGraph:
    """load a DependencyGraph from the simplified json schema (positions and gaps inferred)."""
    variables = list(payload.get("variables", []))
    positions = {var: idx + 1 for idx, var in enumerate(variables)}
    quantifiers = {var: "ONE" for var in variables}
    wildcard_gaps = [
        WildcardGap(
            left_variable=variables[idx],
            right_variable=variables[idx + 1],
            wildcard="Z",
            mode="GREEDY",
        )
        for idx in range(max(0, len(variables) - 1))
    ]

    raw_vertices = payload.get("vertices", {})
    vertices: Dict[str, DependencyVertex] = {}
    for var in variables:
        ic = [
            DependencyPredicate(
                text=str(expr),
                operator="UNKNOWN",
                referenced_variables=[var],
                source_variable=var,
                kind="INDEPENDENT",
            )
            for expr in raw_vertices.get(var, {}).get("IC", [])
        ]
        vertices[var] = DependencyVertex(
            variable=var,
            position=positions[var],
            quantifier="ONE",
            independent_predicates=ic,
        )

    edges: List[DependencyEdge] = []
    for raw_edge in payload.get("edges", []):
        edge_vars = raw_edge.get("variables", [])
        if len(edge_vars) != 2:
            continue
        left, right = str(edge_vars[0]), str(edge_vars[1])
        edges.append(
            DependencyEdge(
                var_left=left,
                var_right=right,
                span=(positions[left], positions[right]),
                dependent_predicates=[_predicate_from_text(str(expr)) for expr in raw_edge.get("DC", [])],
                source_variables=_normalize_source_variables(raw_edge.get("src", [])),
            )
        )

    return DependencyGraph(
        variables=variables,
        positions=positions,
        quantifiers=quantifiers,
        wildcard_gaps=wildcard_gaps,
        vertices=vertices,
        edges=edges,
        warnings=list(payload.get("warnings", [])),
    )


def build_dependency_graph_from_sql_text(sql_text: str) -> DependencyGraph:
    """compile sql text straight to a DependencyGraph, raising on any diagnostic."""
    result = compile_sql_to_dependency_graph(sql_text)
    if not result.ok or result.dependency_graph is None:
        diagnostics = result.diagnostics_to_dict()
        raise DependencyGraphBuildError(f"Could not compile SQL to dependency graph: {diagnostics}")
    return result.dependency_graph


def build_dependency_graph_from_sql_file(path: str | Path) -> DependencyGraph:
    """compile an sql file to a DependencyGraph, raising on any diagnostic."""
    result = compile_sql_file(path)
    if not result.ok or result.ir is None:
        diagnostics = result.diagnostics_to_dict()
        raise DependencyGraphBuildError(f"Could not compile SQL file to normalized IR: {diagnostics}")
    return build_dependency_graph(result.ir)


def build_dependency_graph_from_json(path: str | Path) -> DependencyGraph:
    """load a DependencyGraph from json, detecting which schema variant the file uses."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("positions") is not None or payload.get("wildcard_gaps") is not None:
        return _from_current_dependency_graph_json(payload)
    return _from_simplified_dependency_graph_json(payload)


def schema_min_enabled() -> bool:
    """schema-min kill switch (default on). set EIMER_DISABLE_SCHEMA_MIN=1 to revert to the
    wide-view behavior where views carry every predicate-referenced column."""
    return os.environ.get("EIMER_DISABLE_SCHEMA_MIN", "").lower() not in ("1", "true", "yes")


def collect_referenced_columns(dep_graph: DependencyGraph, *, minimize: bool | None = None) -> Dict[str, List[str]]:
    """per-variable columns each view must carry: always id/ts, plus dependent-predicate and
    MEASURES-referenced columns. under schema-min (the default) a column referenced ONLY by an
    independent predicate is dropped, since that predicate runs on the base scan and the column is
    dead in the view. minimize=False keeps the wide set (id/ts plus every predicate-referenced column)."""
    if minimize is None:
        minimize = schema_min_enabled()
    # a qualified column reference ,,var.col''
    qualified_ref_re = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\b")
    refs: Dict[str, set[str]] = {var: {"id", "ts"} for var in dep_graph.variables}

    def _ingest(expressions: Iterable[str]) -> None:
        for expr in expressions:
            for match in qualified_ref_re.finditer(expr):
                var, col = match.group(1), match.group(2)
                if var in refs:
                    refs[var].add(col)

    # dependent-predicate columns: live (applied at compose, or when a multi-variable view is built)
    for edge in dep_graph.edges:
        _ingest(edge.dependent_conditions)
    # MEASURES-referenced columns: live (projected into the result); (variable, attribute) from the IR
    for measure in dep_graph.measures:
        if measure.variable in refs and measure.attribute not in ("*",):
            refs[measure.variable].add(measure.attribute)
    # independent-predicate columns: dead after the base scan -> keep ONLY when NOT minimizing
    if not minimize:
        for var in dep_graph.variables:
            _ingest(dep_graph.independent_conditions.get(var, []))

    # Case-insensitive dedup: SQL folds UNQUOTED identifiers, so two case-variant refs to one column
    # (measure C.G vs predicate C.g) would emit two view columns that collide at CTAS. Keep one per case-folded name.
    def _dedup_ci(cols: set) -> List[str]:
        kept: Dict[str, str] = {}
        for col in sorted(cols):
            kept.setdefault(col.lower(), col)
        return sorted(kept.values())

    return {var: _dedup_ci(cols) for var, cols in refs.items()}
