#!/usr/bin/env python3
"""The storage-budget and the distribution-shift experiment on Trino (the two right panels of the paper's Fig. 11).

Part "budget": for each workload, the storage budget M is shrunk from the unconstrained pick's footprint towards zero.
  The selector's repair drops views in a fixed order (the view with the least cost increase per predicted byte freed
  first), so one descent gives the plan for every budget. Every plan of the descent, the all-singleton strategy and
  native MATCH_RECOGNIZE are executed; the figure shows the speed-up over native MR of the plan selected at each
  budget (median and min-max over the workloads; "drop-only": without the all-singleton strategy as a fallback).
  paper: 12 workloads (chain, star, diamond, complete, triangle, cycle; k = 4 .. 6) on tables of 100k-200k rows
Part "shift": a chain query (k = 4) over a table whose selectivities loosen after batch 6 of 20. Four arms, per-batch
  latency: native MR; the plan selected before the shift, kept ("stale"); re-selection right at the shift ("instant":
  the new plan's views are rebuilt over the history, then it continues); re-selection after a lag of four batches,
  on statistics of the post-shift batches seen so far ("delayed").
  paper: N = 100,000; (sigma, type rate) = (0.002, 0.02) before and (0.013, 0.03) after the shift
Native MR has no window in these queries, so it is feasible on Trino for small tables only.

Arms run one after the other, single-threaded, join reordering off, no warm-up of the approaches; ingestion is untimed for EIMER.
Gates: every executed plan returns the tuples of native MR (of the unconstrained pick / the stale plan where MR does
not finish); the descent equals the selector's own decisions under the corresponding budgets.

Axis values and limits (environment):
  EIMER_REPRO_BS_PARTS=budget,shift
  EIMER_REPRO_BUDGET_WORKLOADS=chain:4:0.05:0.05:3000:5,...     topology:k:sigma:type rate:N:batches per workload
  EIMER_REPRO_SHIFT_N=100000  EIMER_REPRO_SHIFT_PARAMS=0.002,0.02,0.013,0.03   (sigma1, rate1, sigma2, rate2)
  EIMER_REPRO_WALL=<s>               limit per statement (default 1,800 s)
  EIMER_REPRO_MR_CELL_BOUND=<s>      limit for the total of the native-MR arm (default 7,200 s)

  python3 budget_shift.py --list
  python3 budget_shift.py --only budget_chain_k4_s0.05_f0.05_n3000_b5 --results-dir <dir>
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import trino_lib as tl  # noqa: E402  (wires the repository onto sys.path)
import trino_exec  # noqa: E402
import common  # noqa: E402  (demo/common.py)
import eimer.selection.plan_selection_clique_grow as cg  # noqa: E402
from eimer.selection.state_size_trino import peak_state_bytes_trino  # noqa: E402
from eimer.query.temporal_propagation import explicit_selection_inputs  # noqa: E402
from eimer.query.query_spec import load_query_spec_file, query_spec_to_dependency_graph  # noqa: E402
from eimer.sql.backend import SqlDialect, backend_for  # noqa: E402
from eimer.models import make_view  # noqa: E402
from workload.data.datagen import generate_events  # noqa: E402

BACKEND = backend_for(SqlDialect.TRINO)
PAPER_WORKLOADS = ("chain:4:0.01:0.02:200000:25,chain:5:0.005:0.02:100000:25,chain:5:0.002:0.05:100000:25,chain:6:0.003:0.02:200000:25,"
                   "star:4:0.01:0.02:200000:25,star:5:0.005:0.02:100000:25,diamond:4:0.02:0.02:200000:25,diamond:4:0.05:0.02:100000:25,"
                   "complete:4:0.05:0.02:100000:25,triangle:4:0.02:0.02:200000:25,cycle:5:0.005:0.02:200000:25,chain:5:0.005:0.02:100000:100")
SHIFT_B, SHIFT_AFTER_BATCH, SHIFT_LAG = 20, 6, 4


def _workloads() -> list:
    out = []
    for w in os.environ.get("EIMER_REPRO_BUDGET_WORKLOADS", PAPER_WORKLOADS).replace(" ", "").split(","):
        if w:
            topo, k, sigma, ftype, n, b = w.split(":")
            out.append(dict(part="budget", topo=topo, k=int(k), sigma=float(sigma), ftype=float(ftype), n=int(n), batches=int(b)))
    return out


PARTS = [p for p in os.environ.get("EIMER_REPRO_BS_PARTS", "budget,shift").split(",") if p]
SHIFT_N = int(os.environ.get("EIMER_REPRO_SHIFT_N", 100_000))
SHIFT_PARAMS = [float(x) for x in os.environ.get("EIMER_REPRO_SHIFT_PARAMS", "0.002,0.02,0.013,0.03").split(",")]
assert SHIFT_N % SHIFT_B == 0 and len(SHIFT_PARAMS) == 4
if 8 * max(SHIFT_PARAMS[1], SHIFT_PARAMS[3]) > 1:
    raise SystemExit(f"shift: type rates {SHIFT_PARAMS[1]}, {SHIFT_PARAMS[3]}: eight event types, so a rate is at most 0.125")


def cells() -> list:
    out = []
    if "budget" in PARTS:
        out += _workloads()
    if "shift" in PARTS:
        s1, f1, s2, f2 = SHIFT_PARAMS
        out.append(dict(part="shift", topo="chain", k=4, n=SHIFT_N, batches=SHIFT_B, shift_after_batch=SHIFT_AFTER_BATCH, lag_batches=SHIFT_LAG,
                        sigma1=s1, ftype1=f1, sigma2=s2, ftype2=f2))
    return out


def cell_name(c: dict) -> str:
    if c["part"] == "budget":
        return f"budget_{c['topo']}_k{c['k']}_s{c['sigma']}_f{c['ftype']}_n{c['n']}_b{c['batches']}"
    return f"shift_{c['topo']}_k{c['k']}_n{c['n']}_s{c['sigma1']}_f{c['ftype1']}_to_s{c['sigma2']}_f{c['ftype2']}"


def wall_s() -> int:
    return int(os.environ.get("EIMER_REPRO_WALL", 1800))


def with_driver(toks: list, vs: list) -> list:
    """the views to execute: the cover plus the last variable's own view if no view holds it (the query is driven by it)."""
    return list(toks) + ([vs[-1]] if not any(vs[-1] in make_view(t.split("_")) for t in toks) else [])


