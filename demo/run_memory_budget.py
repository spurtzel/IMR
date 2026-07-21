#!/usr/bin/env python3
"""Demo: the storage budget M on a small cell.

Two layers:

1. Engine-free descent: sweep the budget M from above the unconstrained pick's
   predicted peak down to 0. At each step select_clique_grow_plan runs under
   max_storage_bytes=M, and the distinct plan sequence with its predicted peaks and
   repair drops is printed and recorded.

2. Live verification on Trino: each distinct plan is executed with the full
   per-batch streaming protocol on the loaded table, measuring resident state.
   Gates:
     (a) predicted peak <= M at every step;
     (b) every budget plan yields the identical final result count (the budget
         changes maintained state, never answers);
     (c) measured resident state shrinks monotonically along the descent.
   With --catalog iceberg the measured Parquet bytes per cache table are also
   compared against the predicted peak.

  python3 demo/run_memory_budget.py --n 4000 --k 4 --topology star --sigma 0.05 \
      --batches 4 --catalog memory --out-dir out/demo_memory_budget
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import trino_exec  # noqa: E402

from eimer.selection.plan_selection_clique_grow import select_clique_grow_plan  # noqa: E402
from eimer.selection.state_size_trino import peak_state_bytes_trino  # noqa: E402


def _cover_tokens(cover_views, positions) -> list[str]:
    return sorted("_".join(sorted(v, key=lambda x: positions[x])) for v in cover_views)


def descend(dep, wl, qb):
    """Sweep M downward through the distinct-pick breakpoints: start at M=inf
    (unconstrained pick), then repeatedly set M just below the current pick's
    predicted peak. Terminates at the empty cover (predicted peak 0). Returns the
    list of steps [{m_max, cover, predicted_peak_bytes, drops, total_cost}]."""
    steps = []
    m = None  # None == M=inf for the first step
    while True:
        res = select_clique_grow_plan(dep, wl, query_batches=qb,
                                      max_storage_bytes=(float("inf") if m is None else m),
                                      peak_bytes_fn=peak_state_bytes_trino)
        cert = res.certificate
        cover = _cover_tokens(res.compose, dep.positions)
        steps.append(dict(
            m_bytes=("inf" if m is None else m),
            cover=cover,
            predicted_peak_bytes=cert["predicted_peak_bytes"],
            budget_repair_drops=cert["budget_repair_drops"],
            total_cost=round(cert["total_cost"], 6),
        ))
        peak = cert["predicted_peak_bytes"]
        if peak <= 0:
            break
        m = peak - 1  # the next breakpoint: force a strictly smaller plan
    return steps


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=4000, help="total events N (keep <= 5000)")
    ap.add_argument("--k", type=int, default=4)
    ap.add_argument("--topology", default="star")
    ap.add_argument("--sigma", type=float, default=0.05)
    ap.add_argument("--batches", type=int, default=4)
    ap.add_argument("--seed", type=int, default=common.SEED)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--server", default=os.environ.get("TRINO_SERVER", "localhost:8080"))
    ap.add_argument("--catalog", default=os.environ.get("TRINO_CATALOG", "memory"))
    ap.add_argument("--schema", default=os.environ.get("TRINO_SCHEMA", "default"))
    ap.add_argument("--user", default=os.environ.get("TRINO_USER", "eimer"))
    ap.add_argument("--skip-live", action="store_true",
                    help="engine-free descent only (no Trino needed)")
    ap.add_argument("--out-dir", default="out/demo_memory_budget")
    a = ap.parse_args()

    spec, dep, spec_path = common.load_spec_and_dep(a.k, a.topology)
    all_vars = set(dep.variables)
    wl = common.const_sigma_workload(dep, total_events=a.n, batches=a.batches,
                                     sigma=a.sigma)
    qb = frozenset(range(1, a.batches + 1))
    key_cols = [f"{v}_id" for v in sorted(dep.variables, key=lambda v: dep.positions[v])]

    # --- layer 1: engine-free descent ---
    steps = descend(dep, wl, qb)
    print(f"cell: {a.topology} k={a.k} N={a.n} sigma={a.sigma} B={a.batches}")
    print(f"budget descent (M sweep, {len(steps)} distinct plans):")
    for s in steps:
        print(f"  M<= {s['m_bytes']:>12}: cover={s['cover'] or '(empty)'}  "
              f"predicted_peak={s['predicted_peak_bytes']:,} B  "
              f"repair_drops={s['budget_repair_drops']}  cost={s['total_cost']:.1f}")

    out = Path(a.out_dir)
    cell = dict(topology=a.topology, k=a.k, n=a.n, sigma=a.sigma, batches=a.batches,
                seed=a.seed)
    common.write_json(out / "descent.json", dict(cell=cell, steps=steps))
    common.write_json(out / "regression_fingerprint.json", dict(
        cell=cell, spec_sha256=common.sha256_text(Path(spec_path).read_text()),
        descent=steps))

    if a.skip_live:
        print(f"engine-free descent recorded -> {out / 'descent.json'}")
        return 0

    # --- layer 2: live compliance ---
    frame = common.generate_frame(a.n, a.sigma, seed=a.seed)
    client = trino_exec.connect(server=a.server, catalog=a.catalog, schema=a.schema,
                                user=a.user, timeout_s=a.timeout)
    table = "demo_events_mb"
    loaded = trino_exec.load_frame(client, frame, table=table)
    assert loaded == a.n, (loaded, a.n)
    print(f"\nloaded {loaded} rows into {a.catalog}.{a.schema}.{table}; "
          f"executing each distinct plan with untimed resident-state snapshots:")

    live = []
    measured_peaks = {}
    for s in steps:
        toks = list(s["cover"])
        ms, n, _tuples, state = trino_exec.eimer_streaming(
            client, dep, toks, all_vars, wl, spec, table, a.n, a.batches, qb,
            key_cols, a.timeout, measure_state=True)
        if ms is None:
            print(f"  {s['cover'] or '(empty)'}: DNF")
            live.append(dict(cover=s["cover"], status="DNF"))
            continue
        peak_rows = state["max_resident_rows"]
        measured_peaks[tuple(s["cover"])] = peak_rows
        bytes_note = ""
        if a.catalog == "iceberg":
            measured_bytes = 0
            for cache in state["per_cache_rows"]:
                try:
                    _, rows = client.execute(
                        f'SELECT coalesce(sum(file_size_in_bytes), 0) FROM "{cache}$files"')
                    measured_bytes += int(rows[0][0])
                except trino_exec.TrinoClientError:
                    pass
            ok = measured_bytes <= s["predicted_peak_bytes"]
            bytes_note = (f"  measured_bytes={measured_bytes:,} <= "
                          f"predicted {s['predicted_peak_bytes']:,}: "
                          f"{'COMPLIANT' if ok else 'BREACH'}")
        print(f"  {str(s['cover'] or '(empty)'):<40} total={ms:>9.1f} ms  "
              f"final={n}  measured_peak_rows={peak_rows:,}{bytes_note}")
        live.append(dict(cover=s["cover"], total_ms=ms, final_count=n,
                         measured_peak_rows=peak_rows, state=state))

    # --- hard gates ---
    gate_a = all(s["m_bytes"] == "inf" or s["predicted_peak_bytes"] <= s["m_bytes"]
                 for s in steps)
    finals = [l["final_count"] for l in live if l.get("final_count") is not None]
    gate_b = len(set(finals)) <= 1 and bool(finals)
    peaks_along = [l["measured_peak_rows"] for l in live
                   if l.get("measured_peak_rows") is not None]
    gate_c = all(x >= y for x, y in zip(peaks_along, peaks_along[1:]))
    print("\ngates:")
    print(f"  (a) predicted peak <= M at every step .......... "
          f"{'PASSED' if gate_a else 'FAILED'}")
    print(f"  (b) identical final result under every budget .. "
          f"{'PASSED' if gate_b else 'FAILED'}  (final counts: {sorted(set(finals))})")
    print(f"  (c) measured resident state shrinks monotonically "
          f"{'PASSED' if gate_c else 'FAILED'}  (peaks: {peaks_along})")

    common.write_json(out / "live_compliance.json",
                      dict(cell=cell, catalog=a.catalog, live=live,
                           gates=dict(predicted_within_budget=gate_a,
                                      identical_results=gate_b,
                                      monotone_state=gate_c)))
    all_ok = gate_a and gate_b and gate_c
    print(f"\nverdict: {'ALL GATES PASSED' if all_ok else 'GATE FAILURE'} "
          f"-> {out / 'live_compliance.json'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
