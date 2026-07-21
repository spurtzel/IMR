"""workload-level introspection for fixed EIMER execution plans.

,,EvaluationPlan'' is the reusable lowered program, independent of any concrete workload
schedule. ,,WorkloadEvaluationPlan'' binds it to a workload and query schedule and exposes a
per-batch ,,BatchProgram'' view. introspection-only: it does not execute SQL or change cost
formulas.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Collection, Mapping, Tuple

from eimer.plans.evaluation_plan import (
    CompositionOp,
    EvaluationPlan,
    PostFilterOp,
    UpdateOp,
)
from eimer.query.query_schedule import normalize_query_batches
from eimer.workload import Workload


class BatchStepKind(str, Enum):
    UPDATE = "update"
    COMPOSE = "compose"
    POST_FILTER = "post_filter"
    WATERMARK = "watermark"


@dataclass(frozen=True)
class WatermarkAdvance:
    operator_id: str
    sql: str


@dataclass(frozen=True)
class BatchStep:
    kind: BatchStepKind
    batch_index: int
    operator_id: str
    phase: str
    payload: UpdateOp | CompositionOp | PostFilterOp | WatermarkAdvance
    parent_operator_id: str | None = None
    active: bool = True


@dataclass(frozen=True)
class BatchProgram:
    batch_index: int
    steps: Tuple[BatchStep, ...]


@dataclass(frozen=True)
class WorkloadEvaluationPlan:
    """a fixed execution plan bound to a workload and optional query schedule.

    ,,query_batches=None'' means every 1-based batch is followed by composition and
    post-filtering; an empty frozenset means update-only maintenance. no adaptive per-batch
    strategy changes.
    """

    evaluation_plan: EvaluationPlan
    query_batches: frozenset[int] | None
    batch_count: int

    def has_query(self, batch_index: int) -> bool:
        _validate_batch_index(batch_index, self.batch_count)
        return self.query_batches is None or batch_index in self.query_batches

    def program_for_batch(self, batch_index: int) -> BatchProgram:
        return batch_program(self, batch_index)


def _validate_batch_index(batch_index: int, batch_count: int) -> None:
    if batch_index < 1 or batch_index > batch_count:
        raise ValueError(
            f"batch_index is 1-based and must be within [1, {batch_count}], got {batch_index!r}"
        )


def _normalize_query_batches(query_batches: Collection[int] | None, batch_count: int) -> frozenset[int] | None:
    return normalize_query_batches(query_batches, batch_count)


def build_workload_evaluation_plan(
    evaluation_plan: EvaluationPlan,
    workload: Workload,
    *,
    query_batches: Collection[int] | None = None,
) -> WorkloadEvaluationPlan:
    """bind a lowered plan to a workload and query schedule."""

    return WorkloadEvaluationPlan(
        evaluation_plan=evaluation_plan,
        query_batches=_normalize_query_batches(query_batches, workload.k),
        batch_count=workload.k,
    )




def batch_program(workload_evaluation_plan: WorkloadEvaluationPlan, batch_index: int) -> BatchProgram:
    """return the ordered introspection program for one 1-based batch."""

    _validate_batch_index(batch_index, workload_evaluation_plan.batch_count)
    plan = workload_evaluation_plan.evaluation_plan
    steps: list[BatchStep] = []

    for update_op in plan.update_ops:
        steps.append(
            BatchStep(
                kind=BatchStepKind.UPDATE,
                batch_index=batch_index,
                operator_id=update_op.operator_id or update_op.op_id,
                phase="update",
                payload=update_op,
            )
        )

    watermark_operator_id = "watermark:advance"
    steps.append(
        BatchStep(
            kind=BatchStepKind.WATERMARK,
            batch_index=batch_index,
            operator_id=watermark_operator_id,
            phase="watermark_advance",
            payload=WatermarkAdvance(
                operator_id=watermark_operator_id,
                sql=f"INSERT INTO watermark_state SELECT max(time) FROM {plan.base_table};",
            ),
        )
    )

    if workload_evaluation_plan.has_query(batch_index):
        steps.append(
            BatchStep(
                kind=BatchStepKind.COMPOSE,
                batch_index=batch_index,
                operator_id=plan.composition.operator_id,
                phase="compose",
                payload=plan.composition,
            )
        )
        steps.append(
            BatchStep(
                kind=BatchStepKind.POST_FILTER,
                batch_index=batch_index,
                operator_id=plan.post_filter.operator_id or plan.post_filter.op_id,
                phase="post_filter",
                payload=plan.post_filter,
            )
        )

    return BatchProgram(batch_index=batch_index, steps=tuple(steps))
