"""boundary descriptors for resolving plan metadata into executable plans.

,,PlanDescriptor'' is a configuration/boundary object, not the internal plan representation;
once resolved, costing, validation, and execution all share one ,,ExecutableEvaluationPlan'' path.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from eimer.plans.executable_plan import ExecutableEvaluationPlan, build_executable_evaluation_plan, validate_executable_plan
from eimer.plans.evaluation_plan import build_evaluation_plan_for_composition_variant
from eimer.models import DependencyGraph, StrategyWithPlans, View, view_name, view_sort_key
from eimer.selection.plan_selection import get_composition_variants
from eimer.workload import Workload


@dataclass(frozen=True)
class PlanDescriptor:
    """external identifier for a concrete EIMER evaluation plan.

    ,,kind="composition_variant"'' selects a generated ,,CompositionVariant'' by its index
    and resolves to the executable-plan representation before costing or validation.
    """

    kind: str
    strategy_id: str
    update_plan_idx: int
    composition_plan_idx: int | None = None
    composition_variant_idx: int | None = None
    composition_variant_mode: str = "canonical"
    max_join_orders_per_tree: int | None = None
    query_batches: frozenset[int] | None = None
    query_spec: str | None = None
    query_spec_source: str | None = None
    query_spec_fingerprint: str | None = None
    allowed_query_schedules: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


def _strategy_idx(strategy_id: str) -> int | None:
    if len(strategy_id) > 1 and strategy_id[0].upper() == "N" and strategy_id[1:].isdigit():
        return int(strategy_id[1:])
    return None


def _views_payload(views: set[View] | frozenset[View], dep_graph: DependencyGraph) -> tuple[str, ...]:
    return tuple(view_name(view, dep_graph.positions) for view in sorted(views, key=lambda v: view_sort_key(v, dep_graph.positions)))


def resolve_plan_descriptor(
    descriptor: PlanDescriptor,
    *,
    dep_graph: DependencyGraph,
    workload: Workload,
    strategy_catalog: Mapping[str, StrategyWithPlans],
    query_spec: str | None = None,
    base_table: str = "events",
    batch_table: str = "events_batch",
) -> ExecutableEvaluationPlan:
    """resolve a boundary descriptor into the canonical executable plan.

    the returned plan already includes the workload schedule and predicate ledger. validation
    errors are raised immediately; validation warnings are preserved as metadata.
    """

    if descriptor.query_spec and query_spec and descriptor.query_spec != query_spec:
        raise ValueError(
            f"descriptor query_spec {descriptor.query_spec!r} does not match requested query_spec {query_spec!r}"
        )

    try:
        strategy_with_plans = strategy_catalog[descriptor.strategy_id]
    except KeyError as exc:
        known = ", ".join(sorted(strategy_catalog))
        raise ValueError(f"unknown EIMER strategy id {descriptor.strategy_id!r}; known: {known}") from exc

    metadata: dict[str, Any] = {
        "descriptor_kind": descriptor.kind,
        "strategy_id": descriptor.strategy_id,
        "update_plan_idx": descriptor.update_plan_idx,
        "composition_plan_idx": descriptor.composition_plan_idx,
        "composition_variant_idx": descriptor.composition_variant_idx,
        "composition_variant_mode": descriptor.composition_variant_mode,
        "max_join_orders_per_tree": descriptor.max_join_orders_per_tree,
        "query_spec": descriptor.query_spec or query_spec,
        "query_spec_source": descriptor.query_spec_source,
        "query_spec_fingerprint": descriptor.query_spec_fingerprint,
        "allowed_query_schedules": descriptor.allowed_query_schedules,
        "materialized_views": _views_payload(strategy_with_plans.strategy, dep_graph),
        **dict(descriptor.metadata),
    }

    if descriptor.kind == "composition_variant":
        if descriptor.composition_variant_idx is None:
            raise ValueError("composition_variant descriptor requires composition_variant_idx")
        variants = get_composition_variants(
            dep_graph,
            strategy_with_plans,
            mode=descriptor.composition_variant_mode,
            max_join_orders_per_tree=descriptor.max_join_orders_per_tree,
        )
        if descriptor.composition_variant_idx < 0 or descriptor.composition_variant_idx >= len(variants):
            raise ValueError(
                f"{descriptor.strategy_id} has {len(variants)} composition variants for "
                f"mode {descriptor.composition_variant_mode!r}; requested {descriptor.composition_variant_idx}"
            )
        variant = variants[descriptor.composition_variant_idx]
        evaluation_plan = build_evaluation_plan_for_composition_variant(
            dep_graph,
            strategy_with_plans,
            variant,
            update_plan_idx=descriptor.update_plan_idx,
            base_table=base_table,
            batch_table=batch_table,
        )
        metadata.update(
            {
                "generation_method": variant.generation_method,
                "cover_idx": variant.cover_idx,
                "plan_idx_within_cover": variant.plan_idx_within_cover,
                "variant_idx": variant.variant_idx,
                "base_tree_idx": variant.base_tree_idx,
                "join_order_idx": variant.join_order_idx,
                "composition_cover": _views_payload(variant.cover.effective_view_set, dep_graph),
            }
        )
        composition_variant_idx = descriptor.composition_variant_idx
    else:
        raise ValueError(f"unknown PlanDescriptor kind {descriptor.kind!r}")

    executable = build_executable_evaluation_plan(
        dep_graph,
        evaluation_plan,
        workload,
        query_batches=descriptor.query_batches,
        strategy_idx=_strategy_idx(descriptor.strategy_id),
        update_plan_idx=descriptor.update_plan_idx,
        composition_variant_idx=composition_variant_idx,
        metadata=metadata,
    )
    validation = validate_executable_plan(executable)
    if not validation.ok:
        errors = "; ".join(issue.message for issue in validation.issues if issue.severity == "error")
        raise ValueError(f"resolved plan failed validation: {errors}")

    warnings = tuple(issue.code for issue in validation.issues if issue.severity == "warning")
    if warnings:
        executable = replace(
            executable,
            metadata={
                **dict(executable.metadata),
                "validation_warnings": warnings,
            },
        )
    return executable
