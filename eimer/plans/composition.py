"""composition-plan construction: the effective view set, cover enumeration, and the
compose join graph and its spanning trees over a cover."""

from __future__ import annotations

import re
from itertools import combinations
from typing import Dict, FrozenSet, Iterable, List, Set, Tuple

from eimer.models import (
    CompositionCover,
    CompositionJoinEdge,
    CompositionJoinGraph,
    CompositionPlan,
    ConditionType,
    DeferredCondition,
    DependencyGraph,
    JoinType,
    Strategy,
    View,
    ViewEdge,
    canonical_edge_key,
    make_view,
    strategy_sort_key,
    view_sort_key,
)
from eimer.plans.strategy_space import strategy_coverage


# matches a qualified equality like ,,a.x = b.y'' (table.column on both sides of ,,='')
_QUALIFIED_EQ_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*\s*\.\s*[A-Za-z_][A-Za-z0-9_]*\s*=\s*"
    r"[A-Za-z_][A-Za-z0-9_]*\s*\.\s*[A-Za-z_][A-Za-z0-9_]*\b"
)


def classify_condition_type(expression: str) -> ConditionType:
    """classify a predicate expression as equality, temporal-window, range/spatial, or other."""
    upper = expression.upper()

    if _QUALIFIED_EQ_RE.search(expression) and " BETWEEN " not in upper:
        return ConditionType.EQUALITY

    if "INTERVAL" in upper:
        return ConditionType.TEMPORAL_WINDOW

    if re.search(r"\b(TIME|TS)\b", upper) and re.search(r"[<>]=?|BETWEEN", upper):
        return ConditionType.TEMPORAL_WINDOW

    if " BETWEEN " in upper:
        return ConditionType.RANGE_SPATIAL

    if any(token in upper for token in ("LAT", "LON", "DISTANCE", "RADIUS")):
        return ConditionType.RANGE_SPATIAL

    return ConditionType.OTHER


def build_effective_view_set(strategy: Strategy, variables: List[str]) -> Set[View]:
    effective: Set[View] = set(strategy)
    covered = strategy_coverage(strategy)
    for var in variables:
        if var not in covered:
            effective.add(make_view([var]))
    return effective


def _covered_variables(views: Iterable[View]) -> Set[str]:
    covered: Set[str] = set()
    for view in views:
        covered.update(view)
    return covered


def _composition_cover_for_materialized_subset(
    materialized_subset: Iterable[View],
    variables: List[str],
    positions: Dict[str, int],
) -> CompositionCover:
    """build a query-composition cover from selected maintained view leaves."""

    materialized = tuple(sorted(set(materialized_subset), key=lambda view: view_sort_key(view, positions)))
    covered = _covered_variables(materialized)
    singleton_views = tuple(
        make_view([var])
        for var in variables
        if var not in covered
    )
    effective = frozenset(set(materialized).union(singleton_views))
    if _covered_variables(effective) != set(variables):
        raise ValueError("composition cover must cover all query variables")
    return CompositionCover(
        materialized_views=materialized,
        singleton_views=singleton_views,
        effective_view_set=effective,
    )


def _cover_sort_key(cover: CompositionCover, positions: Dict[str, int]) -> Tuple:
    return (
        len(cover.materialized_views),
        tuple(view_sort_key(view, positions) for view in cover.materialized_views),
        tuple(view_sort_key(view, positions) for view in cover.singleton_views),
    )


def enumerate_composition_covers(strategy: Strategy, dep_graph: DependencyGraph) -> List[CompositionCover]:
    """enumerate query-composition leaf covers for a fixed materialization strategy.

    the first cover keeps all maintained strategy views plus singleton fillers for
    uncovered variables; the rest allow maintained views to be omitted from composition
    while still being maintained as update helpers.
    """

    ordered_strategy = tuple(sorted(strategy, key=lambda view: view_sort_key(view, dep_graph.positions)))
    legacy_cover = _composition_cover_for_materialized_subset(
        ordered_strategy,
        dep_graph.variables,
        dep_graph.positions,
    )

    by_effective: Dict[FrozenSet[View], CompositionCover] = {
        legacy_cover.effective_view_set: legacy_cover,
    }
    covers: List[CompositionCover] = [legacy_cover]

    for size in range(0, len(ordered_strategy) + 1):
        for combo in combinations(ordered_strategy, size):
            cover = _composition_cover_for_materialized_subset(
                combo,
                dep_graph.variables,
                dep_graph.positions,
            )
            if cover.effective_view_set in by_effective:
                continue
            by_effective[cover.effective_view_set] = cover
            covers.append(cover)

    tail = sorted(covers[1:], key=lambda cover: _cover_sort_key(cover, dep_graph.positions))
    return [legacy_cover] + tail


