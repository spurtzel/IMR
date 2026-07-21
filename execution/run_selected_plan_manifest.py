from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import bootstrap  # noqa: F401

from query_spec_source import add_query_spec_args, load_query_spec_from_args
from eimer.plans.plan_descriptor_codec import descriptor_from_json_dict
from eimer.query.query_schedule import format_query_batches, parse_query_batches
from selectivity_payload import (
    SelectivityPayloadValidation as SelectivityRowcountCheck,
    load_selectivity_payload,
    selectivity_source_rowcount,
    validate_selectivity_payload_against_workload,
)
from sql_generator import DEFAULT_WORKLOAD_PROFILE, DEFAULT_WORKLOAD_SEED, WORKLOAD_PROFILES


@dataclass(frozen=True)
class ManifestDescriptor:
    selector_rank: int
    query_spec: str | None
    query_spec_source: str | None
    query_spec_fingerprint: str | None
    allowed_query_schedules: tuple[str, ...]
    kind: str
    strategy_id: str
    update_plan_idx: int
    composition_plan_idx: int | None
    composition_variant_idx: int | None
    composition_variant_mode: str
    max_join_orders_per_tree: int | None
    query_batches: frozenset[int] | None
    ranking_profile_name: str
    ranking_score: float | None
    raw_total_cost: float | None
    raw_update_cost: float | None
    raw_compose_cost: float | None
    raw_post_filter_cost: float | None
    generation_method: str | None
    join_order_idx: int | None
    metadata: Mapping[str, Any]


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _read_selectivity_payload(path: Path) -> Mapping[str, Any]:
    return load_selectivity_payload(path)


def _read_selectivity_source_rowcount(path: Path) -> int | None:
    return selectivity_source_rowcount(_read_selectivity_payload(path))


def _requested_total_events(args: argparse.Namespace) -> int:
    if args.total_events is not None:
        return int(args.total_events)
    return int(args.initial_history) + int(args.updates) * int(args.update_size)


def check_selectivity_rowcount(
    selectivities_path: Path,
    *,
    requested_total_events: int,
    dry_run: bool,
    allow_mismatch: bool,
    expected_config: Mapping[str, Any] | None = None,
    allow_config_mismatch: bool = False,
    expected_query_spec: str | None = None,
    expected_query_spec_fingerprint: str | None = None,
) -> SelectivityRowcountCheck:
    return validate_selectivity_payload_against_workload(
        selectivities_path,
        requested_total_events=requested_total_events,
        dry_run=dry_run,
        allow_rowcount_mismatch=allow_mismatch,
        expected_config=expected_config,
        allow_config_mismatch=allow_config_mismatch,
        expected_query_spec=expected_query_spec,
        expected_query_spec_fingerprint=expected_query_spec_fingerprint,
        allow_query_spec_mismatch=allow_config_mismatch,
    )


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _query_batches_from_manifest(value: Any, batch_count: int) -> frozenset[int] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return parse_query_batches(value, batch_count)
    if isinstance(value, Sequence):
        return frozenset(int(item) for item in value)
    raise ValueError(f"unsupported query_batches payload: {value!r}")


