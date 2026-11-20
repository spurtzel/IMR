#!/usr/bin/env python3
"""Query topology and memory footprint on Trino (the two experiments of the paper's Section 7.4 without a figure).

  topology  six dependency structures at k = 4 (chain, star, triangle, cycle, diamond, complete), each at several band
            selectivities: native MATCH_RECOGNIZE vs. clique-grow's pick, the query after every batch
            paper: N = 50,000 in 25 batches, type rate 0.01, sigma = 0.01, 0.05, 0.1
  memory    the chain at k = 3 .. 6 under four materialization strategies: all singletons, clique-grow's pick, one view
            over the full pattern, all pattern prefixes. Recorded per strategy: the persistent state (the Iceberg data
            files of its views at the end of the run) and the peak transient query memory (the largest peak memory
            Trino reports for one of its statements). Native MATCH_RECOGNIZE is not run: it keeps no state, and on
            Trino it does not finish at table sizes at which the strategies differ.
            paper: N = 400,000, sigma = 0.002, type rate 0.02

Tables: the generator's (eight event types, 1,000 events per second, seed 42). The queries have no time window, so
native MR is the statement as written. Single-threaded, join reordering off, no warm-up of the approaches, ingestion untimed.

Gates: topology: mr == eimer on full tuples where native MR finishes. memory: every strategy that finishes returns the
tuples of the all-singleton strategy.

Options (environment):
  EIMER_REPRO_TM_PARTS=topology,memory
  EIMER_REPRO_TOPO_TOPOLOGIES=chain,star,triangle,cycle,diamond,complete   EIMER_REPRO_TOPO_SIGMAS=0.01,0.05,0.1
  EIMER_REPRO_TOPO_N=8000   EIMER_REPRO_TOPO_B=25   EIMER_REPRO_TOPO_RATE=0.02
  EIMER_REPRO_MEM_KS=3,4,5,6   EIMER_REPRO_MEM_N=400000   EIMER_REPRO_MEM_B=25   EIMER_REPRO_MEM_SIGMA=0.002
  EIMER_REPRO_MEM_RATE=0.02    EIMER_REPRO_MEM_TOTAL=<s>   limit for the total of one strategy (default 3,600 s)
  EIMER_REPRO_WALL=<s>         limit per statement (default 1,800 s)

  python3 topology_memory.py --list
  python3 topology_memory.py --only topology_star_k4_s0.05_f0.02_n8000_b25 --results-dir <dir>
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import trino_lib as tl  # noqa: E402  (wires the repository onto sys.path)
import common  # noqa: E402  (demo/common.py)
from workload.data.datagen import generate_events  # noqa: E402
from eimer.models import make_view  # noqa: E402
from eimer.selection.plan_selection_clique_grow import select_clique_grow_plan  # noqa: E402
from eimer.sql.backend import SqlDialect, backend_for  # noqa: E402

BACKEND = backend_for(SqlDialect.TRINO)
PARTS = [p for p in os.environ.get("EIMER_REPRO_TM_PARTS", "topology,memory").split(",") if p]
TOPOLOGIES = [t for t in os.environ.get("EIMER_REPRO_TOPO_TOPOLOGIES", "chain,star,triangle,cycle,diamond,complete").split(",") if t]
TOPO_SIGMAS = tl.env_list("EIMER_REPRO_TOPO_SIGMAS", (0.01, 0.05, 0.1), float)
TOPO_N = tl.env_list("EIMER_REPRO_TOPO_N", (8000,))[0]
TOPO_B = tl.env_list("EIMER_REPRO_TOPO_B", (25,))[0]
TOPO_RATE = tl.env_list("EIMER_REPRO_TOPO_RATE", (0.02,), float)[0]
MEM_KS = tl.env_list("EIMER_REPRO_MEM_KS", (3, 4, 5, 6))
MEM_N = tl.env_list("EIMER_REPRO_MEM_N", (400_000,))[0]
MEM_B = tl.env_list("EIMER_REPRO_MEM_B", (25,))[0]
MEM_SIGMA = tl.env_list("EIMER_REPRO_MEM_SIGMA", (0.002,), float)[0]
MEM_RATE = tl.env_list("EIMER_REPRO_MEM_RATE", (0.02,), float)[0]
STRATEGIES = ("singletons", "pick", "full_pattern", "prefixes")
assert set(PARTS) <= {"topology", "memory"}, PARTS
assert all((4, t) in common.TOPOLOGY_SPECS for t in TOPOLOGIES), TOPOLOGIES
assert all((k, "chain") in common.TOPOLOGY_SPECS for k in MEM_KS), MEM_KS
assert TOPO_N % TOPO_B == 0 and MEM_N % MEM_B == 0, "the table size is a multiple of the number of batches"


def cells() -> list:
    out = []
    if "topology" in PARTS:
        for topo in TOPOLOGIES:
            for sigma in sorted(TOPO_SIGMAS):
                out.append(dict(part="topology", topo=topo, k=4, sigma=sigma, ftype=TOPO_RATE, n=TOPO_N, batches=TOPO_B))
    if "memory" in PARTS:
        for k in sorted(MEM_KS):
            out.append(dict(part="memory", topo="chain", k=k, sigma=MEM_SIGMA, ftype=MEM_RATE, n=MEM_N, batches=MEM_B))
    return out


def cell_name(c: dict) -> str:
    return f"{c['part']}_{c['topo']}_k{c['k']}_s{c['sigma']}_f{c['ftype']}_n{c['n']}_b{c['batches']}"


def wall_s() -> int:
    return int(os.environ.get("EIMER_REPRO_WALL", 1800))


def strategy_total_s() -> float:
    return float(os.environ.get("EIMER_REPRO_MEM_TOTAL", 3600))


def with_driver(toks: list, vs: list) -> list:
    """the views to execute: the cover plus the last variable's own view if no view holds it (the query is driven by it)."""
    return list(toks) + ([vs[-1]] if not any(vs[-1] in make_view(t.split("_")) for t in toks) else [])


