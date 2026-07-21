"""scoped predicate-placement ledger for EIMER execution plans.

predicate specs are global facts from the dependency graph; placements record where each
predicate is applied or satisfied (per update target, in composition, in post-filter). the
ledger records a fixed placement policy; it does not search over alternatives.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, Mapping, Tuple

from eimer.plans.evaluation_plan import CompositionNode, EvaluationPlan, JoinOp, SourceBlock, UpdateOp
from eimer.models import DependencyGraph, View, canonical_variable_pair, view_name


class PredicateKind(str, Enum):
    INDEPENDENT = "INDEPENDENT"
    DEPENDENT = "DEPENDENT"
    STRUCTURAL = "STRUCTURAL"
    REPEATED_VARIABLE = "REPEATED_VARIABLE"
    POST_FILTER_SEMANTIC = "POST_FILTER_SEMANTIC"
    UNKNOWN = "UNKNOWN"


class PredicateScopeKind(str, Enum):
    UPDATE_TARGET = "UPDATE_TARGET"
    COMPOSITION = "COMPOSITION"
    POST_FILTER = "POST_FILTER"
    INITIALIZATION = "INITIALIZATION"


class PredicatePhase(str, Enum):
    UPDATE = "UPDATE"
    COMPOSITION = "COMPOSITION"
    POST_FILTER = "POST_FILTER"
    INITIALIZATION = "INITIALIZATION"


class PredicatePlacementKind(str, Enum):
    APPLIED_SCAN_FILTER = "APPLIED_SCAN_FILTER"
    APPLIED_JOIN_ON = "APPLIED_JOIN_ON"
    APPLIED_WHERE_FILTER = "APPLIED_WHERE_FILTER"
    INTERNALIZED_BY_TARGET_CACHE = "INTERNALIZED_BY_TARGET_CACHE"
    SATISFIED_BY_SOURCE_CACHE = "SATISFIED_BY_SOURCE_CACHE"
    SATISFIED_BY_COMPOSITION_CACHE = "SATISFIED_BY_COMPOSITION_CACHE"
    POST_FILTER_SEMANTIC = "POST_FILTER_SEMANTIC"
    STRUCTURAL_JOIN_ON = "STRUCTURAL_JOIN_ON"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PredicateSpec:
    predicate_id: str
    kind: PredicateKind
    referenced_variables: Tuple[str, ...]
    expression: str
    selectivity_key: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class PredicatePlacement:
    predicate_id: str
    scope_id: str
    scope_kind: PredicateScopeKind
    phase: PredicatePhase
    operator_id: str | None
    placement_kind: PredicatePlacementKind
    referenced_variables: Tuple[str, ...]
    target_view: View | None = None
    source_view: View | None = None
    internalized_by_view: View | None = None
    satisfied_by_view: View | None = None
    chosen_step: int | None = None
    earliest_legal_step: int | None = None
    costed: bool | None = None


@dataclass(frozen=True)
class PredicateLedger:
    specs: Tuple[PredicateSpec, ...]
    placements: Tuple[PredicatePlacement, ...]

    @property
    def specs_by_id(self) -> Mapping[str, PredicateSpec]:
        return {spec.predicate_id: spec for spec in self.specs}

    def placements_for_scope(self, scope_id: str) -> Tuple[PredicatePlacement, ...]:
        return tuple(placement for placement in self.placements if placement.scope_id == scope_id)

    def placements_for_predicate(self, predicate_id: str) -> Tuple[PredicatePlacement, ...]:
        return tuple(placement for placement in self.placements if placement.predicate_id == predicate_id)

    def validate_basic(self, plan: EvaluationPlan | None = None) -> None:
        spec_ids = set(self.specs_by_id)
        operator_ids = _operator_ids(plan) if plan is not None else set()
        seen: set[PredicatePlacement] = set()
        errors: list[str] = []

        if len(spec_ids) != len(self.specs):
            errors.append("duplicate PredicateSpec.predicate_id values")

        for placement in self.placements:
            if placement.predicate_id not in spec_ids:
                errors.append(f"placement references unknown predicate_id {placement.predicate_id!r}")
            if not placement.scope_id:
                errors.append(f"placement for {placement.predicate_id!r} has empty scope_id")
            if placement.operator_id is not None and plan is not None and placement.operator_id not in operator_ids:
                errors.append(
                    f"placement for {placement.predicate_id!r} references unknown operator_id "
                    f"{placement.operator_id!r}"
                )
            if placement in seen:
                errors.append(f"duplicate predicate placement: {placement!r}")
            seen.add(placement)

        if errors:
            raise ValueError("\n".join(errors))

    def validate_composition_scope(self, dep_graph: DependencyGraph, plan: EvaluationPlan) -> None:
        composition_vars = {var for node in plan.composition.nodes for var in node.view}
        placements = self.placements_for_scope(_composition_scope_id())
        by_predicate: Dict[str, list[PredicatePlacement]] = {}
        for placement in placements:
            by_predicate.setdefault(placement.predicate_id, []).append(placement)

        errors: list[str] = []
        for spec in self.specs:
            if spec.kind not in {PredicateKind.INDEPENDENT, PredicateKind.DEPENDENT}:
                continue
            if not set(spec.referenced_variables).issubset(composition_vars):
                continue
            current = by_predicate.get(spec.predicate_id, [])
            if len(current) != 1:
                errors.append(
                    f"composition scope expected exactly one placement for {spec.predicate_id}, "
                    f"got {len(current)}"
                )
                continue
            kinds = {placement.placement_kind for placement in current}
            if (
                PredicatePlacementKind.APPLIED_WHERE_FILTER in kinds
                and PredicatePlacementKind.SATISFIED_BY_COMPOSITION_CACHE in kinds
            ):
                errors.append(f"composition scope both applies and satisfies {spec.predicate_id}")

        if errors:
            raise ValueError("\n".join(errors))

    def validate_update_target_scopes(self, dep_graph: DependencyGraph, plan: EvaluationPlan) -> None:
        errors: list[str] = []
        for update_op in plan.update_ops:
            scope_id = _update_scope_id(update_op.target_view, dep_graph)
            placements = self.placements_for_scope(scope_id)
            by_predicate: Dict[str, list[PredicatePlacement]] = {}
            for placement in placements:
                by_predicate.setdefault(placement.predicate_id, []).append(placement)

            for spec in self.specs:
                if spec.kind not in {PredicateKind.INDEPENDENT, PredicateKind.DEPENDENT}:
                    continue
                if not set(spec.referenced_variables).issubset(update_op.target_view):
                    continue
                current = by_predicate.get(spec.predicate_id, [])
                if len(current) != 1:
                    errors.append(
                        f"{scope_id} expected exactly one placement for {spec.predicate_id}, "
                        f"got {len(current)}"
                    )

        if errors:
            raise ValueError("\n".join(errors))

    def validate(self, dep_graph: DependencyGraph, plan: EvaluationPlan) -> None:
        self.validate_basic(plan)
        self.validate_composition_scope(dep_graph, plan)
        self.validate_update_target_scopes(dep_graph, plan)


@dataclass(frozen=True)
class PredicateLedgerConsistencyIssue:
    code: str
    message: str
    operator_id: str | None = None


def build_predicate_ledger(dep_graph: DependencyGraph, evaluation_plan: EvaluationPlan) -> PredicateLedger:
    specs = list(_predicate_specs(dep_graph))
    placements: list[PredicatePlacement] = []
    placements.extend(_build_update_placements(dep_graph, evaluation_plan, tuple(specs)))
    placements.extend(_build_composition_placements(dep_graph, evaluation_plan, tuple(specs)))
    structural_specs, structural_placements = _build_structural_specs_and_placements(
        dep_graph,
        evaluation_plan,
    )
    specs.extend(structural_specs)
    placements.extend(structural_placements)
    ledger = PredicateLedger(specs=tuple(specs), placements=tuple(placements))
    ledger.validate(dep_graph, evaluation_plan)
    return ledger


def validate_predicate_ledger_against_evaluation_plan(
    ledger: PredicateLedger,
    evaluation_plan: EvaluationPlan,
) -> Tuple[PredicateLedgerConsistencyIssue, ...]:
    """check that ledger placements correspond to lowered plan operators.

    the checks are structural and SQL-neutral: each lowered ,,JoinOp.structural_predicates''
    entry has a structural placement, and placements point only at known operators.
    """

    issues: list[PredicateLedgerConsistencyIssue] = []
    specs_by_id = ledger.specs_by_id
    try:
        ledger.validate_basic(evaluation_plan)
    except ValueError as exc:
        issues.append(
            PredicateLedgerConsistencyIssue(
                code="LEDGER_BASIC_INVALID",
                message=str(exc),
            )
        )

    for expected in _expected_structural_join_predicates(evaluation_plan):
        if not _has_structural_placement(ledger, specs_by_id, expected):
            scope_id, operator_id, expression = expected
            issues.append(
                PredicateLedgerConsistencyIssue(
                    code="MISSING_STRUCTURAL_PLACEMENT",
                    message=(
                        f"missing STRUCTURAL_JOIN_ON placement for {expression!r} "
                        f"in scope {scope_id!r}"
                    ),
                    operator_id=operator_id,
                )
            )

    for placement in ledger.placements:
        spec = specs_by_id.get(placement.predicate_id)
        if spec is None:
            continue
        if (
            spec.kind == PredicateKind.STRUCTURAL
            and placement.placement_kind == PredicatePlacementKind.STRUCTURAL_JOIN_ON
        ):
            if not _structural_expression_on_join(
                evaluation_plan,
                placement.operator_id,
                spec.expression,
            ):
                issues.append(
                    PredicateLedgerConsistencyIssue(
                        code="STRUCTURAL_PLACEMENT_NOT_ON_JOIN",
                        message=(
                            f"structural placement {placement.predicate_id!r} does not match "
                            "a JoinOp.structural_predicates entry"
                        ),
                        operator_id=placement.operator_id,
                    )
                )

    return tuple(issues)


def _predicate_specs(dep_graph: DependencyGraph) -> Tuple[PredicateSpec, ...]:
    specs: list[PredicateSpec] = []
    for variable in dep_graph.variables:
        vertex = dep_graph.vertices[variable]
        for idx, predicate in enumerate(vertex.independent_predicates):
            specs.append(
                PredicateSpec(
                    predicate_id=f"ind:{variable}:{idx}",
                    kind=PredicateKind.INDEPENDENT,
                    referenced_variables=(variable,),
                    expression=predicate.text,
                    selectivity_key=f"phi:{variable}",
                    source=predicate.source_variable,
                )
            )

    for edge in dep_graph.edges:
        left, right = canonical_variable_pair(edge.var_left, edge.var_right, dep_graph.positions)
        for idx, predicate in enumerate(edge.dependent_predicates):
            specs.append(
                PredicateSpec(
                    predicate_id=f"dep:{left}:{right}:{idx}",
                    kind=PredicateKind.DEPENDENT,
                    referenced_variables=(left, right),
                    expression=predicate.text,
                    selectivity_key=f"sigma:{left}:{right}",
                    source=predicate.source_variable,
                )
            )

    return tuple(specs)


def _build_structural_specs_and_placements(
    dep_graph: DependencyGraph,
    plan: EvaluationPlan,
) -> Tuple[Tuple[PredicateSpec, ...], Tuple[PredicatePlacement, ...]]:
    specs: list[PredicateSpec] = []
    placements: list[PredicatePlacement] = []

    for update_op in plan.update_ops:
        scope_id = _update_scope_id(update_op.target_view, dep_graph)
        for join_idx, join_op in enumerate(update_op.join_ops, start=1):
            new_specs, new_placements = _structural_specs_for_join(
                dep_graph=dep_graph,
                scope_id=scope_id,
                scope_kind=PredicateScopeKind.UPDATE_TARGET,
                phase=PredicatePhase.UPDATE,
                join_op=join_op,
                join_idx=join_idx,
                target_view=update_op.target_view,
            )
            specs.extend(new_specs)
            placements.extend(new_placements)

    scope_id = _composition_scope_id()
    for join_idx, join_op in enumerate(plan.composition.join_ops, start=1):
        new_specs, new_placements = _structural_specs_for_join(
            dep_graph=dep_graph,
            scope_id=scope_id,
            scope_kind=PredicateScopeKind.COMPOSITION,
            phase=PredicatePhase.COMPOSITION,
            join_op=join_op,
            join_idx=join_idx,
            target_view=None,
        )
        specs.extend(new_specs)
        placements.extend(new_placements)

    return tuple(specs), tuple(placements)


def _structural_specs_for_join(
    *,
    dep_graph: DependencyGraph,
    scope_id: str,
    scope_kind: PredicateScopeKind,
    phase: PredicatePhase,
    join_op: JoinOp,
    join_idx: int,
    target_view: View | None,
) -> Tuple[Tuple[PredicateSpec, ...], Tuple[PredicatePlacement, ...]]:
    specs: list[PredicateSpec] = []
    placements: list[PredicatePlacement] = []
    referenced_variables = _join_referenced_variables(join_op, dep_graph)

    for local_idx, expression in enumerate(join_op.structural_predicates):
        predicate_id = _structural_predicate_id(
            scope_id,
            join_op.operator_id,
            local_idx,
            expression,
        )
        specs.append(
            PredicateSpec(
                predicate_id=predicate_id,
                kind=PredicateKind.STRUCTURAL,
                referenced_variables=referenced_variables,
                expression=expression,
                selectivity_key=None,
                source="evaluation_plan.join_op",
            )
        )
        placements.append(
            PredicatePlacement(
                predicate_id=predicate_id,
                scope_id=scope_id,
                scope_kind=scope_kind,
                phase=phase,
                operator_id=join_op.operator_id,
                placement_kind=PredicatePlacementKind.STRUCTURAL_JOIN_ON,
                referenced_variables=referenced_variables,
                target_view=target_view,
                chosen_step=join_idx,
                earliest_legal_step=join_idx,
                costed=None,
            )
        )

    return tuple(specs), tuple(placements)


def _join_referenced_variables(join_op: JoinOp, dep_graph: DependencyGraph) -> Tuple[str, ...]:
    variables = set(join_op.anchor_view) | set(join_op.new_view)
    return tuple(var for var in dep_graph.variables if var in variables)


def _structural_predicate_id(scope_id: str, operator_id: str, local_idx: int, expression: str) -> str:
    normalized = _normalize_expression_for_id(expression)
    return f"struct:{scope_id}:{operator_id}:{local_idx}:{normalized}"


def _normalize_expression_for_id(expression: str) -> str:
    return "_".join(expression.split())


def _expected_structural_join_predicates(plan: EvaluationPlan) -> Tuple[Tuple[str, str, str], ...]:
    expected: list[Tuple[str, str, str]] = []
    for update_op in plan.update_ops:
        scope_id = update_op.operator_id
        for join_op in update_op.join_ops:
            expected.extend(
                (scope_id, join_op.operator_id, expression)
                for expression in join_op.structural_predicates
            )
    expected.extend(
        (_composition_scope_id(), join_op.operator_id, expression)
        for join_op in plan.composition.join_ops
        for expression in join_op.structural_predicates
    )
    return tuple(expected)


def _has_structural_placement(
    ledger: PredicateLedger,
    specs_by_id: Mapping[str, PredicateSpec],
    expected: Tuple[str, str, str],
) -> bool:
    scope_id, operator_id, expression = expected
    for placement in ledger.placements:
        spec = specs_by_id.get(placement.predicate_id)
        if spec is None:
            continue
        if (
            placement.scope_id == scope_id
            and placement.operator_id == operator_id
            and placement.placement_kind == PredicatePlacementKind.STRUCTURAL_JOIN_ON
            and spec.kind == PredicateKind.STRUCTURAL
            and spec.expression == expression
        ):
            return True
    return False


def _structural_expression_on_join(
    plan: EvaluationPlan,
    operator_id: str | None,
    expression: str,
) -> bool:
    if operator_id is None:
        return False
    for join_op in _all_join_ops(plan):
        if join_op.operator_id==operator_id and expression in join_op.structural_predicates:
            return True
    return False


def _all_join_ops(plan: EvaluationPlan) -> Tuple[JoinOp, ...]:
    joins: list[JoinOp] = []
    for update_op in plan.update_ops:
        joins.extend(update_op.join_ops)
    joins.extend(plan.composition.join_ops)
    return tuple(joins)


def _update_scope_id(view: View, dep_graph: DependencyGraph) -> str:
    return f"update:{view_name(view, dep_graph.positions)}"


def _composition_scope_id() -> str:
    return "composition"


def _operator_ids(plan: EvaluationPlan | None) -> set[str]:
    if plan is None:
        return set()
    operator_ids = {plan.composition.operator_id, plan.post_filter.operator_id}
    for node in plan.composition.nodes:
        operator_ids.add(node.operator_id)
    for join_op in plan.composition.join_ops:
        operator_ids.add(join_op.operator_id)
    for update_op in plan.update_ops:
        operator_ids.add(update_op.operator_id)
        for block in update_op.source_blocks:
            operator_ids.add(block.operator_id)
        for join_op in update_op.join_ops:
            operator_ids.add(join_op.operator_id)
    return {operator_id for operator_id in operator_ids if operator_id}


def _source_block_for_variable(update_op: UpdateOp) -> dict[str, SourceBlock]:
    by_variable: dict[str, SourceBlock] = {}
    for block in update_op.source_blocks:
        for variable in block.variables:
            by_variable[variable] = block
    return by_variable


def _build_update_placements(
    dep_graph: DependencyGraph,
    plan: EvaluationPlan,
    specs: Tuple[PredicateSpec, ...],
) -> Tuple[PredicatePlacement, ...]:
    placements: list[PredicatePlacement] = []
    for update_op in plan.update_ops:
        scope_id = _update_scope_id(update_op.target_view, dep_graph)
        block_by_variable = _source_block_for_variable(update_op)

        for spec in specs:
            if not set(spec.referenced_variables).issubset(update_op.target_view):
                continue
            if spec.kind == PredicateKind.INDEPENDENT:
                block = block_by_variable.get(spec.referenced_variables[0])
                if block is None:
                    continue
                placements.append(
                    _update_independent_placement(spec, scope_id, update_op, block)
                )
                continue

            if spec.kind == PredicateKind.DEPENDENT:
                left, right = spec.referenced_variables
                left_block = block_by_variable.get(left)
                right_block = block_by_variable.get(right)
                if left_block is None or right_block is None:
                    continue
                placements.append(
                    _update_dependent_placement(
                        spec,
                        scope_id,
                        update_op,
                        left_block,
                        right_block,
                    )
                )

    return tuple(placements)


def _update_independent_placement(
    spec: PredicateSpec,
    scope_id: str,
    update_op: UpdateOp,
    block: SourceBlock,
) -> PredicatePlacement:
    if block.kind == "cache":
        return PredicatePlacement(
            predicate_id=spec.predicate_id,
            scope_id=scope_id,
            scope_kind=PredicateScopeKind.UPDATE_TARGET,
            phase=PredicatePhase.UPDATE,
            operator_id=block.operator_id,
            placement_kind=PredicatePlacementKind.SATISFIED_BY_SOURCE_CACHE,
            referenced_variables=spec.referenced_variables,
            target_view=update_op.target_view,
            source_view=block.source_view or block.variables,
            internalized_by_view=update_op.target_view,
            satisfied_by_view=block.source_view or block.variables,
            costed=False,
        )

    return PredicatePlacement(
        predicate_id=spec.predicate_id,
        scope_id=scope_id,
        scope_kind=PredicateScopeKind.UPDATE_TARGET,
        phase=PredicatePhase.UPDATE,
        operator_id=block.operator_id,
        placement_kind=PredicatePlacementKind.APPLIED_SCAN_FILTER,
        referenced_variables=spec.referenced_variables,
        target_view=update_op.target_view,
        source_view=block.variables,
        internalized_by_view=update_op.target_view,
        costed=True,
    )


def _update_dependent_placement(
    spec: PredicateSpec,
    scope_id: str,
    update_op: UpdateOp,
    left_block: SourceBlock,
    right_block: SourceBlock,
) -> PredicatePlacement:
    if left_block.variables == right_block.variables and left_block.kind == "cache":
        return PredicatePlacement(
            predicate_id=spec.predicate_id,
            scope_id=scope_id,
            scope_kind=PredicateScopeKind.UPDATE_TARGET,
            phase=PredicatePhase.UPDATE,
            operator_id=left_block.operator_id,
            placement_kind=PredicatePlacementKind.SATISFIED_BY_SOURCE_CACHE,
            referenced_variables=spec.referenced_variables,
            target_view=update_op.target_view,
            source_view=left_block.source_view or left_block.variables,
            internalized_by_view=update_op.target_view,
            satisfied_by_view=left_block.source_view or left_block.variables,
            costed=False,
        )

    return PredicatePlacement(
        predicate_id=spec.predicate_id,
        scope_id=scope_id,
        scope_kind=PredicateScopeKind.UPDATE_TARGET,
        phase=PredicatePhase.UPDATE,
        operator_id=update_op.operator_id,
        placement_kind=PredicatePlacementKind.APPLIED_WHERE_FILTER,
        referenced_variables=spec.referenced_variables,
        target_view=update_op.target_view,
        internalized_by_view=update_op.target_view,
        chosen_step=len(update_op.join_ops),
        earliest_legal_step=None,
        costed=True,
    )


def _node_for_variable(nodes: Iterable[CompositionNode], variable: str) -> CompositionNode | None:
    candidates = [node for node in nodes if variable in node.view]
    if not candidates:
        return None
    cache_candidates = [node for node in candidates if node.kind == "cache"]
    if cache_candidates:
        return sorted(cache_candidates, key=lambda node: (len(node.view), node.operator_id))[0]
    return sorted(candidates, key=lambda node: (len(node.view), node.operator_id))[0]


def _node_covering_variables(nodes: Iterable[CompositionNode], variables: Iterable[str]) -> CompositionNode | None:
    required = set(variables)
    candidates = [node for node in nodes if required.issubset(node.view) and node.kind == "cache"]
    if not candidates:
        return None
    return sorted(candidates, key=lambda node: (len(node.view), node.operator_id))[0]


def _composition_availability(plan: EvaluationPlan) -> Tuple[Tuple[View, ...], ...]:
    ordered_nodes = tuple(sorted(plan.composition.nodes, key=lambda node: node.operator_id))
    if not ordered_nodes:
        return ((),)
    node_by_view = {node.view: node for node in ordered_nodes}
    root_view = plan.composition.join_ops[0].anchor_view if plan.composition.join_ops else ordered_nodes[0].view
    if root_view not in node_by_view:
        return (tuple(node.view for node in ordered_nodes),)
    visited = [root_view]
    seen = {root_view}
    availability: list[Tuple[View, ...]] = [tuple(visited)]
    for join_op in plan.composition.join_ops:
        if join_op.new_view not in seen:
            visited.append(join_op.new_view)
            seen.add(join_op.new_view)
        availability.append(tuple(visited))
    return tuple(availability)


def _earliest_compose_step(plan: EvaluationPlan, variables: Iterable[str]) -> int | None:
    required = set(variables)
    for step_idx, views in enumerate(_composition_availability(plan)):
        covered = {var for view in views for var in view}
        if required.issubset(covered):
            return step_idx
    return None


def _build_composition_placements(
    dep_graph: DependencyGraph,
    plan: EvaluationPlan,
    specs: Tuple[PredicateSpec, ...],
) -> Tuple[PredicatePlacement, ...]:
    placements: list[PredicatePlacement] = []
    scope_id = _composition_scope_id()
    composition_variables = {var for node in plan.composition.nodes for var in node.view}

    for spec in specs:
        if not set(spec.referenced_variables).issubset(composition_variables):
            continue
        if spec.kind == PredicateKind.INDEPENDENT:
            node = _node_for_variable(plan.composition.nodes, spec.referenced_variables[0])
            if node is None:
                continue
            if node.kind == "cache":
                placements.append(
                    PredicatePlacement(
                        predicate_id=spec.predicate_id,
                        scope_id=scope_id,
                        scope_kind=PredicateScopeKind.COMPOSITION,
                        phase=PredicatePhase.COMPOSITION,
                        operator_id=node.operator_id,
                        placement_kind=PredicatePlacementKind.SATISFIED_BY_COMPOSITION_CACHE,
                        referenced_variables=spec.referenced_variables,
                        source_view=node.view,
                        satisfied_by_view=node.view,
                        costed=False,
                    )
                )
            else:
                placements.append(
                    PredicatePlacement(
                        predicate_id=spec.predicate_id,
                        scope_id=scope_id,
                        scope_kind=PredicateScopeKind.COMPOSITION,
                        phase=PredicatePhase.COMPOSITION,
                        operator_id=node.operator_id,
                        placement_kind=PredicatePlacementKind.APPLIED_SCAN_FILTER,
                        referenced_variables=spec.referenced_variables,
                        source_view=node.view,
                        costed=True,
                    )
                )
            continue

        if spec.kind == PredicateKind.DEPENDENT:
            covering_node = _node_covering_variables(plan.composition.nodes, spec.referenced_variables)
            if covering_node is not None:
                placements.append(
                    PredicatePlacement(
                        predicate_id=spec.predicate_id,
                        scope_id=scope_id,
                        scope_kind=PredicateScopeKind.COMPOSITION,
                        phase=PredicatePhase.COMPOSITION,
                        operator_id=covering_node.operator_id,
                        placement_kind=PredicatePlacementKind.SATISFIED_BY_COMPOSITION_CACHE,
                        referenced_variables=spec.referenced_variables,
                        source_view=covering_node.view,
                        satisfied_by_view=covering_node.view,
                        costed=False,
                    )
                )
                continue

            earliest = _earliest_compose_step(plan, spec.referenced_variables)
            placements.append(
                PredicatePlacement(
                    predicate_id=spec.predicate_id,
                    scope_id=scope_id,
                    scope_kind=PredicateScopeKind.COMPOSITION,
                    phase=PredicatePhase.COMPOSITION,
                    operator_id=plan.composition.operator_id,
                    placement_kind=PredicatePlacementKind.APPLIED_WHERE_FILTER,
                    referenced_variables=spec.referenced_variables,
                    chosen_step=len(plan.composition.join_ops),
                    earliest_legal_step=earliest,
                    costed=True,
                )
            )

    return tuple(placements)
