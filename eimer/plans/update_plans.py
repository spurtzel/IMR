"""update-plan construction: per-view incremental-maintenance source selections and their
bushy join trees."""

from __future__ import annotations

from itertools import combinations, product
from typing import Dict, Iterable, List, Set, Tuple

from eimer.models import DependencyGraph, Strategy, UpdateMode, UpdatePlan, View, view_sort_key


def eligible_index_views(strategy: Strategy, target_view: View) -> List[View]:
    eligible = [candidate for candidate in strategy if candidate < target_view]
    eligible.sort(key=lambda view: (len(view), tuple(sorted(view))))
    return eligible


def _pairwise_disjoint(views: Iterable[View]) -> bool:
    seen: Set[str] = set()
    for view in views:
        if seen.intersection(view):
            return False
        seen.update(view)
    return True


def enumerate_source_selections_for_view(strategy: Strategy, target_view: View) -> List[Set[View]]:
    eligible = eligible_index_views(strategy, target_view)
    selections: List[Set[View]] = []
    for size in range(0, len(eligible) + 1):
        for combo in combinations(eligible, size):
            if _pairwise_disjoint(combo):
                selections.append(set(combo))

    selections.sort(key=lambda sel: (len(sel), sorted(tuple(sorted(v)) for v in sel)))
    return selections


def effective_sources_for_view(target_view: View, selection: Set[View]) -> Set[View]:
    covered: Set[str] = set()
    for source in selection:
        covered.update(source)

    effective = set(selection)
    for var in sorted(target_view):
        if var not in covered:
            effective.add(frozenset([var]))
    return effective


def _build_update_dag(strategy: Strategy, source_selection_map: Dict[View, Set[View]]) -> Dict[View, Set[View]]:
    dag: Dict[View, Set[View]] = {view: set() for view in strategy}
    for target_view, selection in source_selection_map.items():
        for source in selection:
            dag[source].add(target_view)
    return dag


def _topological_order(dag: Dict[View, Set[View]], positions: Dict[str, int]) -> List[View]:
    incoming: Dict[View, int] = {node: 0 for node in dag}
    for source, targets in dag.items():
        for target in targets:
            incoming[target] += 1

    available = sorted(
        [node for node, degree in incoming.items() if degree == 0],
        key=lambda view: view_sort_key(view, positions),
    )

    order: List[View] = []
    while available:
        node = available.pop(0)
        order.append(node)

        for target in sorted(dag[node], key=lambda view: view_sort_key(view, positions)):
            incoming[target] -= 1
            if incoming[target] == 0:
                available.append(target)
        available.sort(key=lambda view: view_sort_key(view, positions))

    if len(order) != len(dag):
        raise ValueError("Update DAG contains a cycle")
    return order


def _last_variable_source(target_view: View, effective_sources: Set[View], positions: Dict[str, int]) -> View:
    last_var = max(target_view, key=lambda var: positions[var])
    candidates = [source for source in effective_sources if last_var in source]
    if len(candidates) != 1:
        raise ValueError(
            f"Expected exactly one source covering last variable {last_var}, found {len(candidates)}"
        )
    return candidates[0]


def _kleene_variables(dep_graph: DependencyGraph) -> Set[str]:
    return {
        variable
        for variable, quantifier in dep_graph.quantifiers.items()
        if quantifier in {"PLUS", "RELUCTANT_PLUS"}
    }


def _variables_with_dependent_conditions(dep_graph: DependencyGraph) -> Set[str]:
    dependent_vars: Set[str] = set()
    for edge in dep_graph.edges:
        if not edge.dependent_conditions:
            continue
        dependent_vars.add(edge.var_left)
        dependent_vars.add(edge.var_right)
    return dependent_vars


def _update_mode_for_view(view: View, dep_graph: DependencyGraph, dependent_vars: Set[str]) -> UpdateMode:
    if len(view) != 1:
        return UpdateMode.JOIN

    variable = next(iter(view))
    if variable in _kleene_variables(dep_graph) and variable not in dependent_vars:
        return UpdateMode.MR_GROUP_DETECT

    return UpdateMode.JOIN


def construct_canonical_update_plan(strategy: Strategy, dep_graph: DependencyGraph) -> UpdatePlan:
    """directly construct the canonical update plan (,,enumerate_update_plans(strategy, dep_graph)[0]'')
    without the ,,product()'' source-selection fan-out.

    the empty selection sorts first for every view, so index 0 is the all-empty source selection,
    reproducible in O(|strategy|).
    """
    positions = dep_graph.positions
    ordered_views = sorted(strategy, key=lambda view: view_sort_key(view, positions))

    source_selection_map: Dict[View, Set[View]] = {view: set() for view in ordered_views}
    effective_sources: Dict[View, Set[View]] = {
        view: effective_sources_for_view(view, set()) for view in ordered_views
    }
    dag = _build_update_dag(strategy, source_selection_map)
    topo = _topological_order(dag, positions)
    last_source_map = {
        view: _last_variable_source(view, effective_sources[view], positions)
        for view in ordered_views
    }

    dependent_vars = _variables_with_dependent_conditions(dep_graph)
    update_mode_by_view = {
        view: _update_mode_for_view(view, dep_graph, dependent_vars)
        for view in ordered_views
    }

    return UpdatePlan(
        source_selections=source_selection_map,
        effective_sources=effective_sources,
        update_dag=dag,
        topological_order=topo,
        last_variable_source=last_source_map,
        update_mode_by_view=update_mode_by_view,
    )


