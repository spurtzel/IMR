"""runtime invariant checks for sigma placement in the cost model.

each dependency edge must be applied exactly once across the composition cover: internalized when both
its variables sit in one view leaf, deferred when it crosses a composition join cut. maintained helper
views omitted from composition are not part of the query-result sigma partition.

the strict ,,== 1'' rule is enforced only for non-nested plans; nested covers (e.g. ,,{R}'' joined with
,,{R, M}'') reuse the same edge across materialized outputs, so it does not apply to them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Tuple

from eimer.plans.evaluation_plan import CompositionNode, EvaluationPlan, JoinOp, SourceBlock, UpdateOp
from eimer.models import canonical_variable_pair
from eimer.workload import Workload


@dataclass(frozen=True)
class EdgeAccounting:
    internalized_ops: Tuple[str, ...]
    cross_block_ops: Tuple[str, ...]
    deferred_ops: Tuple[str, ...]

    @property
    def internalized(self) -> int:
        return len(self.internalized_ops)

    @property
    def cross_block(self) -> int:
        return len(self.cross_block_ops)

    @property
    def deferred(self) -> int:
        return len(self.deferred_ops)

    @property
    def total(self) -> int:
        return self.internalized + self.cross_block + self.deferred


def _block_covers_variable(block: SourceBlock, variable: str) -> bool:
    return variable in block.variables


def _update_bucket(update_op: UpdateOp, left: str, right: str) -> str | None:
    left_blocks: list[int] = []
    right_blocks: list[int] = []
    for idx, block in enumerate(update_op.source_blocks):
        left_here = _block_covers_variable(block, left)
        right_here = _block_covers_variable(block, right)
        if left_here and right_here:
            return "internalized"
        if left_here:
            left_blocks.append(idx)
        if right_here:
            right_blocks.append(idx)

    if left_blocks and right_blocks and any(left_idx != right_idx for left_idx in left_blocks for right_idx in right_blocks):
        return "cross_block"
    return None


def _composition_node_contains_edge(node: CompositionNode, left: str, right: str) -> bool:
    return node.kind == "cache" and left in node.view and right in node.view


def _node_accounting_label(node: CompositionNode, update_by_target: dict[frozenset[str], UpdateOp]) -> str:
    update_op = update_by_target.get(node.view)
    if update_op is not None:
        return update_op.op_id
    return node.alias


def _strictly_crosses_cut(join_op: JoinOp, left: str, right: str) -> bool:
    left_in_anchor = left in join_op.anchor_view
    left_in_new = left in join_op.new_view
    right_in_anchor = right in join_op.anchor_view
    right_in_new = right in join_op.new_view
    return (
        (left_in_anchor and not left_in_new and right_in_new and not right_in_anchor)
        or (right_in_anchor and not right_in_new and left_in_new and not left_in_anchor)
    )


def _format_bucket(name: str, op_ids: Iterable[str]) -> str:
    ops = tuple(op_ids)
    if not ops:
        return f"{name}=0"
    return f"{name}={len(ops)} [{', '.join(ops)}]"


def _violation_explanation(pair: tuple[str, str], accounting: EdgeAccounting) -> str:
    sigma_name = f"sigma({pair[0]}, {pair[1]})"
    if accounting.total==0:
        return (
            f"This means {sigma_name} is never applied in the cost model, "
            "which over-predicts cardinality."
        )
    if accounting.total == 2:
        return (
            f"This means {sigma_name} is applied twice in the cost model, "
            "which under-predicts cardinality."
        )
    return (
        f"This means {sigma_name} is applied {accounting.total} times in the cost model, "
        "which makes cardinality predictions inconsistent."
    )


def _has_nested_composition_cover(plan: EvaluationPlan) -> bool:
    views = [node.view for node in plan.composition.nodes]
    return any(a < b or b < a for i, a in enumerate(views) for b in views[i + 1 :])


def _has_nested_composition_join(plan: EvaluationPlan) -> bool:
    for join_op in plan.composition.join_ops:
        if join_op.anchor_view < join_op.new_view or join_op.new_view < join_op.anchor_view:
            return True
    return False


def validate_sigma_partition(plan: EvaluationPlan, workload: Workload) -> None:
    from eimer.cost.cost_model import CostModelError

    if _has_nested_composition_cover(plan) or _has_nested_composition_join(plan):
        return

    positions = workload.dependency_graph.positions
    update_by_target = {update_op.target_view: update_op for update_op in plan.update_ops}
    composition_variables = set()
    for node in plan.composition.nodes:
        composition_variables.update(node.view)
    violations: list[str] = []

    for edge in workload.dependency_graph.edges:
        pair = canonical_variable_pair(edge.var_left, edge.var_right, positions)
        internalized_ops: list[str] = []
        cross_block_ops: list[str] = []
        deferred_ops: list[str] = []

        for node in plan.composition.nodes:
            if _composition_node_contains_edge(node, pair[0], pair[1]):
                update_op = update_by_target.get(node.view)
                bucket = _update_bucket(update_op, pair[0], pair[1]) if update_op is not None else None
                if bucket == "cross_block":
                    cross_block_ops.append(_node_accounting_label(node, update_by_target))
                else:
                    internalized_ops.append(_node_accounting_label(node, update_by_target))

        for join_op in plan.composition.join_ops:
            if _strictly_crosses_cut(join_op, pair[0], pair[1]):
                deferred_ops.append(join_op.op_id)
                break

        if (
            not internalized_ops
            and not cross_block_ops
            and not deferred_ops
            and pair[0] in composition_variables
            and pair[1] in composition_variables
        ):
            deferred_ops.append("compose")

        accounting = EdgeAccounting(
            internalized_ops=tuple(internalized_ops),
            cross_block_ops=tuple(cross_block_ops),
            deferred_ops=tuple(deferred_ops),
        )
        if accounting.total == 1:
            continue

        violations.append(
            "\n".join(
                [
                    f"sigma-partition invariant violated for edge ({pair[0]}, {pair[1]}):",
                    "  "
                    + _format_bucket("internalized", accounting.internalized_ops)
                    + ", "
                    + _format_bucket("cross_block", accounting.cross_block_ops)
                    + ", "
                    + _format_bucket("deferred", accounting.deferred_ops),
                    f"  Total={accounting.total}, expected 1.",
                    f"  {_violation_explanation(pair, accounting)}",
                ]
            )
        )

    if violations:
        raise CostModelError("\n\n".join(violations))