def gate(tuples, truth):
    if tuples is None or truth is None:
        return None
    return dict(n=len(tuples), n_reference=len(truth), missing=len(truth - tuples), extra=len(tuples - truth), identical=(tuples == truth))


# ---------------------------------------------------------------- part "budget"
def drop_trajectory(dep, wl, qb, kappa):
    """[(cover, predicted peak bytes, predicted cost)]: the unconstrained pick, then one entry per repair drop (the view
    with the least cost increase per predicted byte freed, re-evaluated after every drop) down to the empty cover.
    The same rule as the selector's budget repair; run_budget() checks the two against each other."""
    pos = dep.positions
    pick = cg.select_clique_grow_plan(dep, wl, query_batches=qb, kappa=kappa)
    dsel, wsel = explicit_selection_inputs(dep, wl)
    bidx = len(wsel.batch_sizes)
    cost = lambda c: cg._total_cost(dsel, c, wsel, bidx, bidx, len(qb), kappa)
    peak = lambda c: peak_state_bytes_trino([frozenset(s) for s in c], dsel, wsel, query_batches=qb)
    cover = cg._sorted([frozenset(v) for v in pick.compose], pos)
    steps = [(cover, peak(cover), cost(cover))]
    while cover:
        base_cost, base_peak = cost(cover), peak(cover)
        best = None
        for i in range(len(cover)):
            nc = cg._sorted(cg._drop(cover, i), pos)
            freed = base_peak - peak(nc)
            if freed <= 0:
                continue
            key = ((cost(nc) - base_cost) / freed, cg._skey(nc, pos))
            if best is None or key < best[0]:
                best = (key, nc)
        cover = best[1] if best is not None else []
        steps.append((cover, peak(cover), cost(cover)))
    return steps