def _row_value(row: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in row:
            return row[name]
    return default


def descriptor_from_manifest_row(
    row: Mapping[str, Any],
    *,
    batch_count: int,
    query_batches_override: frozenset[int] | None | object = None,
) -> ManifestDescriptor:
    row_payload = dict(row)
    if query_batches_override is _NO_QUERY_BATCH_OVERRIDE:
        query_batches = _query_batches_from_manifest(row_payload.get("query_batches"), batch_count)
    else:
        query_batches = query_batches_override  # type: ignore[assignment]
        row_payload["query_batches"] = None if query_batches is None else sorted(query_batches)

    descriptor = descriptor_from_json_dict(row_payload, batch_count=batch_count)
    if descriptor.kind != "composition_variant":
        raise ValueError(f"unsupported descriptor kind {descriptor.kind!r}")

    return ManifestDescriptor(
        selector_rank=int(_row_value(row, "rank", "selector_rank", default=0) or 0),
        query_spec=descriptor.query_spec,
        query_spec_source=descriptor.query_spec_source,
        query_spec_fingerprint=descriptor.query_spec_fingerprint,
        allowed_query_schedules=descriptor.allowed_query_schedules,
        kind=descriptor.kind,
        strategy_id=descriptor.strategy_id,
        update_plan_idx=descriptor.update_plan_idx,
        composition_plan_idx=descriptor.composition_plan_idx,
        composition_variant_idx=descriptor.composition_variant_idx,
        composition_variant_mode=descriptor.composition_variant_mode,
        max_join_orders_per_tree=descriptor.max_join_orders_per_tree,
        query_batches=query_batches,
        ranking_profile_name=str(row.get("ranking_profile_name") or ""),
        ranking_score=_optional_float(row.get("ranking_score")),
        raw_total_cost=_optional_float(row.get("raw_total_cost")),
        raw_update_cost=_optional_float(row.get("raw_update_cost")),
        raw_compose_cost=_optional_float(row.get("raw_compose_cost")),
        raw_post_filter_cost=_optional_float(row.get("raw_post_filter_cost")),
        generation_method=(None if row.get("generation_method") in (None, "") else str(row.get("generation_method"))),
        join_order_idx=_optional_int(row.get("join_order_idx")),
        metadata=dict(row),
    )


_NO_QUERY_BATCH_OVERRIDE = object()


def load_manifest(
    path: Path,
    *,
    batch_count: int,
    query_batches_override: frozenset[int] | None | object = _NO_QUERY_BATCH_OVERRIDE,
    max_descriptors: int | None = None,
) -> tuple[ManifestDescriptor, ...]:
    descriptors: list[ManifestDescriptor] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                descriptors.append(
                    descriptor_from_manifest_row(
                        row,
                        batch_count=batch_count,
                        query_batches_override=query_batches_override,
                    )
                )
            except Exception as exc:
                raise ValueError(f"invalid manifest row {line_number}: {exc}") from exc
            if max_descriptors is not None and len(descriptors) >= max_descriptors:
                break
    return tuple(descriptors)


def descriptor_output_dir_name(descriptor: ManifestDescriptor, fallback_index: int) -> str:
    rank = descriptor.selector_rank or fallback_index
    suffix = f"v{descriptor.composition_variant_idx}"
    raw = f"rank{rank:03d}_{descriptor.strategy_id}_{descriptor.kind}_{suffix}_u{descriptor.update_plan_idx}"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)


