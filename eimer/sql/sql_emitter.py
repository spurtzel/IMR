"""sql emitter: renders the update, composition, and post-filter statements for a
maintained plan. update SQL keeps each materialized view current per batch;
composition joins the covered views (left-deep or bushy tree) into the query result;
the post-filter dedups to one row per match so the output equals native
MATCH_RECOGNIZE.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Dict, Iterable, List, Mapping, Sequence, Set, Tuple

from eimer.models import (
    CompositionPlan,
    DependencyGraph,
    JoinTreeNode,
    JoinType,
    SQLStatements,
    StrategyWithPlans,
    UpdateMode,
    UpdatePlan,
    View,
    view_name,
    view_sort_key,
)
from eimer.sql.sql_dialect import SqlDialect, rewrite_sql
from eimer.sql.sql_render import (
    is_kleene_variable,
    prefixed_condition_column,
    render_expression_for_base_scan,
    render_expression_for_prefixed_columns,
    replace_qualified_refs,
)
from eimer.query.graph_adapter import collect_referenced_columns, schema_min_enabled
from eimer.plans.composition import determine_join_type

if TYPE_CHECKING:
    from eimer.plans.evaluation_plan import EvaluationPlan


def _ordered_vars(view: View, positions: Dict[str, int]) -> List[str]:
    return sorted(view, key=lambda var: positions[var])


def _view_table_name(view: View, positions: Dict[str, int]) -> str:
    # ,,cache'' and ,,view'' mean the same materialized sub-pattern view here; the table uses a ,,cache_'' prefix.
    return f"cache_{view_name(view, positions)}"


def _kleene_staging_table_name(view: View, positions: Dict[str, int]) -> str:
    # Per-view append staging table: a 1:1 column clone of cache_<view>, no index; the runner appends staging -> view each batch.
    return f"stg_{view_name(view, positions)}"


def _alias_for_view(view: View, prefix: str, positions: Dict[str, int]) -> str:
    return f"{prefix}_{view_name(view, positions).lower()}"


def _join_precedence(join_type: JoinType) -> int:
    if join_type == JoinType.EQUI:
        return 0
    if join_type == JoinType.BAND:
        return 1
    return 2


def _kleene_variables(dep_graph: DependencyGraph) -> Set[str]:
    return {
        variable
        for variable, quantifier in dep_graph.quantifiers.items()
        if quantifier in {"PLUS", "RELUCTANT_PLUS"}
    }


def _is_kleene_variable(variable: str, dep_graph: DependencyGraph) -> bool:
    return is_kleene_variable(variable, dep_graph)


def _reluctant_predecessor_variables(dep_graph: DependencyGraph) -> Set[str]:
    return {
        gap.right_variable
        for gap in dep_graph.wildcard_gaps
        if gap.mode == "RELUCTANT"
    }


def _kleene_guardrails(strategy_with_plans: StrategyWithPlans, dep_graph: DependencyGraph) -> None:
    kleene_vars = _kleene_variables(dep_graph)
    if not kleene_vars:
        return

    for edge in dep_graph.edges:
        if not edge.dependent_conditions:
            continue
        if edge.var_left in kleene_vars or edge.var_right in kleene_vars:
            raise NotImplementedError(
                "Dependent predicates involving Kleene variables are not supported in Path A yet"
            )

    for view in strategy_with_plans.strategy:
        if len(view) > 1 and any(var in kleene_vars for var in view):
            raise NotImplementedError(
                "Multi-variable materialized views containing Kleene variables are not supported in Path A yet"
            )


def _earliest_ts_expr(alias: str, variable: str, dep_graph: DependencyGraph) -> str:
    if _is_kleene_variable(variable, dep_graph):
        return f"{alias}.{variable}_first_ts"
    return f"{alias}.{variable}_ts"


def _latest_ts_expr(alias: str, variable: str, dep_graph: DependencyGraph) -> str:
    if _is_kleene_variable(variable, dep_graph):
        return f"{alias}.{variable}_last_ts"
    return f"{alias}.{variable}_ts"


def _structural_predicates(left: View, right: View, left_alias: str, right_alias: str,
                           positions: Dict[str, int], dep_graph: DependencyGraph) -> List[str]:
    join_type = determine_join_type(left, right, positions)

    if join_type == JoinType.EQUI:
        shared = sorted(left.intersection(right), key=lambda var: positions[var])
        predicates: List[str] = []
        for var in shared:
            if _is_kleene_variable(var, dep_graph):
                predicates.append(f"{left_alias}.{var}_first_id = {right_alias}.{var}_first_id")
                predicates.append(f"{left_alias}.{var}_last_id = {right_alias}.{var}_last_id")
            else:
                predicates.append(f"{left_alias}.{var}_id = {right_alias}.{var}_id")
        return predicates

    if join_type == JoinType.BAND:
        left_span = (min(positions[var] for var in left), max(positions[var] for var in left))
        right_span = (min(positions[var] for var in right), max(positions[var] for var in right))

        if left_span[0] < right_span[0] and right_span[1] < left_span[1]:
            outer, inner = left, right
            outer_alias, inner_alias = left_alias, right_alias
        else:
            outer, inner = right, left
            outer_alias, inner_alias = right_alias, left_alias

        outer_first, outer_last = _ordered_vars(outer, positions)[0], _ordered_vars(outer, positions)[-1]
        inner_first, inner_last = _ordered_vars(inner, positions)[0], _ordered_vars(inner, positions)[-1]

        return [
            f"{_earliest_ts_expr(outer_alias, outer_first, dep_graph)} < "
            f"{_earliest_ts_expr(inner_alias, inner_first, dep_graph)}",
            f"{_latest_ts_expr(inner_alias, inner_last, dep_graph)} < "
            f"{_latest_ts_expr(outer_alias, outer_last, dep_graph)}",
        ]

    # INEQ: views are disjoint and neither frames the other, so positions may separate or cross.
    # Emit the position-correct per-variable ordering for every cross pair (earlier.latest_ts <
    # later.earliest_ts); a view-level "is left before right?" test would emit a backwards
    # predicate (0 rows) for crossing pairs.
    predicates = []
    for left_var in sorted(left, key=lambda v: positions[v]):
        for right_var in sorted(right, key=lambda v: positions[v]):
            if positions[left_var] < positions[right_var]:
                e_alias, e_var, l_alias_, l_var = left_alias, left_var, right_alias, right_var
            else:
                e_alias, e_var, l_alias_, l_var = right_alias, right_var, left_alias, left_var
            predicates.append(
                f"{_latest_ts_expr(e_alias, e_var, dep_graph)} < {_earliest_ts_expr(l_alias_, l_var, dep_graph)}"
            )
    return predicates


def _dependent_predicates_in_on_enabled() -> bool:
    """place dependent value predicates at the lowest evaluable JOIN ON instead of the final
    WHERE. Trino (reordering NONE) does not push band predicates below cross joins, so ON
    placement filters at the earliest join. EIMER_DEPENDENT_PREDICATES_IN_ON=0 restores
    final-WHERE rendering; the regime is part of plan content, so plan fingerprints depend on it."""
    return os.environ.get("EIMER_DEPENDENT_PREDICATES_IN_ON", "").lower() not in ("0", "false", "off", "no")


def _collect_internalized_conditions(target_view: View, dep_graph: DependencyGraph) -> List[str]:
    internalized: List[str] = []

    # Independent predicates are already applied on the RAW base scan, so re-rendering them here on
    # the PROJECTED columns is redundant. Under schema-min the projected filter column is dropped, so
    # this prefixed re-reference would be an invalid identifier; skip it. Kept when schema-min is off.
    if not schema_min_enabled():
        for var in _ordered_vars(target_view, dep_graph.positions):
            internalized.extend(dep_graph.independent_conditions.get(var, []))

    for edge in dep_graph.edges:
        if edge.var_left in target_view and edge.var_right in target_view:
            internalized.extend(edge.dependent_conditions)

    return internalized


def _ordered_vars_for_sequence(vars_or_view: Iterable[str], positions: Dict[str, int]) -> List[str]:
    return sorted(set(vars_or_view), key=lambda var: positions[var])


def _adjacent_sequence_predicates(ordered_vars: Sequence[str], var_to_alias: Dict[str, str],
                                  dep_graph: DependencyGraph) -> List[str]:
    predicates: List[str] = []
    for left_var, right_var in zip(ordered_vars, ordered_vars[1:]):
        predicates.append(
            f"{_latest_ts_expr(var_to_alias[left_var], left_var, dep_graph)} < "
            f"{_earliest_ts_expr(var_to_alias[right_var], right_var, dep_graph)}"
        )
    return predicates


def _variable_aliases_in_nodes(nodes: Set[View], node_aliases: Dict[View, str],
                               positions: Dict[str, int]) -> Dict[str, List[str]]:
    var_aliases: Dict[str, List[str]] = {}
    for node in sorted(nodes, key=lambda view: view_sort_key(view, positions)):
        alias = node_aliases[node]
        for var in _ordered_vars(node, positions):
            var_aliases.setdefault(var, []).append(alias)
    return var_aliases


def _repeat_var_consistency_predicates(var_aliases: Dict[str, List[str]], dep_graph: DependencyGraph) -> List[str]:
    predicates: List[str] = []
    for var in sorted(var_aliases.keys(), key=lambda current: dep_graph.positions[current]):
        aliases = list(dict.fromkeys(var_aliases[var]))
        if len(aliases) <= 1:
            continue

        canonical = aliases[0]
        for alias in aliases[1:]:
            if _is_kleene_variable(var, dep_graph):
                predicates.append(f"{canonical}.{var}_first_id = {alias}.{var}_first_id")
                predicates.append(f"{canonical}.{var}_last_id = {alias}.{var}_last_id")
            else:
                predicates.append(f"{canonical}.{var}_id = {alias}.{var}_id")
                predicates.append(f"{canonical}.{var}_ts = {alias}.{var}_ts")
    return predicates


def _edge_internalized_in_nodes(nodes: Set[View], var_left: str, var_right: str) -> bool:
    return any(var_left in node and var_right in node for node in nodes)


def _composition_dependent_predicates(nodes: Set[View], var_aliases: Dict[str, List[str]],
                                      dep_graph: DependencyGraph) -> List[str]:
    """render every dependent edge predicate not already enforced inside a node. an edge
    contained in some composition node is internalized by that view/update SQL; otherwise
    it must appear in the final compose WHERE."""

    var_to_alias = {
        var: aliases[0]
        for var, aliases in sorted(var_aliases.items(), key=lambda item: dep_graph.positions[item[0]])
    }
    predicates: List[str] = []
    for edge in dep_graph.edges:
        if not edge.dependent_conditions:
            continue
        if _edge_internalized_in_nodes(nodes, edge.var_left, edge.var_right):
            continue
        for condition in edge.dependent_conditions:
            predicates.append(render_expression_for_prefixed_columns(condition, var_to_alias, dep_graph))
    return predicates


def _canonical_source_join_sequence(sources: Set[View],
                                    positions: Dict[str, int]) -> Tuple[View, List[Tuple[View, View]]]:
    ordered = sorted(sources, key=lambda view: view_sort_key(view, positions))
    root = ordered[0]
    if len(ordered) == 1:
        return root, []

    visited = {root}
    sequence: List[Tuple[View, View]] = []

    while len(visited) < len(ordered):
        candidates: List[Tuple[int, Tuple[int, ...], Tuple[int, ...], View, View]] = []
        for anchor in sorted(visited, key=lambda view: view_sort_key(view, positions)):
            for candidate in ordered:
                if candidate in visited:
                    continue
                join_type = determine_join_type(anchor, candidate, positions)
                candidates.append(
                    (
                        _join_precedence(join_type),
                        view_sort_key(anchor, positions),
                        view_sort_key(candidate, positions),
                        anchor,
                        candidate,
                    )
                )

        candidates.sort()
        _, _, _, anchor, candidate = candidates[0]
        sequence.append((anchor, candidate))
        visited.add(candidate)

    return root, sequence


def _singleton_scan_sql(variable: str, table_name: str, alias: str,
                        columns_by_var: Dict[str, List[str]], independent_conditions: List[str]) -> str:
    projected = [
        f"id AS {variable}_id",
        f"ts AS {variable}_ts",
    ]

    for col in columns_by_var.get(variable, []):
        if col.lower() in {"id", "ts"}:   # case-insensitive: id/ts are keys regardless of the referenced casing
            continue
        projected.append(f"{col} AS {variable}_{col}")

    where_sql = ""
    if independent_conditions:
        rendered = [
            render_expression_for_base_scan(cond, variable, "b")
            for cond in independent_conditions
        ]
        where_sql = " WHERE " + " AND ".join(rendered)

    return f"(SELECT {', '.join(projected)} FROM {table_name} b{where_sql}) {alias}"


def _mr_group_detect_columns(variable: str, columns_by_var: Dict[str, List[str]]) -> Tuple[List[str], List[str]]:
    insert_cols = [
        f"{variable}_first_id",
        f"{variable}_first_ts",
        f"{variable}_last_id",
        f"{variable}_last_ts",
        f"{variable}_count",
    ]
    measures = [
        f"FIRST({variable}.id) AS {variable}_first_id",
        f"FIRST({variable}.ts) AS {variable}_first_ts",
        f"LAST({variable}.id) AS {variable}_last_id",
        f"LAST({variable}.ts) AS {variable}_last_ts",
        f"COUNT({variable}.id) AS {variable}_count",
    ]

    for col in columns_by_var.get(variable, []):
        if col.lower() in {"id", "ts"}:   # case-insensitive: id/ts are keys regardless of the referenced casing
            continue
        insert_cols.append(f"{variable}_first_{col}")
        insert_cols.append(f"{variable}_last_{col}")
        measures.append(f"FIRST({variable}.{col}) AS {variable}_first_{col}")
        measures.append(f"LAST({variable}.{col}) AS {variable}_last_{col}")

    return insert_cols, measures


def _mr_group_detect_update_sql(variable: str, dep_graph: DependencyGraph, columns_by_var: Dict[str, List[str]],
                                base_table: str, batch_table: str, *,
                                redetect_lo: "int | str | None" = None, target_table: "str | None" = None) -> str:
    table_name = _view_table_name(frozenset({variable}), dep_graph.positions)
    insert_cols, measures = _mr_group_detect_columns(variable, columns_by_var)
    conditions = dep_graph.independent_conditions.get(variable, [])
    define_expr = " AND ".join(conditions) if conditions else "TRUE"

    if target_table is not None and redetect_lo is None:
        # target_table is honored only in window mode; without redetect_lo it would full-rescan into
        # the real view instead of staging, almost certainly a caller bug.
        raise ValueError(
            "_mr_group_detect_update_sql: target_table requires redetect_lo (variant-B window mode)")

    if redetect_lo is None:
        # default path: full rescan of the base table.
        return "\n".join(
            [
                f"INSERT INTO {table_name} ({', '.join(insert_cols)})",
                "SELECT mr.*",
                f"FROM {base_table} MATCH_RECOGNIZE (",
                "    ORDER BY ts",
                "    MEASURES",
                "        " + ",\n        ".join(measures),
                "    ONE ROW PER MATCH",
                "    AFTER MATCH SKIP TO NEXT ROW",
                f"    PATTERN ({variable}+)",
                "    DEFINE",
                f"        {variable} AS {define_expr}",
                ") mr",
                f"WHERE EXISTS (SELECT 1 FROM {batch_table} eb WHERE eb.id = mr.{variable}_last_id)",
                ";",
            ]
        )

    # opt-in incremental "stitch": re-detect runs over ONLY the window [redetect_lo, MAX(id) of the
    # current batch] and APPEND every detected run (no WHERE EXISTS). The read-side _kleene_dedup_cte
    # keeps the longest run per first_id, so the view is never rewritten in place and never indexed.
    # ,,redetect_lo'' is the run start left open at the previous boundary (O(1) state the runner
    # carries); it is rendered VERBATIM, so a caller may pass an int literal or a ':redetect_lo_<label>'
    # placeholder.
    dest = target_table or table_name
    redetect_src = (
        f"(SELECT * FROM {base_table} "
        f"WHERE id BETWEEN {redetect_lo} AND (SELECT MAX(id) FROM {batch_table}))"
    )
    return "\n".join(
        [
            f"INSERT INTO {dest} ({', '.join(insert_cols)})",
            "SELECT mr.*",
            f"FROM {redetect_src} MATCH_RECOGNIZE (",
            "    ORDER BY ts",
            "    MEASURES",
            "        " + ",\n        ".join(measures),
            "    ONE ROW PER MATCH",
            "    AFTER MATCH SKIP TO NEXT ROW",
            f"    PATTERN ({variable}+)",
            "    DEFINE",
            f"        {variable} AS {define_expr}",
            ") mr",
            ";",
        ]
    )


def _render_deferred_condition_for_edge(expression: str, left: View, right: View, left_alias: str,
                                        right_alias: str, dep_graph: DependencyGraph) -> str:
    def resolver(var: str, col: str) -> str | None:
        in_left = var in left
        in_right = var in right

        if in_left and not in_right:
            return f"{left_alias}.{prefixed_condition_column(var, col, dep_graph)}"
        if in_right and not in_left:
            return f"{right_alias}.{prefixed_condition_column(var, col, dep_graph)}"
        if in_left and in_right:
            return f"{left_alias}.{prefixed_condition_column(var, col, dep_graph)}"
        return None

    return replace_qualified_refs(expression, resolver)


def _emit_source_tree_node(tree: JoinTreeNode, source_sql: Dict[View, str], source_aliases: Dict[View, str],
                           dep_graph: DependencyGraph, columns_by_var: Dict[str, List[str]],
                           counter: List[int]) -> Tuple[str, str, View, Dict[str, str]]:
    """recursively emit the nested-subquery FROM for a bushy SOURCE tree of an update build, returning
    ,,(sql_fragment, alias, merged_view, var_to_alias)''. Leaves reuse the build's ,,source_sql''; inner
    nodes carry the structural predicates (equi / band / inequality) at their ON, and shared-source
    children add the ,,{V}_ts'' companion so each variable surfaces once. Dependent value predicates and
    the adjacent ts-sequence stay in the build WHERE; inner aliases are ,,stn<k>''."""
    if tree.node is not None:
        alias = source_aliases[tree.node]
        return source_sql[tree.node], alias, tree.node, {var: alias for var in tree.node}

    left_sql, left_alias, left_view, left_map = _emit_source_tree_node(
        tree.left, source_sql, source_aliases, dep_graph, columns_by_var, counter
    )
    right_sql, right_alias, right_view, right_map = _emit_source_tree_node(
        tree.right, source_sql, source_aliases, dep_graph, columns_by_var, counter
    )

    predicates = _structural_predicates(
        left_view,
        right_view,
        left_alias,
        right_alias,
        dep_graph.positions,
        dep_graph,
    )
    shared = left_view & right_view
    for var in sorted(shared, key=lambda v: dep_graph.positions[v]):
        # _structural_predicates' EQUI branch emits only the id key; add the ts companion so a
        # shared-source tree stays consistent with the left-deep repeat-var contract.
        predicates.append(f"{left_alias}.{var}_ts = {right_alias}.{var}_ts")
    # Push each DEPENDENT band predicate that first spans these two children (one endpoint
    # left-exclusive, the other right-exclusive) into THIS ON, so the bushy source tree filters
    # early instead of at the top WHERE under Trino's no-pushdown. The build WHERE still re-emits
    # these bands, so each ON band is logically implied by a WHERE band: it only adds early filters
    # and never changes the result set. Bands internal to a single source stay internalized there.
    left_map = {var: left_alias for var in left_view}
    right_map = {var: right_alias for var in right_view}
    resolved_map = {**right_map, **left_map}
    left_only, right_only = set(left_map) - shared, set(right_map) - shared
    for edge in dep_graph.edges:
        if not edge.dependent_conditions:
            continue
        spans = (edge.var_left in left_only and edge.var_right in right_only) or (
            edge.var_left in right_only and edge.var_right in left_only
        )
        if not spans:
            continue
        for condition in edge.dependent_conditions:
            predicates.append(render_expression_for_prefixed_columns(condition, resolved_map, dep_graph))
    on_sql = " AND ".join(predicates) if predicates else "1 = 1"

    counter[0] += 1
    alias = f"stn{counter[0]}"
    select_list = _tree_node_select_list(
        left_alias, right_alias, left_map, right_map, dep_graph, columns_by_var
    )
    sql = (
        f"(SELECT {select_list} "
        f"FROM {left_sql} JOIN {right_sql} ON {on_sql}) {alias}"
    )
    merged_view = left_view | right_view
    return sql, alias, merged_view, {var: alias for var in merged_view}


def generate_update_sql_statements(strategy_with_plans: StrategyWithPlans, dep_graph: DependencyGraph,
                                   update_plan: UpdatePlan, base_table: str, batch_table: str) -> List[str]:
    _kleene_guardrails(strategy_with_plans, dep_graph)

    positions = dep_graph.positions
    strategy = strategy_with_plans.strategy
    columns_by_var = collect_referenced_columns(dep_graph)

    statements: List[str] = []
    for target_view in update_plan.topological_order:
        update_mode = update_plan.update_mode_by_view.get(target_view, UpdateMode.JOIN)

        if update_mode == UpdateMode.MR_GROUP_DETECT:
            if len(target_view) != 1:
                raise ValueError("MR_GROUP_DETECT mode requires singleton target view")
            variable = next(iter(target_view))
            if not _is_kleene_variable(variable, dep_graph):
                raise ValueError("MR_GROUP_DETECT mode requires a Kleene variable")
            statements.append(
                _mr_group_detect_update_sql(
                    variable,
                    dep_graph,
                    columns_by_var,
                    base_table,
                    batch_table,
                )
            )
            continue

        kleene_vars_in_target = [var for var in target_view if _is_kleene_variable(var, dep_graph)]
        if kleene_vars_in_target:
            raise NotImplementedError(
                "JOIN-mode updates for target views containing Kleene variables are not supported in Path A yet"
            )

        selected_sources = update_plan.source_selections[target_view]
        effective_sources = update_plan.effective_sources[target_view]
        last_source = update_plan.last_variable_source[target_view]

        source_aliases = {
            source: _alias_for_view(source, "src", positions)
            for source in sorted(effective_sources, key=lambda view: view_sort_key(view, positions))
        }

        source_sql: Dict[View, str] = {}
        for source in effective_sources:
            alias = source_aliases[source]
            if source in selected_sources:
                source_sql[source] = f"{_view_table_name(source, positions)} {alias}"
                continue

            if len(source) != 1:
                raise ValueError("Non-singleton implicit source encountered")

            variable = next(iter(source))
            if _is_kleene_variable(variable, dep_graph):
                raise NotImplementedError(
                    "Implicit singleton base scans for Kleene variables are not supported in Path A yet"
                )
            is_last_source = source == last_source
            table_name = batch_table if is_last_source else base_table
            source_sql[source] = _singleton_scan_sql(
                variable,
                table_name,
                alias,
                columns_by_var,
                dep_graph.independent_conditions.get(variable, []),
            )

        source_tree = (
            update_plan.source_join_trees.get(target_view)
            if update_plan.source_join_trees is not None
            else None
        )

        if source_tree is not None:
            # Bushy source tree: structural predicates at the lowest node; dependent value predicates
            # and the adjacent ts-sequence stay in the WHERE as the left-deep path emits them, so
            # bands-in-ON attachment does not run here.
            if set(source_tree.views) != set(effective_sources):
                raise ValueError("source_join_trees leaves must equal the build's effective sources")
            root_sql, _root_alias, _merged_view, var_to_alias = _emit_source_tree_node(
                source_tree, source_sql, source_aliases, dep_graph, columns_by_var, [0]
            )
            from_clause = f"FROM {root_sql}"
            join_clauses: List[str] = []
            attached_on_conditions: Set[str] = set()
        else:
            root, join_sequence = _canonical_source_join_sequence(effective_sources, positions)

            bands_in_on = _dependent_predicates_in_on_enabled()
            pending_dependent_edges = [
                edge
                for edge in dep_graph.edges
                if edge.dependent_conditions
                and edge.var_left in target_view
                and edge.var_right in target_view
                and not any(
                    edge.var_left in source and edge.var_right in source for source in effective_sources
                )
            ] if bands_in_on else []
            present_alias_by_var: Dict[str, str] = {}
            for var in sorted(root, key=lambda v: positions[v]):
                present_alias_by_var.setdefault(var, source_aliases[root])
            attached_on_conditions = set()

            from_clause = f"FROM {source_sql[root]}"
            join_clauses = []
            for anchor, new_source in join_sequence:
                predicates = _structural_predicates(
                    anchor,
                    new_source,
                    source_aliases[anchor],
                    source_aliases[new_source],
                    positions,
                    dep_graph,
                )
                if bands_in_on:
                    for var in sorted(new_source, key=lambda v: positions[v]):
                        present_alias_by_var.setdefault(var, source_aliases[new_source])
                    still_pending: List = []
                    for dep_edge in pending_dependent_edges:
                        if dep_edge.var_left in present_alias_by_var and dep_edge.var_right in present_alias_by_var:
                            for condition in dep_edge.dependent_conditions:
                                predicates.append(
                                    render_expression_for_prefixed_columns(
                                        condition, present_alias_by_var, dep_graph
                                    )
                                )
                                attached_on_conditions.add(condition)
                        else:
                            still_pending.append(dep_edge)
                    pending_dependent_edges = still_pending
                on_sql = " AND ".join(predicates) if predicates else "1 = 1"
                join_clauses.append(
                    f"JOIN {source_sql[new_source]} ON {on_sql}"
                )

            var_to_alias: Dict[str, str] = {}
            for source, alias in source_aliases.items():
                for var in source:
                    var_to_alias[var] = alias

        where_parts: List[str] = []
        for condition in _collect_internalized_conditions(target_view, dep_graph):
            if condition in attached_on_conditions:
                continue
            where_parts.append(render_expression_for_prefixed_columns(condition, var_to_alias, dep_graph))

        where_parts.extend(
            _adjacent_sequence_predicates(
                _ordered_vars_for_sequence(target_view, positions),
                var_to_alias,
                dep_graph,
            )
        )

        last_var = max(target_view, key=lambda var: positions[var])
        last_alias = var_to_alias[last_var]
        where_parts.append(
            f"EXISTS (SELECT 1 FROM {batch_table} batch_new WHERE batch_new.id = {last_alias}.{last_var}_id)"
        )

        dedup_where = list(dict.fromkeys(where_parts))

        select_columns: List[str] = []
        for var in _ordered_vars(target_view, positions):
            if _is_kleene_variable(var, dep_graph):
                raise NotImplementedError(
                    "JOIN-mode projection for Kleene variables is not supported in Path A yet"
                )
            alias = var_to_alias[var]
            select_columns.append(f"{alias}.{var}_id AS {var}_id")
            select_columns.append(f"{alias}.{var}_ts AS {var}_ts")
            for col in columns_by_var.get(var, []):
                if col in {"id", "ts"}:
                    continue
                select_columns.append(f"{alias}.{var}_{col} AS {var}_{col}")

        sql_lines = [
            f"INSERT INTO {_view_table_name(target_view, positions)}",
            "SELECT",
            "    " + ",\n    ".join(select_columns),
            from_clause,
        ]
        sql_lines.extend(join_clauses)
        if dedup_where:
            sql_lines.append("WHERE " + "\n  AND ".join(dedup_where))
        sql_lines.append(";")

        statements.append("\n".join(sql_lines))

    return statements


def _kleene_dedup_cte(view: View, positions: Dict[str, int]) -> Tuple[str, str]:
    variable = next(iter(view))
    table_name = _view_table_name(view, positions)
    cte_name = f"{table_name}_deduped"

    cte_sql = "\n".join(
        [
            f"{cte_name} AS (",
            "    SELECT *",
            "    FROM (",
            "        SELECT *,",
            f"               ROW_NUMBER() OVER (PARTITION BY {variable}_first_id ORDER BY {variable}_last_ts DESC) AS dedup_rn",
            f"        FROM {table_name}",
            "    ) t",
            "    WHERE dedup_rn = 1",
            ")",
        ]
    )
    return cte_name, cte_sql


def _composition_root_for_join_order(nodes: Set[View], composition_plan: CompositionPlan,
                                     positions: Dict[str, int]) -> View:
    if composition_plan.join_order:
        first_left = composition_plan.join_order[0][0]
        if first_left in nodes:
            return first_left
    return min(nodes, key=lambda view: view_sort_key(view, positions))


def _tree_mode_kleene_guard(dep_graph: DependencyGraph) -> None:
    """fail loud if any pattern variable is Kleene: bushy tree emission does not support Kleene
    grouping/dedup. raised before any tree SQL is built."""
    if _kleene_variables(dep_graph):
        raise NotImplementedError(
            "Kleene variables are not supported in bushy tree composition mode yet"
        )


def _tree_leaf_sql(node: View, alias: str, strategy: Set[View], dep_graph: DependencyGraph,
                   columns_by_var: Dict[str, List[str]], base_table: str) -> str:
    """the leaf relation for a composition node: a maintained view table reference
    (,,cache_<view> <alias>'') or, for an implicit singleton, the inline base scan. Kleene nodes are
    unreachable here (,,_tree_mode_kleene_guard'' runs first)."""
    if node in strategy:
        return f"{_view_table_name(node, dep_graph.positions)} {alias}"

    if len(node) != 1:
        raise ValueError("Implicit composition node must be singleton")
    variable = next(iter(node))
    return _singleton_scan_sql(
        variable,
        base_table,
        alias,
        columns_by_var,
        dep_graph.independent_conditions.get(variable, []),
    )


def _tree_node_conditions(left_map: Dict[str, str], right_map: Dict[str, str], dep_graph: DependencyGraph) -> List[str]:
    """every predicate whose endpoints first co-occur at this inner node, rendered against the child
    aliases: shared-variable equality keys ({V}_id and {V}_ts), each dependent-edge value predicate
    that spans the two children (one endpoint left-exclusive, the other right-exclusive; an edge
    within a single leaf is internalized there), and the cross-child pairwise ts-ordering by position.
    Shared variables resolve to the LEFT alias, so each variable has one unambiguous column."""
    positions = dep_graph.positions
    shared = set(left_map) & set(right_map)
    # Left wins for shared variables: the equality keys collapse the two physical copies, so every
    # later reference (bands, ts-ordering, ancestor ON, projection) uses the surviving LEFT alias.
    resolved_map = {**right_map, **left_map}
    left_only = set(left_map) - shared
    right_only = set(right_map) - shared

    def crosses(var_a: str, var_b: str) -> bool:
        # An edge spans this node only if one endpoint is left-EXCLUSIVE and the other
        # right-EXCLUSIVE. A shared endpoint means the edge is internal to a leaf below (already
        # enforced in its view/scan), so it must not be re-emitted here.
        return (var_a in left_only and var_b in right_only) or (
            var_a in right_only and var_b in left_only
        )

    conditions: List[str] = []
    for var in sorted(shared, key=lambda v: positions[v]):
        conditions.append(f"{left_map[var]}.{var}_id = {right_map[var]}.{var}_id")
        conditions.append(f"{left_map[var]}.{var}_ts = {right_map[var]}.{var}_ts")

    for edge in dep_graph.edges:
        if not edge.dependent_conditions:
            continue
        if not crosses(edge.var_left, edge.var_right):
            continue
        for condition in edge.dependent_conditions:
            conditions.append(
                render_expression_for_prefixed_columns(condition, resolved_map, dep_graph)
            )

    for left_var in sorted(left_map, key=lambda var: positions[var]):
        for right_var in sorted(right_map, key=lambda var: positions[var]):
            if left_var==right_var:
                # shared variable: the equality keys above already tie the two copies; a ts-ordering
                # of an event against itself would be an always-false self-comparison.
                continue
            earlier, later = (
                (left_var, right_var)
                if positions[left_var] < positions[right_var]
                else (right_var, left_var)
            )
            conditions.append(
                f"{resolved_map[earlier]}.{earlier}_ts < {resolved_map[later]}.{later}_ts"
            )

    return list(dict.fromkeys(conditions))


def _var_columns(alias: str, variable: str, columns_by_var: Dict[str, List[str]]) -> List[str]:
    """the qualified pass-through columns the view/scan exposes for ,,variable'' under ,,alias'':
    ,,{alias}.{var}_id'', ,,{alias}.{var}_ts'', then each referenced non-id/ts column."""
    cols = [f"{alias}.{variable}_id", f"{alias}.{variable}_ts"]
    for col in columns_by_var.get(variable, []):
        if col.lower() in {"id", "ts"}:   # case-insensitive: id/ts are keys regardless of the referenced casing
            continue
        cols.append(f"{alias}.{variable}_{col}")
    return cols


def _tree_node_select_list(left_alias: str, right_alias: str, left_map: Dict[str, str],
                           right_map: Dict[str, str], dep_graph: DependencyGraph,
                           columns_by_var: Dict[str, List[str]]) -> str:
    """the node's SELECT list. Disjoint children keep the ,,<l>.*, <r>.*'' wildcard. When the children
    share variables, the shared columns survive on the LEFT only: keep ,,<l>.*'' whole and project just
    the right-exclusive variables' columns, so each variable surfaces exactly once."""
    shared = set(left_map) & set(right_map)
    if not shared:
        return f"{left_alias}.*, {right_alias}.*"

    positions = dep_graph.positions
    right_only = sorted(set(right_map) - shared, key=lambda var: positions[var])
    columns = [f"{left_alias}.*"]
    for var in right_only:
        columns.extend(_var_columns(right_map[var], var, columns_by_var))
    return ", ".join(columns)