def gate(tuples, truth):
    if tuples is None or truth is None:
        return None
    return dict(n=len(tuples), n_reference=len(truth), missing=len(truth - tuples), extra=len(tuples - truth), identical=(tuples == truth))


def setup(c: dict, client):
    n, B = c["n"], c["batches"]
    spec, dep, _ = common.load_spec_and_dep(c["k"], c["topo"])
    pos = dep.positions
    vs = sorted(dep.variables, key=lambda v: pos[v])
    qb = frozenset(tl.query_schedule(B))
    table = tl.ensure_events(client, f"events_tm_n{n}_s{str(c['sigma']).replace('.', '')}_f{str(c['ftype']).replace('.', '')}", n,
                             lambda: generate_events(common.make_datagen_config(n, c["sigma"], ftype=c["ftype"], ntypes=8, seed=42)).frame)
    t0 = time.time()
    wl, payload = tl.estimated_workload(client, dep, table, n, B, BACKEND.window_rule)
    pick = tl.toks_of(select_clique_grow_plan(dep, wl, query_batches=qb, kappa=BACKEND.kappa).compose, pos)
    selection = dict(executed="this run's selection", algorithm="clique-grow", kappa=BACKEND.kappa, pick=pick, selection_s=round(time.time() - t0, 1),
                     estimator="b_unified on the loaded table, each stated predicate on its own", estimates_this_run=payload)
    return spec, dep, vs, qb, table, wl, pick, selection


# ---------------------------------------------------------------- part "topology"
def run_topology(c: dict, client) -> dict:
    n, B, T = c["n"], c["batches"], wall_s()
    spec, dep, vs, qb, table, wl, pick, selection = setup(c, client)
    key_cols = [f"{v}_id" for v in vs]
    toks = with_driver(pick, vs)
    sql = tl.cover_sql(dep, toks, set(dep.variables), wl)
    print(f"   pick {pick} | {table}", flush=True)
    mr, m_t = tl.mr_streaming(client, spec, table, n, B, sorted(qb), key_cols, T, bounded=False)
    print(f"   mr    : {mr['status']} total={mr.get('total_ms')} ms matches={mr.get('final_count')} {mr.get('censored') or ''}", flush=True)
    eimer, e_t = tl.eimer_streaming(client, dep, toks, set(dep.variables), wl, spec, table, n, B, qb, key_cols, T, measure_storage=True)
    g = gate(e_t, m_t)
    print(f"   eimer : {eimer['status']} total={eimer.get('total_ms')} ms matches={eimer.get('final_count')} state={eimer.get('state')} | "
          f"mr == eimer {g and g['identical']}", flush=True)
    return dict(table=table, key_cols=key_cols, limits=dict(statement_s=T, mr_total_s=tl.MR_CELL_BOUND_S), selection=selection, pick=pick,
                eimer_sql=dict(updates=sql["updates"], compose=sql["compose"], compose_nested=(") tn" in sql["compose"])),
                mr_form=dict(gap_bounded_by_window=False, note="the query has no time window"), mr=mr, eimer=eimer,
                gates=dict(results_identical=(g["identical"] if g else None), n_compared=(1 if g else 0), gate=g))


