"""select a sweep plan with clique_grow or edge_cover.

runs one cover selector over the scored workload and writes a rank-1 plan descriptor
carrying the chosen cover (its view names). the executor rebuilds and runs exactly that
cover via the --explicit-cover path (execution/cli.py emit-sql).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

import bootstrap  # noqa: F401

from explicit_cover import (SELECTORS, SEL_STRATEGY_ID, cover_view_names,
                            dependency_graph_for_spec, run_selector)
from query_spec_source import add_query_spec_args, load_query_spec_from_args
from selectivity_payload import load_selectivities, payload_rho, validate_selectivity_payload_against_workload
from eimer.query.query_schedule import parse_query_batches
from eimer.workload import make_workload
from eimer.plans.plan_descriptor import PlanDescriptor
from eimer.plans.plan_descriptor_codec import descriptor_to_json_dict


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pick a sweep plan with clique_grow or edge_cover.")
    add_query_spec_args(parser)
    parser.add_argument("--selector", choices=sorted(SELECTORS), default="clique_grow")
    parser.add_argument("--selectivities", type=Path, required=True)
    parser.add_argument("--total-events", type=int, required=True)
    parser.add_argument("--batches", type=int, required=True)
    parser.add_argument("--batch-sizes", default=None,
                        help="explicit comma-separated batch schedule; must match --total-events/--batches")
    parser.add_argument("--query-batches", default="last")
    parser.add_argument("--max-storage", type=int, default=None,
                        help="storage budget M in rows; default None = unbounded")
    parser.add_argument("--descriptor-out", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    loaded_query_spec = load_query_spec_from_args(args)
    query_spec = loaded_query_spec.spec
    dep = dependency_graph_for_spec(query_spec)

    selectivities, _mode = load_selectivities(args.selectivities, dep)
    validate_selectivity_payload_against_workload(
        args.selectivities, requested_total_events=args.total_events,
        expected_query_spec=query_spec.name, expected_query_spec_fingerprint=loaded_query_spec.fingerprint)

    batch_sizes = None
    if args.batch_sizes:
        batch_sizes = [int(part) for part in str(args.batch_sizes).split(",") if part.strip()]
        if len(batch_sizes) != args.batches or sum(batch_sizes) != args.total_events:
            raise SystemExit(f"--batch-sizes {batch_sizes} must have {args.batches} entries "
                             f"summing to {args.total_events}")
    workload = (make_workload(dep, batch_sizes=batch_sizes, selectivities=selectivities,
                              rho=payload_rho(args.selectivities))
                if batch_sizes is not None
                else make_workload(dep, total_events=args.total_events, batches=args.batches,
                                   selectivities=selectivities, rho=payload_rho(args.selectivities)))
    query_batches = parse_query_batches(args.query_batches, args.batches)

    cover, pick = run_selector(dep, workload, args.selector,
                               max_storage_rows=args.max_storage, query_batches=query_batches)
    cover_names = cover_view_names(cover, dep)

    descriptor = PlanDescriptor(
        kind="composition_variant", strategy_id=SEL_STRATEGY_ID, update_plan_idx=0,
        composition_variant_idx=0, composition_variant_mode="canonical",
        query_batches=None if query_batches is None else frozenset(query_batches),
        query_spec=query_spec.name, query_spec_source=loaded_query_spec.source,
        query_spec_fingerprint=loaded_query_spec.fingerprint)
    payload = descriptor_to_json_dict(descriptor)
    payload.update({"rank": 1, "selector": args.selector, "explicit_cover": cover_names,
                    "cover_label": "|".join(cover_names) or "(empty)",
                    "certificate": pick.certificate})

    args.descriptor_out.parent.mkdir(parents=True, exist_ok=True)
    args.descriptor_out.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    print(f"selector={args.selector} cover={cover_names} -> {args.descriptor_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