def _emit_tree_node(tree: JoinTreeNode, node_sql: Dict[View, str], node_aliases: Dict[View, str],
                    dep_graph: DependencyGraph, columns_by_var: Dict[str, List[str]],
                    counter: List[int]) -> Tuple[str, str, Dict[str, str]]:
    """recursively emit the nested-subquery FROM for a bushy tree node, returning
    ,,(sql_fragment, alias, var_to_alias)''. Inner nodes carry every predicate that first becomes
    evaluable here at their ON; leaves reuse the left-deep leaf SQL. Leaves keep their left-deep
    alias, inner nodes are ,,tn<k>''."""
    if tree.node is not None:
        alias = node_aliases[tree.node]
        return node_sql[tree.node], alias, {var: alias for var in tree.node}

    left_sql, left_alias, left_map = _emit_tree_node(
        tree.left, node_sql, node_aliases, dep_graph, columns_by_var, counter
    )
    right_sql, right_alias, right_map = _emit_tree_node(
        tree.right, node_sql, node_aliases, dep_graph, columns_by_var, counter
    )

    conditions = _tree_node_conditions(left_map, right_map, dep_graph)
    on_sql = " AND ".join(conditions) if conditions else "1 = 1"

    counter[0] += 1
    alias = f"tn{counter[0]}"
    select_list = _tree_node_select_list(
        left_alias, right_alias, left_map, right_map, dep_graph, columns_by_var
    )
    sql = (
        f"(SELECT {select_list} "
        f"FROM {left_sql} JOIN {right_sql} ON {on_sql}) {alias}"
    )
    merged = {var: alias for var in {**left_map, **right_map}}
    return sql, alias, merged