# ---------------------------------------------------------------- part "memory"
def strategy_views(name: str, vs: list, pick: list) -> list:
    """the views a strategy maintains, as view names in pattern order."""
    if name == "singletons":
        return list(vs)
    if name == "pick":
        return with_driver(pick, vs)
    if name == "full_pattern":
        return ["_".join(vs)]
    return ["_".join(vs[:i]) for i in range(2, len(vs) + 1)]


def run_memory(c: dict, client) -> dict:
    n, B, T = c["n"], c["batches"], wall_s()
    spec, dep, vs, qb, table, wl, pick, selection = setup(c, client)
    key_cols = [f"{v}_id" for v in vs]
    print(f"   pick {pick} | {table}", flush=True)
    strategies, truth = [], None
    for name in STRATEGIES:
        toks = strategy_views(name, vs, pick)
        rec, tuples = tl.eimer_streaming(client, dep, toks, set(dep.variables), wl, spec, table, n, B, qb, key_cols, T, measure_storage=True,
                                         total_wall_s=strategy_total_s())
        if truth is None and tuples is not None:
            truth = tuples
        st = rec.get("state") or {}
        strategies.append(dict(strategy=name, views=toks, status=rec["status"], total_ms=rec.get("total_ms"), spent_ms=rec.get("spent_ms"),
                               final_count=rec.get("final_count"), state=st, peak_query_memory_bytes=rec.get("peak_query_memory_bytes"),
                               gate=(gate(tuples, truth) if tuples is not truth else None), notes=rec.get("notes")))
        print(f"   {name:<13}: {rec['status']} total={rec.get('total_ms')} ms state={st.get('bytes')} B ({st.get('rows')} rows) "
              f"peak query memory={rec.get('peak_query_memory_bytes')} B matches={rec.get('final_count')} | "
              f"== singletons {strategies[-1]['gate'] and strategies[-1]['gate']['identical']}", flush=True)
    g = [s["gate"]["identical"] for s in strategies if s["gate"]]
    return dict(table=table, key_cols=key_cols, limits=dict(statement_s=T, strategy_total_s=strategy_total_s()), selection=selection, pick=pick,
                strategies=strategies,
                gates=dict(results_identical=(all(g) if g else None), n_compared=len(g), reference="the all-singleton strategy",
                           singletons_finished=(strategies[0]["status"] == "ok")))


def run_cell(c: dict, out_root: Path) -> dict:
    name = cell_name(c)
    (out_root / "records").mkdir(parents=True, exist_ok=True)
    client = tl.connect(timeout_s=wall_s())
    print(f"[{name}] {c}", flush=True)
    body = run_topology(c, client) if c["part"] == "topology" else run_memory(c, client)
    rec = dict(cell=name, figure="Section 7.4, query topology and memory footprint (Trino)", params=dict(c), **tl.engine_info(client),
               protocol=dict(warmup=False, ingestion="untimed", query_schedule="after every batch",
                             server_warmed_by_untimed_first_workload=(os.environ.get("EIMER_REPRO_SERVER_WARMUP") == "1")),
               **body)
    print(f"   gates : {rec['gates']}", flush=True)
    rec["_recorded"] = tl.write_record(out_root, name, rec)
    return rec


def main():
    tl.run_main(__doc__, cells, cell_name, run_cell, HERE / "results_topology_memory")


if __name__ == "__main__":
    main()
