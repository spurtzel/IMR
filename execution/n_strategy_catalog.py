from __future__ import annotations

import bootstrap  # noqa: F401

import os
import re
from typing import Any, Dict, Iterable

from eimer.plans.evaluation_plan import (
    build_evaluation_plan,
)
from eimer.models import ordered_variables, view_name, view_sort_key
from eimer.pipeline import _build_strategy_outputs, generate_sql
from eimer.query.query_spec import (
    QuerySpec,
    query_spec_fingerprint,
    query_spec_to_dependency_graph,
    query_spec_to_dict,
)
from eimer.query.query_schema import variable_prefixed_column_type
from eimer.query.graph_adapter import collect_referenced_columns
from eimer.plans.strategy_space import enumerate_strategies
from eimer.plans.strategy_space import strategy_has_non_contiguous_subpattern
try:
    from eimer.plans.evaluation_plan import build_evaluation_plan_for_composition_variant
    from eimer.selection.plan_selection import get_composition_variants
    from eimer.sql.sql_emitter import generate_sql_statements_for_cover
except ImportError:
    build_evaluation_plan_for_composition_variant = None
    get_composition_variants = None
    generate_sql_statements_for_cover = None

from canonical_query import (
    canonical_query_spec,
    canonical_validation_fragments,
    validate_dependency_graph_matches_canonical,
)
from sql_compiler import build_path_a_sql, build_path_b_sql, normalize_n_composition_sql
from benchmark_types import BenchmarkStrategy, CanonicalDependentConfig, StrategyCatalogBundle


def _enumerate_eimer_strategies(
    config: CanonicalDependentConfig,
    *,
    only_with_nc_subpattern: bool,
    query_spec: QuerySpec | None = None,
):
    query_spec = query_spec or canonical_query_spec(config)
    dep_graph = query_spec_to_dependency_graph(query_spec)
    if query_spec.name == "canonical_r_b_m":
        validate_dependency_graph_matches_canonical(dep_graph, config)
    # Attaching plans to all 2^|views| strategies is intractable at n=4, so curate the view-sets
    # here before the attach when EIMER_CURATED_MAX_STRATEGIES>0. 0 = full enumeration.
    _strategies = enumerate_strategies(dep_graph)
    _curated = int(os.environ.get("EIMER_CURATED_MAX_STRATEGIES", "0"))
    if _curated > 0:
        _strategies = _curate_strategy_subset(
            dep_graph, _strategies, max_strategies=_curated,
            max_views_per_strategy=int(os.environ.get("EIMER_CURATED_MAX_VIEWS", "4")),
        )
    all_eimer_strategies = _build_strategy_outputs(dep_graph, _strategies)

    if only_with_nc_subpattern:
        eimer_strategies = [
            strategy
            for strategy in all_eimer_strategies
            if strategy_has_non_contiguous_subpattern(strategy.strategy, dep_graph.positions)
        ]
    else:
        eimer_strategies = list(all_eimer_strategies)

    return dep_graph, eimer_strategies


def build_eimer_strategy_index(
    config: CanonicalDependentConfig,
    *,
    only_with_nc_subpattern: bool = False,
    query_spec: QuerySpec | None = None,
):
    # Full enumeration attaches plans to all 2^|views| strategies, intractable at n=4. When
    # EIMER_CURATED_MAX_STRATEGIES>0, curate to a bounded subset here so every call site uses it.
    # 0 = full enumeration.
    _curated = int(os.environ.get("EIMER_CURATED_MAX_STRATEGIES", "0"))
    if _curated > 0:
        return build_curated_strategy_index(config, query_spec=query_spec, max_strategies=_curated)

    dep_graph, eimer_strategies = _enumerate_eimer_strategies(
        config,
        only_with_nc_subpattern=only_with_nc_subpattern,
        query_spec=query_spec,
    )
    index = {
        f"N{idx}": strategy_with_plans
        for idx, strategy_with_plans in enumerate(eimer_strategies, start=1)
    }
    return dep_graph, index


def _curate_strategy_subset(dep_graph, strategies, *, max_strategies, max_views_per_strategy):
    """Return a bounded, cross-spanning subset of the enumerated strategy view-sets.

    Keeps representatives spanning the materialization-granularity spectrum and the NC shapes,
    one+ per (,,max_view_size'', has_nc, n_views) class; drops strategies with more than
    ,,max_views_per_strategy'' views (redundant and expensive to attach). Composition-topology
    diversity comes from the per-strategy variant enumeration downstream."""
    positions = dep_graph.positions
    full_view = frozenset(dep_graph.variables)
    all_singletons = frozenset(frozenset({v}) for v in dep_graph.variables)

    def feats(st):
        return (max(len(v) for v in st), strategy_has_non_contiguous_subpattern(st, positions), len(st))

    # a deterministic sort key for a strategy (so selection is reproducible across runs)
    def skey(st):
        return tuple(sorted(view_sort_key(v, positions) for v in st))

    pool = [st for st in strategies if len(st) <= max_views_per_strategy]
    selected: list = []
    seen: set = set()

    def add(st):
        if st is None or st in seen or len(selected) >= max_strategies:
            return
        seen.add(st)
        selected.append(st)

    # endpoints first: the wide-internalizing full view, then the fully-deferred singletons.
    add(next((st for st in pool if st == frozenset({full_view})), None))
    add(next((st for st in pool if st == all_singletons), None))

    # one representative (the lowest-view-count, then deterministic) per structural class.
    groups: dict = {}
    for st in pool:
        groups.setdefault(feats(st), []).append(st)
    for key in sorted(groups):
        add(min(groups[key], key=lambda st: (len(st), skey(st))))

    # fill remaining budget breadth-first across classes (a 2nd representative each), small views first.
    for key in sorted(groups):
        rest = sorted(groups[key], key=lambda st: (len(st), skey(st)))
        for st in rest[1:]:
            add(st)

    return selected


