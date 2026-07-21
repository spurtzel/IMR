"""materialization strategy space: the maintained-view strategies enumerated over a
dependency graph, filtered to the allowed subpatterns."""

from __future__ import annotations

from itertools import combinations
from typing import Iterable, List, Sequence, Set

from eimer.models import DependencyGraph, Strategy, View, make_view, strategy_sort_key, view_sort_key


def is_contiguous(view: View, positions: dict[str, int]) -> bool:
    """true when the view's variable positions form a gap-free run."""
    ordered_positions = sorted(positions[var] for var in view)
    if not ordered_positions:
        return False
    start = ordered_positions[0]
    end = ordered_positions[-1]
    return len(ordered_positions)==(end - start + 1)


def _has_gap_bridge(view: View, left_pos: int, right_pos: int, dep_graph: DependencyGraph) -> bool:
    """true when some in-view edge spans the [left_pos, right_pos] position gap."""
    for edge in dep_graph.edges:
        if edge.var_left not in view or edge.var_right not in view:
            continue
        pos_left = dep_graph.positions[edge.var_left]
        pos_right = dep_graph.positions[edge.var_right]
        edge_left = min(pos_left, pos_right)
        edge_right = max(pos_left, pos_right)
        if edge_left <= left_pos and edge_right >= right_pos:
            return True
    return False


def is_nc_available(view: View, dep_graph: DependencyGraph) -> bool:
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


def enumerate_valid_subpatterns(dep_graph: DependencyGraph) -> List[View]:
    """all contiguous or gap-bridged views over the graph, in canonical sort order."""
    variables = dep_graph.variables
    valid: List[View] = []
    for size in range(1, len(variables) + 1):
        for combo in combinations(variables, size):
            view = make_view(combo)
            if is_contiguous(view, dep_graph.positions) or is_nc_available(view, dep_graph):
                valid.append(view)

    valid.sort(key=lambda view: view_sort_key(view, dep_graph.positions))
    return valid


def enumerate_all_strategies(valid_subpatterns: Sequence[View], positions: dict[str, int]) -> List[Strategy]:
    """every non-empty subset of the valid subpatterns, as a sorted list of strategies."""
    strategies: List[Strategy] = []
    for size in range(1, len(valid_subpatterns) + 1):
        for combo in combinations(valid_subpatterns, size):
            strategies.append(frozenset(combo))

    strategies.sort(key=lambda strategy: strategy_sort_key(strategy, positions))
    return strategies


def enumerate_strategies(dep_graph: DependencyGraph) -> List[Strategy]:
    """enumerate all strategies over the graph's valid subpatterns."""
    valid_subpatterns = enumerate_valid_subpatterns(dep_graph)
    return enumerate_all_strategies(valid_subpatterns, dep_graph.positions)


def strategy_coverage(strategy: Iterable[View]) -> Set[str]:
    """the set of variables covered by a strategy's views."""
    covered: Set[str] = set()
    for view in strategy:
        covered.update(view)
    return covered


def strategy_has_non_contiguous_subpattern(strategy: Iterable[View], positions: dict[str, int]) -> bool:
    """true when the strategy holds a multi-variable non-contiguous view."""
    for view in strategy:
        if len(view) <= 1:
            continue
        if not is_contiguous(view, positions):
            return True
    return False


def filter_strategies_with_non_contiguous_subpattern(strategies: Sequence[Strategy], positions: dict[str, int]) -> List[Strategy]:
    """keep only strategies that contain a non-contiguous subpattern."""
    filtered = [
        strategy
        for strategy in strategies
        if strategy_has_non_contiguous_subpattern(strategy, positions)
    ]
    filtered.sort(key=lambda strategy: strategy_sort_key(strategy, positions))
    return filtered
