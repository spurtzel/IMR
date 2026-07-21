"""valid-subpattern enumeration: the contiguous, gap-bridge-legal variable subsets that
can back a materialized view."""

from __future__ import annotations

from itertools import combinations
from typing import Dict, Iterable, List, Sequence, Set

from eimer.models import (
    DependencyGraph,
    Strategy,
    StrategyMode,
    View,
    make_view,
    strategy_sort_key,
    view_name,
    view_sort_key,
)


def is_contiguous(view: View, positions: Dict[str, int]) -> bool:
    """true when the view's variable positions form a gap-free run."""
    ordered_positions = sorted(positions[var] for var in view)
    if not ordered_positions:
        return False
    start = ordered_positions[0]
    end = ordered_positions[-1]
    return len(ordered_positions) == (end - start + 1)


def view_contains_dependency_edge(view: View, dep_graph: DependencyGraph) -> bool:
    """true when both endpoints of some dependency edge lie inside the view."""
    return any(edge.var_left in view and edge.var_right in view for edge in dep_graph.edges)


def _has_gap_bridge(view: View, left_pos: int, right_pos: int, dep_graph: DependencyGraph) -> bool:
    """true when some in-view edge spans the [left_pos, right_pos] position gap."""
    for edge in dep_graph.edges:
        if edge.var_left not in view or edge.var_right not in view:
            continue
        edge_left, edge_right = edge.span
        if edge_left <= left_pos and edge_right >= right_pos:
            return True
    return False


def is_gap_bridge_valid(view: View, dep_graph: DependencyGraph) -> bool:
    """true when every position gap in a non-contiguous view is bridged by an edge."""
    positions = dep_graph.positions
    if len(view) <= 1:
        return True
    if is_contiguous(view, positions):
        return True

    ordered_positions = sorted(positions[var] for var in view)
    for idx in range(len(ordered_positions) - 1):
        left_pos = ordered_positions[idx]
        right_pos = ordered_positions[idx + 1]
        if right_pos == left_pos + 1:
            continue
        if not _has_gap_bridge(view, left_pos, right_pos, dep_graph):
            return False
    return True


def enumerate_valid_subpatterns(dep_graph: DependencyGraph, mode: StrategyMode = StrategyMode.DEPENDENCY_RESTRICTED) -> List[View]:
    """valid views for the given mode: all subsets, or only contiguous/gap-bridged ones."""
    variables = dep_graph.variables
    valid: List[View] = []

    for size in range(1, len(variables) + 1):
        for combo in combinations(variables, size):
            view = make_view(combo)
            if mode == StrategyMode.FULL_SUBSETS:
                valid.append(view)
                continue
            if is_contiguous(view, dep_graph.positions) or is_gap_bridge_valid(view, dep_graph):
                valid.append(view)

    valid.sort(key=lambda item: view_sort_key(item, dep_graph.positions))
    return valid


def enumerate_strategies(dep_graph: DependencyGraph, mode: StrategyMode = StrategyMode.DEPENDENCY_RESTRICTED) -> List[Strategy]:
    """every non-empty subset of the valid subpatterns, as a sorted list of strategies."""
    valid_subpatterns = enumerate_valid_subpatterns(dep_graph, mode)
    strategies: List[Strategy] = []
    for size in range(1, len(valid_subpatterns) + 1):
        for combo in combinations(valid_subpatterns, size):
            strategies.append(frozenset(combo))
    strategies.sort(key=lambda strategy: strategy_sort_key(strategy, dep_graph.positions))
    return strategies


def strategy_coverage(strategy: Iterable[View]) -> Set[str]:
    """the set of variables covered by a strategy's views."""
    covered: Set[str] = set()
    for view in strategy:
        covered.update(view)
    return covered


def strategy_has_non_contiguous_subpattern(strategy: Iterable[View], positions: Dict[str, int]) -> bool:
    """true when the strategy holds a multi-variable non-contiguous view."""
    for view in strategy:
        if len(view) <= 1:
            continue
        if not is_contiguous(view, positions):
            return True
    return False


def describe_view(view: View, dep_graph: DependencyGraph) -> dict[str, object]:
    """a small dict summarizing a view's name, variables, and validity flags."""
    positions = dep_graph.positions
    return {
        "name": view_name(view, positions),
        "variables": list(sorted(view, key=lambda var: positions[var])),
        "is_contiguous": is_contiguous(view, positions),
        "contains_dependency_edge": view_contains_dependency_edge(view, dep_graph),
        "is_gap_bridge_valid": is_gap_bridge_valid(view, dep_graph),
    }


def serialize_strategy(strategy: Strategy, dep_graph: DependencyGraph) -> dict[str, object]:
    """a json-friendly dict of a strategy's ordered views, variables, and coverage."""
    positions = dep_graph.positions
    ordered_views = sorted(strategy, key=lambda item: view_sort_key(item, positions))
    coverage = sorted(strategy_coverage(strategy), key=lambda var: positions[var])
    return {
        "views": [view_name(view, positions) for view in ordered_views],
        "view_variables": [list(sorted(view, key=lambda var: positions[var])) for view in ordered_views],
        "coverage": coverage,
        "has_non_contiguous_subpattern": strategy_has_non_contiguous_subpattern(strategy, positions),
    }
