"""composition-variant expansion for a materialized strategy: the canonical variants, plus the
bounded left-deep join-order expansion used when a plan descriptor is resolved."""

from __future__ import annotations

from dataclasses import replace
from typing import Tuple

from eimer.models import (
    CompositionCover,
    CompositionPlan,
    CompositionVariant,
    DependencyGraph,
    StrategyWithPlans,
    view_sort_key,
)
from eimer.plans.composition import build_composition_join_graph_for_nodes, enumerate_left_deep_join_orders_for_tree


def _legacy_composition_variants(
    dep_graph: DependencyGraph, strategy_with_plans: StrategyWithPlans
) -> Tuple[CompositionVariant, ...]:
    if strategy_with_plans.composition_variants:
        return tuple(strategy_with_plans.composition_variants)

    positions = dep_graph.positions
    materialized = tuple(
        sorted(
            (view for view in strategy_with_plans.effective_view_set if view in strategy_with_plans.strategy),
            key=lambda view: view_sort_key(view, positions),
        )
    )
    singleton_views = tuple(
        sorted(
            (view for view in strategy_with_plans.effective_view_set if view not in strategy_with_plans.strategy),
            key=lambda view: view_sort_key(view, positions),
        )
    )
    cover = CompositionCover(
        materialized_views=materialized,
        singleton_views=singleton_views,
        effective_view_set=frozenset(strategy_with_plans.effective_view_set),
    )
    join_graph = strategy_with_plans.composition_join_graph or build_composition_join_graph_for_nodes(
        dep_graph,
        set(cover.effective_view_set),
    )
    return tuple(
        CompositionVariant(
            cover=cover,
            join_graph=join_graph,
            plan=plan,
            cover_idx=0,
            plan_idx_within_cover=plan_idx,
            variant_idx=plan_idx,
        )
        for plan_idx, plan in enumerate(strategy_with_plans.composition_plans)
    )


def get_composition_variants(
    dep_graph: DependencyGraph, strategy_with_plans: StrategyWithPlans,
    *, mode: str = "canonical", max_join_orders_per_tree: int | None = None,
) -> Tuple[CompositionVariant, ...]:
    if max_join_orders_per_tree is not None and max_join_orders_per_tree <= 0:
        raise ValueError("max_join_orders_per_tree must be positive when provided")

    base_variants = _legacy_composition_variants(dep_graph, strategy_with_plans)
    if mode == "canonical":
        return base_variants
    if mode != "bounded_left_deep":
        raise ValueError(f"unknown composition variant mode: {mode!r}")

    expanded: list[CompositionVariant] = []
    for base_idx, base_variant in enumerate(base_variants):
        orders = enumerate_left_deep_join_orders_for_tree(
            base_variant.cover.effective_view_set,
            base_variant.plan.edges,
            dep_graph.positions,
            canonical_order=base_variant.plan.join_order,
            max_orders=max_join_orders_per_tree,
        )
        for order_idx, order in enumerate(orders):
            plan = CompositionPlan(
                edges=list(base_variant.plan.edges),
                join_types=dict(base_variant.plan.join_types),
                join_order=list(order),
                deferred_conditions={
                    edge: list(conditions)
                    for edge, conditions in base_variant.plan.deferred_conditions.items()
                },
            )
            expanded.append(
                replace(
                    base_variant,
                    plan=plan,
                    generation_method="bounded_left_deep",
                    variant_idx=len(expanded),
                    base_tree_idx=base_variant.variant_idx if base_variant.variant_idx is not None else base_idx,
                    join_order_idx=order_idx,
                )
            )
    return tuple(expanded)