def generate_composition_tree_sql(strategy_with_plans: StrategyWithPlans, dep_graph: DependencyGraph,
                                  composition_nodes: Iterable[View], join_tree: JoinTreeNode,
                                  base_table: str) -> str:
    """render bushy-tree composition: the same outer shape as ,,generate_composition_sql_for_nodes''
    (identical SELECT columns and global-sequence WHERE) but a FROM built from recursive nested
    subqueries over ,,join_tree''. Predicates always sit at their lowest evaluable tree node
    (independent of ,,EIMER_DEPENDENT_PREDICATES_IN_ON''); the top WHERE re-emits only the global
    adjacent ts-sequence. Edges internal to a single leaf view are internalized there. Kleene
    variables raise."""

    _tree_mode_kleene_guard(dep_graph)

    positions = dep_graph.positions
    strategy = strategy_with_plans.strategy
    nodes = set(composition_nodes)
    columns_by_var = collect_referenced_columns(dep_graph)

    if set(join_tree.views) != nodes:
        raise ValueError("join_tree leaves must equal the composition node set")

    node_aliases = {
        node: _alias_for_view(node, "cmp", positions)
        for node in sorted(nodes, key=lambda view: view_sort_key(view, positions))
    }
    node_sql = {
        node: _tree_leaf_sql(node, node_aliases[node], strategy, dep_graph, columns_by_var, base_table)
        for node in sorted(nodes, key=lambda view: view_sort_key(view, positions))
    }

    root_sql, _root_alias, var_to_alias = _emit_tree_node(
        join_tree, node_sql, node_aliases, dep_graph, columns_by_var, [0]
    )

    select_columns: List[str] = []
    for var in dep_graph.variables:
        alias = var_to_alias[var]
        select_columns.append(f"{alias}.{var}_id AS {var}_id")
        select_columns.append(f"{alias}.{var}_ts AS {var}_ts")

    global_sequence_predicates = _adjacent_sequence_predicates(
        _ordered_vars_for_sequence(dep_graph.variables, positions),
        var_to_alias,
        dep_graph,
    )

    lines = [
        "SELECT",
        "    " + ",\n    ".join(select_columns),
        f"FROM {root_sql}",
    ]
    if global_sequence_predicates:
        lines.append("WHERE " + "\n  AND ".join(dict.fromkeys(global_sequence_predicates)))
    lines.append(";")
    return "\n".join(lines)


