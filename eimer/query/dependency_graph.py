"""dependency-graph construction: compile a match_recognize query into the typed
variable/edge graph the rest of the pipeline plans over."""

from __future__ import annotations

from typing import Dict, List, Tuple

from eimer.models import (
    CompileToDependencyGraphResult,
    DependencyEdge,
    DependencyGraph,
    DependencyPredicate,
    DependencyVertex,
    Diagnostic,
    NormalizedIR,
    WildcardGap,
    canonical_variable_pair,
)
from eimer.query.parser import compile_sql_text


class DependencyGraphBuildError(ValueError):
    pass


def _build_quantifier_map(ir: NormalizedIR) -> Dict[str, str]:
    """map each pattern variable to its quantifier, checking for dupes and full coverage."""
    quantifiers: Dict[str, str] = {}
    for entry in ir.variable_quantifiers:
        name = entry.get("name")
        quantifier = entry.get("quantifier")
        if not name or not quantifier:
            raise DependencyGraphBuildError("Normalized IR contains an invalid variable quantifier entry.")
        if name in quantifiers:
            raise DependencyGraphBuildError(f"Duplicate variable quantifier entry for '{name}'.")
        quantifiers[name] = quantifier

    if quantifiers.keys() != set(ir.pattern_variables):
        raise DependencyGraphBuildError("Normalized IR variable quantifiers do not match the pattern variables.")
    return quantifiers


def _build_wildcard_gaps(ir: NormalizedIR, variables: List[str]) -> List[WildcardGap]:
    """build the n-1 wildcard gaps bridging consecutive pattern variables."""
    expected_gap_count = max(0, len(variables) - 1)
    if len(ir.wildcard_gaps) != expected_gap_count:
        raise DependencyGraphBuildError(
            f"Expected {expected_gap_count} wildcard gaps for {len(variables)} pattern variables, "
            f"found {len(ir.wildcard_gaps)}."
        )

    gaps: List[WildcardGap] = []
    for idx, raw_gap in enumerate(ir.wildcard_gaps):
        wildcard = raw_gap.get("wildcard")
        mode = raw_gap.get("mode")
        if not wildcard or not mode:
            raise DependencyGraphBuildError("Normalized IR contains an invalid wildcard gap entry.")
        gaps.append(
            WildcardGap(
                left_variable=variables[idx],
                right_variable=variables[idx + 1],
                wildcard=wildcard,
                mode=mode,
            )
        )
    return gaps


def build_dependency_graph(ir: NormalizedIR) -> DependencyGraph:
    """build the DependencyGraph from the normalized ir: vertices carry independent predicates,
    dependent conjuncts become edges, then implied windows are propagated."""
    variables = list(ir.pattern_variables)
    positions = {var: idx + 1 for idx, var in enumerate(variables)}
    quantifiers = _build_quantifier_map(ir)
    wildcard_gaps = _build_wildcard_gaps(ir, variables)

    vertices: Dict[str, DependencyVertex] = {
        var: DependencyVertex(
            variable=var,
            position=positions[var],
            quantifier=quantifiers[var],
            independent_predicates=[],
        )
        for var in variables
    }

    edge_predicates: Dict[Tuple[str, str], List[DependencyPredicate]] = {}
    edge_sources: Dict[Tuple[str, str], List[str]] = {}

    for define_entry in ir.define_entries:
        source_variable = define_entry.variable
        if source_variable == "Z":
            continue
        if source_variable not in vertices:
            raise DependencyGraphBuildError(
                f"Normalized IR DEFINE entry refers to unknown variable '{source_variable}'."
            )

        for conjunct in define_entry.conjuncts:
            predicate = DependencyPredicate(
                text=conjunct.text,
                operator=conjunct.operator,
                referenced_variables=list(conjunct.referenced_variables),
                source_variable=source_variable,
                kind=conjunct.kind,
            )
            if conjunct.kind == "INDEPENDENT":
                vertices[source_variable].independent_predicates.append(predicate)
                continue
            if conjunct.kind != "DEPENDENT":
                raise DependencyGraphBuildError(
                    f"Unsupported predicate kind '{conjunct.kind}' in normalized IR."
                )

            if len(conjunct.referenced_variables) != 2:
                raise DependencyGraphBuildError(
                    "Dependent predicates in normalized IR must reference exactly two variables."
                )

            left, right = canonical_variable_pair(
                conjunct.referenced_variables[0],
                conjunct.referenced_variables[1],
                positions,
            )
            key = (left, right)
            edge_predicates.setdefault(key, []).append(predicate)
            edge_sources.setdefault(key, [])
            if source_variable not in edge_sources[key]:
                edge_sources[key].append(source_variable)

    edges: List[DependencyEdge] = []
    for key in sorted(edge_predicates.keys(), key=lambda item: (positions[item[0]], positions[item[1]])):
        left, right = key
        edges.append(
            DependencyEdge(
                var_left=left,
                var_right=right,
                span=(positions[left], positions[right]),
                dependent_predicates=list(edge_predicates[key]),
                source_variables=list(edge_sources.get(key, [])),
            )
        )

    graph = DependencyGraph(
        variables=variables,
        positions=positions,
        quantifiers=quantifiers,
        wildcard_gaps=wildcard_gaps,
        vertices=vertices,
        edges=edges,
        warnings=[],
        measures=tuple(ir.measures),
    )
    # propagate implied pairwise windows from the explicit ones (identity for window-less queries;
    # kill-switch EIMER_DISABLE_WINDOW_PROPAGATION=1). deferred import keeps the module graph acyclic.
    from eimer.query.temporal_propagation import propagate_default

    return propagate_default(graph)


def compile_sql_to_dependency_graph(sql_text: str) -> CompileToDependencyGraphResult:
    """compile sql text to a DependencyGraph result, threading through parse diagnostics."""
    compile_result = compile_sql_text(sql_text)
    if not compile_result.ok or compile_result.ir is None:
        return CompileToDependencyGraphResult(
            ok=False,
            diagnostics=list(compile_result.diagnostics),
            ast=compile_result.ast,
            ir=compile_result.ir,
            dependency_graph=None,
        )

    try:
        graph = build_dependency_graph(compile_result.ir)
    except DependencyGraphBuildError as exc:
        diagnostics = list(compile_result.diagnostics)
        diagnostics.append(
            Diagnostic(
                code="dependency_graph_build_failed",
                message="Could not build dependency graph from normalized IR.",
                clause="DEPENDENCY_GRAPH",
                detail=str(exc),
            )
        )
        return CompileToDependencyGraphResult(
            ok=False,
            diagnostics=diagnostics,
            ast=compile_result.ast,
            ir=compile_result.ir,
            dependency_graph=None,
        )

    return CompileToDependencyGraphResult(
        ok=True,
        diagnostics=list(compile_result.diagnostics),
        ast=compile_result.ast,
        ir=compile_result.ir,
        dependency_graph=graph,
    )
