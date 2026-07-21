"""join-order ranking for compose plans: a connected left-deep DP and a subset-DP bushy tree.

ranks orders with a lightweight mirror of the estimator's per-step arithmetic; the chosen order is
always re-scored by the full estimator, so a ranking miss costs optimality, never correctness.
deterministic tie-breaks keep plan ids stable. also holds the row-based storage-budget helpers.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import factorial
from typing import Dict, FrozenSet, List, Mapping, Tuple

from eimer.models import CompositionPlan, View, make_view, view_sort_key
from eimer.plans.composition import CompositionJoinGraph
from eimer.workload import ConstantSelectivity, WindowSelectivity, Workload

# equi-join rows cost ~ij_rate/fp_rate of fp rows; selection-inert, affecting only absolute
# compose scores. the clique-grow selector ranks with ij_over_fp=1.0.
_IJ_OVER_FP = 1.06


def _sigma_value(components, workload: Workload, batch_index: int) -> float:
    """product selectivity of a component list (constant factors and window factors combined)."""
    out = 1.0
    for comp in components:
        if isinstance(comp, ConstantSelectivity):
            out *= comp.value
        elif isinstance(comp, WindowSelectivity):
            base = workload.n_at(batch_index)
            out *= max(0.0, min(1.0, (2.0 * comp.w * workload.rho) / base)) if base > 0 else 1.0
        else:  # pragma: no cover - new component kinds must be added deliberately
            raise TypeError(f"unknown selectivity component {type(comp)!r}")
    return out


def _edge_sigma(workload: Workload, batch_index: int, a: str, b: str) -> float:
    """selectivity of the value edge between variables a and b; 1.0 if the pair carries no edge."""
    positions = workload.dependency_graph.positions
    key = (a, b) if positions[a] < positions[b] else (b, a)
    components = workload.selectivities.dependent.get(key)
    if components is None:
        return 1.0
    return _sigma_value(components, workload, batch_index)


def analytic_node_size(view: View, workload: Workload, batch_index: int) -> float:
    """analytic view cardinality: product of sigma-filtered singletons x internal edge sigmas x the
    1/k! pattern-ordering factor."""
    h = float(workload.n_at(batch_index))
    size = 1.0
    variables = sorted(view, key=lambda v: workload.dependency_graph.positions[v])
    for var in variables:
        size *= h * workload.selectivities.independent.get(var, 1.0)
    for i, a in enumerate(variables):
        for b in variables[i + 1:]:
            size *= _edge_sigma(workload, batch_index, a, b)
    size /= factorial(len(variables))
    return max(size, 0.0)


# --- storage budget M (rows) -------------------------------------------------------------------------------
# resident state for the budget is exactly the maintained-view set. analytic_node_size is the un-pruned
# per-view cardinality, so it upper-bounds the true resident size at every batch k. constant-selectivity
# edges are monotone in k (worst case at k=K), but windowed edges can be non-monotone, so we take the
# explicit max over k in 0..K: a correct upper bound regardless of monotonicity.
def resident_rows(views, workload: Workload, batch_index: int) -> float:
    """Summed un-pruned analytic resident rows of a maintained-view set at one batch index.
    ,,views'' is any iterable of View / frozenset[str] (make_view is idempotent on a frozenset)."""
    return sum(analytic_node_size(make_view(v), workload, batch_index) for v in views)


def max_resident_rows(views, workload: Workload) -> float:
    """worst-case resident rows over the horizon: max over k in 0..K of the summed maintained-view rows."""
    materialized = tuple(views)
    return max((resident_rows(materialized, workload, k) for k in range(workload.k + 1)), default=0.0)


def within_storage_budget(views, workload: Workload, max_storage_rows) -> bool:
    """true iff the maintained views fit the row budget. none budget = infinity = always feasible."""
    if max_storage_rows is None:
        return True
    return max_resident_rows(views, workload) <= float(max_storage_rows)


@dataclass(frozen=True)
class OrderedJoin:
    """One DP transition: join ,,node'' to the current state via ,,edge'' (a join-graph edge key)."""
    node: View
    edge: Tuple[View, View]


def _step(
    state_vars: FrozenSet[str],
    state_card: float,
    node: View,
    node_card: float,
    workload: Workload,
    batch_index: int,
) -> Tuple[float, float]:
    """(stream_cost, output_card) of joining ,,node'' onto a state. cross/band steps stream
    state_card x node_card; shared-variable steps are equi joins charged max(in, out) x ij/fp. output
    applies the new cross edges' sigmas, the interleave ordering factor, and the shared-key collapse."""
    shared = state_vars & set(node)
    new_vars = set(node) - state_vars
    output = state_card * node_card
    for a in shared:
        # repeat-key collapse: the equi key pins the shared singleton copy
        h = float(workload.n_at(batch_index))
        singleton = max(h * workload.selectivities.independent.get(a, 1.0), 1.0)
        output /= singleton
    for a in sorted(state_vars):
        for b in sorted(new_vars):
            output *= _edge_sigma(workload, batch_index, a, b)
    s, w = len(state_vars), len(new_vars)
    if w:
        output *= factorial(s) * factorial(w) / factorial(s + w)  # interleave ordering factor
    if shared:
        cost = max(state_card, output) * _IJ_OVER_FP
    else:
        cost = state_card * node_card
    return cost, max(output, 0.0)