def canonical_composition_cover(strategy: Strategy, dep_graph: DependencyGraph) -> CompositionCover:
    """the canonical composition cover, ,,enumerate_composition_covers(strategy, dep_graph)[0]'' (the
    ,,maintained views + singleton fillers'' cover), built directly without the 2^|strategy|
    ,,combinations'' fan-out of the full enumeration."""
    ordered_strategy = tuple(sorted(strategy, key=lambda view: view_sort_key(view, dep_graph.positions)))
    return _composition_cover_for_materialized_subset(
        ordered_strategy,
        dep_graph.variables,
        dep_graph.positions,
    )


def _span(view: View, positions: Dict[str, int]) -> Tuple[int, int]:
    ordered = sorted(positions[var] for var in view)
    return (ordered[0], ordered[-1])


def _frames(outer: View, inner: View, positions: Dict[str, int]) -> bool:
    outer_span = _span(outer, positions)
    inner_span = _span(inner, positions)
    return outer_span[0] < inner_span[0] and inner_span[1] < outer_span[1]


def determine_join_type(left: View, right: View, positions: Dict[str, int]) -> JoinType:
    if left.intersection(right):
        return JoinType.EQUI

    if _frames(left, right, positions) or _frames(right, left, positions):
        return JoinType.BAND

    return JoinType.INEQ


def _deferred_conditions_for_edge(dep_graph: DependencyGraph, left: View, right: View) -> List[DeferredCondition]:
    deferred: List[DeferredCondition] = []

    for edge in dep_graph.edges:
        left_in_left = edge.var_left in left and edge.var_right in right
        right_in_left = edge.var_right in left and edge.var_left in right
        if not (left_in_left or right_in_left):
            continue

        for condition in edge.dependent_conditions:
            deferred.append(
                DeferredCondition(
                    var_left=edge.var_left,
                    var_right=edge.var_right,
                    expression=condition,
                    condition_type=classify_condition_type(condition),
                )
            )

    deferred.sort(key=lambda item: (item.var_left, item.var_right, item.expression))
    return deferred


def build_composition_join_graph_for_nodes(dep_graph: DependencyGraph, nodes: Set[View]) -> CompositionJoinGraph:
    sorted_views = sorted(nodes, key=lambda view: view_sort_key(view, dep_graph.positions))

    edges: Dict[ViewEdge, CompositionJoinEdge] = {}
    for left, right in combinations(sorted_views, 2):
        key = canonical_edge_key(left, right, dep_graph.positions)
        join_type = determine_join_type(left, right, dep_graph.positions)
        deferred = tuple(_deferred_conditions_for_edge(dep_graph, left, right))
        edges[key] = CompositionJoinEdge(
            left=key[0],
            right=key[1],
            join_type=join_type,
            deferred_conditions=deferred,
        )

    return CompositionJoinGraph(nodes=set(nodes), edges=edges)


def build_composition_join_graph(dep_graph: DependencyGraph, strategy: Strategy) -> CompositionJoinGraph:
    effective_views = build_effective_view_set(strategy, dep_graph.variables)
    return build_composition_join_graph_for_nodes(dep_graph, effective_views)


def _edge_sort_key(edge: ViewEdge, positions: Dict[str, int]) -> Tuple[Tuple[int, ...], Tuple[int, ...]]:
    left_key = view_sort_key(edge[0], positions)
    right_key = view_sort_key(edge[1], positions)
    return (left_key, right_key)


def _tree_is_connected(nodes: Set[View], edges: List[ViewEdge]) -> bool:
    if not nodes:
        return False
    if len(nodes) == 1:
        return len(edges) == 0

    adjacency: Dict[View, Set[View]] = {node: set() for node in nodes}
    for left, right in edges:
        adjacency[left].add(right)
        adjacency[right].add(left)

    start = next(iter(nodes))
    stack = [start]
    seen = {start}
    while stack:
        current = stack.pop()
        for neighbor in adjacency[current]:
            if neighbor in seen:
                continue
            seen.add(neighbor)
            stack.append(neighbor)

    return len(seen) == len(nodes)