def generate_composition_sql_for_nodes(strategy_with_plans: StrategyWithPlans, dep_graph: DependencyGraph,
                                       composition_nodes: Iterable[View], composition_plan: CompositionPlan,
                                       base_table: str, join_tree: JoinTreeNode | None = None) -> str:
    """render composition for explicit query leaf nodes. The materialization strategy determines which
    view tables are maintained; the composition node set determines which leaves answer this query.
    When ,,join_tree'' is supplied the FROM is built from recursive nested subqueries instead of the
    left-deep JOIN chain."""

    if join_tree is not None and not _kleene_variables(dep_graph):
        return generate_composition_tree_sql(
            strategy_with_plans,
            dep_graph,
            composition_nodes,
            join_tree,
            base_table,
        )

    _kleene_guardrails(strategy_with_plans, dep_graph)

    positions = dep_graph.positions
    strategy = strategy_with_plans.strategy
    nodes = set(composition_nodes)
    columns_by_var = collect_referenced_columns(dep_graph)

    node_aliases = {
        node: _alias_for_view(node, "cmp", positions)
        for node in sorted(nodes, key=lambda view: view_sort_key(view, positions))
    }

    kleene_ctes: List[str] = []
    kleene_cte_name_by_node: Dict[View, str] = {}
    for node in sorted(nodes, key=lambda view: view_sort_key(view, positions)):
        if len(node) != 1:
            continue
        variable = next(iter(node))
        if not _is_kleene_variable(variable, dep_graph):
            continue
        if node not in strategy:
            raise NotImplementedError(
                "Implicit singleton base scans for Kleene variables are not supported in Path A yet"
            )
        cte_name, cte_sql = _kleene_dedup_cte(node, positions)
        kleene_cte_name_by_node[node] = cte_name
        kleene_ctes.append(cte_sql)

    node_sql: Dict[View, str] = {}
    for node in nodes:
        alias = node_aliases[node]
        if node in strategy:
            if len(node) > 1 and any(_is_kleene_variable(var, dep_graph) for var in node):
                raise NotImplementedError(
                    "Composition over multi-variable views containing Kleene variables is not supported in Path A yet"
                )
            if node in kleene_cte_name_by_node:
                node_sql[node] = f"{kleene_cte_name_by_node[node]} {alias}"
            else:
                node_sql[node] = f"{_view_table_name(node, positions)} {alias}"
            continue

        if len(node) != 1:
            raise ValueError("Implicit composition node must be singleton")

        variable = next(iter(node))
        if _is_kleene_variable(variable, dep_graph):
            raise NotImplementedError(
                "Implicit singleton base scans for Kleene variables are not supported in Path A yet"
            )
        node_sql[node] = _singleton_scan_sql(
            variable,
            base_table,
            alias,
            columns_by_var,
            dep_graph.independent_conditions.get(variable, []),
        )

    root = _composition_root_for_join_order(nodes, composition_plan, positions)
    visited = {root}

    from_clause = f"FROM {node_sql[root]}"
    join_clauses: List[str] = []

    bands_in_on = _dependent_predicates_in_on_enabled()
    pending_dependent_edges = [
        edge
        for edge in dep_graph.edges
        if edge.dependent_conditions
        and not _edge_internalized_in_nodes(nodes, edge.var_left, edge.var_right)
    ] if bands_in_on else []
    present_alias_by_var: Dict[str, str] = {}
    for var in sorted(root, key=lambda v: positions[v]):
        present_alias_by_var.setdefault(var, node_aliases[root])
    attached_on_conditions: Set[str] = set()

    for edge in composition_plan.join_order:
        left, right = edge
        if left in visited and right not in visited:
            anchor, new_node = left, right
        elif right in visited and left not in visited:
            anchor, new_node = right, left
        else:
            # Defensive fallback if ordering is malformed.
            anchor, new_node = left, right

        predicates = _structural_predicates(
            anchor,
            new_node,
            node_aliases[anchor],
            node_aliases[new_node],
            positions,
            dep_graph,
        )
        if bands_in_on:
            for var in sorted(new_node, key=lambda v: positions[v]):
                present_alias_by_var.setdefault(var, node_aliases[new_node])
            still_pending: List = []
            for dep_edge in pending_dependent_edges:
                if dep_edge.var_left in present_alias_by_var and dep_edge.var_right in present_alias_by_var:
                    for condition in dep_edge.dependent_conditions:
                        predicates.append(
                            render_expression_for_prefixed_columns(
                                condition, present_alias_by_var, dep_graph
                            )
                        )
                        attached_on_conditions.add(condition)
                else:
                    still_pending.append(dep_edge)
            pending_dependent_edges = still_pending
        on_sql = " AND ".join(predicates) if predicates else "1 = 1"
        join_clauses.append(f"JOIN {node_sql[new_node]} ON {on_sql}")

        visited.add(new_node)

    var_aliases = _variable_aliases_in_nodes(nodes, node_aliases, positions)
    var_to_alias = {
        var: aliases[0]
        for var, aliases in sorted(var_aliases.items(), key=lambda item: positions[item[0]])
    }

    measure_cols_by_var: Dict[str, set] = {}
    for _m in dep_graph.measures:
        measure_cols_by_var.setdefault(_m.variable, set()).add(_measure_cache_column(_m))
    select_columns: List[str] = []
    for var in dep_graph.variables:
        alias = var_to_alias[var]
        if _is_kleene_variable(var, dep_graph):
            key_cols = [f"{var}_first_id", f"{var}_first_ts", f"{var}_last_id", f"{var}_last_ts", f"{var}_count"]
        else:
            key_cols = [f"{var}_id", f"{var}_ts"]
        for col in key_cols:
            select_columns.append(f"{alias}.{col} AS {col}")
        # forward MEASURES value columns (FIRST/LAST(v.attr) / non-Kleene v.attr) that are not already a key
        for col in sorted(measure_cols_by_var.get(var, set()) - set(key_cols)):
            select_columns.append(f"{alias}.{col} AS {col}")

    lines = [
        "SELECT",
        "    " + ",\n    ".join(select_columns),
        from_clause,
    ]
    lines.extend(join_clauses)

    repeat_consistency_predicates = _repeat_var_consistency_predicates(var_aliases, dep_graph)
    global_sequence_predicates = _adjacent_sequence_predicates(
        _ordered_vars_for_sequence(dep_graph.variables, positions),
        var_to_alias,
        dep_graph,
    )
    if bands_in_on:
        # Attached conditions live in their JOIN ON; only never-attachable leftovers (none in practice)
        # fall through to WHERE.
        dependent_predicates = []
        for dep_edge in pending_dependent_edges:
            for condition in dep_edge.dependent_conditions:
                dependent_predicates.append(
                    render_expression_for_prefixed_columns(condition, var_to_alias, dep_graph)
                )
    else:
        dependent_predicates = _composition_dependent_predicates(nodes, var_aliases, dep_graph)
    all_where_predicates = list(
        dict.fromkeys(
            repeat_consistency_predicates
            + global_sequence_predicates
            + dependent_predicates
        )
    )
    if all_where_predicates:
        lines.append("WHERE " + "\n  AND ".join(all_where_predicates))

    lines.append(";")
    body_sql = "\n".join(lines)
    if not kleene_ctes:
        return body_sql

    return "WITH\n" + ",\n".join(kleene_ctes) + "\n" + body_sql