def _reach_edge_key(edge_key: Tuple[View, View], positions: Dict[str, int]):
    """deterministic preference among the edges that reach the same neighbor from a state: a
    shared-variable (non-cross) edge beats a cross edge, then canonical view order. keeps the DP's edge
    attribution hashseed-independent without changing any join cost (the edge never enters ,,_step'')."""
    left, right = edge_key
    shares = bool(set(left) & set(right))
    return (0 if shares else 1, view_sort_key(left, positions), view_sort_key(right, positions))


def best_connected_left_deep_order(
    join_graph: CompositionJoinGraph,
    workload: Workload,
    batch_index: int,
) -> List[OrderedJoin]:
    """Exact DP over connected left-deep orders; returns the join sequence (empty for single-node)."""
    positions = workload.dependency_graph.positions
    nodes: List[View] = sorted(join_graph.nodes, key=lambda v: view_sort_key(v, positions))
    if len(nodes)<=1:
        return []
    adjacency: Dict[View, List[Tuple[View, Tuple[View, View]]]] = {n: [] for n in nodes}
    for edge_key in join_graph.edges:
        left, right = edge_key
        adjacency[left].append((right, edge_key))
        adjacency[right].append((left, edge_key))
    sizes = {n: analytic_node_size(n, workload, batch_index) for n in nodes}

    # state -> (cost, card, vars, path); seeded with every node as root
    best: Dict[FrozenSet[View], Tuple[float, float, FrozenSet[str], Tuple[OrderedJoin, ...]]] = {}
    for n in nodes:
        best[frozenset({n})] = (0.0, sizes[n], frozenset(n), ())
    frontier = list(best.keys())
    for _ in range(len(nodes) - 1):
        nxt: Dict[FrozenSet[View], Tuple[float, float, FrozenSet[str], Tuple[OrderedJoin, ...]]] = {}
        for state in frontier:
            cost, card, svars, path = best[state]
            reachable: Dict[View, Tuple[View, View]] = {}
            for member in state:
                for neighbor, edge_key in adjacency[member]:
                    if neighbor in state:
                        continue
                    prev = reachable.get(neighbor)
                    if prev is None or _reach_edge_key(edge_key, positions) < _reach_edge_key(prev, positions):
                        reachable[neighbor] = edge_key  # deterministic shared-edge tie-break
            for neighbor in sorted(reachable, key=lambda v: view_sort_key(v, positions)):
                step_cost, out_card = _step(svars, card, neighbor, sizes[neighbor], workload, batch_index)
                key = state | {neighbor}
                cand = (cost + step_cost, out_card, svars | set(neighbor),
                        path + (OrderedJoin(node=neighbor, edge=reachable[neighbor]),))
                prev = nxt.get(key)
                if prev is None or cand[0] < prev[0] - 1e-12:
                    nxt[key] = cand
        if not nxt:
            raise ValueError("composition join graph is disconnected; no connected left-deep order exists")
        best = nxt
        frontier = list(best.keys())
    full_state = frozenset(nodes)
    if full_state not in best:
        raise ValueError("composition join graph is disconnected; no connected left-deep order exists")
    _, _, _, path = best[full_state]
    return list(path)