def descriptor_to_command(
    descriptor: ManifestDescriptor,
    *,
    args: argparse.Namespace,
    out_dir: Path,
) -> list[str]:
    query_batches = format_query_batches(descriptor.query_batches)
    cmd = [
        "bash",
        "execution/run_benchmark.sh",
        "--initial-history",
        str(args.initial_history),
        "--updates",
        str(args.updates),
        "--update-size",
        str(args.update_size),
        *(("--batch-sizes", ",".join(str(s) for s in args.batch_sizes_list))
          if getattr(args, "batch_sizes_list", None) else ()),
        "--warmup",
        str(args.warmup),
        "--strategies",
        descriptor.strategy_id,
        "--update-plan-idx",
        str(descriptor.update_plan_idx),
        "--query-batches",
        query_batches,
        "--cost-model-validation",
        "--selectivities",
        str(args.selectivities),
        "--selectivity-mode",
        args.selectivity_mode,
        "--trino-join-reordering",
        args.trino_join_reordering,
        "--workload-profile",
        args.workload_profile,
        "--workload-seed",
        str(args.workload_seed),
    ]
    if getattr(args, "external_events_manifest", None) is not None:
        cmd.extend(["--external-events-manifest", str(args.external_events_manifest)])
    if getattr(args, "query_spec_file", None) is not None:
        cmd.extend(["--query-spec-file", str(args.query_spec_file)])
    else:
        cmd.extend(["--query-spec", getattr(args, "effective_query_spec_name", args.query_spec)])
    if args.scale is not None:
        cmd.extend(["--scale", str(args.scale)])
    if args.sel_r is not None:
        cmd.extend(["--sel-r", str(args.sel_r)])
    if args.sel_b is not None:
        cmd.extend(["--sel-b", str(args.sel_b)])
    if args.sel_m is not None:
        cmd.extend(["--sel-m", str(args.sel_m)])
    if descriptor.composition_variant_idx is None:
        raise ValueError("composition_variant descriptor requires composition_variant_idx")
    cmd.extend(
        [
            "--composition-variant-idx",
            str(descriptor.composition_variant_idx),
            "--composition-variant-mode",
            descriptor.composition_variant_mode,
        ]
    )
    if descriptor.max_join_orders_per_tree is not None:
        cmd.extend(["--max-join-orders-per-tree", str(descriptor.max_join_orders_per_tree)])
    metadata = getattr(descriptor, "metadata", None)
    explicit_cover = metadata.get("explicit_cover") if isinstance(metadata, dict) else None
    if explicit_cover:
        # a clique_grow/edge_cover pick: emit-sql rebuilds this cover directly (--strategies is N1)
        cmd.extend(["--explicit-cover", ",".join(explicit_cover)])
    if args.dump_qep:
        cmd.append("--dump-qep")
    if args.dump_explain_analyze:
        cmd.append("--dump-explain-analyze")
    if args.with_correctness:
        cmd.append("--with-correctness")
    if args.correctness_total_events is not None:
        cmd.extend(["--correctness-total-events", str(args.correctness_total_events)])
    return cmd


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _read_correctness_check(run_dir: Path) -> tuple[str, str, str]:
    for path in sorted(run_dir.glob("run*_correctness_check.txt")):
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            return "UNKNOWN", "", str(path)
        status = lines[0].strip()
        message = "\n".join(lines[1:]).strip()
        return status, message, str(path)
    return "", "", ""


