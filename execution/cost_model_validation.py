from __future__ import annotations

import argparse
import ast
import csv
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Mapping

import bootstrap  # noqa: F401

from benchmark_types import CanonicalDependentConfig
from n_strategy_catalog import build_eimer_strategy_index
from query_registry import CANONICAL_QUERY_SPEC_NAME, get_query_spec
from eimer.query.query_spec import load_query_spec_file, query_spec_from_dict
from selectivity_payload import (
    load_selectivities as _load_selectivities_from_payload,
    parse_selectivities_payload as _parse_selectivities_from_payload,
    payload_rho,
)
from eimer.cost.cost_estimation import CostEstimateTrace, estimate_executable_plan
from eimer.cost.cost_model import (
    CostModelError,
    StateLevelTape,
    compose_cost,
    postfilter_cost,
    update_op_cost,
)
from eimer.models import View, view_name
from eimer.plans.plan_descriptor import PlanDescriptor, resolve_plan_descriptor
from eimer.query.query_schedule import normalize_query_batches
from eimer.workload import Selectivities, Workload


BENCHMARK_RHO = 1000.0
# the per-run measurement filename ,,run<N>_cost_validation_measurements.csv'' (captures N).
RUN_MEASUREMENTS_RE = re.compile(r"^run(\d+)_cost_validation_measurements\.csv$")


def parse_sql_header_metadata(path: Path) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("-- "):
            break
        if "=" not in line:
            continue
        key, value = line[3:].split("=", 1)
        metadata[key.strip()] = value.strip()

    batch_sizes_raw = metadata.get("BATCH_SIZES")
    if batch_sizes_raw is None:
        raise ValueError(f"Missing BATCH_SIZES header in {path}")
    strategies_raw = metadata.get("STRATEGIES", "")
    metadata["batch_sizes"] = tuple(int(value) for value in ast.literal_eval(str(batch_sizes_raw)))
    metadata["strategies"] = tuple(item.strip() for item in str(strategies_raw).split(",") if item.strip())
    metadata["query_spec"] = str(metadata.get("QUERY_SPEC", CANONICAL_QUERY_SPEC_NAME) or CANONICAL_QUERY_SPEC_NAME)
    metadata["query_spec_fingerprint"] = str(metadata.get("QUERY_SPEC_FINGERPRINT", "") or "")
    query_batches_raw = str(metadata.get("QUERY_BATCHES", "all")).strip().lower()
    metadata["query_batches"] = normalize_query_batches(query_batches_raw, len(metadata["batch_sizes"]))
    return metadata


def resolve_run_plan_indices(
    catalog_payload: Mapping[str, object],
    *,
    run_dir: Path | None = None,
) -> tuple[int, int]:
    run_config = catalog_payload.get("run_config")
    if not isinstance(run_config, Mapping):
        if run_dir is not None:
            print(
                f"Warning: {run_dir / 'strategy_catalog.json'} missing run_config; "
                "defaulting composition_plan_idx=0 and update_plan_idx=0",
                file=sys.stderr,
            )
        return 0, 0

    composition_plan_idx = int(run_config.get("composition_plan_idx", 0))
    update_plan_idx = int(run_config.get("update_plan_idx", 0))
    return composition_plan_idx, update_plan_idx


def _normalize_query_batches_from_config(
    run_config: Mapping[str, object] | None,
    metadata: Mapping[str, object],
) -> frozenset[int] | None:
    raw = None
    if isinstance(run_config, Mapping) and "query_batches" in run_config:
        raw = run_config.get("query_batches")
    else:
        raw = metadata.get("query_batches")
    if raw is None:
        return None
    return normalize_query_batches(raw, len(metadata["batch_sizes"]))


