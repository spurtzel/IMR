"""explicit-cover strategy bundle for the config-runner sweep.

the sweep selects a plan with clique_grow or edge_cover and passes the chosen cover (its
view names) to the executor. this module turns that cover into the single ,,SEL'' strategy
the SQL renderer expects, via plan_build.build_plan (bushy update trees, bushy-priced
compose), bypassing the enumerated strategy catalog so it is robust at every pattern length.
"""
from __future__ import annotations

from typing import Iterable

from benchmark_types import CanonicalDependentConfig
from n_strategy_catalog import _compile_n_strategy, _serialize_n_summary
from benchmark_types import StrategyCatalogBundle
from eimer.models import view_name
from eimer.query.graph_adapter import build_dependency_graph_from_sql_text
from eimer.query.query_spec import query_spec_to_dict, query_spec_to_match_recognize_sql
from eimer.selection.plan_build import build_plan, valid_views
from eimer.selection.plan_selection_clique_grow import select_clique_grow_plan
from eimer.selection.plan_selection_edge_cover import select_edge_cover_plan

SELECTORS = {"clique_grow": select_clique_grow_plan, "edge_cover": select_edge_cover_plan}
# the single strategy id the cover is registered under; N<int> so the bundle's ordering key parses.
SEL_STRATEGY_ID = "N1"


def dependency_graph_for_spec(query_spec):
    """the dependency graph the selectors and the SQL renderer plan over."""
    return build_dependency_graph_from_sql_text(query_spec_to_match_recognize_sql(query_spec))


def run_selector(dep, workload, selector, *, max_storage_rows=None, query_batches=None):
    """run one cover selector and return the chosen cover (a frozenset of Views)."""
    if selector not in SELECTORS:
        raise ValueError(f"unknown selector {selector!r}; expected one of {sorted(SELECTORS)}")
    pick = SELECTORS[selector](dep, workload, max_storage_rows=max_storage_rows,
                               query_batches=query_batches)
    return frozenset(pick.compose), pick


def cover_view_names(cover, dep) -> list[str]:
    """the sorted view-name labels of a cover (the wire form passed to the executor)."""
    return sorted(view_name(v, dep.positions) for v in cover)


def _views_from_names(names: Iterable[str], dep):
    """map view-name labels back to View objects via the valid-view set."""
    by_name = {view_name(v, dep.positions): v for v in valid_views(dep)}
    out = set()
    for name in names:
        if name not in by_name:
            raise ValueError(f"cover view {name!r} is not a valid view; known: {sorted(by_name)}")
        out.add(by_name[name])
    return frozenset(out)


def build_explicit_cover_bundle(config, query_spec, cover_names, workload, *,
                                query_spec_source="registry", query_spec_fingerprint=None) -> StrategyCatalogBundle:
    """build the single-strategy bundle for an explicit cover, exactly as the catalog would for a
    materialized strategy (bushy update + compose plans from plan_build.build_plan)."""
    dep = dependency_graph_for_spec(query_spec)
    cover = _views_from_names(cover_names, dep)
    all_vars = set(dep.variables)
    strategy_with_plans, _plan, _nodes = build_plan(dep, cover, cover, all_vars, workload=workload)
    benchmark_strategy = _compile_n_strategy(SEL_STRATEGY_ID, strategy_with_plans, dep,
                                             config or CanonicalDependentConfig(), query_spec,
                                             composition_plan_idx=0)
    summary = _serialize_n_summary(strategy_with_plans, dep)
    return StrategyCatalogBundle(
        config=config or CanonicalDependentConfig(),
        query_spec_name=query_spec.name,
        dep_graph=dep,
        legacy_strategies={},
        n_strategies={SEL_STRATEGY_ID: benchmark_strategy},
        n_summaries={SEL_STRATEGY_ID: summary},
        only_with_nc_subpattern=False,
        query_spec_source=query_spec_source,
        query_spec_fingerprint=query_spec_fingerprint,
        query_spec_payload=query_spec_to_dict(query_spec),
    )
