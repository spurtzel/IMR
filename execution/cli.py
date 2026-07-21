from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Sequence

import bootstrap  # noqa: F401
from benchmark_types import (
    CanonicalDependentConfig,
    StrategyCatalogBundle,
    dedupe_preserve_order,
    merge_selected_strategies,
)
from query_spec_source import add_query_spec_args, load_query_spec_from_args
from sql_generator import (
    DEFAULT_WORKLOAD_PROFILE,
    DEFAULT_WORKLOAD_SEED,
    WORKLOAD_PROFILES,
    _batch_sizes,
    render_benchmark_sql,
)
from eimer.query.query_spec import QuerySpec
from eimer.sql.sql_dialect import resolve as resolve_dialect


def _default_local_runner() -> Path:
    return Path(__file__).resolve().parent / "run_benchmark.sh"


def _parse_strategy_ids_csv(value: str) -> list[str]:
    strategy_ids = [item.strip() for item in value.split(",") if item.strip()]
    if not strategy_ids:
        raise ValueError("No strategies provided. Use comma-separated ids like S0,S1,N1.")
    return dedupe_preserve_order(strategy_ids)


def _parse_query_batches(value: str | None) -> tuple[int, ...] | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"all", "*"}:
        return None
    if normalized in {"", "none", "update-only", "updates-only"}:
        return ()
    batches: list[int] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        batches.append(int(item))
    return tuple(batches)