def enumerate_update_plans(strategy: Strategy, dep_graph: DependencyGraph) -> List[UpdatePlan]:
    ordered_views = sorted(strategy, key=lambda view: view_sort_key(view, dep_graph.positions))
    per_view_options = [
        enumerate_source_selections_for_view(strategy, view)
        for view in ordered_views
    ]

    dependent_vars = _variables_with_dependent_conditions(dep_graph)

    plans: List[UpdatePlan] = []
    for option_tuple in product(*per_view_options):
        source_selection_map: Dict[View, Set[View]] = {
            view: set(selection) for view, selection in zip(ordered_views, option_tuple)
        }

        effective_sources: Dict[View, Set[View]] = {
            view: effective_sources_for_view(view, source_selection_map[view])
            for view in ordered_views
        }

        dag = _build_update_dag(strategy, source_selection_map)
        topo = _topological_order(dag, dep_graph.positions)

        last_source_map = {
            view: _last_variable_source(view, effective_sources[view], dep_graph.positions)
            for view in ordered_views
        }

        update_mode_by_view = {
            view: _update_mode_for_view(view, dep_graph, dependent_vars)
            for view in ordered_views
        }

        plans.append(
            UpdatePlan(
                source_selections=source_selection_map,
                effective_sources=effective_sources,
                update_dag=dag,
                topological_order=topo,
                last_variable_source=last_source_map,
                update_mode_by_view=update_mode_by_view,
            )
        )

    plans.sort(
        key=lambda plan: tuple(
            (
                view_sort_key(view, dep_graph.positions),
                tuple(sorted(tuple(sorted(src)) for src in plan.source_selections[view])),
            )
            for view in ordered_views
        )
    )
    return plans


def best_bushy_source_tree(target_view, effective_sources, dep_graph, workload, batch_index):
    """cost-based bushy join order for the UPDATE build of ,,target_view'' over its ,,effective_sources''.

    reuses the compose subset-DP (:func:,,join_order.best_bushy_tree'') so the smallest / most-selective
    operands join first, keeping the band cross-product intermediate small at long patterns. returns
    ,,None'' for ,,<= 1'' source; reorder is a logically-equivalent join, identical to the canonical order."""
    sources = set(effective_sources)
    if len(sources) <= 1:
        return None
    from eimer.selection.join_order import best_bushy_tree, to_join_tree_node
    from eimer.plans.composition import build_composition_join_graph_for_nodes

    join_graph = build_composition_join_graph_for_nodes(dep_graph, sources)
    tree, _cost = best_bushy_tree(join_graph, workload, batch_index)
    return to_join_tree_node(tree)


def with_bushy_source_trees(update_plan: UpdatePlan, dep_graph: DependencyGraph, workload, batch_index=None) -> UpdatePlan:
    """attach cost-DP bushy ,,source_join_trees'' to an EXISTING update plan, preserving its source selections.

    only ,,source_join_trees'' changes; ,,effective_sources'', ,,update_dag'', ,,topological_order'',
    ,,last_variable_source'', and ,,update_mode_by_view'' stay untouched, so the emitter's leaf-equality
    guard and the EXISTS anchor / band WHERE stay path-invariant. ,,batch_index'' defaults to the last
    batch, where the band cross-product is largest."""
    import dataclasses

    if batch_index is None:
        batch_index = len(workload.batch_sizes)
    trees: Dict[View, "JoinTreeNode"] = {}
    for view in update_plan.topological_order:
        tree = best_bushy_source_tree(view, update_plan.effective_sources[view], dep_graph, workload, batch_index)
        if tree is not None:
            trees[view] = tree
    return dataclasses.replace(update_plan, source_join_trees=(trees or None))


def construct_bushy_update_plan(strategy: Strategy, dep_graph: DependencyGraph, workload, batch_index=None) -> UpdatePlan:
    """the canonical update plan with cost-based bushy ,,source_join_trees'' attached per target view
    (:func:,,with_bushy_source_trees'' over :func:,,construct_canonical_update_plan''); the selector's
    bushy-update entry point."""
    return with_bushy_source_trees(
        construct_canonical_update_plan(strategy, dep_graph), dep_graph, workload, batch_index
    )