def resolve_run_plan_descriptor(
    catalog_payload: Mapping[str, object],
    strategy_id: str,
    metadata: Mapping[str, object],
    *,
    run_dir: Path | None = None,
) -> PlanDescriptor:
    """Return the boundary descriptor for one benchmarked strategy."""

    run_config = catalog_payload.get("run_config")
    if not isinstance(run_config, Mapping):
        composition_plan_idx, update_plan_idx = resolve_run_plan_indices(catalog_payload, run_dir=run_dir)
        run_config = {}
    else:
        composition_plan_idx = int(run_config.get("composition_plan_idx", 0))
        update_plan_idx = int(run_config.get("update_plan_idx", 0))

    composition_variant_idx = run_config.get("composition_variant_idx")
    if composition_variant_idx is None:
        composition_variant_idx = 0
    descriptor_metadata = {
        "run_config": dict(run_config),
    }
    query_spec = (
        run_config.get("query_spec")
        or metadata.get("query_spec")
        or catalog_payload.get("query_spec")
        or CANONICAL_QUERY_SPEC_NAME
    )
    query_spec_source = (
        run_config.get("query_spec_source")
        or catalog_payload.get("query_spec_source")
        or metadata.get("query_spec_source")
        or ""
    )
    query_spec_fingerprint = (
        run_config.get("query_spec_fingerprint")
        or catalog_payload.get("query_spec_fingerprint")
        or metadata.get("query_spec_fingerprint")
        or ""
    )
    return PlanDescriptor(
        kind="composition_variant",
        strategy_id=strategy_id,
        update_plan_idx=update_plan_idx,
        composition_plan_idx=composition_plan_idx,
        composition_variant_idx=int(composition_variant_idx),
        composition_variant_mode=str(run_config.get("composition_variant_mode", "canonical")),
        max_join_orders_per_tree=(
            None
            if run_config.get("max_join_orders_per_tree") in (None, "")
            else int(run_config["max_join_orders_per_tree"])
        ),
        query_batches=_normalize_query_batches_from_config(run_config, metadata),
        query_spec=str(query_spec),
        query_spec_source=None if query_spec_source in (None, "") else str(query_spec_source),
        query_spec_fingerprint=None if query_spec_fingerprint in (None, "") else str(query_spec_fingerprint),
        metadata=descriptor_metadata,
    )


def _recorded_explicit_cover(catalog_payload: Mapping[str, object]) -> list[str] | None:
    """The view names of the executed cover for an explicit-cover run: taken from run_config
    (preferred) or the stored strategy summary. None for a catalog with no such record."""
    from explicit_cover import SEL_STRATEGY_ID

    run_config = catalog_payload.get("run_config")
    if isinstance(run_config, Mapping) and run_config.get("explicit_cover"):
        return [str(name) for name in run_config["explicit_cover"]]
    for strategy in catalog_payload.get("strategies") or []:
        if isinstance(strategy, Mapping) and strategy.get("id") == SEL_STRATEGY_ID:
            summary = strategy.get("summary")
            if isinstance(summary, Mapping) and summary.get("strategy_views"):
                return [str(name) for name in summary["strategy_views"]]
    return None


def _parse_selectivities_payload(payload: Mapping[str, object], dep_graph) -> Selectivities:
    return _parse_selectivities_from_payload(payload, dep_graph)


def _load_selectivities_with_mode(
    path: Path,
    dep_graph,
    *,
    selectivity_mode: str | None = None,
) -> tuple[Selectivities, str]:
    return _load_selectivities_from_payload(path, dep_graph, selectivity_mode=selectivity_mode)


def _load_selectivities(path: Path, dep_graph) -> Selectivities:
    selectivities, _mode = _load_selectivities_with_mode(path, dep_graph)
    return selectivities


def _analytic_singleton_size(workload: Workload, batch_index: int, variable: str) -> int:
    return int(math.floor(workload.n_at(batch_index) * workload.selectivities.independent[variable]))


def _flatten_statement_op_ids(plan) -> list[str]:
    op_ids: list[str] = [update_op.op_id for update_op in plan.update_ops]
    op_ids.append("compose")
    op_ids.append(plan.post_filter.op_id)
    return op_ids