def _apply_correctness_total_events(args: argparse.Namespace) -> argparse.Namespace:
    total_events = getattr(args, "correctness_total_events", None)
    if total_events is None:
        return args
    if total_events <= 0:
        raise ValueError("--correctness-total-events must be positive")
    if total_events > 5_000:
        raise ValueError("--correctness-total-events is capped at 5000 for live correctness runs")

    updates = max(0, int(args.updates))
    if updates == 0:
        args.initial_history = total_events
        return args

    per_batch = max(1, total_events // (updates + 1))
    args.update_size = per_batch
    args.initial_history = total_events - (updates * per_batch)
    return args


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _config_from_args(args: argparse.Namespace) -> CanonicalDependentConfig:
    return CanonicalDependentConfig(
        b_lon_window=args.b_lon_window,
        b_lat_window=args.b_lat_window,
        m_lon_window=args.m_lon_window,
        m_lat_window=args.m_lat_window,
    )


def _query_spec_from_args(args: argparse.Namespace) -> QuerySpec:
    return load_query_spec_from_args(args).spec


def _select_strategies(bundle: StrategyCatalogBundle, strategies_csv: str):
    strategy_ids = _parse_strategy_ids_csv(strategies_csv)
    known = bundle.all_strategies
    unknown = [sid for sid in strategy_ids if sid not in known]
    if unknown:
        known_ids = ", ".join(known.keys())
        raise ValueError(f"Unknown strategy ids: {unknown}. Known: {known_ids}")
    return strategy_ids, merge_selected_strategies(bundle, strategy_ids)


def _validate_schema_contract(query_spec: QuerySpec, allowed_names: set[str] | None = None) -> None:
    # Default names come from the synthetic generator's fixed schema; a loaded
    # external dataset declares its own columns via the manifest's event_columns.
    generated_names = allowed_names or {"id", "time", "ts", "primary_type", "etype", "lon", "lat"}
    required_names = {column.name for column in query_spec.event_schema}
    missing = sorted(required_names - generated_names)
    if missing:
        raise ValueError(
            f"QuerySpec {query_spec.name!r} requires generated event columns not provided "
            f"by the current benchmark generator: {missing!r}"
        )


def _load_external_events_manifest(args: argparse.Namespace) -> str | None:
    """Validate the external-events manifest against the sizing args and return its
    fingerprint. Must run after _apply_correctness_total_events; only the total row
    count must match, since the batch schedule is a separate emit-sql axis.
    """
    manifest_path = getattr(args, "external_events_manifest", None)
    if manifest_path is None:
        return None
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    fingerprint = manifest.get("fingerprint")
    total_rows = manifest.get("total_rows")
    if not fingerprint or not isinstance(total_rows, int):
        raise ValueError(f"external-events manifest {manifest_path} lacks fingerprint/total_rows")
    # The fingerprint is interpolated into the rendered SQL stream guard; accept
    # only sha256 hex.
    if not re.fullmatch(r"[0-9a-f]{64}", str(fingerprint)):
        raise ValueError(f"external-events manifest fingerprint is not a sha256 hexdigest: {fingerprint!r}")
    args.external_events_seed = manifest.get("seed")
    args.external_events_config = manifest.get("config")
    event_columns = manifest.get("event_columns")
    if event_columns is not None:
        # event_columns is a list of name strings; tolerate the alternate
        # [{"name": ...}, ...] dict form too.
        args.external_events_columns = {
            (entry["name"] if isinstance(entry, dict) else entry) for entry in event_columns
        }
    explicit_batch_sizes = _parse_batch_sizes(getattr(args, "batch_sizes", None))
    derived_total = sum(
        explicit_batch_sizes
        if explicit_batch_sizes is not None
        else _batch_sizes(
            initial_history=args.initial_history,
            num_updates=args.updates,
            update_size=args.update_size,
            scale_factor=args.scale,
        )
    )
    if derived_total != total_rows:
        raise ValueError(
            f"external-events manifest holds {total_rows} rows but the benchmark sizing "
            f"args derive {derived_total}; the loaded dataset cannot serve this run"
        )
    return str(fingerprint)



def _parse_batch_sizes(raw: "str | None") -> "list[int] | None":
    """Comma list -> the native batch schedule; None passes the scalar flags through."""
    if raw is None or str(raw).strip() == "":
        return None
    sizes = [int(part) for part in str(raw).split(",") if part.strip()]
    if not sizes or any(size <= 0 for size in sizes):
        raise ValueError(f"--batch-sizes must be positive ints, got {raw!r}")
    return sizes

def _build_explicit_cover_workload(args, dep):
    """rebuild the exact scoring workload the selection step used, from --selectivities and the
    batch schedule, so the cover's bushy build_plan trees match the pick."""
    from selectivity_payload import load_selectivities, payload_rho
    from eimer.workload import make_workload
    selectivities, _mode = load_selectivities(Path(args.selectivities), dep)
    batch_sizes = _parse_batch_sizes(getattr(args, "batch_sizes", None))
    if batch_sizes is None:
        batch_sizes = [args.initial_history] + [args.update_size] * args.updates
    return make_workload(dep, batch_sizes=batch_sizes, selectivities=selectivities,
                         rho=payload_rho(Path(args.selectivities)))


def _emit_sql_explicit_cover(args, query_spec, loaded_query_spec):
    from explicit_cover import build_explicit_cover_bundle, dependency_graph_for_spec, SEL_STRATEGY_ID
    if not args.selectivities:
        raise ValueError("--explicit-cover requires --selectivities to rebuild the scoring workload")
    dep = dependency_graph_for_spec(query_spec)
    workload = _build_explicit_cover_workload(args, dep)
    cover_names = [name.strip() for name in args.explicit_cover.split(",") if name.strip()]
    bundle = build_explicit_cover_bundle(
        _config_from_args(args), query_spec, cover_names, workload,
        query_spec_source=loaded_query_spec.source,
        query_spec_fingerprint=loaded_query_spec.fingerprint,
    )
    strategy_ids, selected = _select_strategies(bundle, SEL_STRATEGY_ID)
    sql = render_benchmark_sql(
        config=bundle.config, strategies=selected,
        initial_history=args.initial_history, num_updates=args.updates, update_size=args.update_size,
        scale_factor=args.scale, include_correctness_checks=args.with_correctness,
        mr_postprocessing=args.mr_postprocessing, selectivity_r=args.sel_r, selectivity_b=args.sel_b,
        selectivity_m=args.sel_m, cost_model_validation=getattr(args, "cost_model_validation", False),
        events_table=args.events_table, batch_table=args.batch_table,
        query_batches=_parse_query_batches(args.query_batches), query_spec=query_spec,
        query_spec_fingerprint=loaded_query_spec.fingerprint, workload_profile=args.workload_profile,
        workload_seed=args.workload_seed, external_events_fingerprint=args.external_events_fingerprint,
        dialect=resolve_dialect(args.target_dialect),
        batch_sizes=_parse_batch_sizes(getattr(args, "batch_sizes", None)),
    )
    return bundle, strategy_ids, sql


def _emit_sql(args: argparse.Namespace) -> tuple[StrategyCatalogBundle, list[str], str]:
    args = _apply_correctness_total_events(args)
    args.external_events_fingerprint = _load_external_events_manifest(args)
    query_spec = _query_spec_from_args(args)
    loaded_query_spec = load_query_spec_from_args(args)
    _validate_schema_contract(query_spec, getattr(args, "external_events_columns", None))
    if getattr(args, "explicit_cover", None):
        return _emit_sql_explicit_cover(args, query_spec, loaded_query_spec)
    raise ValueError("emit-sql now requires --explicit-cover; the enumerated strategy catalog was removed")


def _cmd_emit_sql(args: argparse.Namespace) -> int:
    bundle, strategy_ids, sql = _emit_sql(args)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(sql, encoding="utf-8")

    if args.catalog_out:
        catalog_path = Path(args.catalog_out)
        payload = bundle.to_catalog_payload()
        run_config = {
            "composition_plan_idx": args.composition_plan_idx,
            "update_plan_idx": args.update_plan_idx,
        }
        if args.composition_variant_idx is not None:
            run_config["composition_variant_idx"] = args.composition_variant_idx
            run_config["composition_variant_mode"] = args.composition_variant_mode
        if args.max_join_orders_per_tree is not None:
            run_config["max_join_orders_per_tree"] = args.max_join_orders_per_tree
        if args.query_batches is not None:
            run_config["query_batches"] = _parse_query_batches(args.query_batches)
        if getattr(args, "selectivity_mode", None) is not None:
            run_config["selectivity_mode"] = args.selectivity_mode
        loaded_query_spec = load_query_spec_from_args(args)
        run_config["query_spec"] = loaded_query_spec.spec.name
        run_config["query_spec_source"] = loaded_query_spec.source
        run_config["query_spec_fingerprint"] = loaded_query_spec.fingerprint
        if loaded_query_spec.path is not None:
            run_config["query_spec_file"] = str(loaded_query_spec.path)
        if getattr(args, "trino_join_reordering", "default") != "default":
            run_config["trino_join_reordering"] = args.trino_join_reordering
        if getattr(args, "external_events_manifest", None) is not None:
            run_config["external_events_manifest"] = str(args.external_events_manifest)
            run_config["external_events_fingerprint"] = args.external_events_fingerprint
            run_config["external_events_seed"] = args.external_events_seed
            run_config["external_events_config"] = args.external_events_config
        run_config["workload_profile"] = args.workload_profile
        run_config["workload_seed"] = args.workload_seed
        run_config["initial_history"] = args.initial_history
        run_config["updates"] = args.updates
        run_config["update_size"] = args.update_size
        run_config["scale"] = args.scale
        run_config["sel_r"] = args.sel_r
        run_config["sel_b"] = args.sel_b
        run_config["sel_m"] = args.sel_m
        if getattr(args, "explicit_cover", None):
            # record the executed cover so cost-model validation scores the plan that
            # actually ran (the selector's pick).
            run_config["explicit_cover"] = [n.strip() for n in args.explicit_cover.split(",") if n.strip()]
        payload["run_config"] = run_config
        _write_json(catalog_path, payload)
        print(catalog_path)

    print(out_path)
    print("selected_strategies=" + ",".join(strategy_ids))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    args = _apply_correctness_total_events(args)
    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path(__file__).resolve().parent / "results_bench" / f"run_{ts}"

    out_dir.mkdir(parents=True, exist_ok=True)

    runner_path = Path(args.runner_script) if args.runner_script else _default_local_runner()
    if not runner_path.exists():
        raise FileNotFoundError(f"Benchmark runner script not found: {runner_path}")

    env = dict(os.environ)
    if args.runs is not None:
        env["RUNS"] = str(args.runs)
    env["OUT_DIR"] = str(out_dir)
    if args.cost_model_validation:
        cmd = [
            str(runner_path),
            "--initial-history",
            str(args.initial_history),
            "--updates",
            str(args.updates),
            "--update-size",
            str(args.update_size),
            "--warmup",
            str(args.warmup),
            "--strategies",
            args.strategies,
            "--composition-plan-idx",
            str(args.composition_plan_idx),
            "--update-plan-idx",
            str(args.update_plan_idx),
            "--cost-model-validation",
            "--selectivities",
            str(args.selectivities),
        ]
        if args.scale is not None:
            cmd.extend(["--scale", str(args.scale)])
        if args.sel_r is not None:
            cmd.extend(["--sel-r", str(args.sel_r)])
        if args.sel_b is not None:
            cmd.extend(["--sel-b", str(args.sel_b)])
        if args.sel_m is not None:
            cmd.extend(["--sel-m", str(args.sel_m)])
        cmd.extend(["--workload-profile", args.workload_profile])
        cmd.extend(["--workload-seed", str(args.workload_seed)])
        if args.query_spec_file is not None:
            cmd.extend(["--query-spec-file", str(args.query_spec_file)])
        else:
            cmd.extend(["--query-spec", args.query_spec])
        if args.with_correctness:
            cmd.append("--with-correctness")
        if args.composition_variant_idx is not None:
            cmd.extend(["--composition-variant-idx", str(args.composition_variant_idx)])
            cmd.extend(["--composition-variant-mode", args.composition_variant_mode])
        elif args.composition_variant_mode != "canonical":
            cmd.extend(["--composition-variant-mode", args.composition_variant_mode])
        if args.max_join_orders_per_tree is not None:
            cmd.extend(["--max-join-orders-per-tree", str(args.max_join_orders_per_tree)])
        if args.query_batches is not None:
            cmd.extend(["--query-batches", args.query_batches])
        if getattr(args, "external_events_manifest", None) is not None:
            cmd.extend(["--external-events-manifest", str(args.external_events_manifest)])
        if args.selectivity_mode is not None:
            cmd.extend(["--selectivity-mode", args.selectivity_mode])
        if args.trino_join_reordering != "default":
            cmd.extend(["--trino-join-reordering", args.trino_join_reordering])
        if args.dump_qep:
            cmd.append("--dump-qep")
    else:
        if getattr(args, "external_events_manifest", None) is not None:
            # This branch hands run_benchmark.sh a prewritten SQL file with
            # --no-generate, bypassing its loader invocation entirely.
            raise SystemExit(
                "--external-events-manifest on the run command requires --cost-model-validation; "
                "for other paths use emit-sql + run_benchmark.sh (EXTERNAL_EVENTS_MANIFEST)"
            )
        sql_out = out_dir / "benchmark_generated.sql"
        emit_ns = argparse.Namespace(**vars(args))
        emit_ns.out = str(sql_out)
        emit_ns.catalog_out = str(out_dir / "strategy_catalog.json")

        _cmd_emit_sql(emit_ns)

        cmd = [
            str(runner_path),
            str(sql_out),
            "--no-generate",
            "--warmup",
            str(args.warmup),
        ]
        if args.dump_qep:
            cmd.append("--dump-qep")

    print("running=" + " ".join(cmd))
    proc = subprocess.run(cmd, env=env)
    return proc.returncode


def _add_common_config_args(parser: argparse.ArgumentParser) -> None:
    add_query_spec_args(parser)
    parser.add_argument("--b-lon-window", type=float, default=0.05)
    parser.add_argument("--b-lat-window", type=float, default=0.02)
    parser.add_argument("--m-lon-window", type=float, default=0.05)
    parser.add_argument("--m-lat-window", type=float, default=0.02)
    parser.add_argument(
        "--only-with-nc-subpattern",
        action="store_true",
        help="Only enumerate/use EIMER strategies with at least one non-contiguous subpattern.",
    )


def _add_benchmark_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--strategies", default="S0,S1,S2,S3,S4")
    parser.add_argument("--composition-plan-idx", type=int, default=0)
    parser.add_argument(
        "--composition-variant-idx",
        type=int,
        default=None,
        help="Use a concrete composition variant instead of the legacy composition_plan_idx path.",
    )
    parser.add_argument(
        "--composition-variant-mode",
        choices=("canonical", "bounded_left_deep"),
        default="canonical",
    )
    parser.add_argument("--max-join-orders-per-tree", type=int, default=None)
    parser.add_argument("--update-plan-idx", type=int, default=0)
    parser.add_argument("--initial-history", type=int, default=10_000)
    parser.add_argument("--updates", type=int, default=5)
    parser.add_argument("--update-size", type=int, default=2_000)
    parser.add_argument("--scale", type=float, default=None)
    parser.add_argument(
        "--batch-sizes",
        default=None,
        help="explicit comma-separated batch schedule [initial,b1,b2,...] (the native, "
             "possibly NON-UNIFORM form); overrides --initial-history/--updates/"
             "--update-size/--scale",
    )
    parser.add_argument("--sel-r", type=float, default=None)
    parser.add_argument("--sel-b", type=float, default=None)
    parser.add_argument("--sel-m", type=float, default=None)
    parser.add_argument(
        "--workload-profile",
        choices=WORKLOAD_PROFILES,
        default=DEFAULT_WORKLOAD_PROFILE,
        help="Deterministic generated_events workload profile. Default preserves the historical modular grid.",
    )
    parser.add_argument(
        "--workload-seed",
        type=int,
        default=DEFAULT_WORKLOAD_SEED,
        help="Deterministic seed used by seeded workload profiles.",
    )
    parser.add_argument("--with-correctness", action="store_true")
    parser.add_argument(
        "--target-dialect",
        choices=("trino",),
        default="trino",
        help="Target SQL dialect (trino is the only shipped dialect; anything else fails loud).",
    )
    parser.add_argument(
        "--query-batches",
        default=None,
        help="1-based comma-separated batches to run compose/post-filter/correctness; "
        "omit or use 'all' for legacy every-batch behavior; use 'none' for update-only.",
    )
    parser.add_argument(
        "--selectivity-mode",
        choices=("b", "c", "b_unified"),
        default=None,
        help="Record the selectivity-estimation mode used for cost-model validation artifacts.",
    )
    parser.add_argument(
        "--trino-join-reordering",
        choices=("default", "none"),
        default="default",
        help="For benchmark execution, optionally set Trino join_reordering_strategy=NONE.",
    )
    parser.add_argument(
        "--correctness-total-events",
        type=int,
        default=None,
        help="Small live-correctness preset: cap total generated rows at <=5000 and distribute across batches.",
    )
    parser.add_argument(
        "--MR-postprocessing",
        dest="mr_postprocessing",
        action="store_true",
        help="Run Path B (MATCH_RECOGNIZE postprocessing) in addition to Path A.",
    )
    parser.add_argument("--events-table", default="events")
    parser.add_argument("--batch-table", default="events_batch")
    parser.add_argument(
        "--external-events-manifest",
        default=None,
        help=(
            "Manifest of a preloaded external dataset (execution/load_external_events.py): "
            "skip the in-Trino generated_events CTAS and emit a fail-fast fingerprint guard instead."
        ),
    )


def _validate_cost_model_validation_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not getattr(args, "cost_model_validation", False):
        return

    if getattr(args, "mr_postprocessing", False):
        parser.error("--cost-model-validation is incompatible with --MR-postprocessing")

    strategy_ids = _parse_strategy_ids_csv(getattr(args, "strategies", ""))
    legacy = [sid for sid in strategy_ids if sid.upper().startswith("S")]
    if legacy:
        parser.error(
            "--cost-model-validation only supports EIMER N* strategies; "
            f"legacy strategies not allowed: {', '.join(legacy)}"
        )

    if args.command == "run":
        selectivities = getattr(args, "selectivities", None)
        if selectivities is None:
            parser.error("--cost-model-validation requires --selectivities")
        if not Path(selectivities).is_file():
            parser.error(f"selectivities file not found: {selectivities}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="EIMER benchmark integration for canonical 3-variable dependent query")
    sub = parser.add_subparsers(dest="command", required=True)

    emit_sql = sub.add_parser("emit-sql", help="Emit benchmark SQL for a materialized-view cover (requires --explicit-cover)")
    emit_sql.add_argument("--out", required=True)
    emit_sql.add_argument("--catalog-out", default=None)
    _add_common_config_args(emit_sql)
    _add_benchmark_args(emit_sql)
    emit_sql.add_argument(
        "--cost-model-validation",
        action="store_true",
        help="Emit validation op-id markers for cost-model validation mode. "
        "Only supported for EIMER N* strategies and incompatible with --MR-postprocessing.",
    )
    emit_sql.add_argument(
        "--explicit-cover",
        default=None,
        help="Comma-separated cover view names (e.g. 'R_B,R_M') from a clique_grow/edge_cover "
        "pick. The SQL is emitted for that cover directly, built via plan_build; --strategies "
        "must be N1 and --selectivities is required to rebuild the scoring workload.",
    )
    emit_sql.add_argument(
        "--selectivities",
        default=None,
        help="Path to the selectivities JSON; required by --explicit-cover (to rebuild the "
        "scoring workload) and by --cost-model-validation.",
    )

    run = sub.add_parser("run", help="Generate SQL then execute via the local benchmark runner")
    run.add_argument("--out-dir", default=None)
    run.add_argument("--runs", type=int, default=3)
    run.add_argument("--warmup", type=int, default=1)
    run.add_argument(
        "--dump-qep",
        action="store_true",
        help="Dump Trino EXPLAIN (distributed JSON) plans for update/compose statements per strategy+batch.",
    )
    run.add_argument("--runner-script", default=None)
    run.add_argument(
        "--cost-model-validation",
        action="store_true",
        help="Enable cost-model validation mode with per-operator measurements and predicted-vs-measured outputs.",
    )
    run.add_argument(
        "--selectivities",
        type=Path,
        default=None,
        help="Path to the selectivities JSON file required by --cost-model-validation.",
    )
    _add_common_config_args(run)
    _add_benchmark_args(run)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    _validate_cost_model_validation_args(parser, args)

    if args.command == "emit-sql":
        return _cmd_emit_sql(args)
    if args.command == "run":
        return _cmd_run(args)

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
