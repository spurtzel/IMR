"""temporal-constraint propagation: derive implied pairwise windows from the explicit ones.

an explicit window between u and v (pos(u) < pos(v)) bounds ts_v - ts_u. combined with the pattern's
total order these bounds propagate: the tightest implied bound is the shortest path in the
difference-constraint graph, computed by floyd-warshall over the n <= 8 variables. derived edges then
participate like declared ones downstream, injecting sigma as a native WindowSelectivity. identity for
window-less queries; kill-switch EIMER_DISABLE_WINDOW_PROPAGATION=1.
"""
from __future__ import annotations

import os
import re
from dataclasses import replace
from typing import Dict, List, Tuple

from eimer.models import DependencyEdge, DependencyGraph, DependencyPredicate, canonical_variable_pair
from eimer.workload import Selectivities, WindowSelectivity

_UNIT_MS = {"MILLISECOND": 1.0, "SECOND": 1000.0, "MINUTE": 60000.0, "HOUR": 3600000.0}

# a symmetric window predicate: abs(date_diff('unit', X.ts, Y.ts)) <= K bounds |ts_Y - ts_X|
_ABS_DATE_DIFF_RE = re.compile(
    r"abs\s*\(\s*date_diff\s*\(\s*'(?P<unit>\w+)'\s*,\s*(?P<v1>\w+)\.ts\s*,\s*(?P<v2>\w+)\.ts\s*\)\s*\)"
    r"\s*<=\s*(?P<bound>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# a one-sided interval window predicate: X.ts <= Y.ts + INTERVAL 'k' UNIT bounds ts_X - ts_Y <= k
_INTERVAL_RE = re.compile(
    r"(?P<vl>\w+)\.ts\s*<=\s*(?P<vr>\w+)\.ts\s*\+\s*INTERVAL\s*'(?P<bound>\d+(?:\.\d+)?)'\s*(?P<unit>\w+)",
    re.IGNORECASE,
)


def propagation_enabled() -> bool:
    """on by default (identity for window-less queries); kill-switch EIMER_DISABLE_WINDOW_PROPAGATION=1."""
    return os.environ.get("EIMER_DISABLE_WINDOW_PROPAGATION", "").lower() not in ("1", "true", "yes")


def propagate_default(dep_graph: DependencyGraph) -> DependencyGraph:
    """entry point: augment unless the kill-switch is set. idempotent: derived edges re-parse to the
    same bounds, and pairs with edges are never re-derived."""
    if not propagation_enabled():
        return dep_graph
    augmented, _ = augment_dependency_graph(dep_graph)
    return augmented


def parse_window_bounds_ms(text: str) -> List[Tuple[str, str, float]]:
    """directed bounds [(u, v, W_ms)] meaning ts_v - ts_u <= W_ms, extracted from one predicate text."""
    bounds: List[Tuple[str, str, float]] = []
    for m in _ABS_DATE_DIFF_RE.finditer(text):
        unit = _UNIT_MS.get(m.group("unit").upper())
        if unit is None:
            continue
        w = float(m.group("bound")) * unit
        v1, v2 = m.group("v1"), m.group("v2")
        bounds.append((v1, v2, w))
        bounds.append((v2, v1, w))
    for m in _INTERVAL_RE.finditer(text):
        unit = _UNIT_MS.get(m.group("unit").upper().rstrip("S"))
        if unit is None:
            continue
        w = float(m.group("bound")) * unit
        # vl.ts <= vr.ts + W  bounds  ts_vl - ts_vr <= W : arc vr -> vl
        bounds.append((m.group("vr"), m.group("vl"), w))
    return bounds


def implied_windows(dep_graph: DependencyGraph) -> Dict[Tuple[str, str], float]:
    """tightest implied window (ms) for every position-ordered pair, via shortest paths.

    returns only pairs with a finite bound, keyed (x, y) with pos(x) < pos(y). queries without explicit
    windows return {}; pure orderings propagate nothing (they bound differences by 0 only backwards).
    """
    variables = sorted(dep_graph.variables, key=lambda v: dep_graph.positions[v])
    pos = dep_graph.positions
    inf = float("inf")
    d = {u: {v: (0.0 if u == v else inf) for v in variables} for u in variables}
    for x in variables:
        for y in variables:
            if pos[x] < pos[y]:
                d[y][x] = 0.0  # ordering: ts_x - ts_y < 0 <= 0
    for edge in dep_graph.edges:
        for predicate in edge.dependent_predicates:
            for u, v, w in parse_window_bounds_ms(predicate.text):
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


def augment_dependency_graph(dep_graph: DependencyGraph) -> Tuple[DependencyGraph, Dict[Tuple[str, str], float]]:
    """add derived window edges for implied-window pairs that have no explicit dependent edge.

    returns (augmented graph, {canonical pair key: W_ms}); the map drives sigma injection. the original
    graph is unchanged; with no explicit windows this is the identity (derived map empty).
    """
    implied = implied_windows(dep_graph)
    existing = {frozenset((e.var_left, e.var_right)) for e in dep_graph.edges}
    pos = dep_graph.positions
    derived_edges: List[DependencyEdge] = []
    derived_map: Dict[Tuple[str, str], float] = {}
    for (x, y), w_ms in sorted(implied.items(), key=lambda kv: (pos[kv[0][0]], pos[kv[0][1]])):
        if frozenset((x, y)) in existing:
            continue
        text = f"abs(date_diff('millisecond', {x}.ts, {y}.ts)) <= {int(round(w_ms))}"
        derived_edges.append(
            DependencyEdge(
                var_left=x,
                var_right=y,
                span=(pos[x], pos[y]),
                dependent_predicates=[
                    DependencyPredicate(
                        text=text,
                        operator="UNKNOWN",
                        referenced_variables=[x, y],
                        source_variable="",
                        kind="DEPENDENT",
                    )
                ],
                source_variables=[],
            )
        )
        derived_map[canonical_variable_pair(x, y, pos)] = w_ms
    if not derived_edges:
        return dep_graph, {}
    return replace(dep_graph, edges=[*dep_graph.edges, *derived_edges]), derived_map


def inject_derived_window_selectivities(
    selectivities: Selectivities, derived_map: Dict[Tuple[str, str], float], *,
    rho_time_unit_ms: float = 1000.0,
) -> Selectivities:
    """add WindowSelectivity entries for derived pairs missing from the measured payload.

    WindowSelectivity.w must be in the workload's rho time unit (the benchmark convention: rho = events
    per second), so w = W_ms / rho_time_unit_ms. existing keys are never overwritten.
    """
    dependent = dict(selectivities.dependent)
    for key, w_ms in derived_map.items():
        if key not in dependent:
            dependent[key] = (WindowSelectivity(w=w_ms / rho_time_unit_ms),)
    return replace(selectivities, dependent=dependent)