def build_curated_strategy_index(
    config: CanonicalDependentConfig,
    *,
    query_spec: QuerySpec | None = None,
    max_strategies: int = 40,
    max_views_per_strategy: int | None = None,
):
    """Like ``build_eimer_strategy_index`` but attaches composition plans to a bounded, curated
    subset of strategies (``_curate_strategy_subset``), the tractable path at n=4. Returns
    ``(dep_graph, {N<i>: strategy_with_plans})`` like ``build_eimer_strategy_index``."""
    # max_views bounds the per-strategy composition-variant blow-up (2^views covers). Default 4
    # keeps the fully-deferred 4-singleton endpoint; override via EIMER_CURATED_MAX_VIEWS.
    if max_views_per_strategy is None:
        max_views_per_strategy = int(os.environ.get("EIMER_CURATED_MAX_VIEWS", "4"))
    query_spec = query_spec or canonical_query_spec(config)
    dep_graph = query_spec_to_dependency_graph(query_spec)
    if query_spec.name == "canonical_r_b_m":
        validate_dependency_graph_matches_canonical(dep_graph, config)
    curated = _curate_strategy_subset(
        dep_graph,
        enumerate_strategies(dep_graph),
        max_strategies=max_strategies,
        max_views_per_strategy=max_views_per_strategy,
    )
    eimer_strategies = _build_strategy_outputs(dep_graph, curated)
    index = {f"N{idx}": swp for idx, swp in enumerate(eimer_strategies, start=1)}
    return dep_graph, index


def _column_type(column: str, query_spec: QuerySpec | None = None, variable: str | None = None) -> str:
    if query_spec is not None and variable is not None:
        return variable_prefixed_column_type(query_spec, f"{variable}_{column}")
    col = column.lower()
    if col == "id":
        return "BIGINT"
    if col in {"time", "ts"}:
        return "TIMESTAMP(6)"
    if col in {"lat", "lon"}:
        return "DOUBLE"
    if col in {"primary_type", "etype"}:
        return "VARCHAR"
    return "VARCHAR"


def _cache_schema_for_strategy(strategy_with_plans, dep_graph, query_spec: QuerySpec | None = None) -> tuple[str, ...]:
    positions = dep_graph.positions
    columns_by_var = collect_referenced_columns(dep_graph)

    ddls: list[str] = []
    for view in sorted(strategy_with_plans.strategy, key=lambda v: view_sort_key(v, positions)):
        table_name = f"cache_{view_name(view, positions)}"
        cols: list[str] = []
        for variable in ordered_variables(view, positions):
            is_kleene = dep_graph.quantifiers.get(variable) in {"PLUS", "RELUCTANT_PLUS"}
            if is_kleene:
                cols.append(f"{variable}_first_id BIGINT")
                cols.append(f"{variable}_first_ts TIMESTAMP(6)")
                cols.append(f"{variable}_last_id BIGINT")
                cols.append(f"{variable}_last_ts TIMESTAMP(6)")
                cols.append(f"{variable}_count BIGINT")
            else:
                cols.append(f"{variable}_id {_column_type('id', query_spec, variable)}")
                cols.append(f"{variable}_ts {_column_type('ts', query_spec, variable)}")
            for col in columns_by_var.get(variable, []):
                if col in {"id", "ts"}:
                    continue
                if is_kleene:
                    cols.append(f"{variable}_first_{col} {_column_type(f'first_{col}', query_spec, variable)}")
                    cols.append(f"{variable}_last_{col} {_column_type(f'last_{col}', query_spec, variable)}")
                else:
                    cols.append(f"{variable}_{col} {_column_type(col, query_spec, variable)}")

        ddl = "CREATE TABLE {table} (\n    {cols}\n)\n".format(
            table=table_name,
            cols=",\n    ".join(cols),
        )
        ddls.append(ddl)

    return tuple(ddls)


