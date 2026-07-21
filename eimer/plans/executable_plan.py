"""executable evaluation-plan bundle.

packages the lowered ,,EvaluationPlan'', the workload schedule wrapper, and the scoped
predicate ledger into one inspectable object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Collection, Literal, Mapping, Tuple

from eimer.plans.evaluation_plan import (
    EvaluationPlan,
    FiringPatternKind,
    JoinOp,
    SourceRole,
    SourceStateVersion,
)
from eimer.models import DependencyGraph, View
from eimer.plans.predicate_ledger import (
    PredicateLedger,
    build_predicate_ledger,
    validate_predicate_ledger_against_evaluation_plan,
)
from eimer.workload import Workload
from eimer.plans.workload_evaluation_plan import (
    BatchStep,
    BatchStepKind,
    WorkloadEvaluationPlan,
    build_workload_evaluation_plan,
)


class InitializationMode(str, Enum):
    PREBUILT_CACHES = "PREBUILT_CACHES"
    EMPTY_CACHES = "EMPTY_CACHES"
    INITIALIZATION_NOT_REPRESENTED = "INITIALIZATION_NOT_REPRESENTED"


@dataclass(frozen=True)
class ExecutableEvaluationPlan:
    evaluation_plan: EvaluationPlan
    workload_evaluation_plan: WorkloadEvaluationPlan
    predicate_ledger: PredicateLedger
    query_batches: frozenset[int] | None
    initialization_mode: InitializationMode = InitializationMode.INITIALIZATION_NOT_REPRESENTED
    strategy_idx: int | None = None
    update_plan_idx: int | None = None
    composition_variant_idx: int | None = None
    cost: int | None = None
    dep_graph: DependencyGraph | None = None
    workload: Workload | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanValidationIssue:
    severity: Literal["error", "warning"]
    code: str
    message: str
    operator_id: str | None = None


@dataclass(frozen=True)
class PlanValidationResult:
    issues: Tuple[PlanValidationIssue, ...]

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)


@dataclass(frozen=True)
class SQLProgramStep:
    batch_index: int
    step_kind: BatchStepKind
    operator_id: str
    sql: str
    description: str | None = None


def build_executable_evaluation_plan(
    dep_graph: DependencyGraph,
    evaluation_plan: EvaluationPlan,
    workload: Workload,
    *,
    query_batches: Collection[int] | None = None,
    initialization_mode: InitializationMode = InitializationMode.INITIALIZATION_NOT_REPRESENTED,
    strategy_idx: int | None = None,
    update_plan_idx: int | None = None,
    composition_variant_idx: int | None = None,
    cost: int | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ExecutableEvaluationPlan:
    workload_evaluation_plan = build_workload_evaluation_plan(
        evaluation_plan,
        workload,
        query_batches=query_batches,
    )
    predicate_ledger = build_predicate_ledger(dep_graph, evaluation_plan)
    return ExecutableEvaluationPlan(
        evaluation_plan=evaluation_plan,
        workload_evaluation_plan=workload_evaluation_plan,
        predicate_ledger=predicate_ledger,
        query_batches=workload_evaluation_plan.query_batches,
        initialization_mode=initialization_mode,
        strategy_idx=strategy_idx,
        update_plan_idx=update_plan_idx,
        composition_variant_idx=composition_variant_idx,
        cost=cost,
        dep_graph=dep_graph,
        workload=workload,
        metadata=dict(metadata or {}),
    )


def build_executable_evaluation_plan_for_candidate(
    dep_graph: DependencyGraph,
    workload: Workload,
    candidate: Any,
    *,
    initialization_mode: InitializationMode = InitializationMode.INITIALIZATION_NOT_REPRESENTED,
    metadata: Mapping[str, Any] | None = None,
) -> ExecutableEvaluationPlan:
    return build_executable_evaluation_plan(
        dep_graph,
        candidate.evaluation_plan,
        workload,
        query_batches=candidate.query_batches,
        initialization_mode=initialization_mode,
        strategy_idx=candidate.strategy_idx,
        update_plan_idx=candidate.update_plan_idx,
        composition_variant_idx=candidate.composition_variant_idx,
        cost=candidate.cost,
        metadata=metadata,
    )


def validate_executable_plan(plan: ExecutableEvaluationPlan) -> PlanValidationResult:
    issues: list[PlanValidationIssue] = []
    evaluation_plan = plan.evaluation_plan
    maintained = tuple(evaluation_plan.maintained_view_set or evaluation_plan.strategy)
    maintained_set = set(maintained)

    _validate_updates(evaluation_plan, maintained_set, issues)
    _validate_composition(evaluation_plan, maintained_set, plan.dep_graph, issues)
    _validate_predicate_ledger(plan, issues)
    _validate_schedule(plan, issues)
    _validate_initialization(plan, issues)

    return PlanValidationResult(issues=tuple(issues))


def render_batch_sql_program(executable_plan: ExecutableEvaluationPlan, batch_index: int) -> Tuple[SQLProgramStep, ...]:
    program = executable_plan.workload_evaluation_plan.program_for_batch(batch_index)
    steps = []
    for step in program.steps:
        sql = _sql_for_batch_step(step)
        if sql is None:
            continue
        steps.append(
            SQLProgramStep(
                batch_index=batch_index,
                step_kind=step.kind,
                operator_id=step.operator_id,
                sql=sql,
                description=step.phase,
            )
        )
    return tuple(steps)


def render_workload_sql_program(executable_plan: ExecutableEvaluationPlan) -> Tuple[SQLProgramStep, ...]:
    steps: list[SQLProgramStep] = []
    for batch_index in range(1, executable_plan.workload_evaluation_plan.batch_count + 1):
        steps.extend(render_batch_sql_program(executable_plan, batch_index))
    return tuple(steps)


def _sql_for_batch_step(step: BatchStep) -> str | None:
    payload = step.payload
    sql = getattr(payload, "sql", None)
    if sql is None:
        return None
    return sql


def _issue(
    issues: list[PlanValidationIssue],
    severity: Literal["error", "warning"],
    code: str,
    message: str,
    operator_id: str | None = None,
) -> None:
    issues.append(
        PlanValidationIssue(
            severity=severity,
            code=code,
            message=message,
            operator_id=operator_id,
        )
    )


def _operator_ids(evaluation_plan: EvaluationPlan) -> set[str]:
    ids = {evaluation_plan.composition.operator_id, evaluation_plan.post_filter.operator_id}
    for update_op in evaluation_plan.update_ops:
        ids.add(update_op.operator_id)
        ids.update(block.operator_id for block in update_op.source_blocks)
        ids.update(join_op.operator_id for join_op in update_op.join_ops)
    ids.update(node.operator_id for node in evaluation_plan.composition.nodes)
    ids.update(join_op.operator_id for join_op in evaluation_plan.composition.join_ops)
    return {operator_id for operator_id in ids if operator_id}


def _validate_updates(
    evaluation_plan: EvaluationPlan,
    maintained_set: set[View],
    issues: list[PlanValidationIssue],
) -> None:
    update_targets = [update_op.target_view for update_op in evaluation_plan.update_ops]
    if set(update_targets) != maintained_set:
        missing = sorted(tuple(sorted(view)) for view in maintained_set - set(update_targets))
        extra = sorted(tuple(sorted(view)) for view in set(update_targets) - maintained_set)
        _issue(
            issues,
            "error",
            "UPDATE_TARGET_MISMATCH",
            f"update targets must match maintained views; missing={missing}, extra={extra}",
        )

    if len(update_targets) != len(set(update_targets)):
        _issue(issues, "error", "DUPLICATE_UPDATE_TARGET", "duplicate update target view")

    for update_op in evaluation_plan.update_ops:
        if update_op.target_view not in maintained_set:
            _issue(
                issues,
                "error",
                "UPDATE_TARGET_NOT_MAINTAINED",
                "update target is not in maintained strategy",
                update_op.operator_id or update_op.op_id,
            )
        if not update_op.operator_id:
            _issue(issues, "error", "MISSING_OPERATOR_ID", "update op has no operator_id", update_op.op_id)
        if update_op.last_variable_source not in update_op.effective_sources:
            _issue(
                issues,
                "error",
                "LAST_SOURCE_NOT_EFFECTIVE",
                "update last_variable_source is not in effective_sources",
                update_op.operator_id or update_op.op_id,
            )

        for block in update_op.source_blocks:
            if not block.operator_id:
                _issue(issues, "error", "MISSING_OPERATOR_ID", "source block has no operator_id")
            if block.kind == "cache":
                source_view = block.source_view or block.variables
                if source_view not in maintained_set:
                    _issue(
                        issues,
                        "error",
                        "HELPER_CACHE_NOT_MAINTAINED",
                        "cache source view is not maintained",
                        block.operator_id,
                    )
                if block.source_role == SourceRole.UNKNOWN or block.state_version == SourceStateVersion.UNKNOWN:
                    _issue(
                        issues,
                        "error",
                        "UNKNOWN_SOURCE_METADATA",
                        "cache source has UNKNOWN role/state metadata",
                        block.operator_id,
                    )
            elif block.kind == "base_scan":
                if block.source_role == SourceRole.UNKNOWN or block.state_version == SourceStateVersion.UNKNOWN:
                    _issue(
                        issues,
                        "error",
                        "UNKNOWN_SOURCE_METADATA",
                        "base source has UNKNOWN role/state metadata",
                        block.operator_id,
                    )

        for join_op in update_op.join_ops:
            if not join_op.operator_id:
                _issue(issues, "error", "MISSING_OPERATOR_ID", "update join has no operator_id", join_op.op_id)


def _validate_composition(
    evaluation_plan: EvaluationPlan,
    maintained_set: set[View],
    dep_graph: DependencyGraph | None,
    issues: list[PlanValidationIssue],
) -> None:
    nodes = tuple(evaluation_plan.composition.nodes)
    if dep_graph is not None:
        covered = {var for node in nodes for var in node.view}
        required = set(dep_graph.variables)
        if covered != required:
            _issue(
                issues,
                "error",
                "COMPOSITION_COVER_INCOMPLETE",
                f"composition cover must equal query variables; covered={sorted(covered)}, required={sorted(required)}",
            )

    for node in nodes:
        if not node.operator_id:
            _issue(issues, "error", "MISSING_OPERATOR_ID", "composition node has no operator_id")
        if node.kind == "cache" and node.view not in maintained_set:
            _issue(
                issues,
                "error",
                "COMPOSITION_CACHE_NOT_MAINTAINED",
                "cache composition node is not maintained",
                node.operator_id,
            )
        if node.kind != "cache" and node.view in maintained_set:
            _issue(
                issues,
                "error",
                "SINGLETON_LEAF_TREATED_AS_MAINTAINED",
                "non-cache composition node is also maintained",
                node.operator_id,
            )

    if not evaluation_plan.composition.operator_id:
        _issue(issues, "error", "MISSING_OPERATOR_ID", "composition op has no operator_id")
    for join_op in evaluation_plan.composition.join_ops:
        if not join_op.operator_id:
            _issue(issues, "error", "MISSING_OPERATOR_ID", "composition join has no operator_id", join_op.op_id)

    _validate_composition_join_consumes_nodes(evaluation_plan.composition.join_ops, {node.view for node in nodes}, issues)


def _validate_composition_join_consumes_nodes(
    join_ops: Tuple[JoinOp, ...],
    nodes: set[View],
    issues: list[PlanValidationIssue],
) -> None:
    if len(nodes) <= 1:
        if join_ops:
            _issue(issues, "error", "COMPOSITION_JOIN_EXTRA", "single-node composition has join ops")
        return
    if not join_ops:
        _issue(issues, "error", "COMPOSITION_JOIN_MISSING", "multi-node composition has no join ops")
        return

    visited = {join_ops[0].anchor_view}
    if join_ops[0].anchor_view not in nodes:
        _issue(
            issues,
            "error",
            "COMPOSITION_JOIN_UNKNOWN_NODE",
            "composition join anchor is not a composition node",
            join_ops[0].operator_id,
        )

    for join_op in join_ops:
        if join_op.new_view not in nodes:
            _issue(
                issues,
                "error",
                "COMPOSITION_JOIN_UNKNOWN_NODE",
                "composition join new_view is not a composition node",
                join_op.operator_id,
            )
        if join_op.new_view in visited:
            _issue(
                issues,
                "error",
                "COMPOSITION_JOIN_DUPLICATE_NODE",
                "composition join consumes a node more than once",
                join_op.operator_id,
            )
        visited.add(join_op.new_view)

    if visited != nodes:
        missing = sorted(tuple(sorted(view)) for view in nodes - visited)
        _issue(
            issues,
            "error",
            "COMPOSITION_JOIN_INCOMPLETE",
            f"composition join order does not consume all nodes; missing={missing}",
        )


def _validate_predicate_ledger(plan: ExecutableEvaluationPlan, issues: list[PlanValidationIssue]) -> None:
    try:
        plan.predicate_ledger.validate_basic(plan.evaluation_plan)
        if plan.dep_graph is not None:
            plan.predicate_ledger.validate(plan.dep_graph, plan.evaluation_plan)
    except ValueError as exc:
        _issue(
            issues,
            "error",
            "PREDICATE_LEDGER_INVALID",
            str(exc),
        )
    for ledger_issue in validate_predicate_ledger_against_evaluation_plan(
        plan.predicate_ledger,
        plan.evaluation_plan,
    ):
        _issue(
            issues,
            "error",
            ledger_issue.code,
            ledger_issue.message,
            ledger_issue.operator_id,
        )




def _validate_schedule(plan: ExecutableEvaluationPlan, issues: list[PlanValidationIssue]) -> None:
    workload_plan = plan.workload_evaluation_plan
    if plan.query_batches != workload_plan.query_batches:
        _issue(
            issues,
            "error",
            "QUERY_BATCH_SCHEDULE_MISMATCH",
            "ExecutableEvaluationPlan.query_batches differs from workload_evaluation_plan.query_batches",
        )

    if plan.query_batches is not None:
        invalid = sorted(batch for batch in plan.query_batches if batch < 1 or batch > workload_plan.batch_count)
        if invalid:
            _issue(
                issues,
                "error",
                "QUERY_BATCH_OUT_OF_RANGE",
                f"query_batches are 1-based and must be within [1, {workload_plan.batch_count}], got {invalid!r}",
            )

    expected_update_count = len(plan.evaluation_plan.update_ops)
    for batch_index in range(1, workload_plan.batch_count + 1):
        program = workload_plan.program_for_batch(batch_index)
        update_count = sum(1 for step in program.steps if step.kind == BatchStepKind.UPDATE)
        if update_count != expected_update_count:
            _issue(
                issues,
                "error",
                "SCHEDULE_MISSING_UPDATES",
                f"batch {batch_index} expected {expected_update_count} update steps, got {update_count}",
            )
        has_query = workload_plan.has_query(batch_index)
        has_compose = any(step.kind == BatchStepKind.COMPOSE for step in program.steps)
        has_post_filter = any(step.kind == BatchStepKind.POST_FILTER for step in program.steps)
        if has_query != has_compose or has_query != has_post_filter:
            _issue(
                issues,
                "error",
                "QUERY_STEP_SCHEDULE_MISMATCH",
                f"batch {batch_index} query schedule mismatch: has_query={has_query}, compose={has_compose}, post_filter={has_post_filter}",
            )


def _validate_initialization(plan: ExecutableEvaluationPlan, issues: list[PlanValidationIssue]) -> None:
    if (
        plan.workload is not None
        and plan.workload.initial_base_size > 0
        and plan.initialization_mode == InitializationMode.INITIALIZATION_NOT_REPRESENTED
    ):
        _issue(
            issues,
            "warning",
            "INITIALIZATION_NOT_REPRESENTED",
            "workload.initial_base_size > 0, but cache initialization/backfill is outside the explicit plan",
        )