def enumerate_spanning_trees(join_graph: CompositionJoinGraph, positions: Dict[str, int]) -> List[List[ViewEdge]]:
    nodes = join_graph.nodes
    if len(nodes) == 1:
        return [[]]

    all_edges = sorted(join_graph.edges.keys(), key=lambda edge: _edge_sort_key(edge, positions))
    tree_size = len(nodes) - 1

    trees: List[List[ViewEdge]] = []
    for combo in combinations(all_edges, tree_size):
        combo_list = list(combo)
        if _tree_is_connected(nodes, combo_list):
            trees.append(combo_list)

    trees.sort(key=lambda edges: tuple(_edge_sort_key(edge, positions) for edge in edges))
    return trees


def _join_type_precedence(join_type: JoinType) -> int:
    if join_type==JoinType.EQUI:
        return 0
    if join_type == JoinType.BAND:
        return 1
    return 2


def _canonical_join_order(
    tree_edges: List[ViewEdge],
    join_graph: CompositionJoinGraph,
    positions: Dict[str, int],
) -> List[ViewEdge]:
    if not tree_edges:
        return []

    adjacency: Dict[View, Set[View]] = {}
    for left, right in tree_edges:
        adjacency.setdefault(left, set()).add(right)
        adjacency.setdefault(right, set()).add(left)

    root = min(adjacency.keys(), key=lambda view: view_sort_key(view, positions))
    visited: Set[View] = {root}
    order: List[ViewEdge] = []

    while len(visited) < len(adjacency):
        candidates: List[Tuple[int, Tuple[int, ...], Tuple[int, ...], ViewEdge, View]] = []
        for current in sorted(visited, key=lambda view: view_sort_key(view, positions)):
            for neighbor in sorted(adjacency[current], key=lambda view: view_sort_key(view, positions)):
                if neighbor in visited:
                    continue
                edge_key = canonical_edge_key(current, neighbor, positions)
                join_type = join_graph.edges[edge_key].join_type
                candidates.append(
                    (
                        _join_type_precedence(join_type),
                        view_sort_key(current, positions),
                        view_sort_key(neighbor, positions),
                        edge_key,
                        neighbor,
                    )
                )

        if not candidates:
            raise ValueError("Tree traversal failed: no candidate edge found")

        candidates.sort()
        chosen = candidates[0]
        order.append(chosen[3])
        visited.add(chosen[4])

    return order


def enumerate_composition_plans(join_graph: CompositionJoinGraph, positions: Dict[str, int]) -> List[CompositionPlan]:
    trees = enumerate_spanning_trees(join_graph, positions)
    plans: List[CompositionPlan] = []

    for tree_edges in trees:
        sorted_edges = sorted(tree_edges, key=lambda edge: _edge_sort_key(edge, positions))
        join_types = {edge: join_graph.edges[edge].join_type for edge in sorted_edges}
        deferred = {
            edge: list(join_graph.edges[edge].deferred_conditions)
            for edge in sorted_edges
        }
        join_order = _canonical_join_order(sorted_edges, join_graph, positions)
        plans.append(
            CompositionPlan(
                edges=sorted_edges,
                join_types=join_types,
                join_order=join_order,
                deferred_conditions=deferred,
            )
        )

    plans.sort(
        key=lambda plan: (
            tuple(_edge_sort_key(edge, positions) for edge in plan.edges),
            tuple(_edge_sort_key(edge, positions) for edge in plan.join_order),
        )
    )
    return plans


def _kruskal_lex_first_spanning_tree(join_graph: CompositionJoinGraph, positions: Dict[str, int]) -> List[ViewEdge]:
    """the lexicographically-minimal spanning tree by ,,_edge_sort_key'', built directly via
    Kruskal/union-find without the ,,enumerate_spanning_trees'' fan-out.

    over the graphic matroid of the join graph the greedy basis is the lexicographically-minimal
    basis, so the Kruskal tree matches the tree underlying the first plan of
    ,,enumerate_composition_plans''.
    """
    nodes = join_graph.nodes
    if len(nodes) <= 1:
        return []

    parent: Dict[View, View] = {node: node for node in nodes}

    def find(node: View) -> View:
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    tree_edges: List[ViewEdge] = []
    needed = len(nodes) - 1
    for edge in sorted(join_graph.edges.keys(), key=lambda e: _edge_sort_key(e, positions)):
        left_root, right_root = find(edge[0]), find(edge[1])
        if left_root == right_root:
            continue
        parent[left_root] = right_root
        tree_edges.append(edge)
        if len(tree_edges) == needed:
            break
    return tree_edges