def _summary_row_from_run_dir(
    descriptor: ManifestDescriptor,
    *,
    descriptor_index: int,
    run_dir: Path,
    status: str,
    selectivity_check: SelectivityRowcountCheck | None = None,
    error: str = "",
) -> dict[str, object]:
    summaries = []
    for summary_path in sorted(run_dir.glob("run*_cost_validation_plan_summary.csv")):
        summaries.extend(_read_csv_rows(summary_path))
    summary = summaries[0] if summaries else {}
    correctness_status, correctness_message, correctness_path = _read_correctness_check(run_dir)
    row_status = status if status != "OK" else summary.get("status", "OK")
    row_error = error or summary.get("error", "")
    if row_status == "OK" and correctness_status == "FAILED":
        row_status = "FAIL_CORRECTNESS"
        row_error = correctness_message
    return {
        "selector_rank": descriptor.selector_rank,
        "descriptor_index": descriptor_index,
        "candidate_id": summary.get("candidate_id", ""),
        "query_spec": descriptor.query_spec or summary.get("query_spec", ""),
        "query_spec_source": descriptor.query_spec_source or summary.get("query_spec_source", ""),
        "query_spec_fingerprint": descriptor.query_spec_fingerprint or summary.get("query_spec_fingerprint", ""),
        "allowed_query_schedules": "|".join(descriptor.allowed_query_schedules),
        "strategy_id": descriptor.strategy_id,
        "update_plan_idx": descriptor.update_plan_idx,
        "composition_plan_idx": descriptor.composition_plan_idx,
        "composition_variant_idx": descriptor.composition_variant_idx,
        "composition_variant_mode": descriptor.composition_variant_mode,
        "generation_method": summary.get("generation_method", descriptor.generation_method or ""),
        "join_order_idx": summary.get("join_order_idx", descriptor.join_order_idx),
        "query_batches": format_query_batches(descriptor.query_batches),
        "ranking_profile_name": descriptor.ranking_profile_name,
        "ranking_score": descriptor.ranking_score,
        "raw_total_cost": descriptor.raw_total_cost,
        "raw_update_cost": descriptor.raw_update_cost,
        "raw_compose_cost": descriptor.raw_compose_cost,
        "raw_post_filter_cost": descriptor.raw_post_filter_cost,
        "estimated_total_cost": summary.get("estimated_total_cost", ""),
        "estimated_update_cost": summary.get("estimated_update_cost", ""),
        "estimated_compose_cost": summary.get("estimated_compose_cost", ""),
        "estimated_post_filter_cost": summary.get("estimated_post_filter_cost", ""),
        "measured_total_ms": summary.get("measured_total_ms", ""),
        "measured_update_ms": summary.get("measured_update_ms", ""),
        "measured_compose_ms": summary.get("measured_compose_ms", ""),
        "measured_post_filter_ms": summary.get("measured_post_filter_ms", ""),
        "output_row_count": summary.get("output_row_count", ""),
        "status": row_status,
        "error": row_error,
        "correctness_status": correctness_status,
        "correctness_message": correctness_message,
        "correctness_path": correctness_path,
        "run_dir": str(run_dir),
        "selectivity_source_rowcount": "" if selectivity_check is None or selectivity_check.source_rowcount is None else selectivity_check.source_rowcount,
        "requested_total_events": "" if selectivity_check is None else selectivity_check.requested_total_events,
        "selectivity_rowcount_status": "" if selectivity_check is None else selectivity_check.status,
        "selectivity_config_status": "" if selectivity_check is None else selectivity_check.config_status,
        "selectivity_config_message": "" if selectivity_check is None else selectivity_check.config_message,
        "selectivity_query_spec_status": "" if selectivity_check is None else selectivity_check.query_spec_status,
        "selectivity_query_spec_message": "" if selectivity_check is None else selectivity_check.query_spec_message,
        "selectivity_query_spec_fingerprint_status": "" if selectivity_check is None else selectivity_check.query_spec_fingerprint_status,
        "selectivity_query_spec_fingerprint_message": "" if selectivity_check is None else selectivity_check.query_spec_fingerprint_message,
        "workload_profile": "" if selectivity_check is None else (selectivity_check.payload_config or {}).get("workload_profile", ""),
        "workload_seed": "" if selectivity_check is None else (selectivity_check.payload_config or {}).get("workload_seed", ""),
    }