def run_budget(c: dict, client) -> dict:
    n, B, k = c["n"], c["batches"], c["k"]
    T = wall_s()
    spec, dep, _ = common.load_spec_and_dep(k, c["topo"])
    pos = dep.positions
    vs = sorted(dep.variables, key=lambda v: pos[v])
    all_vars = set(dep.variables)
    key_cols = [f"{v}_id" for v in vs]
    qb = frozenset(tl.query_schedule(B))
    table = tl.ensure_events(client, f"events_bs_n{n}_s{str(c['sigma']).replace('.', '')}_f{str(c['ftype']).replace('.', '')}", n,
                             lambda: generate_events(common.make_datagen_config(n, c["sigma"], ftype=c["ftype"], ntypes=8, seed=42)).frame)
    wl, payload = tl.estimated_workload(client, dep, table, n, B, BACKEND.window_rule)
    traj = drop_trajectory(dep, wl, qb, BACKEND.kappa)
    # the descent against the selector's own decisions: under M = a step's predicted peak the selector returns that
    # step's cover, or the empty cover (= all singletons) where that is cheaper
    consistent = True
    for i, (cover, pk, _) in enumerate(traj):
        res = cg.select_clique_grow_plan(dep, wl, query_batches=qb, kappa=BACKEND.kappa, max_storage_bytes=pk, peak_bytes_fn=peak_state_bytes_trino)
        got = tl.toks_of(res.compose, pos)
        consistent &= (got in (tl.toks_of(cover, pos), []) and res.certificate["budget_repair_drops"] == i)
    print(f"   descent: {[(tl.toks_of(cv, pos) or '(empty)', round(pk / 1e6, 3)) for cv, pk, _ in traj]} MB predicted | equals the selector's repair: {consistent}", flush=True)

    t0 = time.time()
    mr, m_t = tl.mr_streaming(client, spec, table, n, B, sorted(qb), key_cols, T, bounded=False)
    mr["wall_clock_s"] = round(time.time() - t0, 1)
    print(f"   mr      : {mr['status']} total={mr.get('total_ms')} ms matches={mr.get('final_count')} {mr.get('censored') or ''}", flush=True)
    steps, truth, truth_name = [], m_t, "mr"
    for cover, pk, cost in traj:
        toks = tl.toks_of(cover, pos)
        floor = not toks
        run_toks = list(vs) if floor else with_driver(toks, vs)   # the empty cover is executed as the all-singleton strategy
        rec, tuples = tl.eimer_streaming(client, dep, run_toks, all_vars, wl, spec, table, n, B, qb, key_cols, T, measure_storage=True)
        if truth is None and tuples is not None:
            truth, truth_name = tuples, "the unconstrained pick"
        st = rec.get("state") or {}
        dsel, wsel = explicit_selection_inputs(dep, wl)
        modelled = peak_state_bytes_trino([frozenset(make_view(t.split("_"))) for t in run_toks], dsel, wsel, query_batches=qb)
        steps.append(dict(plan=("(empty)" if floor else "|".join(toks)), views=toks, executed_views=run_toks, predicted_peak_bytes=pk, predicted_cost=cost,
                          modelled_state_bytes=modelled, state_rows=st.get("rows"), state_uncompressed_bytes=st.get("uncompressed_bytes"),
                          peak_query_memory_bytes=rec.get("peak_query_memory_bytes"),
                          status=rec["status"], measured_ms=rec.get("total_ms"), final_count=rec.get("final_count"),
                          measured_peak_bytes=st.get("bytes"), state_files=st.get("files"),
                          compliant=(None if st.get("bytes") is None or floor else bool(st["bytes"] <= max(pk, 1))),
                          gate=gate(tuples, truth) if tuples is not truth else None))
        print(f"   {steps[-1]['plan']:<28}: {rec['status']} total={rec.get('total_ms')} ms state={st.get('bytes')} B (predicted {pk:.0f}) "
              f"matches={rec.get('final_count')} | == {truth_name} {steps[-1]['gate'] and steps[-1]['gate']['identical']}", flush=True)
    g = [s["gate"]["identical"] for s in steps if s["gate"]]
    return dict(table=table, key_cols=key_cols, limits=dict(statement_s=T, mr_total_s=tl.MR_CELL_BOUND_S), pick=steps[0]["views"],
                selection=dict(algorithm="clique-grow with the storage budget M", kappa=BACKEND.kappa, state_model="calibrated Trino / Iceberg byte model",
                               estimator="b_unified on the loaded table, each stated predicate on its own", estimates_this_run=payload),
                mr=mr, mr_total_ms=mr.get("total_ms"), steps=steps, tag=cell_name(c),
                gates=dict(results_identical=(all(g) if g else None), n_compared=len(g), reference=truth_name, descent_equals_selector=consistent,
                           all_plans_finished=all(s["status"] == "ok" for s in steps)))