def construct_dp_composition_plan(
    join_graph: CompositionJoinGraph,
    workload: Workload,
    batch_index: int,
) -> CompositionPlan:
    """build a CompositionPlan whose join_order follows the DP-chosen connected left-deep sequence
    (edges sorted, types and deferred conditions taken from the join graph)."""
    positions = workload.dependency_graph.positions
    path = best_connected_left_deep_order(join_graph, workload, batch_index)
    tree_edges = [oj.edge for oj in path]
    sorted_edges = sorted(set(tree_edges), key=lambda e: (view_sort_key(e[0], positions),
                                                          view_sort_key(e[1], positions)))
    join_types = {edge: join_graph.edges[edge].join_type for edge in sorted_edges}
    deferred = {edge: list(join_graph.edges[edge].deferred_conditions) for edge in sorted_edges}
    return CompositionPlan(
        edges=sorted_edges,
        join_types=join_types,
        join_order=list(tree_edges),
        deferred_conditions=deferred,
    )


@dataclass(frozen=True)
class BushyTree:
    """A compose join tree: either a leaf (node is a View) or an inner node joining two subtrees."""
    node: View | None = None
    left: "BushyTree | None" = None
    right: "BushyTree | None" = None

    @property
    def views(self) -> FrozenSet[View]:
        if self.node is not None:
            return frozenset({self.node})
        return self.left.views | self.right.views

    @property
    def variables(self) -> FrozenSet[str]:
        if self.node is not None:
            return frozenset(self.node)
        return self.left.variables | self.right.variables

    def label(self, positions) -> str:
        if self.node is not None:
            return "_".join(sorted(self.node, key=lambda v: positions[v]))
        return f"({self.left.label(positions)} x {self.right.label(positions)})"


def _merge_view(views: FrozenSet[View]) -> View:
    out = set()
    for v in views:
        out |= set(v)
    return frozenset(out)


def _subset_card(subset: FrozenSet[View], workload: Workload, batch_index: int) -> float:
    """selinger per-subset cardinality: ,,analytic_node_size'' over the merged variable set of S's views.
    with no order-exploiting operator the joined cardinality is order-independent, so it is computed once
    per subset, which is what Bellman's state-sufficiency needs for overlapping covers."""
    return analytic_node_size(_merge_view(subset), workload, batch_index)


def _split_cost(
    card_left: float,
    vars_left: FrozenSet[str],
    card_right: float,
    vars_right: FrozenSet[str],
    output: float,
    ij_over_fp: float = _IJ_OVER_FP,
) -> float:
    """orientation-aware split cost of joining the two halves {L, R}, min over both operand orientations.
    shared variable: equi join charged ,,max(streaming-side card, output) * _IJ_OVER_FP'', streaming the
    smaller operand. disjoint: cross/band charged ,,card_L * card_R'', symmetric across orientations."""
    if vars_left & vars_right:
        return min(max(card_left, output), max(card_right, output)) * ij_over_fp
    return card_left * card_right


