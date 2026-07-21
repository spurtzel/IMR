"""command-line entry point: compile a MATCH_RECOGNIZE query and emit a parser
artifact (ir, dependency graph, subpatterns, strategies) or a full planned pipeline
summary with generated sql."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

from eimer.query.dependency_graph import build_dependency_graph
from eimer.models import ConditionType, JoinType, StrategyMode, StrategyWithPlans, view_name
from eimer.query.parser import compile_sql_file, compile_sql_text
from eimer.pipeline import build_from_dep_graph_json, build_from_sql, generate_sql
from eimer.plans.subpattern_enumeration import (
    describe_view,
    enumerate_strategies as enumerate_legacy_strategies,
    enumerate_valid_subpatterns as enumerate_legacy_valid_subpatterns,
    serialize_strategy,
)
from eimer.plans.strategy_space import strategy_has_non_contiguous_subpattern


EMIT_CHOICES = ("ir", "dependency-graph", "valid-subpatterns", "strategies")


def build_arg_parser() -> argparse.ArgumentParser:
    """build the argument parser for the cli."""
    parser = argparse.ArgumentParser(
        description="EIMER: parser/dependency-graph analysis plus full pipeline planning and SQL generation"
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--sql-file", help="Path to a SQL file containing exactly one MATCH_RECOGNIZE query")
    src.add_argument("--sql-text", help="Inline SQL text containing exactly one MATCH_RECOGNIZE query")
    src.add_argument("--dep-graph-json", help="Prebuilt dependency graph JSON path")

    parser.add_argument(
        "--emit",
        choices=EMIT_CHOICES,
        default=None,
        help="Emit the legacy parser/dependency-graph artifact. Defaults to 'ir' when no full-pipeline options are used.",
    )
    parser.add_argument(
        "--strategy-mode",
        choices=[item.value for item in StrategyMode],
        help="Legacy strategy/view enumeration mode for --emit valid-subpatterns|strategies.",
    )

    parser.add_argument("--base-table", default="events", help="Base table name for generated SQL")
    parser.add_argument("--batch-table", default="events_batch", help="Batch table name for generated SQL")
    parser.add_argument("--strategy-index", type=int, default=None, help="Strategy index for SQL generation")
    parser.add_argument("--composition-plan-index", type=int, default=0, help="Composition plan index")
    parser.add_argument("--update-plan-index", type=int, default=0, help="Update plan index")
    parser.add_argument(
        "--only-with-nc-subpattern",
        action="store_true",
        help="Only keep strategies that contain at least one non-contiguous subpattern",
    )
    parser.add_argument("--output-json", default=None, help="Write full-pipeline summary JSON to path")
    return parser



def _resolve_strategy_mode(raw_value: str | None, emit: str) -> StrategyMode:
    """pick the strategy enumeration mode, defaulting to dependency-restricted."""
    if emit not in {"valid-subpatterns", "strategies"}:
        if raw_value is None:
            return StrategyMode.DEPENDENCY_RESTRICTED
        return StrategyMode(raw_value)
    if raw_value is None:
        return StrategyMode.DEPENDENCY_RESTRICTED
    return StrategyMode(raw_value)



def _serialize_strategy_summary(strategy: StrategyWithPlans, positions: Dict[str, int]) -> Dict[str, Any]:
    """summarize one strategy (view names, plan counts, join/condition tallies) as a json dict."""
    join_type_counts = {join_type.value: 0 for join_type in JoinType}
    for edge in strategy.composition_join_graph.edges.values():
        join_type_counts[edge.join_type.value] += 1

    deferred_type_counts = {cond.value: 0 for cond in ConditionType}
    for edge in strategy.composition_join_graph.edges.values():
        for cond in edge.deferred_conditions:
            deferred_type_counts[cond.condition_type.value] += 1

    strategy_views = sorted([view_name(view, positions) for view in strategy.strategy])

    return {
        "strategy_views": strategy_views,
        "effective_view_count": len(strategy.effective_view_set),
        "composition_plan_count": len(strategy.composition_plans),
        "update_plan_count": len(strategy.update_plans),
        "join_type_counts": join_type_counts,
        "deferred_condition_type_counts": deferred_type_counts,
        "has_non_contiguous_subpattern": strategy_has_non_contiguous_subpattern(
            strategy.strategy,
            positions,
        ),
    }



def _apply_strategy_filter(strategies: List[StrategyWithPlans], positions: Dict[str, int],
                           only_with_nc_subpattern: bool) -> List[StrategyWithPlans]:
    """optionally keep only strategies with a non-contiguous subpattern."""
    if not only_with_nc_subpattern:
        return strategies

    return [
        strategy
        for strategy in strategies
        if strategy_has_non_contiguous_subpattern(strategy.strategy, positions)
    ]



def _should_run_full_pipeline(args: argparse.Namespace) -> bool:
    """true when the args request full-pipeline planning rather than a legacy emit."""
    return bool(
        args.dep_graph_json
        or args.output_json is not None
        or args.strategy_index is not None
        or args.only_with_nc_subpattern
    )



def _run_legacy_emit_mode(args: argparse.Namespace) -> int:
    """emit a single parser artifact (ir, dependency graph, subpatterns, or strategies) as json."""
    if args.dep_graph_json:
        raise ValueError("--dep-graph-json is only supported in full-pipeline summary mode")

    if args.sql_file:
        result = compile_sql_file(args.sql_file)
    else:
        result = compile_sql_text(args.sql_text)

    if not result.ok or result.ir is None:
        json.dump({"diagnostics": result.diagnostics_to_dict()}, sys.stderr, indent=2, sort_keys=True)
        sys.stderr.write("\n")
        return 1

    emit = args.emit or "ir"
    if emit == "ir":
        payload = result.ir.to_dict()
    else:
        dep_graph = build_dependency_graph(result.ir)
        if emit == "dependency-graph":
            payload = dep_graph.to_dict()
        else:
            strategy_mode = _resolve_strategy_mode(args.strategy_mode, emit)
            valid_subpatterns = enumerate_legacy_valid_subpatterns(dep_graph, strategy_mode)
            if emit == "valid-subpatterns":
                payload = {
                    "strategy_mode": strategy_mode.value,
                    "summary": {
                        "valid_subpattern_count": len(valid_subpatterns),
                    },
                    "valid_subpatterns": [describe_view(view, dep_graph) for view in valid_subpatterns],
                }
            else:
                strategies = enumerate_legacy_strategies(dep_graph, strategy_mode)
                payload = {
                    "strategy_mode": strategy_mode.value,
                    "summary": {
                        "valid_subpattern_count": len(valid_subpatterns),
                        "strategy_count": len(strategies),
                    },
                    "strategies": [serialize_strategy(strategy, dep_graph) for strategy in strategies],
                }

    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0



def _run_full_pipeline_mode(args: argparse.Namespace) -> int:
    """run the full planning pipeline, print/write the summary, and optionally generate sql."""
    if args.dep_graph_json:
        dep_graph, all_strategies = build_from_dep_graph_json(args.dep_graph_json)
    else:
        dep_graph, all_strategies = build_from_sql(
            sql_text=args.sql_text,
            sql_file=args.sql_file,
        )

    strategies = _apply_strategy_filter(
        all_strategies,
        dep_graph.positions,
        args.only_with_nc_subpattern,
    )

    strategy_payloads = []
    for strategy in strategies:
        strategy_summary = _serialize_strategy_summary(strategy, dep_graph.positions)
        strategy_payloads.append(strategy_summary)

    summary = {
        "variable_count": len(dep_graph.variables),
        "variables": dep_graph.variables,
        "strategy_count": len(strategies),
        "strategy_count_total": len(all_strategies),
        "only_with_nc_subpattern": args.only_with_nc_subpattern,
        "strategies": strategy_payloads,
    }

    if args.output_json:
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(out_path)
    else:
        print(json.dumps(summary, indent=2))

    if args.strategy_index is not None:
        if args.strategy_index < 0 or args.strategy_index>=len(strategies):
            raise IndexError("strategy-index out of range")

        statements = generate_sql(
            strategies[args.strategy_index],
            dep_graph,
            composition_plan_idx=args.composition_plan_index,
            update_plan_idx=args.update_plan_index,
            base_table=args.base_table,
            batch_table=args.batch_table,
        )

        print("\n-- UPDATE SQL --")
        for statement in statements.update_statements:
            print(statement)
            print()

        print("-- COMPOSITION SQL --")
        print(statements.composition_sql)
        print()

        print("-- POST FILTER SQL --")
        print(statements.post_filter_sql)

    return 0



def main(argv: list[str] | None = None) -> int:
    """cli entry point: dispatch to the full pipeline or the legacy emit path."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if _should_run_full_pipeline(args):
        return _run_full_pipeline_mode(args)
    return _run_legacy_emit_mode(args)