def _probe_point_before(op_order: list[str], op_id: str) -> str:
    index = op_order.index(op_id)
    if index == 0:
        return "pre_batch"
    return f"after:{op_order[index - 1]}"


def _nullable_int(value) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _safe_ratio(measured: int | None, predicted: int | None) -> float | None:
    if measured is None or predicted in (None, 0):
        return None
    return float(measured) / float(predicted)


def _op_kind(op_id: str) -> str:
    if op_id.startswith("upd:"):
        return "update"
    if op_id == "compose":
        return "compose"
    if op_id == "post_filter":
        return "post_filter"
    return "other"


def _compose_statement_prediction(plan, workload: Workload, batch_index: int, state: StateLevelTape) -> tuple[dict, int]:
    node_sizes: dict[View, int] = {}
    for node in plan.composition.nodes:
        if node.kind == "cache":
            node_sizes[node.view] = state.get(node.view)
        else:
            variable = next(iter(node.view))
            node_sizes[node.view] = _analytic_singleton_size(workload, batch_index, variable)

    compose_op, candidate_size = compose_cost(plan, workload, batch_index, node_sizes)

    return (
        {
            "op_id": "compose",
            "op_kind": "compose",
            "predicted_input": compose_op.input_cardinality,
            "predicted_output": compose_op.output_cardinality,
            "predicted_total": compose_op.total,
        },
        candidate_size,
    )


def _predict_batch_rows(plan, workload: Workload, batch_index: int, state: StateLevelTape) -> tuple[list[dict], Mapping[View, int]]:
    rows: list[dict] = []
    for update_op in plan.update_ops:
        update_cost = update_op_cost(update_op, workload, batch_index, state)
        rows.append(
            {
                "op_id": update_cost.op_id,
                "op_kind": "update",
                "predicted_input": update_cost.input_cardinality,
                "predicted_output": update_cost.output_cardinality,
                "predicted_total": update_cost.total,
            }
        )


    compose_row, candidate_size = _compose_statement_prediction(plan, workload, batch_index, state)
    rows.append(compose_row)

    anchor_var = workload.dependency_graph.variables[0]
    anchor_view = frozenset({anchor_var})
    if anchor_view in state.snapshot():
        anchor_size = state.get(anchor_view)
    else:
        anchor_size = _analytic_singleton_size(workload, batch_index, anchor_var)
    post_filter = postfilter_cost(
        candidate_size,
        anchor_size,
        plan.post_filter.op_id,
    )
    rows.append(
        {
            "op_id": post_filter.op_id,
            "op_kind": "post_filter",
            "predicted_input": post_filter.input_cardinality,
            "predicted_output": post_filter.output_cardinality,
            "predicted_total": post_filter.total,
        }
    )
    return rows, state.snapshot()


def _lookup_state_row_count(
    state_rows: Mapping[tuple[str, str], dict[str, str]],
    probe_point: str,
    table_name: str,
) -> int | None:
    row = state_rows.get((probe_point, table_name))
    if not row:
        return None
    value = _nullable_int(row.get("row_count"))
    if value is None or value < 0:
        return None
    return value


def _derive_measured_input(
    *,
    op_id: str,
    plan,
    workload: Workload,
    batch_index: int,
    measurement_row: Mapping[str, str],
    state_rows: Mapping[tuple[str, str], dict[str, str]],
    op_order: list[str],
) -> int | None:
    before_probe = _probe_point_before(op_order, op_id)
    kind = _op_kind(op_id)
    if kind == "update":
        update_op = next(op for op in plan.update_ops if op.op_id == op_id)
        total = 0
        for block in update_op.source_blocks:
            if block.kind == "cache":
                value = _lookup_state_row_count(state_rows, before_probe, block.table_name)
            else:
                variable = next(iter(block.variables))
                value = _lookup_state_row_count(state_rows, "pre_batch", f"{block.table_name}:{variable}")
            if value is None:
                return None
            total += value
        return total

    if kind == "compose":
        total = 0
        for node in plan.composition.nodes:
            if node.kind == "cache":
                value = _lookup_state_row_count(state_rows, before_probe, node.table_name)
            else:
                variable = next(iter(node.view))
                value = _lookup_state_row_count(state_rows, "pre_batch", f"{node.table_name}:{variable}")
            if value is None:
                return None
            total += value
        return total

    if kind == "post_filter":
        return _lookup_state_row_count(state_rows, "after:compose", "composed")

    return None