def best_bushy_tree(
    join_graph: CompositionJoinGraph,
    workload: Workload,
    batch_index: int,
    *,
    ij_over_fp: float = _IJ_OVER_FP,
) -> Tuple[BushyTree, float]:
    """exact subset-DP over compose join trees (DPsub with cross products). unlike the left-deep DP,
    splits are not restricted to edge-connected extensions, so a cross join of two tiny collapsed
    subresults (the two-island win) is available and the per-step cost polices expensive crosses.
    O(3^n) subsets x splits; compose node counts are <= ~7. per-subset cardinality via ,,_subset_card''
    (plan-independent), each split's cost via ,,_split_cost''; subsets ascend by size, tie-break is
    (cost, canonical partition key)."""
    from itertools import combinations
    positions = workload.dependency_graph.positions
    nodes: List[View] = sorted(join_graph.nodes, key=lambda v: view_sort_key(v, positions))
    if len(nodes)==1:
        return BushyTree(node=nodes[0]), 0.0

    def part_key(left: FrozenSet[View]) -> Tuple[Tuple[int, ...], ...]:
        return tuple(sorted(view_sort_key(v, positions) for v in left))

    # best[S] = (cost, tree); card(S) comes from _subset_card, not from chaining steps.
    best: Dict[FrozenSet[View], Tuple[float, BushyTree]] = {
        frozenset({n}): (0.0, BushyTree(node=n)) for n in nodes
    }
    card: Dict[FrozenSet[View], float] = {
        frozenset({n}): _subset_card(frozenset({n}), workload, batch_index) for n in nodes
    }
    for size in range(2, len(nodes) + 1):
        for combo in combinations(nodes, size):
            subset = frozenset(combo)
            members = sorted(subset, key=lambda v: view_sort_key(v, positions))
            out = _subset_card(subset, workload, batch_index)
            card[subset] = out
            best_cost: float | None = None
            best_tree: BushyTree | None = None
            best_pkey: Tuple[Tuple[int, ...], ...] | None = None
            # iterate each unordered partition {L, R} once: L always contains members[0].
            anchor = members[0]
            for l_size in range(1, size):
                for left_combo in combinations(members[1:], l_size - 1):
                    left = frozenset((anchor,) + left_combo)
                    right = subset - left
                    lc, ltree = best[left]
                    rc, rtree = best[right]
                    step_cost = _split_cost(
                        card[left], _merge_view(left), card[right], _merge_view(right), out,
                        ij_over_fp,
                    )
                    total = lc + rc + step_cost
                    pkey = part_key(left)
                    better = (
                        best_cost is None
                        or total < best_cost - 1e-12
                        or (abs(total - best_cost) <= 1e-12 and pkey < best_pkey)
                    )
                    if better:
                        best_cost = total
                        best_tree = BushyTree(left=ltree, right=rtree)
                        best_pkey = pkey
            best[subset] = (best_cost, best_tree)
    all_nodes = frozenset(nodes)
    cost, tree = best[all_nodes]
    return tree, cost


def enumerate_all_trees(nodes: List[View]):
    """brute-force reference: yield every binary tree shape over the leaf views. orientation is not
    enumerated because the split cost already mins over both operand orientations, so shapes alone are
    exhaustive over costs. checks ,,best_bushy_tree'' on small instances (n <= 6)."""
    if len(nodes) == 1:
        yield BushyTree(node=nodes[0])
        return
    from itertools import combinations
    anchor = nodes[0]
    rest = nodes[1:]
    # every unordered bipartition of the leaves with ,,anchor'' pinned to the left half
    for l_extra in range(0, len(rest)):
        for left_combo in combinations(rest, l_extra):
            left_set = set(left_combo) | {anchor}
            left_nodes = [n for n in nodes if n in left_set]
            right_nodes = [n for n in nodes if n not in left_set]
            for ltree in enumerate_all_trees(left_nodes):
                for rtree in enumerate_all_trees(right_nodes):
                    yield BushyTree(left=ltree, right=rtree)


def tree_rank_cost(tree: BushyTree, workload: Workload, batch_index: int) -> float:
    """brute-force reference: cost one tree with the DP semantics (,,_subset_card'' per merged
    variable-set, ,,_split_cost'' per internal node) without calling ,,best_bushy_tree''. the recursion
    mirrors the DP, so the two agree by construction."""
    if tree.node is not None:
        return 0.0
    left_cost = tree_rank_cost(tree.left, workload, batch_index)
    right_cost = tree_rank_cost(tree.right, workload, batch_index)
    card_left = _subset_card(tree.left.views, workload, batch_index)
    card_right = _subset_card(tree.right.views, workload, batch_index)
    out = _subset_card(tree.views, workload, batch_index)
    step = _split_cost(card_left, tree.left.variables, card_right, tree.right.variables, out)
    return left_cost + right_cost + step


def to_join_tree_node(tree: BushyTree):
    """convert the DP's BushyTree into the production models.JoinTreeNode carried on
    CompositionPlan.join_tree (same leaf/inner shape)."""
    from eimer.models import JoinTreeNode
    if tree.node is not None:
        return JoinTreeNode(node=tree.node)
    return JoinTreeNode(left=to_join_tree_node(tree.left), right=to_join_tree_node(tree.right))
