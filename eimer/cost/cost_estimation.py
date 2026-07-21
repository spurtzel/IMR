"""cost-estimation for executable evaluation plans.

an adapter around the cost formulas: preserves the scalar ,,plan_cost'' while exposing per-batch
and per-operator rows for validation artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from eimer.cost.cost_model import (
    CostModelError,
    StateLevelTape,
    _analytic_singleton_size,
    batch_cost,
    compose_step_trace,
    plan_cost,
)
from eimer.plans.executable_plan import ExecutableEvaluationPlan
from eimer.models import View
from eimer.workload import Workload


@dataclass(frozen=True)
class OpEstimate:
    batch_index: int
    op_id: str
    op_kind: str
    predicted_input: int
    predicted_output: int
    predicted_total: int
    component: str


@dataclass(frozen=True)
class ComposeStepEstimate:
    batch_index: int
    step_index: int
    join_op_id: str
    left_nodes_or_aliases: tuple[str, ...]
    right_node_or_alias: str
    join_type: str
    predicted_left_rows: float
    predicted_right_rows: float
    raw_pre_rule_output_rows: float | None
    predicted_output_rows: float
    predicted_intermediate_work: float
    sigma_applied_here: float | None
    predicates_applied_here: tuple[str, ...]
    structural_predicates: tuple[str, ...]
    cardinality_rule: str = "default"
    containment_side: str | None = None
    shared_key_divisor: float = 1.0


@dataclass(frozen=True)
class BatchEstimate:
    batch_index: int
    run_query: bool
    update_cost: int
    compose_cost: int
    post_filter_cost: int
    total_cost: int
    state_snapshot: Mapping[View, int]
    op_estimates: tuple[OpEstimate, ...]
    compose_step_estimates: tuple[ComposeStepEstimate, ...] = ()


@dataclass(frozen=True)
class CostEstimateTrace:
    total_cost: int
    component_totals: Mapping[str, int]
    batch_estimates: tuple[BatchEstimate, ...]
    op_estimates: tuple[OpEstimate, ...]
    compose_step_estimates: tuple[ComposeStepEstimate, ...] = ()
    update_build_crossproduct: float = 0.0
    composition_view_count: float = 0.0
    update_statement_count: float = 0.0


def estimate_executable_plan(executable_plan: ExecutableEvaluationPlan, workload: Workload | None = None) -> CostEstimateTrace:
    """estimate a concrete executable plan with batch/query-schedule detail."""

    workload = workload or executable_plan.workload
    if workload is None:
        raise CostModelError("estimate_executable_plan requires a workload")

    plan = executable_plan.evaluation_plan
    query_batches = executable_plan.query_batches
    maintained_views = plan.maintained_view_set or plan.strategy
    state = StateLevelTape(maintained_views)

    component_totals = {
        "update": 0,
        "compose": 0,
        "post_filter": 0,
    }
    batch_estimates: list[BatchEstimate] = []
    all_op_estimates: list[OpEstimate] = []
    all_compose_step_estimates: list[ComposeStepEstimate] = []
    total_cost = 0

    for batch_index in range(1, workload.k + 1):
        run_query = query_batches is None or batch_index in query_batches
        breakdown = batch_cost(plan, workload, batch_index, state, run_query=run_query)

        op_estimates: list[OpEstimate] = []
        compose_step_estimates: list[ComposeStepEstimate] = []
        for op in breakdown.update:
            estimate = OpEstimate(
                batch_index=batch_index,
                op_id=op.op_id,
                op_kind="update",
                predicted_input=op.input_cardinality,
                predicted_output=op.output_cardinality,
                predicted_total=op.total,
                component="update",
            )
            op_estimates.append(estimate)
        if run_query:
            for op in breakdown.compose:
                estimate = OpEstimate(
                    batch_index=batch_index,
                    op_id=op.op_id,
                    op_kind="compose",
                    predicted_input=op.input_cardinality,
                    predicted_output=op.output_cardinality,
                    predicted_total=op.total,
                    component="compose",
                )
                op_estimates.append(estimate)
            node_sizes: dict[View, int] = {}
            for node in plan.composition.nodes:
                if node.kind == "cache":
                    node_sizes[node.view] = state.get(node.view)
                else:
                    if len(node.view) != 1:
                        raise CostModelError(f"base_scan composition node must be singleton for {node.alias}")
                    variable = next(iter(node.view))
                    node_sizes[node.view] = _analytic_singleton_size(workload, batch_index, variable)
            compose_step_estimates.extend(
                ComposeStepEstimate(
                    batch_index=int(row["batch_index"]),
                    step_index=int(row["step_index"]),
                    join_op_id=str(row["join_op_id"]),
                    left_nodes_or_aliases=tuple(str(item) for item in row["left_nodes_or_aliases"]),
                    right_node_or_alias=str(row["right_node_or_alias"]),
                    join_type=str(row["join_type"]),
                    predicted_left_rows=float(row["predicted_left_rows"]),
                    predicted_right_rows=float(row["predicted_right_rows"]),
                    raw_pre_rule_output_rows=(
                        None
                        if row.get("raw_pre_rule_output_rows") is None
                        else float(row["raw_pre_rule_output_rows"])
                    ),
                    predicted_output_rows=float(row["predicted_output_rows"]),
                    predicted_intermediate_work=float(row["predicted_intermediate_work"]),
                    sigma_applied_here=(
                        None if row["sigma_applied_here"] is None else float(row["sigma_applied_here"])
                    ),
                    predicates_applied_here=tuple(str(item) for item in row["predicates_applied_here"]),
                    structural_predicates=tuple(str(item) for item in row["structural_predicates"]),
                    cardinality_rule=str(row.get("cardinality_rule", "default")),
                    containment_side=(
                        None if row.get("containment_side") is None else str(row["containment_side"])
                    ),
                    shared_key_divisor=float(row.get("shared_key_divisor", 1.0) or 1.0),
                )
                for row in compose_step_trace(plan, workload, batch_index, node_sizes)
            )
            post_filter = breakdown.post_filter
            op_estimates.append(
                OpEstimate(
                    batch_index=batch_index,
                    op_id=post_filter.op_id,
                    op_kind="post_filter",
                    predicted_input=post_filter.input_cardinality,
                    predicted_output=post_filter.output_cardinality,
                    predicted_total=post_filter.total,
                    component="post_filter",
                )
            )

        update_cost = sum(op.total for op in breakdown.update)
        compose_cost = sum(op.total for op in breakdown.compose) if run_query else 0
        post_filter_cost = breakdown.post_filter.total if run_query else 0
        component_totals["update"] += update_cost
        component_totals["compose"] += compose_cost
        component_totals["post_filter"] += post_filter_cost

        batch_total = update_cost + compose_cost + post_filter_cost
        total_cost += batch_total
        all_op_estimates.extend(op_estimates)
        all_compose_step_estimates.extend(compose_step_estimates)
        batch_estimates.append(
            BatchEstimate(
                batch_index=batch_index,
                run_query=run_query,
                update_cost=update_cost,
                compose_cost=compose_cost,
                post_filter_cost=post_filter_cost,
                total_cost=batch_total,
                state_snapshot=state.snapshot(),
                op_estimates=tuple(op_estimates),
                compose_step_estimates=tuple(compose_step_estimates),
            )
        )

    scalar_total = plan_cost(plan, workload, query_batches=query_batches)
    if scalar_total != total_cost:
        raise CostModelError(
            f"trace total {total_cost} does not match plan_cost scalar total {scalar_total}"
        )

    # update-side features for the ranking layer (do not affect total_cost). build cross-product
    # per materialized multi-var view = product of its constituents' analytic singleton sizes;
    # iterate update target views, not compose cover nodes, since build cost is update-phase.
    update_build_crossproduct = 0.0
    for update_op in plan.update_ops:
        if len(update_op.target_view) >= 2:
            product = 1.0
            for variable in update_op.target_view:
                product *= float(_analytic_singleton_size(workload, workload.k, variable))
            update_build_crossproduct += product
    composition_view_count = float(len(plan.update_ops))
    # per-INSERT commit floor: paid once per executed update statement, not once per view;
    # counted from the trace so schedules that skip update batches stay correct.
    update_statement_count = float(sum(1 for op in all_op_estimates if op.op_kind == "update"))

    return CostEstimateTrace(
        total_cost=total_cost,
        component_totals=dict(component_totals),
        batch_estimates=tuple(batch_estimates),
        op_estimates=tuple(all_op_estimates),
        compose_step_estimates=tuple(all_compose_step_estimates),
        update_build_crossproduct=update_build_crossproduct,
        composition_view_count=composition_view_count,
        update_statement_count=update_statement_count,
    )