def construct_canonical_composition_plan(
    join_graph: CompositionJoinGraph,
    positions: Dict[str, int],
) -> CompositionPlan:
    """directly construct the canonical (lexicographically-first) composition plan
    ,,enumerate_composition_plans(join_graph, positions)[0]'' without enumerating every spanning tree.

    the tree comes from ,,_kruskal_lex_first_spanning_tree'' instead of enumerate-then-sort, so a
    selector can cost the canonical plan for a strategy without paying the spanning-tree fan-out.
    """
    tree_edges = _kruskal_lex_first_spanning_tree(join_graph, positions)
    sorted_edges = sorted(tree_edges, key=lambda edge: _edge_sort_key(edge, positions))
    join_types = {edge: join_graph.edges[edge].join_type for edge in sorted_edges}
    deferred = {
        edge: list(join_graph.edges[edge].deferred_conditions)
        for edge in sorted_edges
    }
    join_order = _canonical_join_order(sorted_edges, join_graph, positions)
    return CompositionPlan(
        edges=sorted_edges,
        join_types=join_types,
        join_order=join_order,
        deferred_conditions=deferred,
    )


def _valid_left_deep_order(nodes: Set[View], edges: Tuple[ViewEdge, ...], order: Tuple[ViewEdge, ...]) -> bool:
    if len(nodes) == 1:
        return order == ()
    if set(order) != set(edges) or len(order) != len(edges):
        return False
    if not order:
        return False

    first = order[0]
    if first[0] not in nodes or first[1] not in nodes:
        return False
    visited = {first[0], first[1]}
    for edge in order[1:]:
        left_seen = edge[0] in visited
        right_seen = edge[1] in visited
        if left_seen == right_seen:
            return False
        visited.update(edge)
    return visited == nodes


def enumerate_left_deep_join_orders_for_tree(
    nodes: Iterable[View],
    edges: Iterable[ViewEdge],
    positions: Dict[str, int],
    *,
    canonical_order: Iterable[ViewEdge] | None = None,
    max_orders: int | None = None,
) -> Tuple[Tuple[ViewEdge, ...], ...]:
    """enumerate deterministic left-deep orders for a fixed composition tree."""

    if max_orders is not None and max_orders <= 0:
        raise ValueError("max_orders must be positive when provided")

    node_set = set(nodes)
    edge_tuple = tuple(sorted(set(edges), key=lambda edge: _edge_sort_key(edge, positions)))
    if len(node_set) == 1:
        return ((),)
    if len(edge_tuple) != max(0, len(node_set) - 1):
        raise ValueError("left-deep join orders require a tree with |nodes|-1 edges")
    if not _tree_is_connected(node_set, list(edge_tuple)):
        raise ValueError("left-deep join orders require a connected tree")

    adjacency: Dict[View, Set[ViewEdge]] = {node: set() for node in node_set}
    for edge in edge_tuple:
        left, right = edge
        if left not in node_set or right not in node_set:
            raise ValueError("tree edge references a node outside the node set")
        adjacency[left].add(edge)
        adjacency[right].add(edge)

    results: list[Tuple[ViewEdge, ...]] = []
    seen: Set[Tuple[ViewEdge, ...]] = set()

    def add_order(order: Tuple[ViewEdge, ...]) -> None:
        if order in seen:
            return
        if not _valid_left_deep_order(node_set, edge_tuple, order):
            return
        seen.add(order)
        results.append(order)

    if canonical_order is not None:
        canonical_tuple = tuple(canonical_order)
        add_order(canonical_tuple)

    def extend(visited: Set[View], remaining: Set[ViewEdge], prefix: Tuple[ViewEdge, ...]) -> None:
        if not remaining:
            add_order(prefix)
            return

        candidates = []
        for edge in remaining:
            left_seen = edge[0] in visited
            right_seen = edge[1] in visited
            if left_seen == right_seen:
                continue
            new_node = edge[1] if left_seen else edge[0]
            candidates.append((_edge_sort_key(edge, positions), view_sort_key(new_node, positions), edge, new_node))
        candidates.sort()

        for _, _, edge, new_node in candidates:
            next_visited = set(visited)
            next_visited.add(new_node)
            next_remaining = set(remaining)
            next_remaining.remove(edge)
            extend(next_visited, next_remaining, prefix + (edge,))

    for first_edge in edge_tuple:
        extend(set(first_edge), set(edge_tuple) - {first_edge}, (first_edge,))

    canonical_first = results[:1] if canonical_order is not None else []
    tail = results[1:] if canonical_first else results
    tail = sorted(tail, key=lambda order: tuple(_edge_sort_key(edge, positions) for edge in order))
    ordered = tuple(canonical_first + tail)
    if max_orders is not None:
        return ordered[:max_orders]
    return ordered