def write_summary(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _configure_sizing(args: argparse.Namespace) -> None:
    if getattr(args, "batch_sizes", None):
        sizes = [int(part) for part in str(args.batch_sizes).split(",") if part.strip()]
        if not sizes or any(size <= 0 for size in sizes):
            raise ValueError(f"--batch-sizes must be positive ints, got {args.batch_sizes!r}")
        if args.total_events is not None and sum(sizes) != args.total_events:
            raise ValueError(f"--batch-sizes sums to {sum(sizes)} but --total-events is {args.total_events}")
        if args.batches is not None and len(sizes) != args.batches:
            raise ValueError(f"--batch-sizes has {len(sizes)} entries but --batches is {args.batches}")
        # the executed schedule is the list; these scalars are kept coherent for bookkeeping
        args.batch_sizes_list = sizes
        args.initial_history = sizes[0]
        args.updates = len(sizes) - 1
        args.update_size = sizes[1] if len(sizes) > 1 else 0
        return
    args.batch_sizes_list = None
    if args.initial_history is not None or args.updates is not None or args.update_size is not None:
        if args.initial_history is None or args.updates is None or args.update_size is None:
            raise ValueError("--initial-history, --updates, and --update-size must be provided together")
        return
    if args.total_events is None or args.batches is None:
        raise ValueError("provide either --total-events/--batches or explicit --initial-history/--updates/--update-size")
    if args.total_events % args.batches != 0:
        raise ValueError("--total-events must be divisible by --batches for derived sizing")
    per_batch = args.total_events // args.batches
    args.initial_history = per_batch
    args.updates = args.batches - 1
    args.update_size = per_batch


def _batch_count(args: argparse.Namespace) -> int:
    if args.batches is not None:
        return args.batches
    return int(args.updates) + 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Execute selector-exported PlanDescriptor JSONL manifests.")
    parser.add_argument("--plan-descriptor-manifest", type=Path, required=True)
    add_query_spec_args(parser)
    parser.add_argument("--total-events", type=int, default=None)
    parser.add_argument("--batch-sizes", default=None,
                        help="explicit comma-separated batch schedule [initial,b1,...] "
                             "(native, possibly NON-UNIFORM); executed verbatim via "
                             "run_benchmark.sh --batch-sizes")
    parser.add_argument("--batches", type=int, default=None)
    parser.add_argument("--initial-history", type=int, default=None)
    parser.add_argument("--updates", type=int, default=None)
    parser.add_argument("--update-size", type=int, default=None)
    parser.add_argument("--selectivity-mode", choices=("b", "c", "b_unified"), required=True)
    parser.add_argument("--selectivities", type=Path, required=True)
    parser.add_argument("--workload-profile", choices=WORKLOAD_PROFILES, default=DEFAULT_WORKLOAD_PROFILE)
    parser.add_argument("--workload-seed", type=int, default=DEFAULT_WORKLOAD_SEED)
    parser.add_argument("--scale", type=float, default=None)
    parser.add_argument("--sel-r", type=float, default=None)
    parser.add_argument("--sel-b", type=float, default=None)
    parser.add_argument("--sel-m", type=float, default=None)
    parser.add_argument(
        "--external-events-manifest",
        type=Path,
        default=None,
        help="Preloaded external dataset manifest (forwarded to run_benchmark.sh; binds the payload fingerprint).",
    )
    parser.add_argument("--trino-join-reordering", choices=("default", "none"), default="none")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--warmup", type=int, default=0)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-descriptors", type=int, default=None)
    parser.add_argument("--query-batches", default=None, help="Override manifest query schedule.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--dump-qep", action="store_true",
                        help="Forward to run_benchmark.sh: EXPLAIN (TYPE DISTRIBUTED, "
                             "FORMAT JSON) dumps per update/compose statement.")
    parser.add_argument("--dump-explain-analyze", action="store_true",
                        help="Forward to run_benchmark.sh: EXPLAIN ANALYZE dumps "
                             "(expensive; re-executes statements).")
    parser.add_argument("--with-correctness", action="store_true")
    parser.add_argument(
        "--correctness-total-events",
        type=int,
        default=None,
        help="Pass through to execution/run_benchmark.sh when --with-correctness is enabled.",
    )
    parser.add_argument(
        "--fail-on-correctness-failure",
        action="store_true",
        help="Exit non-zero if any descriptor writes FAILED in run*_correctness_check.txt.",
    )
    parser.add_argument(
        "--allow-selectivity-rowcount-mismatch",
        action="store_true",
        help="Allow executing with selectivity payload row-count metadata that differs from the requested workload.",
    )
    parser.add_argument(
        "--allow-selectivity-config-mismatch",
        action="store_true",
        help="Allow executing with selectivity payload workload metadata that differs from requested generation config.",
    )
    parser.add_argument(
        "--rerank-top-n",
        type=int,
        default=None,
        help="Repeat only the first N selected descriptors for benchmark-level runtime reranking.",
    )
    parser.add_argument(
        "--rerank-repeats",
        type=int,
        default=None,
        help="Number of RUNS to use for descriptors covered by --rerank-top-n.",
    )
    parser.add_argument(
        "--rerank-metric",
        choices=("mean", "median"),
        default="mean",
        help="Recorded for reranking workflows; analysis chooses mean or median.",
    )
    return parser


