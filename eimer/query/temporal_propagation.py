"""Temporal-constraint propagation: derives implied pairwise windows from the explicit ones.

An explicit window between u and v (pos(u) < pos(v)) bounds ts_v - ts_u. Combined with the pattern's
total order these bounds propagate: the tightest implied bound is the shortest path in the
difference-constraint graph, computed by Floyd-Warshall over the n <= 8 variables. Every pair the
bound reaches receives it: a pair without a dependent edge gets a derived window edge, a pair whose edge
states no window (a band, an equality) or only a looser one gets the implied window appended to that
edge. Derived predicates are used like declared ones downstream (view updates, compose, the estimator's
window sigma). Window-less queries are unchanged; `EIMER_DISABLE_WINDOW_PROPAGATION=1` switches it off.
"""
from __future__ import annotations

import os
import re
from dataclasses import replace
from typing import Dict, List, Tuple

from eimer.models import DependencyEdge, DependencyGraph, DependencyPredicate, canonical_variable_pair
from eimer.workload import Selectivities, WindowSelectivity

_UNIT_MS = {"MILLISECOND": 1.0, "SECOND": 1000.0, "MINUTE": 60000.0, "HOUR": 3600000.0, "DAY": 86400000.0, "WEEK": 604800000.0}

_ABS_DATE_DIFF_RE = re.compile(
    r"abs\s*\(\s*date_diff\s*\(\s*'(?P<unit>\w+)'\s*,\s*(?P<v1>\w+)\.ts\s*,\s*(?P<v2>\w+)\.ts\s*\)\s*\)"
    r"\s*<=\s*(?P<bound>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

_INTERVAL_RE = re.compile(
    r"(?P<vl>\w+)\.ts\s*<=\s*(?P<vr>\w+)\.ts\s*\+\s*INTERVAL\s*'(?P<bound>\d+(?:\.\d+)?)'\s*(?P<unit>\w+)",
    re.IGNORECASE,
)


def propagation_enabled() -> bool:
    """On by default; `EIMER_DISABLE_WINDOW_PROPAGATION=1` switches it off."""
    return os.environ.get("EIMER_DISABLE_WINDOW_PROPAGATION", "").lower() not in ("1", "true", "yes")


def propagate_default(dep_graph: DependencyGraph) -> DependencyGraph:
    """Entry point: augments unless `EIMER_DISABLE_WINDOW_PROPAGATION` is set. Idempotent."""
    if not propagation_enabled():
        return dep_graph
    augmented, _ = augment_dependency_graph(dep_graph)
    return augmented


_BETWEEN_RE = re.compile(
    r"(?P<vy>\w+)\.(?P<col>\w+)\s+BETWEEN\s+(?P<vx>\w+)\.(?P=col)\s+AND\s+(?P=vx)\.(?P=col)\s*\+\s*INTERVAL\s*'(?P<bound>\d+(?:\.\d+)?)'\s*(?P<unit>\w+)",
    re.IGNORECASE,
)
_SUBTRACT_RE = re.compile(
    r"(?P<vy>\w+)\.(?P<col>\w+)\s*-\s*(?P<vx>\w+)\.(?P=col)\s*<=\s*INTERVAL\s*'(?P<bound>\d+(?:\.\d+)?)'\s*(?P<unit>\w+)",
    re.IGNORECASE,
)
_COLUMN_RES = (_BETWEEN_RE, _SUBTRACT_RE)

DERIVED_WINDOW_OPERATOR = "DERIVED_WINDOW"


def window_column(dep_graph: DependencyGraph) -> str:
    """The timestamp column of the graph's explicit window bounds (first found); `ts` when none names one."""
    for edge in dep_graph.edges:
        for predicate in edge.dependent_predicates:
            for rx in _COLUMN_RES:
                m = rx.search(predicate.text)
                if m:
                    return m.group("col")
    return "ts"


def is_derived_window_edge(edge: DependencyEdge) -> bool:
    """An edge that exists only because of propagation (every predicate derived)."""
    return bool(edge.dependent_predicates) and all(pr.operator == DERIVED_WINDOW_OPERATOR for pr in edge.dependent_predicates)


def strip_derived_window_edges(dep_graph: DependencyGraph) -> DependencyGraph:
    """The graph without its derived window edges (derived predicates appended to explicit edges stay)."""
    kept = [e for e in dep_graph.edges if not is_derived_window_edge(e)]
    return dep_graph if len(kept) == len(dep_graph.edges) else replace(dep_graph, edges=kept)


def explicit_selection_inputs(dep_graph: DependencyGraph, workload):
    dep_sel = strip_derived_window_edges(dep_graph)
    if dep_sel is dep_graph or workload is None:
        return dep_graph, workload
    explicit = {frozenset((e.var_left, e.var_right)) for e in dep_sel.edges}
    sels = workload.selectivities
    wl_sel = replace(workload, dependency_graph=dep_sel,
                     selectivities=replace(sels, dependent={k: v for k, v in sels.dependent.items() if frozenset(k) in explicit}))
    return dep_sel, wl_sel


def parse_window_bounds_ms(text: str, *, conservative: bool = False) -> List[Tuple[str, str, float]]:
    """Directed bounds [(u, v, W_ms)] meaning ts_v - ts_u <= W_ms, extracted from one predicate text.

        `conservative` widens a date_diff bound by one unit: `date_diff('millisecond', x, y) <= K`
        becomes (K+1)·unit. The interval form `y <= x + INTERVAL` is unchanged.
    """
    bounds: List[Tuple[str, str, float]] = []
    for m in _ABS_DATE_DIFF_RE.finditer(text):
        unit = _UNIT_MS.get(m.group("unit").upper())
        if unit is None:
            continue
        w = (float(m.group("bound")) + (1.0 if conservative else 0.0)) * unit
        v1, v2 = m.group("v1"), m.group("v2")
        bounds.append((v1, v2, w))
        bounds.append((v2, v1, w))
    for m in _INTERVAL_RE.finditer(text):
        unit = _UNIT_MS.get(m.group("unit").upper().rstrip("S"))
        if unit is None:
            continue
        w = float(m.group("bound")) * unit
        bounds.append((m.group("vr"), m.group("vl"), w))
    for rx in _COLUMN_RES:
        for m in rx.finditer(text):
            unit = _UNIT_MS.get(m.group("unit").upper().rstrip("S"))
            if unit is None:
                continue
            bounds.append((m.group("vx"), m.group("vy"), float(m.group("bound")) * unit))
    return bounds


def implied_windows(dep_graph: DependencyGraph, *, conservative: bool = False) -> Dict[Tuple[str, str], float]:
    """Tightest implied window (ms) for every position-ordered pair, via shortest paths.

        Returns only pairs with a finite bound, keyed (x, y) with pos(x) < pos(y). Queries without explicit
        windows return {}. `conservative` is passed to `parse_window_bounds_ms`.
    """
    variables = sorted(dep_graph.variables, key=lambda v: dep_graph.positions[v])
    pos = dep_graph.positions
    inf = float("inf")
    d = {u: {v: (0.0 if u == v else inf) for v in variables} for u in variables}
    for x in variables:
        for y in variables:
            if pos[x] < pos[y]:
                d[y][x] = 0.0
    for edge in dep_graph.edges:
        for predicate in edge.dependent_predicates:
            for u, v, w in parse_window_bounds_ms(predicate.text, conservative=conservative):
                if u in d and v in d[u] and w < d[u][v]:
                    d[u][v] = w
    for k in variables:
        for i in variables:
            dik = d[i][k]
            if dik == inf:
                continue
            for j in variables:
                if dik + d[k][j] < d[i][j]:
                    d[i][j] = dik + d[k][j]
    out: Dict[Tuple[str, str], float] = {}
    for x in variables:
        for y in variables:
            if pos[x] < pos[y] and d[x][y] != inf:
                out[(x, y)] = d[x][y]
    return out


def _render_ms(w_ms: float) -> str:
    """Renders a bound that re-parses to itself: integral milliseconds as an int, else at most six decimals."""
    if abs(w_ms - round(w_ms)) < 1e-9:
        return str(int(round(w_ms)))
    return f"{w_ms:.6f}".rstrip("0").rstrip(".")


def _derived_window_predicate(x: str, y: str, w_ms: float, col: str = "ts") -> DependencyPredicate:
    return DependencyPredicate(
        text=f"abs(date_diff('millisecond', {x}.{col}, {y}.{col})) <= {_render_ms(w_ms)}",
        operator=DERIVED_WINDOW_OPERATOR,
        referenced_variables=[x, y],
        source_variable="",
        kind="DEPENDENT",
    )


def _stated_bound_ms(edge: DependencyEdge, x: str, y: str) -> float:
    """The tightest bound on ts_y - ts_x the edge's own predicates state (inf when they state none)."""
    best = float("inf")
    for predicate in edge.dependent_predicates:
        for u, v, w in parse_window_bounds_ms(predicate.text):
            if u == x and v == y and w < best:
                best = w
    return best


_TIGHTER_BY_MS = 1e-6


def augment_dependency_graph(dep_graph: DependencyGraph) -> Tuple[DependencyGraph, Dict[Tuple[str, str], float]]:
    """Attaches the implied window to every pair it reaches.

        A pair without a dependent edge gets a derived edge carrying the window; a pair whose edge states no
        window, or only a looser one, gets the window appended after the edge's own predicates.
        Returns (augmented graph, {canonical pair key: W_ms}) over every pair that received a
        window. The input graph is not mutated; with nothing to add it is returned as is.
    """
    col = window_column(dep_graph)
    implied = implied_windows(dep_graph)
    pos = dep_graph.positions
    edges: List[DependencyEdge] = list(dep_graph.edges)
    index_of = {frozenset((e.var_left, e.var_right)): i for i, e in enumerate(edges)}
    quant = getattr(dep_graph, "quantifiers", None) or {}
    kleene = {v for v in dep_graph.variables if str(quant.get(v, "ONE")).upper() != "ONE"}
    derived_map: Dict[Tuple[str, str], float] = {}
    for (x, y), w_ms in sorted(implied.items(), key=lambda kv: (pos[kv[0][0]], pos[kv[0][1]])):
        if x in kleene or y in kleene:
            continue
        i = index_of.get(frozenset((x, y)))
        if i is None:
            edges.append(
                DependencyEdge(
                    var_left=x,
                    var_right=y,
                    span=(pos[x], pos[y]),
                    dependent_predicates=[_derived_window_predicate(x, y, w_ms, col)],
                    source_variables=[],
                )
            )
        elif w_ms < _stated_bound_ms(edges[i], x, y) - _TIGHTER_BY_MS:
            edges[i] = replace(
                edges[i],
                dependent_predicates=[*edges[i].dependent_predicates, _derived_window_predicate(x, y, w_ms, col)],
            )
        else:
            continue
        derived_map[canonical_variable_pair(x, y, pos)] = w_ms
    if not derived_map:
        return dep_graph, {}
    return replace(dep_graph, edges=edges), derived_map


def inject_derived_window_selectivities(
    selectivities: Selectivities, derived_map: Dict[Tuple[str, str], float], *,
    rho_time_unit_ms: float = 1000.0,
) -> Selectivities:
    """Adds WindowSelectivity entries for derived pairs missing from the measured payload.

        WindowSelectivity.w is in the workload's rho time unit (rho = events
        per second), so w = W_ms / rho_time_unit_ms. Existing keys are not overwritten.
    """
    dependent = dict(selectivities.dependent)
    for key, w_ms in derived_map.items():
        if key not in dependent:
            dependent[key] = (WindowSelectivity(w=w_ms / rho_time_unit_ms),)
    return replace(selectivities, dependent=dependent)