def _load_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _format_tuple(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (tuple, list, frozenset, set)):
        return "|".join(str(item) for item in value)
    return str(value)


def _format_query_batches(value: frozenset[int] | None) -> str:
    if value is None:
        return "all"
    if not value:
        return "none"
    return ",".join(str(batch) for batch in sorted(value))


def _candidate_metadata_row(
    *,
    strategy_id: str,
    executable=None,
    descriptor: PlanDescriptor | None = None,
    selectivity_mode: str,
    status: str,
    error: str = "",
) -> dict[str, object]:
    metadata = dict(getattr(executable, "metadata", {}) or {})
    run_config = metadata.get("run_config")
    run_config_mapping = run_config if isinstance(run_config, Mapping) else {}
    query_batches = getattr(executable, "query_batches", None)
    if executable is None and descriptor is not None:
        metadata.update(
            {
                "descriptor_kind": descriptor.kind,
                "strategy_id": descriptor.strategy_id,
                "update_plan_idx": descriptor.update_plan_idx,
                "composition_plan_idx": descriptor.composition_plan_idx,
                "composition_variant_idx": descriptor.composition_variant_idx,
                "composition_variant_mode": descriptor.composition_variant_mode,
                "max_join_orders_per_tree": descriptor.max_join_orders_per_tree,
                "query_spec": descriptor.query_spec,
                "query_spec_source": descriptor.query_spec_source,
                "query_spec_fingerprint": descriptor.query_spec_fingerprint,
            }
        )
        query_batches = descriptor.query_batches

    descriptor_kind = str(metadata.get("descriptor_kind", descriptor.kind if descriptor else "unknown"))
    update_idx = metadata.get("update_plan_idx", descriptor.update_plan_idx if descriptor else "")
    comp_plan_idx = metadata.get("composition_plan_idx", descriptor.composition_plan_idx if descriptor else "")
    comp_variant_idx = metadata.get("composition_variant_idx", descriptor.composition_variant_idx if descriptor else "")
    mode = str(metadata.get("composition_variant_mode", descriptor.composition_variant_mode if descriptor else ""))
    candidate_id = (
        f"{strategy_id}:{descriptor_kind}:c{comp_plan_idx}:v{comp_variant_idx}:"
        f"u{update_idx}:q{_format_query_batches(query_batches)}"
    )
    return {
        "candidate_id": candidate_id,
        "query_spec": metadata.get("query_spec", descriptor.query_spec if descriptor else ""),
        "query_spec_source": metadata.get("query_spec_source", descriptor.query_spec_source if descriptor else ""),
        "query_spec_fingerprint": metadata.get(
            "query_spec_fingerprint",
            descriptor.query_spec_fingerprint if descriptor else "",
        ),
        "descriptor_kind": descriptor_kind,
        "strategy_id": strategy_id,
        "materialized_views": _format_tuple(metadata.get("materialized_views")),
        "update_plan_idx": update_idx,
        "composition_plan_idx": comp_plan_idx,
        "composition_variant_idx": comp_variant_idx,
        "composition_variant_mode": mode,
        "generation_method": metadata.get("generation_method", ""),
        "cover_idx": metadata.get("cover_idx", ""),
        "plan_idx_within_cover": metadata.get("plan_idx_within_cover", ""),
        "base_tree_idx": metadata.get("base_tree_idx", ""),
        "join_order_idx": metadata.get("join_order_idx", ""),
        "composition_cover": _format_tuple(metadata.get("composition_cover")),
        "query_batches": _format_query_batches(query_batches),
        "max_join_orders_per_tree": metadata.get(
            "max_join_orders_per_tree",
            descriptor.max_join_orders_per_tree if descriptor else "",
        ),
        "trino_join_reordering": run_config_mapping.get("trino_join_reordering", "default"),
        "selectivity_mode": selectivity_mode,
        "cost_model_supported": status == "OK",
        "status": status,
        "error": error,
    }