def _runs_for_descriptor(args: argparse.Namespace, descriptor_index: int) -> int:
    if args.rerank_top_n is None:
        return int(args.runs)
    if args.rerank_top_n <= 0:
        raise ValueError("--rerank-top-n must be positive when provided")
    if args.rerank_repeats is None or args.rerank_repeats <= 0:
        raise ValueError("--rerank-repeats must be positive when --rerank-top-n is provided")
    return int(args.rerank_repeats) if descriptor_index <= int(args.rerank_top_n) else int(args.runs)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    loaded_query_spec = load_query_spec_from_args(args)
    args.effective_query_spec_name = loaded_query_spec.spec.name
    args.query_spec = loaded_query_spec.spec.name
    args.query_spec_source = loaded_query_spec.source
    args.query_spec_fingerprint = loaded_query_spec.fingerprint
    _configure_sizing(args)
    batch_count = _batch_count(args)
    requested_total_events = _requested_total_events(args)
    external_manifest_fingerprint = None
    if getattr(args, "external_events_manifest", None) is not None:
        manifest_payload = json.loads(Path(args.external_events_manifest).read_text(encoding="utf-8"))
        external_manifest_fingerprint = manifest_payload.get("fingerprint")
    expected_selectivity_config = {
        "external_manifest_fingerprint": external_manifest_fingerprint,
        "workload_profile": args.workload_profile,
        "workload_seed": args.workload_seed,
        "total_events": requested_total_events,
        "initial_history": args.initial_history,
        "updates": args.updates,
        "update_size": args.update_size,
        "scale": args.scale,
        "sel_r": args.sel_r,
        "sel_b": args.sel_b,
        "sel_m": args.sel_m,
        "query_spec": args.effective_query_spec_name,
        "query_spec_fingerprint": args.query_spec_fingerprint,
    }
    selectivity_check = check_selectivity_rowcount(
        args.selectivities,
        requested_total_events=requested_total_events,
        dry_run=args.dry_run,
        allow_mismatch=args.allow_selectivity_rowcount_mismatch,
        expected_config=expected_selectivity_config,
        allow_config_mismatch=args.allow_selectivity_config_mismatch,
        expected_query_spec=args.effective_query_spec_name,
        expected_query_spec_fingerprint=args.query_spec_fingerprint,
    )
    query_override = (
        _NO_QUERY_BATCH_OVERRIDE
        if args.query_batches is None
        else parse_query_batches(args.query_batches, batch_count)
    )
    descriptors = load_manifest(
        args.plan_descriptor_manifest,
        batch_count=batch_count,
        query_batches_override=query_override,
        max_descriptors=args.max_descriptors,
    )
    descriptor_query_specs = sorted({d.query_spec for d in descriptors if d.query_spec})
    mismatched_descriptor_specs = [value for value in descriptor_query_specs if value != args.effective_query_spec_name]
    if mismatched_descriptor_specs and not args.allow_selectivity_config_mismatch:
        raise ValueError(
            "manifest query_spec does not match requested query_spec: "
            f"manifest={mismatched_descriptor_specs!r} requested={args.effective_query_spec_name!r}; "
            "pass --allow-selectivity-config-mismatch to override"
        )
    descriptor_fingerprints = sorted({d.query_spec_fingerprint for d in descriptors if d.query_spec_fingerprint})
    mismatched_fingerprints = [value for value in descriptor_fingerprints if value != args.query_spec_fingerprint]
    if mismatched_fingerprints and not args.allow_selectivity_config_mismatch:
        raise ValueError(
            "manifest query_spec_fingerprint does not match requested QuerySpec: "
            f"manifest={mismatched_fingerprints!r} requested={args.query_spec_fingerprint!r}; "
            "pass --allow-selectivity-config-mismatch to override"
        )
    args.out_dir.mkdir(parents=True, exist_ok=True)

    print(f"descriptors={len(descriptors)}")
    print(f"query_spec={args.effective_query_spec_name}")
    print(f"query_spec_source={args.query_spec_source}")
    print(f"query_spec_fingerprint={args.query_spec_fingerprint}")
    print(f"strategies={','.join(sorted({d.strategy_id for d in descriptors}))}")
    print(f"update_plan_idx_values={sorted({d.update_plan_idx for d in descriptors})}")
    print(
        "composition_variant_idx_values="
        f"{sorted({d.composition_variant_idx for d in descriptors if d.composition_variant_idx is not None})}"
    )
    print(f"query_batches={sorted({format_query_batches(d.query_batches) for d in descriptors})}")
    print(f"out_dir={args.out_dir}")
    if args.rerank_top_n is not None:
        print(
            "runtime_reranking="
            f"top_n={args.rerank_top_n} repeats={args.rerank_repeats} metric={args.rerank_metric}"
        )
    print(
        "selectivity_rowcount="
        f"{selectivity_check.source_rowcount if selectivity_check.source_rowcount is not None else 'unknown'} "
        f"requested_total_events={selectivity_check.requested_total_events} "
        f"status={selectivity_check.status}"
    )
    print(
        "selectivity_config="
        f"status={selectivity_check.config_status} "
        f"message={selectivity_check.config_message}"
    )
    print(
        "selectivity_query_spec="
        f"status={selectivity_check.query_spec_status} "
        f"message={selectivity_check.query_spec_message}"
    )
    print(
        "selectivity_query_spec_fingerprint="
        f"status={selectivity_check.query_spec_fingerprint_status} "
        f"message={selectivity_check.query_spec_fingerprint_message}"
    )
    if selectivity_check.status in {"MISMATCH_DRY_RUN_WARNING", "MISMATCH_ALLOWED", "UNKNOWN"}:
        print(f"selectivity_rowcount_note={selectivity_check.message}")

    summary_rows: list[dict[str, object]] = []
    correctness_failed = False
    for index, descriptor in enumerate(descriptors, start=1):
        run_dir = args.out_dir / descriptor_output_dir_name(descriptor, index)
        if args.dry_run:
            print(f"[dry-run {index}/{len(descriptors)}] {run_dir}")
            print(f"  requested_runs={_runs_for_descriptor(args, index)}")
            print("  " + " ".join(descriptor_to_command(descriptor, args=args, out_dir=run_dir)))
            summary_rows.append(
                _summary_row_from_run_dir(
                    descriptor,
                    descriptor_index=index,
                    run_dir=run_dir,
                    status="DRY_RUN",
                    selectivity_check=selectivity_check,
                )
            )
            continue

        if args.resume and (run_dir / "run1_cost_validation_plan_summary.csv").exists():
            print(f"[resume {index}/{len(descriptors)}] skipping existing {run_dir}")
            row = _summary_row_from_run_dir(
                descriptor,
                descriptor_index=index,
                run_dir=run_dir,
                status="RESUMED",
                selectivity_check=selectivity_check,
            )
            correctness_failed = correctness_failed or row.get("correctness_status") == "FAILED"
            summary_rows.append(row)
            continue

        run_dir.mkdir(parents=True, exist_ok=True)
        cmd = descriptor_to_command(descriptor, args=args, out_dir=run_dir)
        env = dict(os.environ)
        env["OUT_DIR"] = str(run_dir)
        env["RUNS"] = str(_runs_for_descriptor(args, index))
        print(f"[{index}/{len(descriptors)}] {' '.join(cmd)}")
        completed = subprocess.run(cmd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        (run_dir / "manifest_run.log").write_text(completed.stdout, encoding="utf-8")
        if completed.returncode != 0:
            summary_rows.append(
                _summary_row_from_run_dir(
                    descriptor,
                    descriptor_index=index,
                    run_dir=run_dir,
                    status="FAIL_SQL_ERROR",
                    selectivity_check=selectivity_check,
                    error=completed.stdout[-2000:],
                )
            )
            continue
        row = _summary_row_from_run_dir(
            descriptor,
            descriptor_index=index,
            run_dir=run_dir,
            status="OK",
            selectivity_check=selectivity_check,
        )
        correctness_failed = correctness_failed or row.get("correctness_status") == "FAILED"
        summary_rows.append(row)

    summary_path = args.out_dir / "selected_manifest_benchmark_summary.csv"
    write_summary(summary_path, summary_rows)
    print(summary_path)
    if args.fail_on_correctness_failure and correctness_failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