def one_per_anchor_dedup_key(dep_graph_or_variables):
    """the ONE-ROW-PER-MATCH dedup key that makes a materialized result equal native MATCH_RECOGNIZE.
    Returns (partition_col, [(order_col, direction), ...]): partition_col is the anchor's id (first_id
    for a Kleene anchor); order terms are each non-anchor var's ts (first_ts for Kleene), DESC for a
    greedy predecessor and ASC for a reluctant one. Used by both the post-filter and the streaming
    benchmark's global dedup, so the criterion is identical everywhere."""
    if isinstance(dep_graph_or_variables, DependencyGraph):
        dep_graph = dep_graph_or_variables
        variables = dep_graph.variables
        reluctant_predecessors = _reluctant_predecessor_variables(dep_graph)
    else:
        dep_graph = None
        variables = list(dep_graph_or_variables)
        reluctant_predecessors = set()

    anchor = variables[0]
    part = f"{anchor}_first_id" if dep_graph is not None and _is_kleene_variable(anchor, dep_graph) else f"{anchor}_id"
    terms = []
    for var in variables[1:]:
        ts_col = f"{var}_first_ts" if dep_graph is not None and _is_kleene_variable(var, dep_graph) else f"{var}_ts"
        terms.append((ts_col, "ASC" if var in reluctant_predecessors else "DESC"))
    if not terms:
        fb = f"{anchor}_first_ts" if dep_graph is not None and _is_kleene_variable(anchor, dep_graph) else f"{anchor}_ts"
        terms = [(fb, "DESC")]
    return part, terms