# ---------------------------------------------------------------- part "shift"
def shift_frame(c: dict) -> pd.DataFrame:
    """the generator's table for (sigma1, rate1) up to the shift, then its table for (sigma2, rate2), spliced: ids
    continue and both time columns are moved past the first part's end."""
    bsz = c["n"] // c["batches"]
    n1 = c["shift_after_batch"] * bsz
    f1 = generate_events(common.make_datagen_config(n1, c["sigma1"], ftype=c["ftype1"], ntypes=8, seed=42)).frame
    f2 = generate_events(common.make_datagen_config(c["n"] - n1, c["sigma2"], ftype=c["ftype2"], ntypes=8, seed=49)).frame.copy()
    f2["id"] = f2["id"] + n1
    for col in ("time", "ts"):
        f2[col] = f2[col] + (f1[col].max() - f2[col].min()) + pd.Timedelta(seconds=1)
    return pd.concat([f1, f2], ignore_index=True)


def estimates_on(client, dep, table: str, lo: int, hi: int, n: int, batches: int):
    """(workload, payload): the selectivities measured on the rows lo < id <= hi, for a workload of n rows."""
    win = "bs_estimation_window"
    trino_exec.drop_table(client, win)
    client.execute(f"CREATE TABLE {win} AS SELECT * FROM {table} WHERE id > {lo} AND id <= {hi}")
    try:
        return tl.estimated_workload(client, dep, win, n, batches, BACKEND.window_rule)
    finally:
        trino_exec.drop_table(client, win)


