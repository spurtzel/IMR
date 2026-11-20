"""temporal-constraint propagation: derive implied pairwise windows from the explicit ones.

an explicit window between u and v (pos(u) < pos(v)) bounds ts_v - ts_u. combined with the pattern's
total order these bounds propagate: the tightest implied bound is the shortest path in the
difference-constraint graph, computed by floyd-warshall over the n <= 8 variables. every pair the
bound reaches receives it: a pair without a dependent edge gets a derived window edge, a pair whose edge
states no window (a band, an equality) or only a looser one gets the implied window appended to that
edge, so no maintained view is left unbounded in time. derived predicates participate like declared
ones downstream (view updates, compose, the estimator's window sigma). identity for window-less
queries; kill-switch EIMER_DISABLE_WINDOW_PROPAGATION=1.
"""
from __future__ import annotations

import os
import re
from dataclasses import replace
from typing import Dict, List, Tuple

from eimer.models import DependencyEdge, DependencyGraph, DependencyPredicate, canonical_variable_pair
from eimer.workload import Selectivities, WindowSelectivity

_UNIT_MS = {"MILLISECOND": 1.0, "SECOND": 1000.0, "MINUTE": 60000.0, "HOUR": 3600000.0, "DAY": 86400000.0, "WEEK": 604800000.0}

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
    """entry point: augment unless the kill-switch is set. idempotent: a derived predicate re-parses to
    the bound it was derived from, so a second pass finds nothing tighter to add."""
    if not propagation_enabled():
        return dep_graph
    augmented, _ = augment_dependency_graph(dep_graph)
    return augmented


# Y.c BETWEEN X.c AND X.c + INTERVAL 'k' UNIT  -- the range form: 0 <= c_Y - c_X <= k; any timestamp column, the same
# on all three sides. Y.c - X.c <= INTERVAL 'k' UNIT  -- the subtraction form, same bound.
_BETWEEN_RE = re.compile(
    r"(?P<vy>\w+)\.(?P<col>\w+)\s+BETWEEN\s+(?P<vx>\w+)\.(?P=col)\s+AND\s+(?P=vx)\.(?P=col)\s*\+\s*INTERVAL\s*'(?P<bound>\d+(?:\.\d+)?)'\s*(?P<unit>\w+)",
    re.IGNORECASE,
)
_SUBTRACT_RE = re.compile(
    r"(?P<vy>\w+)\.(?P<col>\w+)\s*-\s*(?P<vx>\w+)\.(?P=col)\s*<=\s*INTERVAL\s*'(?P<bound>\d+(?:\.\d+)?)'\s*(?P<unit>\w+)",
    re.IGNORECASE,
)
_COLUMN_RES = (_BETWEEN_RE, _SUBTRACT_RE)

DERIVED_WINDOW_OPERATOR = "DERIVED_WINDOW"   # marks a predicate that exists only because of propagation


def window_column(dep_graph: DependencyGraph) -> str:
    """the timestamp column of the graph's explicit window bounds (first found); ,,ts'' when none names one."""
    for edge in dep_graph.edges:
        for predicate in edge.dependent_predicates:
            for rx in _COLUMN_RES:
                m = rx.search(predicate.text)
                if m:
                    return m.group("col")
    return "ts"


def is_derived_window_edge(edge: DependencyEdge) -> bool:
    """an edge that exists only because of propagation (every predicate derived)."""
    return bool(edge.dependent_predicates) and all(pr.operator == DERIVED_WINDOW_OPERATOR for pr in edge.dependent_predicates)


def strip_derived_window_edges(dep_graph: DependencyGraph) -> DependencyGraph:
    """the graph without its derived window edges (derived predicates appended to explicit edges stay)."""
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
    """directed bounds [(u, v, W_ms)] meaning ts_v - ts_u <= W_ms, extracted from one predicate text.

    ,,conservative'' widens a date_diff bound by one unit: engines count unit boundaries crossed
    (DuckDB, and Trino truncates), so ,,date_diff('millisecond', x, y) <= K'' admits differences up to
    just under K+1 ms; a consumer that must never exclude an admitted pair (the batch-window prefix
    prune) needs (K+1)·unit. The exact interval form ,,y <= x + INTERVAL'' stays as stated."""
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
        # vl.ts <= vr.ts + W  bounds  ts_vl - ts_vr <= W : arc vr -> vl
        bounds.append((m.group("vr"), m.group("vl"), w))
    for rx in _COLUMN_RES:   # range and subtraction forms: exact bounds, stated on the pair's own column
        for m in rx.finditer(text):
            unit = _UNIT_MS.get(m.group("unit").upper().rstrip("S"))
            if unit is None:
                continue
            bounds.append((m.group("vx"), m.group("vy"), float(m.group("bound")) * unit))
    return bounds


def implied_windows(dep_graph: DependencyGraph, *, conservative: bool = False) -> Dict[Tuple[str, str], float]:
    """tightest implied window (ms) for every position-ordered pair, via shortest paths.

    returns only pairs with a finite bound, keyed (x, y) with pos(x) < pos(y). queries without explicit
    windows return {}; pure orderings propagate nothing (they bound differences by 0 only backwards).
    ,,conservative'' passes through to parse_window_bounds_ms (one extra unit per date_diff bound).
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
    """a bound that re-parses to itself: integral milliseconds as an int, else at most six decimals."""
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
    """the tightest bound on ts_y - ts_x the edge's own predicates state (inf when they state none)."""
    best = float("inf")
    for predicate in edge.dependent_predicates:
        for u, v, w in parse_window_bounds_ms(predicate.text):
            if u == x and v == y and w < best:
                best = w
    return best


# a bound must undercut the stated one by more than the rendering precision to count as tighter
_TIGHTER_BY_MS = 1e-6


def augment_dependency_graph(dep_graph: DependencyGraph) -> Tuple[DependencyGraph, Dict[Tuple[str, str], float]]:
    """attach the implied window to every pair it reaches.

    a pair without a dependent edge gets a derived edge carrying the window; a pair whose edge states no
    window, or only a looser one, gets the window appended after the edge's own predicates (which stay
    untouched). returns (augmented graph, {canonical pair key: W_ms}) over every pair that received a
    window; the map drives sigma injection for pairs a measured payload lacks. the input graph is never
    mutated; with nothing to add (no explicit windows, or a second pass) it is returned as is.
    """
    col = window_column(dep_graph)   # derived predicates on the explicit bounds' timestamp column
    implied = implied_windows(dep_graph)
    pos = dep_graph.positions
    edges: List[DependencyEdge] = list(dep_graph.edges)
    index_of = {frozenset((e.var_left, e.var_right)): i for i, e in enumerate(edges)}
    # a Kleene variable gets no derived window: a dependent condition on its view would route the view's update
    # through the join path, which does not exist for run views. the explicit window keeps bounding the pattern;
    # only the interior bound on the run variable is not materialized.
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
    """add WindowSelectivity entries for derived pairs missing from the measured payload.

    WindowSelectivity.w must be in the workload's rho time unit (the benchmark convention: rho = events
    per second), so w = W_ms / rho_time_unit_ms. existing keys are never overwritten.
    """
    dependent = dict(selectivities.dependent)
    for key, w_ms in derived_map.items():
        if key not in dependent:
            dependent[key] = (WindowSelectivity(w=w_ms / rho_time_unit_ms),)
    return replace(selectivities, dependent=dependent)