def _measure_cache_column(measure) -> str:
    """the view column that carries a MEASURES value. Kleene run-cache: COUNT->{v}_count,
    FIRST(v.a)->{v}_first_{a}, LAST(v.a)->{v}_last_{a}. Non-Kleene VALUE(v.a)->{v}_{a}. The parser
    guarantees this scoping (VALUE only for non-Kleene; FIRST/LAST/COUNT only for Kleene)."""
    v, attr, kind = measure.variable, measure.attribute, measure.kind
    if kind=="COUNT":
        return f"{v}_count"
    if kind == "FIRST":
        return f"{v}_first_{attr}"
    if kind == "LAST":
        return f"{v}_last_{attr}"
    if kind == "VALUE":
        return f"{v}_{attr}"
    raise NotImplementedError(f"Unsupported MEASURES kind {kind!r} for {v}.{attr}")


def _assert_kleene_measures_run_capturing(dep_graph: DependencyGraph) -> None:
    """a Kleene COUNT/FIRST/LAST reflects the MAXIMAL contiguous run (run-cache is PATTERN(v+)), which
    equals native MATCH_RECOGNIZE only when the query is RUN-CAPTURING: the wildcard gap immediately
    before the Kleene variable is RELUCTANT (Z*?), or the variable is pattern-initial. A greedy gap
    (Z*) before a measured Kleene variable is run-eating (COUNT collapses to 1) and cannot be modeled,
    so fail loudly rather than emit a silently-wrong COUNT."""
    gap_before = {gap.right_variable: gap.mode for gap in dep_graph.wildcard_gaps}
    for measure in dep_graph.measures:
        if measure.kind in ("COUNT", "FIRST", "LAST") and _is_kleene_variable(measure.variable, dep_graph):
            var = measure.variable
            if dep_graph.quantifiers.get(var) == "RELUCTANT_PLUS":
                raise NotImplementedError(
                    f"Kleene aggregate MEASURES over reluctant Kleene {var!r} (v+?) are not modeled: EIMER's "
                    f"run-cache captures the MAXIMAL run (v+), which equals the minimal-run reluctant semantics "
                    f"only at length 1. Use a greedy Kleene (v+). See docs/measures_schema_min.md."
                )
            mode = gap_before.get(var)
            if mode is not None and mode != "RELUCTANT":
                raise NotImplementedError(
                    f"Kleene aggregate MEASURES over {var!r} require a run-capturing pattern: a reluctant gap "
                    f"'Z*?' before {var} (or {var} pattern-initial). The greedy gap 'Z*' before it is "
                    f"run-eating (COUNT collapses to 1) and cannot be modeled. See docs/measures_schema_min.md."
                )


