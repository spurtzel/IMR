"""lowering from a maintained strategy and composition cover to an ,,EvaluationPlan''.

builds the update, composition, and post-filter operators (and their SQL) for a plan,
then hashes the canonicalized result into a stable ,,plan_id''. all execution-plan paths
converge on ,,build_evaluation_plan_for_cover''.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
from hashlib import sha1
from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple

from eimer.models import (
    CompositionPlan,
    CompositionVariant,
    DependencyGraph,
    JoinTreeNode,
    JoinType,
    StrategyWithPlans,
    UpdateMode,
    UpdatePlan,
    View,
    canonical_edge_key,
    view_name,
    view_sort_key,
)
from eimer.plans.composition import determine_join_type
from eimer.sql.sql_render import render_expression_for_prefixed_columns
from eimer.query.graph_adapter import collect_referenced_columns
from eimer.sql.sql_emitter import (
    _adjacent_sequence_predicates,
    _alias_for_view,
    _canonical_source_join_sequence,
    _collect_internalized_conditions,
    _dependent_predicates_in_on_enabled,
    _composition_dependent_predicates,
    _emit_source_tree_node,
    _is_kleene_variable,
    _kleene_dedup_cte,
    _kleene_guardrails,
    _composition_root_for_join_order,
    _mr_group_detect_columns,
    _mr_group_detect_update_sql,
    _ordered_vars,
    _ordered_vars_for_sequence,
    _render_deferred_condition_for_edge,
    _repeat_var_consistency_predicates,
    _reluctant_predecessor_variables,
    _singleton_scan_sql,
    _structural_predicates,
    _variable_aliases_in_nodes,
    _view_table_name,
    generate_composition_sql_for_nodes,
    generate_post_filter_sql,
)


class FiringPatternKind(str, Enum):
    ALWAYS = "always"
    NEVER = "never"
    EVERY_K = "every_k"
    AT_BATCHES = "at_batches"
    WHEN_SIZE_EXCEEDS = "when_size_exceeds"


@dataclass(frozen=True)
class FiringPattern:
    kind: FiringPatternKind
    period: int | None = None
    offset: int = 0
    batches: Tuple[int, ...] = ()
    threshold: int | None = None
    view: View | None = None

    def __post_init__(self) -> None:
        if self.kind == FiringPatternKind.ALWAYS:
            return
        if self.kind == FiringPatternKind.NEVER:
            return
        if self.kind == FiringPatternKind.EVERY_K:
            if self.period is None or self.period <= 0:
                raise ValueError("every_k firing pattern requires a positive period")
            return
        if self.kind == FiringPatternKind.AT_BATCHES:
            if any(batch < 0 for batch in self.batches):
                raise ValueError("at_batches firing pattern cannot contain negative batches")
            return
        if self.kind == FiringPatternKind.WHEN_SIZE_EXCEEDS:
            if self.threshold is None or self.threshold < 0:
                raise ValueError("when_size_exceeds firing pattern requires a non-negative threshold")
            if self.view is None:
                raise ValueError("when_size_exceeds firing pattern requires a target view")
            return
        raise ValueError(f"Unsupported firing pattern kind: {self.kind}")

    @classmethod
    def always(cls) -> "FiringPattern":
        return cls(kind=FiringPatternKind.ALWAYS)

    @classmethod
    def never(cls) -> "FiringPattern":
        return cls(kind=FiringPatternKind.NEVER)

    @classmethod
    def every_k(cls, k: int, offset: int = 0) -> "FiringPattern":
        return cls(kind=FiringPatternKind.EVERY_K, period=k, offset=offset)

    @classmethod
    def at_batches(cls, batches: Iterable[int]) -> "FiringPattern":
        return cls(kind=FiringPatternKind.AT_BATCHES, batches=tuple(sorted(set(batches))))

    @classmethod
    def when_size_exceeds(cls, view: View, threshold: int) -> "FiringPattern":
        return cls(kind=FiringPatternKind.WHEN_SIZE_EXCEEDS, view=view, threshold=threshold)


def _state_tape_keys_for_view(view: View) -> Tuple[object, ...]:
    return (
        view,
        tuple(sorted(view)),
        "_".join(sorted(view)),
    )


def fires_at(pattern: FiringPattern, batch_index: int, state_tape: Mapping[object, int] | None = None) -> bool:
    if pattern.kind == FiringPatternKind.ALWAYS:
        return True
    if pattern.kind == FiringPatternKind.NEVER:
        return False
    if pattern.kind == FiringPatternKind.EVERY_K:
        assert pattern.period is not None
        if batch_index < pattern.offset:
            return False
        return (batch_index - pattern.offset) % pattern.period == 0
    if pattern.kind == FiringPatternKind.AT_BATCHES:
        return batch_index in pattern.batches
    if pattern.kind == FiringPatternKind.WHEN_SIZE_EXCEEDS:
        if state_tape is None or pattern.view is None or pattern.threshold is None:
            return False
        for key in _state_tape_keys_for_view(pattern.view):
            if key in state_tape:
                return state_tape[key] > pattern.threshold
        return False
    raise ValueError(f"Unsupported firing pattern kind: {pattern.kind}")




class SourceRole(str, Enum):
    BASE_HISTORY = "BASE_HISTORY"
    CURRENT_BATCH = "CURRENT_BATCH"
    CACHE_FULL = "CACHE_FULL"
    CACHE_DELTA = "CACHE_DELTA"
    UNKNOWN = "UNKNOWN"


class SourceStateVersion(str, Enum):
    BEFORE_BATCH = "BEFORE_BATCH"
    AFTER_EARLIER_UPDATES = "AFTER_EARLIER_UPDATES"
    CURRENT_DELTA = "CURRENT_DELTA"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SourceBlock:
    kind: str
    variables: View
    alias: str
    table_name: str
    source_view: View | None
    sql: str
    operator_id: str = ""
    source_role: SourceRole = SourceRole.UNKNOWN
    state_version: SourceStateVersion = SourceStateVersion.UNKNOWN


@dataclass(frozen=True)
class JoinOp:
    op_id: str
    anchor_view: View
    new_view: View
    structural_predicates: Tuple[str, ...]
    join_type: JoinType = JoinType.INEQ
    deferred_predicates: Tuple[str, ...] = ()
    operator_id: str = ""




@dataclass(frozen=True)
class UpdateOp:
    op_id: str
    target_view: View
    target_table: str
    selected_sources: Tuple[View, ...]
    effective_sources: Tuple[View, ...]
    source_blocks: Tuple[SourceBlock, ...]
    join_ops: Tuple[JoinOp, ...]
    where_predicates: Tuple[str, ...]
    select_columns: Tuple[str, ...]
    last_variable_source: View
    sql: str
    update_mode: UpdateMode = UpdateMode.JOIN
    operator_id: str = ""


@dataclass(frozen=True)
class CompositionNode:
    view: View
    alias: str
    kind: str
    table_name: str
    sql: str
    operator_id: str = ""


@dataclass(frozen=True)
class CompositionOp:
    nodes: Tuple[CompositionNode, ...]
    join_ops: Tuple[JoinOp, ...]
    select_columns: Tuple[str, ...]
    where_predicates: Tuple[str, ...]
    sql: str
    operator_id: str = "compose"


@dataclass(frozen=True)
class OrderKey:
    column: str
    direction: str


@dataclass(frozen=True)
class PostFilterOp:
    op_id: str
    partition_key: str
    order_by: Tuple[OrderKey, ...]
    sql: str
    operator_id: str = "post_filter"


@dataclass(frozen=True)
class EvaluationPlan:
    plan_id: str
    strategy: Tuple[View, ...]
    effective_view_set: Tuple[View, ...]
    composition_plan_idx: int
    update_plan_idx: int
    base_table: str
    batch_table: str
    update_ops: Tuple[UpdateOp, ...]
    composition: CompositionOp
    post_filter: PostFilterOp
    maintained_view_set: Tuple[View, ...] = ()
    composition_cover: Tuple[View, ...] = ()
    composition_cover_idx: int | None = None
    composition_plan_idx_within_cover: int | None = None

    def to_dict(self) -> Dict[str, Any]:
        return _canonicalize(self, exclude_plan_id=False)


def _view_label(view: View, positions: Dict[str, int]) -> str:
    return view_name(view, positions)


def _canonicalize(value: Any, *, exclude_plan_id: bool) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        plan_id_metadata_fields = {"operator_id", "source_role", "state_version"}
        payload: Dict[str, Any] = {}
        for field in fields(value):
            if exclude_plan_id and field.name=="plan_id":
                continue
            if exclude_plan_id and field.name in plan_id_metadata_fields:
                continue
            payload[field.name] = _canonicalize(getattr(value, field.name), exclude_plan_id=exclude_plan_id)
        return payload
    if isinstance(value, dict):
        items = []
        for key, item in value.items():
            rendered_key = _canonicalize(key, exclude_plan_id=exclude_plan_id)
            items.append(
                (
                    json.dumps(rendered_key, sort_keys=True),
                    rendered_key,
                    _canonicalize(item, exclude_plan_id=exclude_plan_id),
                )
            )
        items.sort(key=lambda entry: entry[0])
        return [
            {"key": entry[1], "value": entry[2]}
            for entry in items
        ]
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item, exclude_plan_id=exclude_plan_id) for item in value]
    if isinstance(value, frozenset):
        rendered = [_canonicalize(item, exclude_plan_id=exclude_plan_id) for item in value]
        rendered.sort(key=lambda item: json.dumps(item, sort_keys=True))
        return rendered
    return value


def _compute_plan_id(plan: EvaluationPlan) -> str:
    payload = _canonicalize(plan, exclude_plan_id=True)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha1(encoded.encode("utf-8")).hexdigest()








def _build_update_tree_join_ops(
    source_tree: JoinTreeNode,
    target_label: str,
    source_aliases: Dict[View, str],
    dep_graph: DependencyGraph,
) -> Tuple[JoinOp, ...]:
    """tree-order ,,join_ops'' for a bushy UPDATE source tree, one ,,JoinOp'' per inner node.

    operands are merged source variable-sets (anchor is the left subtree, new the right), emitted
    children before parents. only cross-side ts-ordering structural predicates are carried, so
    ,,deferred_predicates'' stay empty and ,,join_type'' is ,,INEQ''; the SQL FROM is rendered
    separately by ,,_emit_source_tree_node''."""
    positions = dep_graph.positions
    join_ops: list[JoinOp] = []

    def walk(tree: JoinTreeNode) -> View:
        if tree.node is not None:
            return tree.node

        anchor_view = walk(tree.left)
        new_view = walk(tree.right)
        predicates = tuple(
            _structural_predicates(
                anchor_view,
                new_view,
                _alias_for_view(anchor_view, "src", positions),
                _alias_for_view(new_view, "src", positions),
                positions,
                dep_graph,
            )
        )
        join_ops.append(
            JoinOp(
                op_id=f"join:{_view_label(anchor_view, positions)}__{_view_label(new_view, positions)}",
                anchor_view=anchor_view,
                new_view=new_view,
                structural_predicates=predicates,
                join_type=JoinType.INEQ,
                operator_id=f"update:{target_label}:join:{len(join_ops) + 1}",
            )
        )
        return anchor_view | new_view

    walk(source_tree)
    return tuple(join_ops)


def _build_update_sql_parts(
    target_view: View,
    update_plan: UpdatePlan,
    dep_graph: DependencyGraph,
    base_table: str,
    batch_table: str,
) -> tuple[
    Tuple[View, ...],
    Tuple[View, ...],
    Tuple[SourceBlock, ...],
    Tuple[JoinOp, ...],
    Tuple[str, ...],
    Tuple[str, ...],
    str,
]:
    positions = dep_graph.positions
    target_label = _view_label(target_view, positions)
    columns_by_var = collect_referenced_columns(dep_graph)
    selected_sources = update_plan.source_selections[target_view]
    effective_sources = update_plan.effective_sources[target_view]
    last_source = update_plan.last_variable_source[target_view]
    update_mode = update_plan.update_mode_by_view.get(target_view, UpdateMode.JOIN)

    ordered_effective_sources = tuple(
        sorted(effective_sources, key=lambda view: view_sort_key(view, positions))
    )
    ordered_selected_sources = tuple(
        sorted(selected_sources, key=lambda view: view_sort_key(view, positions))
    )

    if update_mode == UpdateMode.MR_GROUP_DETECT:
        if len(target_view) != 1:
            raise ValueError("MR_GROUP_DETECT mode requires singleton target view")
        variable = next(iter(target_view))
        if not _is_kleene_variable(variable, dep_graph):
            raise ValueError("MR_GROUP_DETECT mode requires a Kleene variable")
        insert_cols, _ = _mr_group_detect_columns(variable, columns_by_var)
        return (
            ordered_selected_sources,
            ordered_effective_sources,
            (),
            (),
            (),
            tuple(insert_cols),
            _mr_group_detect_update_sql(
                variable,
                dep_graph,
                columns_by_var,
                base_table,
                batch_table,
            ),
        )

    if any(_is_kleene_variable(var, dep_graph) for var in target_view):
        raise NotImplementedError(
            "JOIN-mode updates for target views containing Kleene variables are not supported in Path A yet"
        )

    source_aliases = {
        source: _alias_for_view(source, "src", positions)
        for source in ordered_effective_sources
    }

    source_sql: Dict[View, str] = {}
    source_blocks: list[SourceBlock] = []
    for source in ordered_effective_sources:
        alias = source_aliases[source]
        if source in selected_sources:
            sql = f"{_view_table_name(source, positions)} {alias}"
            source_sql[source] = sql
            source_blocks.append(
                SourceBlock(
                    kind="cache",
                    variables=source,
                    alias=alias,
                    table_name=_view_table_name(source, positions),
                    source_view=source,
                    sql=sql,
                    operator_id=f"update:{target_label}:source:{_view_label(source, positions)}",
                    source_role=SourceRole.CACHE_FULL,
                    state_version=SourceStateVersion.AFTER_EARLIER_UPDATES,
                )
            )
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
        source_role = SourceRole.CURRENT_BATCH if is_last_source else SourceRole.BASE_HISTORY
        state_version = (
            SourceStateVersion.CURRENT_DELTA
            if is_last_source
            else SourceStateVersion.NOT_APPLICABLE
        )
        sql = _singleton_scan_sql(
            variable,
            table_name,
            alias,
            columns_by_var,
            dep_graph.independent_conditions.get(variable, []),
        )
        source_sql[source] = sql
        source_blocks.append(
            SourceBlock(
                kind="base_scan",
                variables=source,
                alias=alias,
                table_name=table_name,
                source_view=None,
                sql=sql,
                operator_id=f"update:{target_label}:source:{_view_label(source, positions)}",
                source_role=source_role,
                state_version=state_version,
            )
        )

    source_tree = (
        update_plan.source_join_trees.get(target_view)
        if update_plan.source_join_trees is not None
        else None
    )

    if source_tree is not None:
        # bushy source tree: nested-subquery FROM; the dependent value predicates and the adjacent
        # ts-sequence stay in the WHERE below, and join_ops carry the tree order as metadata.
        if set(source_tree.views) != set(effective_sources):
            raise ValueError("source_join_trees leaves must equal the build's effective sources")
        root_sql, _root_alias, _merged_view, var_to_alias = _emit_source_tree_node(
            source_tree, source_sql, source_aliases, dep_graph, columns_by_var, [0]
        )
        from_clause = f"FROM {root_sql}"
        join_clauses: list[str] = []
        join_ops: list[JoinOp] = list(
            _build_update_tree_join_ops(source_tree, target_label, source_aliases, dep_graph)
        )
        attached_on_conditions: set[str] = set()
    else:
        root, join_sequence = _canonical_source_join_sequence(effective_sources, positions)

        # bands-in-ON (default on): attach each spanning dependent value predicate at the earliest
        # JOIN ON where both of its variables are present.
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
        join_ops = []
        for anchor, new_source in join_sequence:
            structural = tuple(
                _structural_predicates(
                    anchor,
                    new_source,
                    source_aliases[anchor],
                    source_aliases[new_source],
                    positions,
                    dep_graph,
                )
            )
            predicates = list(structural)
            if bands_in_on:
                for var in sorted(new_source, key=lambda v: positions[v]):
                    present_alias_by_var.setdefault(var, source_aliases[new_source])
                still_pending = []
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
            join_clauses.append(f"JOIN {source_sql[new_source]} ON {on_sql}")
            join_ops.append(
                JoinOp(
                    op_id=f"join:{_view_label(anchor, positions)}__{_view_label(new_source, positions)}",
                    anchor_view=anchor,
                    new_view=new_source,
                    structural_predicates=structural,
                    join_type=JoinType.INEQ,
                    operator_id=f"update:{target_label}:join:{len(join_ops) + 1}",
                )
            )

        var_to_alias: Dict[str, str] = {}
        for source, alias in source_aliases.items():
            for var in source:
                var_to_alias[var] = alias

    where_parts: list[str] = []
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

    dedup_where = tuple(dict.fromkeys(where_parts))

    select_columns: list[str] = []
    for var in _ordered_vars(target_view, positions):
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

    return (
        ordered_selected_sources,
        ordered_effective_sources,
        tuple(source_blocks),
        tuple(join_ops),
        dedup_where,
        tuple(select_columns),
        "\n".join(sql_lines),
    )


def _build_tree_join_ops(
    join_tree: JoinTreeNode,
    composition_plan: CompositionPlan,
    dep_graph: DependencyGraph,
) -> Tuple[JoinOp, ...]:
    """encode a bushy ,,join_tree'' as ,,join_ops'' in tree topological order (children before
    parents), one ,,JoinOp'' per inner tree node.

    a node's operands are merged variable-sets (,,anchor_view'' the left subtree's leaf variables,
    ,,new_view'' the right subtree's). ,,join_type'' is EQUI when the two sides share a variable, else
    INEQ; ,,deferred_predicates'' are the rendered ,,deferred_conditions'' whose endpoint leaves span
    the node. the SQL comes from the tree renderer, not from here."""
    positions = dep_graph.positions
    join_ops: list[JoinOp] = []

    def merged_alias(merged_view: View) -> str:
        return _alias_for_view(merged_view, "cmp", positions)

    def walk(tree: JoinTreeNode) -> tuple[View, frozenset[View]]:
        if tree.node is not None:
            return tree.node, frozenset({tree.node})

        anchor_view, left_leaves = walk(tree.left)
        new_view, right_leaves = walk(tree.right)
        anchor_alias = merged_alias(anchor_view)
        new_alias = merged_alias(new_view)

        predicates = tuple(
            _structural_predicates(
                anchor_view,
                new_view,
                anchor_alias,
                new_alias,
                positions,
                dep_graph,
            )
        )

        rendered_deferred: list[str] = []
        for edge, deferred_conditions in composition_plan.deferred_conditions.items():
            left_leaf, right_leaf = edge
            spans = (left_leaf in left_leaves and right_leaf in right_leaves) or (
                left_leaf in right_leaves and right_leaf in left_leaves
            )
            if not spans:
                continue
            canonical = canonical_edge_key(anchor_view, new_view, positions)
            canonical_alias = {anchor_view: anchor_alias, new_view: new_alias}
            for deferred in deferred_conditions:
                rendered_deferred.append(
                    _render_deferred_condition_for_edge(
                        deferred.expression,
                        canonical[0],
                        canonical[1],
                        canonical_alias[canonical[0]],
                        canonical_alias[canonical[1]],
                        dep_graph,
                    )
                )

        join_ops.append(
            JoinOp(
                op_id=f"join:{_view_label(anchor_view, positions)}__{_view_label(new_view, positions)}",
                anchor_view=anchor_view,
                new_view=new_view,
                structural_predicates=predicates,
                join_type=determine_join_type(anchor_view, new_view, positions),
                deferred_predicates=tuple(rendered_deferred),
                operator_id=f"compose:join:{len(join_ops) + 1}",
            )
        )
        return anchor_view | new_view, left_leaves | right_leaves

    walk(join_tree)
    return tuple(join_ops)


def _build_composition_op(
    strategy_with_plans: StrategyWithPlans,
    dep_graph: DependencyGraph,
    composition_nodes: Iterable[View],
    composition_plan: CompositionPlan,
    base_table: str,
) -> CompositionOp:
    _kleene_guardrails(strategy_with_plans, dep_graph)

    positions = dep_graph.positions
    strategy = strategy_with_plans.strategy
    nodes = set(composition_nodes)
    columns_by_var = collect_referenced_columns(dep_graph)

    ordered_nodes = tuple(sorted(nodes, key=lambda view: view_sort_key(view, positions)))
    node_aliases = {
        node: _alias_for_view(node, "cmp", positions)
        for node in ordered_nodes
    }

    kleene_cte_name_by_node: Dict[View, str] = {}
    for node in ordered_nodes:
        if len(node) != 1:
            continue
        variable = next(iter(node))
        if not _is_kleene_variable(variable, dep_graph):
            continue
        if node not in strategy:
            raise NotImplementedError(
                "Implicit singleton base scans for Kleene variables are not supported in Path A yet"
            )
        cte_name, _ = _kleene_dedup_cte(node, positions)
        kleene_cte_name_by_node[node] = cte_name

    node_sql: Dict[View, str] = {}
    composition_nodes: list[CompositionNode] = []
    for node in ordered_nodes:
        alias = node_aliases[node]
        if node in strategy:
            if len(node) > 1 and any(_is_kleene_variable(var, dep_graph) for var in node):
                raise NotImplementedError(
                    "Composition over multi-variable views containing Kleene variables is not supported in Path A yet"
                )
            table_name = _view_table_name(node, positions)
            sql = f"{kleene_cte_name_by_node.get(node, table_name)} {alias}"
            node_sql[node] = sql
            composition_nodes.append(
                CompositionNode(
                    view=node,
                    alias=alias,
                    kind="cache",
                    table_name=table_name,
                    sql=sql,
                    operator_id=f"compose:source:{_view_label(node, positions)}",
                )
            )
            continue

        if len(node) != 1:
            raise ValueError("Implicit composition node must be singleton")

        variable = next(iter(node))
        if _is_kleene_variable(variable, dep_graph):
            raise NotImplementedError(
                "Implicit singleton base scans for Kleene variables are not supported in Path A yet"
            )
        sql = _singleton_scan_sql(
            variable,
            base_table,
            alias,
            columns_by_var,
            dep_graph.independent_conditions.get(variable, []),
        )
        node_sql[node] = sql
        composition_nodes.append(
            CompositionNode(
                view=node,
                alias=alias,
                kind="base_scan",
                table_name=base_table,
                sql=sql,
                operator_id=f"compose:source:{_view_label(node, positions)}",
            )
        )

    if composition_plan.join_tree is not None:
        # bushy dispatch: operands are merged variable-sets in tree topological order; the
        # left-deep block below only runs for join_tree=None plans.
        join_ops: list[JoinOp] = list(
            _build_tree_join_ops(composition_plan.join_tree, composition_plan, dep_graph)
        )
    else:
        root = _composition_root_for_join_order(nodes, composition_plan, positions)
        visited = {root}

        join_ops = []
        deferred_predicates: list[str] = []
        for edge in composition_plan.join_order:
            left, right = edge
            if left in visited and right not in visited:
                anchor, new_node = left, right
            elif right in visited and left not in visited:
                anchor, new_node = right, left
            else:
                anchor, new_node = left, right

            predicates = tuple(
                _structural_predicates(
                    anchor,
                    new_node,
                    node_aliases[anchor],
                    node_aliases[new_node],
                    positions,
                    dep_graph,
                )
            )
            canonical = canonical_edge_key(anchor, new_node, positions)
            rendered_deferred = tuple(
                _render_deferred_condition_for_edge(
                    deferred.expression,
                    canonical[0],
                    canonical[1],
                    node_aliases[canonical[0]],
                    node_aliases[canonical[1]],
                    dep_graph,
                )
                for deferred in composition_plan.deferred_conditions.get(canonical, [])
            )
            deferred_predicates.extend(rendered_deferred)
            join_ops.append(
                JoinOp(
                    op_id=f"join:{_view_label(anchor, positions)}__{_view_label(new_node, positions)}",
                    anchor_view=anchor,
                    new_view=new_node,
                    structural_predicates=predicates,
                    join_type=composition_plan.join_types[canonical],
                    deferred_predicates=rendered_deferred,
                    operator_id=f"compose:join:{len(join_ops) + 1}",
                )
            )
            visited.add(new_node)

    var_aliases = _variable_aliases_in_nodes(nodes, node_aliases, positions)
    var_to_alias = {
        var: aliases[0]
        for var, aliases in sorted(var_aliases.items(), key=lambda item: positions[item[0]])
    }

    select_columns: list[str] = []
    for var in dep_graph.variables:
        alias = var_to_alias[var]
        if _is_kleene_variable(var, dep_graph):
            select_columns.append(f"{alias}.{var}_first_id AS {var}_first_id")
            select_columns.append(f"{alias}.{var}_first_ts AS {var}_first_ts")
            select_columns.append(f"{alias}.{var}_last_id AS {var}_last_id")
            select_columns.append(f"{alias}.{var}_last_ts AS {var}_last_ts")
            select_columns.append(f"{alias}.{var}_count AS {var}_count")
        else:
            select_columns.append(f"{alias}.{var}_id AS {var}_id")
            select_columns.append(f"{alias}.{var}_ts AS {var}_ts")

    repeat_consistency_predicates = _repeat_var_consistency_predicates(var_aliases, dep_graph)
    global_sequence_predicates = _adjacent_sequence_predicates(
        _ordered_vars_for_sequence(dep_graph.variables, positions),
        var_to_alias,
        dep_graph,
    )
    dependent_predicates = _composition_dependent_predicates(nodes, var_aliases, dep_graph)
    where_predicates = tuple(
        dict.fromkeys(
            repeat_consistency_predicates
            + global_sequence_predicates
            + dependent_predicates
        )
    )

    sql = generate_composition_sql_for_nodes(
        strategy_with_plans,
        dep_graph,
        nodes,
        composition_plan,
        base_table,
        join_tree=composition_plan.join_tree,
    )

    return CompositionOp(
        nodes=tuple(composition_nodes),
        join_ops=tuple(join_ops),
        select_columns=tuple(select_columns),
        where_predicates=where_predicates,
        sql=sql,
        operator_id="compose",
    )


def _build_post_filter_op(dep_graph: DependencyGraph, composition_sql: str) -> PostFilterOp:
    anchor = dep_graph.variables[0]
    partition_key = f"{anchor}_first_id" if _is_kleene_variable(anchor, dep_graph) else f"{anchor}_id"
    reluctant_predecessors = _reluctant_predecessor_variables(dep_graph)
    order_by = tuple(
        OrderKey(
            column=f"{var}_first_ts" if _is_kleene_variable(var, dep_graph) else f"{var}_ts",
            direction="ASC" if var in reluctant_predecessors else "DESC",
        )
        for var in dep_graph.variables[1:]
    )
    if not order_by:
        order_by = (
            OrderKey(
                column=f"{anchor}_first_ts" if _is_kleene_variable(anchor, dep_graph) else f"{anchor}_ts",
                direction="DESC",
            ),
        )
    return PostFilterOp(
        op_id="post_filter",
        partition_key=partition_key,
        order_by=order_by,
        sql=generate_post_filter_sql(composition_sql, dep_graph),
        operator_id="post_filter",
    )


def build_evaluation_plan(
    dep_graph: DependencyGraph,
    strategy_with_plans: StrategyWithPlans,
    composition_plan_idx: int,
    update_plan_idx: int,
    base_table: str = "events",
    batch_table: str = "events_batch",
) -> EvaluationPlan:
    """build an ,,EvaluationPlan'' from index-based ,,composition_plan_idx''/,,update_plan_idx'' descriptors.

    delegates to the core lowering ,,build_evaluation_plan_for_cover'' (below), through which all
    lowering flows.
    """

    if composition_plan_idx < 0 or composition_plan_idx >= len(strategy_with_plans.composition_plans):
        raise IndexError("composition_plan_idx out of range")
    if update_plan_idx < 0 or update_plan_idx >= len(strategy_with_plans.update_plans):
        raise IndexError("update_plan_idx out of range")

    return build_evaluation_plan_for_cover(
        dep_graph=dep_graph,
        strategy_with_plans=strategy_with_plans,
        composition_nodes=strategy_with_plans.effective_view_set,
        composition_plan=strategy_with_plans.composition_plans[composition_plan_idx],
        update_plan=strategy_with_plans.update_plans[update_plan_idx],
        base_table=base_table,
        batch_table=batch_table,
        composition_plan_idx=composition_plan_idx,
        update_plan_idx=update_plan_idx,
        composition_cover_idx=0,
        composition_plan_idx_within_cover=composition_plan_idx,
    )


def build_evaluation_plan_for_cover(
    dep_graph: DependencyGraph,
    strategy_with_plans: StrategyWithPlans,
    composition_nodes: Iterable[View],
    composition_plan: CompositionPlan,
    update_plan: UpdatePlan,
    base_table: str = "events",
    batch_table: str = "events_batch",
    composition_plan_idx: int = -1,
    update_plan_idx: int = -1,
    composition_cover_idx: int | None = None,
    composition_plan_idx_within_cover: int | None = None,
) -> EvaluationPlan:
    """lower a maintained strategy with an explicit composition cover.

    every execution-plan path converges here: index-based descriptors via
    ,,build_evaluation_plan'' (above), and composition-variant descriptors / plan selection via
    ,,build_evaluation_plan_for_composition_variant''. updates are built for every maintained view
    in the materialization strategy; composition is built only over ,,composition_nodes'' so helper
    views can accelerate updates without being query leaves.
    """

    positions = dep_graph.positions
    ordered_strategy = tuple(sorted(strategy_with_plans.strategy, key=lambda view: view_sort_key(view, positions)))
    ordered_effective_view_set = tuple(
        sorted(set(composition_nodes), key=lambda view: view_sort_key(view, positions))
    )

    update_ops: list[UpdateOp] = []
    for target_view in update_plan.topological_order:
        (
            ordered_selected_sources,
            ordered_effective_sources,
            source_blocks,
            join_ops,
            where_predicates,
            select_columns,
            sql,
        ) = _build_update_sql_parts(
            target_view,
            update_plan,
            dep_graph,
            base_table,
            batch_table,
        )

        update_ops.append(
            UpdateOp(
                op_id=f"upd:{_view_label(target_view, positions)}",
                target_view=target_view,
                target_table=_view_table_name(target_view, positions),
                selected_sources=ordered_selected_sources,
                effective_sources=ordered_effective_sources,
                source_blocks=source_blocks,
                join_ops=join_ops,
                where_predicates=where_predicates,
                select_columns=select_columns,
                last_variable_source=update_plan.last_variable_source[target_view],
                sql=sql,
                update_mode=update_plan.update_mode_by_view.get(target_view, UpdateMode.JOIN),
                operator_id=f"update:{_view_label(target_view, positions)}",
            )
        )

    composition = _build_composition_op(
        strategy_with_plans,
        dep_graph,
        ordered_effective_view_set,
        composition_plan,
        base_table,
    )
    post_filter = _build_post_filter_op(dep_graph, composition.sql)

    plan = EvaluationPlan(
        plan_id="",
        strategy=ordered_strategy,
        effective_view_set=ordered_effective_view_set,
        composition_plan_idx=composition_plan_idx,
        update_plan_idx=update_plan_idx,
        base_table=base_table,
        batch_table=batch_table,
        update_ops=tuple(update_ops),
        composition=composition,
        post_filter=post_filter,
        maintained_view_set=ordered_strategy,
        composition_cover=ordered_effective_view_set,
        composition_cover_idx=composition_cover_idx,
        composition_plan_idx_within_cover=composition_plan_idx_within_cover,
    )
    return replace(plan, plan_id=_compute_plan_id(plan))


def build_evaluation_plan_for_composition_variant(
    dep_graph: DependencyGraph,
    strategy_with_plans: StrategyWithPlans,
    composition_variant: CompositionVariant,
    update_plan_idx: int,
    base_table: str = "events",
    batch_table: str = "events_batch",
) -> EvaluationPlan:
    if update_plan_idx < 0 or update_plan_idx >= len(strategy_with_plans.update_plans):
        raise IndexError("update_plan_idx out of range")
    return build_evaluation_plan_for_cover(
        dep_graph=dep_graph,
        strategy_with_plans=strategy_with_plans,
        composition_nodes=composition_variant.cover.effective_view_set,
        composition_plan=composition_variant.plan,
        update_plan=strategy_with_plans.update_plans[update_plan_idx],
        base_table=base_table,
        batch_table=batch_table,
        composition_plan_idx=composition_variant.plan_idx_within_cover,
        update_plan_idx=update_plan_idx,
        composition_cover_idx=composition_variant.cover_idx,
        composition_plan_idx_within_cover=composition_variant.plan_idx_within_cover,
    )