def _component_totals(trace: CostEstimateTrace | None) -> dict[str, int | None]:
    if trace is None:
        return {
            "estimated_total_cost": None,
            "estimated_update_cost": None,
            "estimated_compose_cost": None,
            "estimated_post_filter_cost": None,
        }
    return {
        "estimated_total_cost": trace.total_cost,
        "estimated_update_cost": trace.component_totals.get("update", 0),
        "estimated_compose_cost": trace.component_totals.get("compose", 0),
        "estimated_post_filter_cost": trace.component_totals.get("post_filter", 0),
    }


def _measurement_totals(strategy_id: str, measurement_rows: Iterable[Mapping[str, str]]) -> dict[str, object]:
    totals = {
        "measured_total_ms": 0,
        "measured_update_ms": 0,
        "measured_compose_ms": 0,
        "measured_post_filter_ms": 0,
        "output_row_count": None,
        "query_id": "",
    }
    seen = False
    for row in measurement_rows:
        if row.get("strategy") != strategy_id:
            continue
        wall = _nullable_int(row.get("statement_wall_ms")) or 0
        totals["measured_total_ms"] += wall
        kind = str(row.get("op_kind") or _op_kind(str(row.get("op_id", ""))))
        key = {
            "update": "measured_update_ms",
            "compose": "measured_compose_ms",
            "post_filter": "measured_post_filter_ms",
        }.get(kind)
        if key is not None:
            totals[key] += wall
        if kind == "post_filter":
            output = _nullable_int(row.get("measured_output"))
            if output is not None:
                totals["output_row_count"] = output
            totals["query_id"] = row.get("query_id", "") or totals["query_id"]
        seen = True
    if not seen:
        totals["measured_total_ms"] = None
    return totals