def _post_filter_key_columns(dep_graph: DependencyGraph) -> List[str]:
    """the internal identity/ts/count key columns the compose produces per variable. Kept alongside the
    MEASURES projection so consumers that read the internal names (the streaming benchmark's global
    dedup projects {v}_id/{v}_ts, never a measure alias) still work."""
    cols: List[str] = []
    for var in dep_graph.variables:
        if _is_kleene_variable(var, dep_graph):
            cols += [f"{var}_first_id", f"{var}_first_ts", f"{var}_last_id", f"{var}_last_ts", f"{var}_count"]
        else:
            cols += [f"{var}_id", f"{var}_ts"]
    return cols


def measure_projection_columns(dep_graph_or_variables: DependencyGraph | Sequence[str]) -> List[str]:
    """the result projection for the MEASURES clause: ,,<view_column> AS <alias>'' per measure, in
    MEASURES order, matching native MATCH_RECOGNIZE's output columns. Empty when there are no measures
    (or the caller passed a bare variable sequence), keeping the SELECT * (id/ts keys) contract."""
    if not isinstance(dep_graph_or_variables, DependencyGraph) or not dep_graph_or_variables.measures:
        return []
    _assert_kleene_measures_run_capturing(dep_graph_or_variables)
    return [f"{_measure_cache_column(m)} AS {m.alias}" for m in dep_graph_or_variables.measures]


