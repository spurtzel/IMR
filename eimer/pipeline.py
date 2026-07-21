"""pipeline glue: build strategies-with-plans from sql or a dependency-graph json,
and render sql for a chosen strategy."""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from eimer.models import CompositionVariant, SQLStatements, StrategyWithPlans
from eimer.query.graph_adapter import (
    build_dependency_graph_from_json,
    build_dependency_graph_from_sql_file,
    build_dependency_graph_from_sql_text,
)
from eimer.plans.strategy_space import enumerate_strategies
from eimer.plans.composition import (
    build_composition_join_graph_for_nodes,
    enumerate_composition_covers,
    enumerate_composition_plans,
)
from eimer.plans.update_plans import enumerate_update_plans
from eimer.sql.sql_emitter import generate_sql_statements


def _build_strategy_outputs(dep_graph, strategies) -> List[StrategyWithPlans]:
    """wrap each raw strategy with its composition covers, plans, update plans, and variants."""
    outputs: List[StrategyWithPlans] = []
    for strategy in strategies:
        composition_covers = tuple(enumerate_composition_covers(strategy, dep_graph))
        legacy_cover = composition_covers[0]
        join_graph = build_composition_join_graph_for_nodes(dep_graph, set(legacy_cover.effective_view_set))
        composition_plans = enumerate_composition_plans(join_graph, dep_graph.positions)
        update_plans = enumerate_update_plans(strategy, dep_graph)

        composition_variants: list[CompositionVariant] = []
        for cover_idx, cover in enumerate(composition_covers):
            cover_join_graph = build_composition_join_graph_for_nodes(dep_graph, set(cover.effective_view_set))
            cover_plans = enumerate_composition_plans(cover_join_graph, dep_graph.positions)
            for plan_idx, plan in enumerate(cover_plans):
                composition_variants.append(
                    CompositionVariant(
                        cover=cover,
                        join_graph=cover_join_graph,
                        plan=plan,
                        cover_idx=cover_idx,
                        plan_idx_within_cover=plan_idx,
                        variant_idx=len(composition_variants),
                    )
                )

        outputs.append(
            StrategyWithPlans(
                strategy=set(strategy),
                effective_view_set=set(join_graph.nodes),
                composition_join_graph=join_graph,
                composition_plans=composition_plans,
                update_plans=update_plans,
                composition_covers=composition_covers,
                composition_variants=tuple(composition_variants),
            )
        )
    return outputs


def build_from_sql(sql_text: Optional[str] = None, sql_file: Optional[str | Path] = None) -> tuple:
    """build strategies-with-plans from inline sql or a sql file (exactly one)."""
    if bool(sql_text)==bool(sql_file):
        raise ValueError("Provide exactly one of sql_text or sql_file")

    if sql_file:
        dep_graph = build_dependency_graph_from_sql_file(sql_file)
    else:
        dep_graph = build_dependency_graph_from_sql_text(sql_text or "")

    strategies = enumerate_strategies(dep_graph)
    outputs = _build_strategy_outputs(dep_graph, strategies)
    return dep_graph, outputs


def build_from_dep_graph_json(path: str | Path) -> tuple:
    """build strategies-with-plans from a prebuilt dependency-graph json."""
    dep_graph = build_dependency_graph_from_json(path)
    strategies = enumerate_strategies(dep_graph)
    outputs = _build_strategy_outputs(dep_graph, strategies)
    return dep_graph, outputs


def generate_sql(strategy_with_plans: StrategyWithPlans, dep_graph, composition_plan_idx: int,
                 update_plan_idx: int, base_table: str = "events",
                 batch_table: str = "events_batch") -> SQLStatements:
    """render sql for a strategy by composition-plan and update-plan index."""

    if composition_plan_idx < 0 or composition_plan_idx >= len(strategy_with_plans.composition_plans):
        raise IndexError("composition_plan_idx out of range")
    if update_plan_idx < 0 or update_plan_idx >= len(strategy_with_plans.update_plans):
        raise IndexError("update_plan_idx out of range")

    composition_plan = strategy_with_plans.composition_plans[composition_plan_idx]
    update_plan = strategy_with_plans.update_plans[update_plan_idx]

    return generate_sql_statements(
        strategy_with_plans,
        dep_graph,
        composition_plan,
        update_plan,
        base_table,
        batch_table,
    )