def _serialize_n_summary(strategy_with_plans, dep_graph) -> Dict[str, Any]:
    positions = dep_graph.positions
    ordered_views = sorted(strategy_with_plans.strategy, key=lambda v: view_sort_key(v, positions))
    payload = {
        "strategy_views": [view_name(view, positions) for view in ordered_views],
        "effective_view_count": len(strategy_with_plans.effective_view_set),
        "composition_plan_count": len(strategy_with_plans.composition_plans),
        "composition_variant_count": len(getattr(strategy_with_plans, "composition_variants", ())),
        "update_plan_count": len(strategy_with_plans.update_plans),
        "has_non_contiguous_subpattern": strategy_has_non_contiguous_subpattern(
            strategy_with_plans.strategy,
            dep_graph.positions,
        ),
    }
    return payload


def _compile_n_strategy(
    strategy_id: str,
    strategy_with_plans,
    dep_graph,
    config: CanonicalDependentConfig,
    query_spec: QuerySpec,
    composition_plan_idx: int = 0,
    composition_variant_idx: int | None = None,
    composition_variant_mode: str = "canonical",
    max_join_orders_per_tree: int | None = None,
    update_plan_idx: int = 0,
) -> BenchmarkStrategy:
    composition_plan_count = len(strategy_with_plans.composition_plans)
    update_plan_count = len(strategy_with_plans.update_plans)
    if composition_variant_idx is None and (composition_plan_idx < 0 or composition_plan_idx >= composition_plan_count):
        raise ValueError(
            f"{strategy_id} has {composition_plan_count} composition plans "
            f"(0..{composition_plan_count - 1}), requested idx={composition_plan_idx}"
        )
    if update_plan_idx < 0 or update_plan_idx >= update_plan_count:
        raise ValueError(
            f"{strategy_id} has {update_plan_count} update plans "
            f"(0..{update_plan_count - 1}), requested idx={update_plan_idx}"
        )

    selected_variant = None
    if composition_variant_idx is None:
        statements = generate_sql(
            strategy_with_plans,
            dep_graph,
            composition_plan_idx=composition_plan_idx,
            update_plan_idx=update_plan_idx,
            base_table="events",
            batch_table="events_batch",
        )
        plan = build_evaluation_plan(
            dep_graph,
            strategy_with_plans,
            composition_plan_idx=composition_plan_idx,
            update_plan_idx=update_plan_idx,
            base_table="events",
            batch_table="events_batch",
        )
    else:
        if (
            build_evaluation_plan_for_composition_variant is None
            or get_composition_variants is None
            or generate_sql_statements_for_cover is None
        ):
            raise RuntimeError(
                "Composition-variant correctness requires the eimer package importable "
                "on the server. Copy eimer/*.py before using --composition-variant-idx."
            )
        variants = get_composition_variants(
            dep_graph,
            strategy_with_plans,
            mode=composition_variant_mode,
            max_join_orders_per_tree=max_join_orders_per_tree,
        )
        if composition_variant_idx < 0 or composition_variant_idx >= len(variants):
            raise ValueError(
                f"{strategy_id} has {len(variants)} composition variants for mode "
                f"{composition_variant_mode!r} (0..{len(variants) - 1}), requested idx={composition_variant_idx}"
            )
        selected_variant = variants[composition_variant_idx]
        statements = generate_sql_statements_for_cover(
            strategy_with_plans,
            dep_graph,
            selected_variant.cover.effective_view_set,
            selected_variant.plan,
            strategy_with_plans.update_plans[update_plan_idx],
            base_table="events",
            batch_table="events_batch",
        )
        plan = build_evaluation_plan_for_composition_variant(
            dep_graph,
            strategy_with_plans,
            selected_variant,
            update_plan_idx=update_plan_idx,
            base_table="events",
            batch_table="events_batch",
        )

    compose_sql = normalize_n_composition_sql(
        statements.composition_sql,
        variables=tuple(dep_graph.variables),
        query_spec=query_spec,
    )
    description = (
        "EIMER strategy over views "
        + ", ".join(_serialize_n_summary(strategy_with_plans, dep_graph)["strategy_views"])
    )
    if selected_variant is not None:
        description += (
            f" [composition_variant_idx={composition_variant_idx}, "
            f"mode={composition_variant_mode}, cover={selected_variant.cover_idx}, "
            f"plan={selected_variant.plan_idx_within_cover}]"
        )
    cache_statement_op_ids: list[str] = [update_op.op_id for update_op in plan.update_ops]

    if len(cache_statement_op_ids) != len(statements.update_statements):
        raise ValueError(
            "Execution-plan op_id alignment mismatch for benchmark strategy "
            f"{strategy_id}: {len(cache_statement_op_ids)} ids vs {len(statements.update_statements)} statements"
        )

    return BenchmarkStrategy(
        strategy_id=strategy_id,
        origin="eimer",
        description=description,
        cache_schemas=_cache_schema_for_strategy(strategy_with_plans, dep_graph, query_spec),
        cache_updates=tuple(statements.update_statements),
        compose_sql=compose_sql,
        path_a_sql=build_path_a_sql("composed", query_spec=query_spec),
        path_b_sql=build_path_b_sql(config, "composed", "events"),
        cache_statement_op_ids=tuple(cache_statement_op_ids),
        compose_op_id="compose",
        post_filter_op_id=plan.post_filter.op_id,
    )