def generate_post_filter_sql(composition_sql: str, dep_graph_or_variables: DependencyGraph | Sequence[str]) -> str:
    composition_inner = composition_sql.strip()
    if composition_inner.endswith(";"):
        composition_inner = composition_inner[:-1]
    anchor_partition_col, terms = one_per_anchor_dedup_key(dep_graph_or_variables)
    order_clause = ", ".join(f"{col} {direction}" for col, direction in terms)
    # MEASURES projection (== native MR output columns) when present, else SELECT * (id/ts keys).
    # Alongside the measure aliases we keep the internal id/ts/count key columns so the streaming
    # benchmark's global dedup still works; a measure aliased to a key name is skipped to avoid a
    # duplicate output column.
    measure_cols = measure_projection_columns(dep_graph_or_variables)
    if measure_cols:
        # case-insensitive skip (SQL engines fold unquoted identifiers): a key column whose name folds
        # to a measure alias (e.g. key A_id vs alias a_id) must NOT be re-projected, else ambiguous column.
        measure_aliases = {m.alias.lower() for m in dep_graph_or_variables.measures}
        key_cols = [f"{c} AS {c}" for c in _post_filter_key_columns(dep_graph_or_variables)
                    if c.lower() not in measure_aliases]
        outer_select = "SELECT " + ", ".join(measure_cols + key_cols)
    else:
        outer_select = "SELECT *"
    return "\n".join(
        [
            outer_select,
            "FROM (",
            "    SELECT composed.*,",
            f"           ROW_NUMBER() OVER (PARTITION BY {anchor_partition_col} ORDER BY {order_clause}) AS rn",
            "    FROM (",
            "        " + composition_inner.replace("\n", "\n        "),
            "    ) composed",
            ") ranked",
            "WHERE rn = 1;",
        ]
    )


def _statements_for_dialect(statements: SQLStatements, dialect: SqlDialect) -> SQLStatements:
    """Apply the dialect's emit-time SQL rewrites. TRINO returns the input unchanged."""
    if dialect is SqlDialect.TRINO:
        return statements
    return SQLStatements(
        update_statements=[rewrite_sql(s, dialect) for s in statements.update_statements],
        composition_sql=rewrite_sql(statements.composition_sql, dialect),
        post_filter_sql=rewrite_sql(statements.post_filter_sql, dialect),
    )


def generate_sql_statements_for_cover(strategy_with_plans: StrategyWithPlans, dep_graph: DependencyGraph,
                                      composition_nodes: Iterable[View], composition_plan: CompositionPlan,
                                      update_plan: UpdatePlan, base_table: str = "events",
                                      batch_table: str = "events_batch", *,
                                      dialect: SqlDialect = SqlDialect.TRINO) -> SQLStatements:
    """render SQL for a maintained strategy with an explicit composition cover. ,,dialect'' selects the
    target SQL dialect: TRINO is the default and only shipped dialect; any other fails loud in the
    backend registry."""

    update_statements = generate_update_sql_statements(
        strategy_with_plans,
        dep_graph,
        update_plan,
        base_table,
        batch_table,
    )

    composition_sql = generate_composition_sql_for_nodes(
        strategy_with_plans,
        dep_graph,
        composition_nodes,
        composition_plan,
        base_table,
    )

    post_filter_sql = generate_post_filter_sql(composition_sql, dep_graph)

    return _statements_for_dialect(
        SQLStatements(
            update_statements=update_statements,
            composition_sql=composition_sql,
            post_filter_sql=post_filter_sql,
        ),
        dialect,
    )


def generate_sql_statements(strategy_with_plans: StrategyWithPlans, dep_graph: DependencyGraph,
                            composition_plan: CompositionPlan, update_plan: UpdatePlan,
                            base_table: str = "events", batch_table: str = "events_batch", *,
                            dialect: SqlDialect = SqlDialect.TRINO) -> SQLStatements:
    return generate_sql_statements_for_cover(
        strategy_with_plans,
        dep_graph,
        strategy_with_plans.effective_view_set,
        composition_plan,
        update_plan,
        base_table,
        batch_table,
        dialect=dialect,
    )


def generate_sql_statements_from_evaluation_plan(plan: "EvaluationPlan", *,
                                                 dialect: SqlDialect = SqlDialect.TRINO) -> SQLStatements:
    update_statements: List[str] = [update_op.sql for update_op in plan.update_ops]

    return _statements_for_dialect(
        SQLStatements(
            update_statements=update_statements,
            composition_sql=plan.composition.sql,
            post_filter_sql=plan.post_filter.sql,
        ),
        dialect,
    )