def process_run_dir(
    run_dir: Path,
    selectivities_path: Path,
    *,
    selectivity_mode: str | None = None,
    query_spec_name: str | None = None,
    query_spec_file: Path | None = None,
) -> list[Path]:
    catalog_path = run_dir / "strategy_catalog.json"
    sql_path = run_dir / "benchmark_generated.sql"
    catalog_payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    metadata = parse_sql_header_metadata(sql_path)
    effective_query_spec_name = (
        query_spec_name
        or str(metadata.get("query_spec") or "")
        or str(catalog_payload.get("query_spec") or "")
        or str((catalog_payload.get("run_config") or {}).get("query_spec") if isinstance(catalog_payload.get("run_config"), Mapping) else "")
        or CANONICAL_QUERY_SPEC_NAME
    )
    if query_spec_file is not None:
        query_spec = load_query_spec_file(query_spec_file)
    elif isinstance(catalog_payload.get("query_spec_payload"), Mapping):
        query_spec = query_spec_from_dict(catalog_payload["query_spec_payload"])  # type: ignore[arg-type]
    else:
        query_spec = get_query_spec(effective_query_spec_name)
    effective_query_spec_name = query_spec.name
    config = CanonicalDependentConfig(**catalog_payload["config"])
    dep_graph, strategy_index = build_eimer_strategy_index(
        config,
        only_with_nc_subpattern=bool(catalog_payload.get("only_with_nc_subpattern", False)),
        query_spec=query_spec,
    )
    selectivities, selectivity_mode_value = _load_selectivities_with_mode(
        selectivities_path,
        dep_graph,
        selectivity_mode=selectivity_mode,
    )
    workload = Workload(
        batch_sizes=tuple(metadata["batch_sizes"]),
        initial_base_size=0,
        rho=payload_rho(selectivities_path, default=BENCHMARK_RHO),
        dependency_graph=dep_graph,
        selectivities=selectivities,
    )

    # explicit-cover runs register the selector's pick under SEL_STRATEGY_ID; the enumerated index's
    # same-id strategy is a different (full-view) plan, so rebuild that strategy from the recorded
    # cover to cost-validate the plan that actually ran.
    explicit_cover_names = _recorded_explicit_cover(catalog_payload)
    if explicit_cover_names:
        from explicit_cover import SEL_STRATEGY_ID, _views_from_names
        from eimer.selection.plan_build import build_plan
        cover = _views_from_names(explicit_cover_names, dep_graph)
        cover_swp, _cover_plan, _cover_nodes = build_plan(
            dep_graph, cover, cover, set(dep_graph.variables), workload=workload
        )
        strategy_index = dict(strategy_index)
        strategy_index[SEL_STRATEGY_ID] = cover_swp

    strategy_payloads: dict[str, tuple[PlanDescriptor, object | None, CostEstimateTrace | None, str, str]] = {}
    for strategy_id in metadata["strategies"]:
        if strategy_id not in strategy_index:
            raise ValueError(f"Validation run references unknown EIMER strategy id {strategy_id}")
        descriptor = resolve_run_plan_descriptor(catalog_payload, strategy_id, metadata, run_dir=run_dir)
        try:
            executable = resolve_plan_descriptor(
                descriptor,
                dep_graph=dep_graph,
                workload=workload,
                strategy_catalog=strategy_index,
                base_table="events",
                batch_table="events_batch",
                query_spec=query_spec.name,
            )
            trace = estimate_executable_plan(executable, workload)
            status = "OK"
            error = ""
        except CostModelError as exc:
            executable = None
            trace = None
            status = "SKIPPED_UNSUPPORTED"
            error = str(exc)
        except Exception as exc:
            executable = None
            trace = None
            status = "FAIL_METADATA_ERROR"
            error = str(exc)
        strategy_payloads[strategy_id] = (descriptor, executable, trace, status, error)

    written: list[Path] = []
    for measurements_path in sorted(run_dir.glob("run*_cost_validation_measurements.csv")):
        match = RUN_MEASUREMENTS_RE.match(measurements_path.name)
        if not match:
            continue
        run_index = int(match.group(1))
        state_path = run_dir / f"run{run_index}_cost_validation_state.csv"
        if not state_path.exists():
            continue

        measurement_rows = _load_csv_rows(measurements_path)
        state_rows_raw = _load_csv_rows(state_path)

        measurement_index = {
            (row["strategy"], int(row["batch_index"]), row["op_id"]): row
            for row in measurement_rows
        }
        state_index: dict[tuple[str, int], dict[tuple[str, str], dict[str, str]]] = defaultdict(dict)
        for row in state_rows_raw:
            key = (row["strategy"], int(row["batch_index"]))
            state_index[key][(row["probe_point"], row["table_name"])] = row

        predicted_rows: list[dict] = []
        state_compare_rows: list[dict] = []
        summary_rows: list[dict] = []
        for strategy_id in metadata["strategies"]:
            descriptor, executable, trace, status, error = strategy_payloads[strategy_id]
            base_metadata = _candidate_metadata_row(
                strategy_id=strategy_id,
                executable=executable,
                descriptor=descriptor,
                selectivity_mode=selectivity_mode_value,
                status=status,
                error=error,
            )
            component_totals = _component_totals(trace)
            measured_totals = _measurement_totals(strategy_id, measurement_rows)
            summary_rows.append(
                {
                    **base_metadata,
                    **component_totals,
                    **measured_totals,
                }
            )
            if executable is None or trace is None:
                predicted_rows.append(
                    {
                        **base_metadata,
                        **component_totals,
                        "plan_id": "",
                        "strategy": strategy_id,
                        "batch_index": "",
                        "op_id": "",
                        "op_kind": "",
                        "predicted_input": "",
                        "predicted_output": "",
                        "predicted_total": "",
                        "measured_input": "",
                        "measured_output": "",
                        "measured_total": "",
                        "statement_wall_ms": "",
                        "query_id": "",
                        "pre_probe_count": "",
                        "post_probe_count": "",
                        "ratio_input": "",
                        "ratio_output": "",
                        "ratio_total": "",
                        "measurement_missing": True,
                    }
                )
                continue

            plan = executable.evaluation_plan
            batch_by_index = {batch.batch_index: batch for batch in trace.batch_estimates}
            for batch_index in range(1, workload.k + 1):
                batch_estimate = batch_by_index[batch_index]
                batch_rows = [
                    {
                        "op_id": op.op_id,
                        "op_kind": op.op_kind,
                        "predicted_input": op.predicted_input,
                        "predicted_output": op.predicted_output,
                        "predicted_total": op.predicted_total,
                    }
                    for op in batch_estimate.op_estimates
                ]
                predicted_state = batch_estimate.state_snapshot
                state_rows = state_index.get((strategy_id, batch_index), {})
                op_order = [row["op_id"] for row in batch_rows]
                final_probe_point = f"after:{op_order[-1]}" if op_order else "pre_batch"
                positions = workload.dependency_graph.positions
                for view in sorted(plan.strategy, key=lambda v: view_name(v, positions)):
                    table_name = f"cache_{view_name(view, positions)}"
                    measured_state = _lookup_state_row_count(state_rows, final_probe_point, table_name)
                    predicted_state_value = predicted_state.get(view)
                    state_compare_rows.append(
                        {
                            "plan_id": plan.plan_id,
                            "strategy": strategy_id,
                            "batch_index": batch_index,
                            "view_name": view_name(view, positions),
                            "predicted_state": predicted_state_value,
                            "measured_state": measured_state,
                            "ratio": _safe_ratio(measured_state, predicted_state_value),
                        }
                    )

                for row in batch_rows:
                    key = (strategy_id, batch_index, row["op_id"])
                    measured = measurement_index.get(key)
                    measured_input = None
                    measured_output = None
                    statement_wall_ms = None
                    query_id = None
                    pre_probe_count = None
                    post_probe_count = None
                    if measured is not None:
                        measured_input = _derive_measured_input(
                            op_id=row["op_id"],
                            plan=plan,
                            workload=workload,
                            batch_index=batch_index,
                            measurement_row=measured,
                            state_rows=state_rows,
                            op_order=op_order,
                        )
                        measured_output = _nullable_int(measured.get("measured_output"))
                        statement_wall_ms = _nullable_int(measured.get("statement_wall_ms"))
                        query_id = measured.get("query_id", "")
                        pre_probe_count = _nullable_int(measured.get("pre_probe_count"))
                        post_probe_count = _nullable_int(measured.get("post_probe_count"))

                    measured_total = (
                        measured_input + measured_output
                        if measured_input is not None and measured_output is not None
                        else None
                    )
                    predicted_rows.append(
                        {
                            **base_metadata,
                            **component_totals,
                            "plan_id": plan.plan_id,
                            "strategy": strategy_id,
                            "batch_index": batch_index,
                            "op_id": row["op_id"],
                            "op_kind": row["op_kind"],
                            "predicted_input": row["predicted_input"],
                            "predicted_output": row["predicted_output"],
                            "predicted_total": row["predicted_total"],
                            "measured_input": measured_input,
                            "measured_output": measured_output,
                            "measured_total": measured_total,
                            "statement_wall_ms": statement_wall_ms,
                            "query_id": query_id,
                            "pre_probe_count": pre_probe_count,
                            "post_probe_count": post_probe_count,
                            "ratio_input": _safe_ratio(measured_input, row["predicted_input"]),
                            "ratio_output": _safe_ratio(measured_output, row["predicted_output"]),
                            "ratio_total": _safe_ratio(measured_total, row["predicted_total"]),
                            "measurement_missing": measured is None,
                        }
                    )

        joined_path = run_dir / f"run{run_index}_cost_validation.csv"
        with joined_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "candidate_id",
                    "query_spec",
                    "query_spec_source",
                    "query_spec_fingerprint",
                    "descriptor_kind",
                    "strategy_id",
                    "materialized_views",
                    "update_plan_idx",
                    "composition_plan_idx",
                    "composition_variant_idx",
                    "composition_variant_mode",
                    "generation_method",
                    "cover_idx",
                    "plan_idx_within_cover",
                    "base_tree_idx",
                    "join_order_idx",
                    "composition_cover",
                    "query_batches",
                    "max_join_orders_per_tree",
                    "trino_join_reordering",
                    "selectivity_mode",
                    "cost_model_supported",
                    "status",
                    "error",
                    "estimated_total_cost",
                    "estimated_update_cost",
                    "estimated_compose_cost",
                    "estimated_post_filter_cost",
                    "plan_id",
                    "strategy",
                    "batch_index",
                    "op_id",
                    "op_kind",
                    "predicted_input",
                    "predicted_output",
                    "predicted_total",
                    "measured_input",
                    "measured_output",
                    "measured_total",
                    "statement_wall_ms",
                    "query_id",
                    "pre_probe_count",
                    "post_probe_count",
                    "ratio_input",
                    "ratio_output",
                    "ratio_total",
                    "measurement_missing",
                ],
            )
            writer.writeheader()
            writer.writerows(predicted_rows)
        print(joined_path)
        written.append(joined_path)

        summary_path = run_dir / f"run{run_index}_cost_validation_plan_summary.csv"
        summary_fieldnames = [
            "candidate_id",
            "query_spec",
            "query_spec_source",
            "query_spec_fingerprint",
            "descriptor_kind",
            "strategy_id",
            "materialized_views",
            "update_plan_idx",
            "composition_plan_idx",
            "composition_variant_idx",
            "composition_variant_mode",
            "generation_method",
            "cover_idx",
            "plan_idx_within_cover",
            "base_tree_idx",
            "join_order_idx",
            "composition_cover",
            "query_batches",
            "max_join_orders_per_tree",
            "trino_join_reordering",
            "selectivity_mode",
            "cost_model_supported",
            "status",
            "error",
            "estimated_total_cost",
            "estimated_update_cost",
            "estimated_compose_cost",
            "estimated_post_filter_cost",
            "measured_total_ms",
            "measured_update_ms",
            "measured_compose_ms",
            "measured_post_filter_ms",
            "output_row_count",
            "query_id",
        ]
        with summary_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=summary_fieldnames)
            writer.writeheader()
            writer.writerows(summary_rows)
        print(summary_path)
        written.append(summary_path)

        state_compare_path = run_dir / f"run{run_index}_cost_validation_state_compare.csv"
        with state_compare_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "plan_id",
                    "strategy",
                    "batch_index",
                    "view_name",
                    "predicted_state",
                    "measured_state",
                    "ratio",
                ],
            )
            writer.writeheader()
            writer.writerows(state_compare_rows)
        print(state_compare_path)
        written.append(state_compare_path)

    return written


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Join cost-model predictions with validation-mode benchmark measurements.")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--selectivities", required=True, type=Path)
    parser.add_argument(
        "--selectivity-mode",
        choices=("b", "c", "b_unified"),
        default=None,
        help="Optional artifact label/override for the selectivity payload mode.",
    )
    parser.add_argument("--query-spec", default=None)
    parser.add_argument("--query-spec-file", type=Path, default=None)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    process_run_dir(
        args.run_dir,
        args.selectivities,
        selectivity_mode=args.selectivity_mode,
        query_spec_name=args.query_spec,
        query_spec_file=args.query_spec_file,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
