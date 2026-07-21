#!/usr/bin/env python3
"""Demo experiment 3: small-scale sensitivity analysis: clique-grow vs MATCH_RECOGNIZE.

A one-at-a-time (OAT) sweep around a shared baseline cell, on chain patterns.
Defaults (every value user-settable via CLI flags):

  baseline: k=4, N=5000, B=5, sigma=0.05     (--baseline-k/-n/-batches/-sigma)
  axis      default values                    flag
  --------- --------------------------------- -----------------
  k         3, 4, 5                           --ks 3,4,5
  N         1000, 2500, 5000, 10000           --ns 1000,...
  B         2, 5, 10   (at the baseline N)    --bs 2,5,10
  sigma     0.01, 0.05, 0.20                  --sigmas 0.01,...

Per cell: generate the frame, load it, estimate selectivities with the b_unified
estimator on the loaded table, run the native MATCH_RECOGNIZE arm and the
clique_grow selector's pick (selected from those estimates; each arm runs an
untimed warm-up pass first), gate the two on full-tuple identity, record both
runtimes. An arm exceeding the per-query wall (--wall) is a DNF; within an axis the
MATCH_RECOGNIZE arm is censored after its first DNF, the clique-grow arm always runs.

Results stream into <out-dir>/results.json after every cell; plots come from
demo/plot_sensitivity.py (unless --no-plot).

  python3 demo/run_sensitivity.py --out-dir out/sensitivity
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import trino_exec  # noqa: E402

from eimer.selection.plan_selection_clique_grow import select_clique_grow_plan  # noqa: E402

BASELINE = dict(k=4, n=5000, batches=5, sigma=0.05)
AXES = {
    "pattern_length": ("k", [3, 4, 5]),
    "table_size": ("n", [1000, 2500, 5000, 10000]),
    "batches": ("batches", [2, 5, 10]),
    "selectivity": ("sigma", [0.01, 0.05, 0.20]),
}
VALID_K = sorted(k for k, topo in common.TOPOLOGY_SPECS if topo == "chain")


def build_cells(baseline, axes):
    """Baseline + one spoke per (axis, value), deduped on the full parameter key.
    Every cell lists every axis it belongs to (the shared baseline appears in all)."""
    cells: dict[tuple, dict] = {}
    def add(params, axis):
        key = (params["k"], params["n"], params["batches"], params["sigma"])
        cell = cells.setdefault(key, {**params, "axes": []})
        cell["axes"].append(axis)
    for axis, (param, values) in axes.items():
        for v in values:
            add({**baseline, param: v}, axis)
    # ascending hardness inside each axis == ascending (k, n, batches, sigma) overall
    return sorted(cells.values(), key=lambda c: (c["k"], c["n"], c["batches"], c["sigma"]))


def _csv(cast):
    return lambda s: [cast(x) for x in str(s).split(",") if x.strip()]


def _cover_tokens(cover_views, positions):
    return sorted("_".join(sorted(v, key=lambda x: positions[x])) for v in cover_views)


def run_cell(client, cell, wall, mr_censored):
    k, n, b, sigma = cell["k"], cell["n"], cell["batches"], cell["sigma"]
    spec, dep, spec_path = common.load_spec_and_dep(k, "chain")
    all_vars = set(dep.variables)
    pos = dep.positions
    qb = frozenset(range(1, b + 1))
    key_cols = [f"{v}_id" for v in sorted(dep.variables, key=lambda v: pos[v])]

    frame = common.generate_frame(n, sigma, seed=common.SEED)
    loaded = trino_exec.load_frame(client, frame, table="sens_events")
    assert loaded == n, (loaded, n)

    wl, _payload = trino_exec.b_unified_workload(client, dep, table="sens_events",
                                                 total_events=n, batches=b)
    pick = select_clique_grow_plan(dep, wl, query_batches=qb)
    toks = _cover_tokens(pick.compose, pos)

    rec = dict(k=k, n=n, batches=b, sigma=sigma, axes=cell["axes"],
               cover=toks, spec_sha256=common.sha256_text(Path(spec_path).read_text()))

    if mr_censored:
        rec["mr"] = dict(status="censored")  # an earlier cell on this axis DNF'd
        mr_tuples = None
    else:
        mr_ms, mr_n, mr_tuples, meta = trino_exec.mr_streaming(
            client, spec, "sens_events", n, b, qb, key_cols, wall)
        rec["mr"] = (dict(status="ok", total_ms=mr_ms, final_count=mr_n,
                          per_scan_ms=meta.get("per_scan_ms"))
                     if mr_ms is not None
                     else dict(status="dnf", wall_s=wall, spent_ms=meta.get("spent_ms"),
                               per_scan_ms=meta.get("per_scan_ms")))

    cg_ms, cg_n, cg_tuples, _state = trino_exec.eimer_streaming(
        client, dep, toks, all_vars, wl, spec, "sens_events", n, b, qb, key_cols, wall)
    if cg_ms is None:
        rec["clique_grow"] = dict(status="dnf", wall_s=wall)
    else:
        rec["clique_grow"] = dict(status="ok", total_ms=cg_ms, final_count=cg_n)
        if mr_tuples is not None:
            rec["identity_gate"] = "PASSED" if cg_tuples == mr_tuples else "FAILED"
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--wall", type=int, default=600,
                    help="per-query wall T in seconds; an arm exceeding it is a DNF")
    ap.add_argument("--baseline-k", type=int, default=BASELINE["k"])
    ap.add_argument("--baseline-n", type=int, default=BASELINE["n"])
    ap.add_argument("--baseline-batches", type=int, default=BASELINE["batches"])
    ap.add_argument("--baseline-sigma", type=float, default=BASELINE["sigma"])
    ap.add_argument("--ks", type=_csv(int), default=AXES["pattern_length"][1],
                    help=f"pattern-length axis values (chain specs exist for k in {VALID_K})")
    ap.add_argument("--ns", type=_csv(int), default=AXES["table_size"][1],
                    help="table-size axis values")
    ap.add_argument("--bs", type=_csv(int), default=AXES["batches"][1],
                    help="batch-count axis values (at the baseline N)")
    ap.add_argument("--sigmas", type=_csv(float), default=AXES["selectivity"][1],
                    help="dependent-selectivity axis values")
    ap.add_argument("--server", default=os.environ.get("TRINO_SERVER", "localhost:8080"))
    ap.add_argument("--catalog", default=os.environ.get("TRINO_CATALOG", "memory"))
    ap.add_argument("--schema", default=os.environ.get("TRINO_SCHEMA", "default"))
    ap.add_argument("--user", default=os.environ.get("TRINO_USER", "eimer"))
    ap.add_argument("--out-dir", default="out/sensitivity")
    ap.add_argument("--no-plot", action="store_true")
    ap.add_argument("--smoke", action="store_true",
                    help="2 tiny cells only (wiring check, not science)")
    a = ap.parse_args()

    baseline = dict(k=a.baseline_k, n=a.baseline_n, batches=a.baseline_batches,
                    sigma=a.baseline_sigma)
    axes = {"pattern_length": ("k", a.ks), "table_size": ("n", a.ns),
            "batches": ("batches", a.bs), "selectivity": ("sigma", a.sigmas)}
    bad_k = [k for k in {baseline["k"], *a.ks} if k not in VALID_K]
    if bad_k:
        raise SystemExit(f"no committed chain spec for k={bad_k}; valid: {VALID_K}")

    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cells = build_cells(baseline, axes)
    if a.smoke:
        cells = [dict(k=3, n=1000, batches=2, sigma=0.05, axes=["smoke"]),
                 dict(k=3, n=1000, batches=2, sigma=0.20, axes=["smoke"])]

    client = trino_exec.connect(server=a.server, catalog=a.catalog, schema=a.schema,
                                user=a.user, timeout_s=a.wall)
    print(f"sensitivity: {len(cells)} cells (baseline {baseline}), wall {a.wall}s, "
          f"catalog {a.catalog}")

    records = []
    mr_dnf_axes: set[str] = set()  # per-axis monotone censoring for the MR arm
    t_suite = time.perf_counter()
    for i, cell in enumerate(cells):
        censored = bool(set(cell["axes"]) & mr_dnf_axes) and cell["axes"] != ["smoke"]
        tag = f"k{cell['k']} N{cell['n']} B{cell['batches']} s{cell['sigma']}"
        print(f"[{i + 1}/{len(cells)}] {tag}" + ("  (MR censored)" if censored else ""),
              flush=True)
        try:
            rec = run_cell(client, cell, a.wall, censored)
        except Exception as exc:  # a bad user-chosen cell must not kill the sweep
            print(f"    cell ERROR: {type(exc).__name__}: {str(exc)[:200]}", flush=True)
            records.append(dict(k=cell["k"], n=cell["n"], batches=cell["batches"],
                                sigma=cell["sigma"], axes=cell["axes"],
                                mr=dict(status="error"),
                                clique_grow=dict(status="error"),
                                error=f"{type(exc).__name__}: {str(exc)[:300]}"))
            common.write_json(out / "results.json", dict(
                baseline=baseline, wall_s=a.wall, catalog=a.catalog,
                axes={k: v[1] for k, v in axes.items()}, records=records))
            continue
        if rec["mr"]["status"] == "dnf":
            mr_dnf_axes.update(cell["axes"])
        records.append(rec)
        mr_s = rec["mr"].get("total_ms")
        cg_s = rec["clique_grow"].get("total_ms")
        print(f"    MR: {rec['mr']['status']}" + (f" {mr_s:.0f} ms" if mr_s else "")
              + f"   clique_grow: {rec['clique_grow']['status']}"
              + (f" {cg_s:.0f} ms" if cg_s else "")
              + (f"   speedup {mr_s / cg_s:.2f}x" if mr_s and cg_s else "")
              + (f"   gate={rec.get('identity_gate')}" if "identity_gate" in rec else ""),
              flush=True)
        common.write_json(out / "results.json", dict(
            baseline=baseline, wall_s=a.wall, catalog=a.catalog,
            axes={k: v[1] for k, v in axes.items()}, records=records))
    print(f"suite done in {time.perf_counter() - t_suite:.0f}s "
          f"-> {out / 'results.json'}")

    gates = [r.get("identity_gate") for r in records if "identity_gate" in r]
    bad = [g for g in gates if g != "PASSED"]
    print(f"identity gates: {len(gates) - len(bad)}/{len(gates)} PASSED"
          + (f"  FAILURES: {len(bad)}" if bad else ""))

    if not a.no_plot and not a.smoke:
        rc = subprocess.call([sys.executable, str(Path(__file__).parent / "plot_sensitivity.py"),
                              "--results", str(out / "results.json"),
                              "--out-dir", str(out)])
        if rc != 0:
            print("plotting failed", file=sys.stderr)
            return 1
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