def run_shift(c: dict, client) -> dict:
    n, B, k = c["n"], c["batches"], c["k"]
    T = wall_s()
    bsz = n // B
    shift_b, lag = c["shift_after_batch"], c["lag_batches"]
    spec, dep, _ = common.load_spec_and_dep(k, c["topo"])
    pos = dep.positions
    vs = sorted(dep.variables, key=lambda v: pos[v])
    all_vars = set(dep.variables)
    key_cols = [f"{v}_id" for v in vs]
    qb = frozenset(tl.query_schedule(B))
    table = tl.ensure_events(client, f"events_shift_n{n}", n, lambda: shift_frame(c))
    # the plan selected before the shift, and the plans a re-selection finds on the post-shift batches seen by then
    wl_old, pay_old = estimates_on(client, dep, table, 0, shift_b * bsz, n, B)
    wl_new, pay_new = estimates_on(client, dep, table, shift_b * bsz, (shift_b + lag) * bsz, n, B)
    pick = lambda wl: tl.toks_of(cg.select_clique_grow_plan(dep, wl, query_batches=qb, kappa=BACKEND.kappa).compose, pos)
    toks_old, toks_new = pick(wl_old), pick(wl_new)
    print(f"   plan before the shift {toks_old or '(all singletons)'} | re-selected after it {toks_new or '(all singletons)'}", flush=True)
    run_old = with_driver(toks_old, vs) if toks_old else list(vs)
    run_new = with_driver(toks_new, vs) if toks_new else list(vs)
    uniform = [b * bsz for b in range(1, B + 1)]

    t0 = time.time()
    mr, m_t = tl.mr_streaming(client, spec, table, n, B, sorted(qb), key_cols, T, bounded=False)
    mr["wall_clock_s"] = round(time.time() - t0, 1)
    print(f"   mr      : {mr['status']} total={mr.get('total_ms')} ms matches={mr.get('final_count')} {mr.get('censored') or ''}", flush=True)
    arms = {}
    stale, s_t = tl.eimer_stream_bounds(client, dep, run_old, all_vars, wl_old, table, uniform, set(qb), key_cols, T, measure_storage=True)
    arms["stale"] = dict(stale, latency_ms=[[b, round(u + (q or 0), 1)] for b, _, u, q in stale.get("per_batch", [])])
    truth, truth_name = (m_t, "mr") if m_t is not None else (s_t, "the stale plan")
    arms["stale"]["gate"] = gate(s_t, truth) if truth_name == "mr" else None
    print(f"   stale   : {stale['status']} total={stale.get('total_ms')} ms state={(stale.get('state') or {}).get('bytes')} B matches={stale.get('final_count')}", flush=True)
    for name, switch in (("instant", shift_b + 1), ("delayed", shift_b + lag)):
        pre = switch - 1
        seg_a, _ = tl.eimer_stream_bounds(client, dep, run_old, all_vars, wl_old, table, uniform[:pre], set(range(1, pre + 1)), key_cols, T)
        # the switch: the new plan's views are rebuilt over the history (first batch, not queried), then the stream continues
        bounds_b = [pre * bsz] + uniform[pre:]
        seg_b, b_t = tl.eimer_stream_bounds(client, dep, run_new, all_vars, wl_new, table, bounds_b, set(range(2, len(bounds_b) + 1)), key_cols, T, measure_storage=True)
        lat = [[b, round(u + (q or 0), 1)] for b, _, u, q in seg_a.get("per_batch", [])]
        pb = seg_b.get("per_batch", [])
        rebuild = pb[0][2] if pb else None
        for j, (_, _, u, q) in enumerate(pb[1:]):
            lat.append([switch + j, round(u + (q or 0) + ((rebuild or 0) if j == 0 else 0), 1)])   # the rebuild is charged to the switch batch
        ok = seg_a["status"] == "ok" and seg_b["status"] == "ok"
        arms[name] = dict(status=("ok" if ok else "dnf" if "dnf" in (seg_a["status"], seg_b["status"]) else "error"), switch_batch=switch,
                          total_ms=(round(seg_a["total_ms"] + seg_b["total_ms"], 1) if ok else None), rebuild_ms=rebuild,
                          final_count=seg_b.get("final_count"), state=seg_b.get("state"), latency_ms=lat, gate=gate(b_t, truth),
                          peak_query_memory_bytes=max(seg_a.get("peak_query_memory_bytes") or 0, seg_b.get("peak_query_memory_bytes") or 0),
                          notes="; ".join(x.get("notes", "") for x in (seg_a, seg_b) if x.get("notes")))
        print(f"   {name:<8}: {arms[name]['status']} total={arms[name]['total_ms']} ms (rebuild {rebuild} ms at batch {switch}) state={(seg_b.get('state') or {}).get('bytes')} B "
              f"| == {truth_name} {arms[name]['gate'] and arms[name]['gate']['identical']}", flush=True)
    g = [a["gate"]["identical"] for a in arms.values() if a.get("gate")]
    return dict(table=table, key_cols=key_cols, limits=dict(statement_s=T, mr_total_s=tl.MR_CELL_BOUND_S),
                plan_before_shift=toks_old, plan_after_reselection=toks_new, plan_changes=(toks_old != toks_new), pick=toks_old,
                selection=dict(algorithm="clique-grow", kappa=BACKEND.kappa, estimator="b_unified, each stated predicate on its own",
                               before_shift=f"rows of batches 1..{shift_b}", after_shift=f"rows of batches {shift_b + 1}..{shift_b + lag}",
                               estimates_before=pay_old, estimates_after=pay_new),
                mr=dict(mr, latency_ms=[[i + 1, ms] for i, (_, ms) in enumerate(mr.get("per_scan_ms", []))]), arms=arms,
                gates=dict(results_identical=(all(g) if g else None), n_compared=len(g), reference=truth_name,
                           all_arms_finished=all(a["status"] == "ok" for a in arms.values())))


def run_cell(c: dict, out_root: Path) -> dict:
    name = cell_name(c)
    rec_path = out_root / "records" / f"{name}.json"
    rec_path.parent.mkdir(parents=True, exist_ok=True)
    client = tl.connect(timeout_s=wall_s())
    print(f"[{name}] {c}", flush=True)
    body = run_budget(c, client) if c["part"] == "budget" else run_shift(c, client)
    rec = dict(cell=name, figure="11, storage budget and distribution shift (Trino)", params=dict(c), **tl.engine_info(client),
               protocol=dict(warmup=False, ingestion="untimed", query_schedule="after every batch",
                             server_warmed_by_untimed_first_workload=(os.environ.get("EIMER_REPRO_SERVER_WARMUP") == "1")),
               **body)
    print(f"   gates   : {rec['gates']}", flush=True)
    rec["_recorded"] = tl.write_record(out_root, name, rec)
    return rec


def main():
    tl.run_main(__doc__, cells, cell_name, run_cell, HERE / "results_budget_shift")


if __name__ == "__main__":
    main()
