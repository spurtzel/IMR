#!/usr/bin/env python3
"""Fig. 10 on Trino: EIMER vs. GDYN on acyclic and cyclic queries, under sparser query schedules, and on skewed type
frequencies.

Workload (the paper's GDYN comparison): chain A Z* B Z* ... with the lon/lat band between consecutive variables; eight
event types at rate 0.05, 1,000 events per second, seed 42. The acyclic query has no further condition; the cyclic
query adds the window last.ts <= first.ts + 1 s. Three parts:
  a   both query classes over the table size N (200 events per batch, k = 4) and the pattern length k (at the center
      table size), band selectivity 0.02, the query after every batch
      paper: N = 10k, 25k, 50k, 100k; k = 3 .. 8 at N = 25k (125 batches)
  b   the cyclic query with only a fraction of the batches queried (evenly spaced, the last batch included), band
      selectivity 0.01
      paper: N = 50k in 1,000 batches; 100, 90, ..., 10, 1 % of the batches queried
  s   both query classes at k = 4 on tables with skewed type frequencies (band selectivity 0.02, 200 events per batch,
      the query after every batch): the first and last pattern type at 1 % of the rows and all other types at 10 %
      ("rare_ends"), or the interior pattern types at 1 % and all other types at 10 % ("rare_interior"), next to the
      uniform table of the same size
      paper: N = 25k (125 batches); reported in the text, not plotted

Approaches (same Iceberg table, single-threaded, join reordering off, ingestion untimed):
  eimer   clique-grow's pick: the views are updated after every batch and composed on the queried batches
  gdyn    reduced sets per variable, maintained after every batch; the result is enumerated on the queried batches
Recorded per approach: total runtime, its split into maintenance (view updates / reduced-set updates) and query time,
and the persistent state (bytes of the Iceberg data files of the views / of the reduced sets at the end of the run).

Checks: gdyn == eimer on full tuples; the number of matches equals the paper's where recorded. GDYN is not run at
larger values of an axis once it did not finish.

Axis values and limits (environment):
  EIMER_REPRO_FIG10_CLASSES=acyclic,cyclic   EIMER_REPRO_FIG10_NS=10000,25000,50000,100000
  EIMER_REPRO_FIG10_NC=25000 (table size of the k axis)   EIMER_REPRO_FIG10_KS=3,4,5,6,7,8
  EIMER_REPRO_FIG10_SCHED_N=50000   EIMER_REPRO_FIG10_SCHED_B=1000   EIMER_REPRO_FIG10_PCTS=100,90,80,70,60,50,40,30,20,10,1
  EIMER_REPRO_FIG10_SKEW_N=25000 (table size of part s)
  EIMER_REPRO_FIG10_PARTS=a,b,s
  EIMER_REPRO_WALL=<s>     limit per statement and for GDYN's total (default 1,800 s; 3,600 s from N 100k and in part b)

  python3 fig10.py --list
  python3 fig10.py --only cyclic_k4_n25000_b125_s0.02_q100 --results-dir <dir>
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import trino_lib as tl  # noqa: E402
import gdyn  # noqa: E402
import common  # noqa: E402
from workload.data.datagen import generate_events  # noqa: E402
from eimer.query.query_spec import load_query_spec_file, query_spec_to_dependency_graph  # noqa: E402
from eimer.selection.plan_selection_clique_grow import select_clique_grow_plan  # noqa: E402
from eimer.sql.backend import SqlDialect, backend_for  # noqa: E402
from eimer.workload import make_workload  # noqa: E402
from eimer.models import make_view  # noqa: E402
from selectivity_payload import parse_selectivities_payload  # noqa: E402

BASE = dict(k=4, ftype=0.05, ntypes=8, seed=42)
W_MS = {"acyclic": 0, "cyclic": 1000}
SIGMA_A, SIGMA_B = 0.02, 0.01
BATCH_EVENTS = 200
SPECS = Path(os.environ.get("EIMER_REPRO_SPECS_DIR", HERE / "specs"))
REFERENCE = json.loads((tl.REPRO / "reference" / "paper_trino.json").read_text()).get("fig10", {})
BACKEND = backend_for(SqlDialect.TRINO)


def _pct(x: str):
    v = float(x)
    return int(v) if v == int(v) else v


CLASSES = [c for c in os.environ.get("EIMER_REPRO_FIG10_CLASSES", "acyclic,cyclic").split(",") if c]
NS = tl.env_list("EIMER_REPRO_FIG10_NS", (10_000, 25_000, 50_000, 100_000))
NC = tl.env_list("EIMER_REPRO_FIG10_NC", (25_000,))[0]
KS = tl.env_list("EIMER_REPRO_FIG10_KS", (3, 4, 5, 6, 7, 8))
SCHED_N = tl.env_list("EIMER_REPRO_FIG10_SCHED_N", (50_000,))[0]
SCHED_B = tl.env_list("EIMER_REPRO_FIG10_SCHED_B", (1000,))[0]
PCTS = tl.env_list("EIMER_REPRO_FIG10_PCTS", (100, 90, 80, 70, 60, 50, 40, 30, 20, 10, 1), _pct)
SKEW_N = tl.env_list("EIMER_REPRO_FIG10_SKEW_N", (25_000,))[0]
SKEWS = ("rare_ends", "rare_interior")
RARE_RATE, OTHER_RATE = 0.01, 0.10
PARTS = [p for p in os.environ.get("EIMER_REPRO_FIG10_PARTS", "a,b,s").split(",") if p]
assert set(CLASSES) <= set(W_MS), CLASSES
assert all(n % BATCH_EVENTS == 0 for n in NS + [NC, SKEW_N]), f"parts a and s: table sizes are multiples of {BATCH_EVENTS}"
assert all(3 <= k <= 8 for k in KS), KS
assert SCHED_N % SCHED_B == 0, "part b: the table size is a multiple of the number of batches"


def cell_name(c: dict) -> str:
    skew = c.get("skew", "uniform")
    return f"{c['cls']}_k{c['k']}_n{c['n']}_b{c['batches']}_s{c['sigma']}_q{c['pct']}" + ("" if skew == "uniform" else f"_{skew}")


def type_rates(c: dict):
    """part s: the rate of each event type (the pattern's variable i has type T<i>), None for the uniform table."""
    skew = c.get("skew", "uniform")
    if skew == "uniform":
        return None
    rare = {0, c["k"] - 1} if skew == "rare_ends" else set(range(1, c["k"] - 1))
    return tuple(RARE_RATE if i in rare else OTHER_RATE for i in range(c["ntypes"]))


def events_frame(c: dict):
    cfg = common.make_datagen_config(c["n"], c["sigma"], ftype=c["ftype"], ntypes=c["ntypes"], seed=c["seed"])
    rates = type_rates(c)
    if rates is not None:
        cfg = dataclasses.replace(cfg, types=tuple((name, rate) for (name, _), rate in zip(cfg.types, rates)))
    return generate_events(cfg).frame


def cells() -> list:
    """the workloads in execution order."""
    out: dict = {}
    if "a" in PARTS:
        for cls in CLASSES:
            for n in sorted(NS):
                c = dict(BASE, cls=cls, n=n, batches=n // BATCH_EVENTS, w_ms=W_MS[cls], sigma=SIGMA_A, pct=100, part="a")
                out.setdefault(cell_name(c), {**c, "axes": []})["axes"].append("N")
            for k in sorted(KS):
                c = dict(BASE, cls=cls, k=k, n=NC, batches=NC // BATCH_EVENTS, w_ms=W_MS[cls], sigma=SIGMA_A, pct=100, part="a")
                out.setdefault(cell_name(c), {**c, "axes": []})["axes"].append("k")
    if "b" in PARTS:
        for p in sorted(PCTS, reverse=True):
            c = dict(BASE, cls="cyclic", n=SCHED_N, batches=SCHED_B, w_ms=W_MS["cyclic"], sigma=SIGMA_B, pct=p, part="b")
            out.setdefault(cell_name(c), {**c, "axes": []})["axes"].append("Q")
    if "s" in PARTS:
        for cls in CLASSES:
            for skew in ("uniform",) + SKEWS:
                c = dict(BASE, cls=cls, n=SKEW_N, batches=SKEW_N // BATCH_EVENTS, w_ms=W_MS[cls], sigma=SIGMA_A, pct=100, part="s", skew=skew)
                out.setdefault(cell_name(c), {**c, "axes": []})["axes"].append("skew")
    return list(out.values())


def limit_s(c: dict) -> int:
    return int(os.environ.get("EIMER_REPRO_WALL", 3600 if (c["n"] >= 100_000 or c["part"] == "b") else 1800))


def chain_spec(k: int, w_ms: int) -> dict:
    """the chain query of length k."""
    hw = common.HALF_WIDTH
    spec = json.loads((common.SPEC_DIR / f"q{k}_chain_rare_spatial.json").read_text())
    vs = list("ABCDEFGH"[:k])
    assert list(spec["pattern"]["variables"]) == vs, spec["pattern"]["variables"]
    spec["independent_predicates"] = [{"predicate_id": f"{v}_type", "kind": "independent", "variables": [v],
                                       "sql_template": f"{v}.primary_type = 'T{i}'", "referenced_columns": ["primary_type"],
                                       "selectivity_key": v} for i, v in enumerate(vs)]
    deps = [{"predicate_id": f"{x}_{y}_band", "kind": "dependent", "variables": [x, y],
             "sql_template": f"{y}.lon BETWEEN {x}.lon - {hw} AND {x}.lon + {hw} AND {y}.lat BETWEEN {x}.lat - {hw} AND {x}.lat + {hw}",
             "referenced_columns": ["lon", "lat"], "selectivity_key": f"{x}|{y}"} for x, y in zip(vs, vs[1:])]
    if w_ms:
        assert w_ms % 1000 == 0
        f, l = vs[0], vs[-1]
        deps.append({"predicate_id": f"{f}_{l}_window", "kind": "dependent", "variables": [f, l],
                     "sql_template": f"{l}.ts BETWEEN {f}.ts AND {f}.ts+INTERVAL '{w_ms // 1000}' SECOND",
                     "referenced_columns": ["ts"], "selectivity_key": f"{f}|{l}"})
    spec["dependent_predicates"] = deps
    spec["name"] = f"fig10_chain_k{k}_w{w_ms}"
    return spec


def build_spec(k: int, w_ms: int) -> Path:
    out = SPECS / f"fig10_chain_k{k}_w{w_ms}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(chain_spec(k, w_ms), indent=2) + "\n")
    os.replace(tmp, out)
    return out


def gdyn_censored_by_smaller(c: dict, records: Path):
    hits = []
    for f in records.glob("*.json"):
        r = json.loads(f.read_text())
        p = r.get("params", {})
        if (r.get("gdyn") or {}).get("status") != "dnf" or p.get("cls") != c["cls"] or p.get("part") != c["part"]:
            continue
        for axis in c["axes"]:
            key = {"N": "n", "k": "k"}.get(axis)
            if key and axis in p.get("axes", []) and p[key] < c[key]:
                hits.append((axis, p[key]))
    return min(hits, key=lambda h: h[1]) if hits else None


def run_cell(c: dict, out_root: Path) -> dict:
    name = cell_name(c)
    rec_path = out_root / "records" / f"{name}.json"
    rec_path.parent.mkdir(parents=True, exist_ok=True)
    n, B, k, sigma = c["n"], c["batches"], c["k"], c["sigma"]
    T = limit_s(c)
    client = tl.connect(timeout_s=T)
    spec = load_query_spec_file(str(build_spec(k, c["w_ms"])))
    dep = query_spec_to_dependency_graph(spec)
    pos = dep.positions
    vs = sorted(dep.variables, key=lambda v: pos[v])
    all_vars = set(dep.variables)
    key_cols = [f"{v}_id" for v in vs]
    qb = tl.query_schedule(B, c["pct"])
    skew = c.get("skew", "uniform")
    table = tl.ensure_events(client, f"events_fig10_n{n}_s{str(sigma).replace('.', '')}" + ("" if skew == "uniform" else f"_{skew}"), n,
                             lambda: events_frame(c))

    t0 = time.time()
    wl_live, payload_live = tl.estimated_workload(client, dep, table, n, B, BACKEND.window_rule)
    pick_live = tl.toks_of(select_clique_grow_plan(dep, wl_live, query_batches=frozenset(qb), kappa=BACKEND.kappa).compose, pos)
    sel_s = round(time.time() - t0, 1)
    ref = REFERENCE.get(name)
    if ref is not None:
        toks = list(ref["pick"])
        wl = make_workload(dep, total_events=n, batches=B, selectivities=parse_selectivities_payload(ref["selectivities"], dep),
                           window_rule=BACKEND.window_rule)
        pick_paper_estimates = tl.toks_of(select_clique_grow_plan(dep, wl, query_batches=frozenset(qb), kappa=BACKEND.kappa).compose, pos)
        selection = dict(executed="the paper's pick with the paper's estimates", pick=toks, pick_this_run=pick_live,
                         this_run_selects_paper_pick=(pick_live == toks),
                         selector_on_paper_estimates=pick_paper_estimates, selector_reproduces_paper_pick=(pick_paper_estimates == toks))
    else:
        toks, wl = pick_live, wl_live
        selection = dict(executed="this run's selection", pick=toks, pick_this_run=pick_live)
    selection.update(algorithm="clique-grow", kappa=BACKEND.kappa, estimator="b_unified on the loaded table, each stated predicate on its own",
                     selection_s=sel_s, estimates_this_run=payload_live)
    ptoks = toks + ([vs[-1]] if not any(vs[-1] in make_view(t.split("_")) for t in toks) else [])
    sql = tl.cover_sql(dep, ptoks, all_vars, wl)

    rec = dict(cell=name, figure=f"10{c['part']} (Trino)", params=dict(c), type_rates=type_rates(c), **tl.engine_info(client), table=table, key_cols=key_cols,
               limits=dict(statement_s=T, gdyn_total_s=T), n_queries=len(qb), selection=selection, pick=toks,
               eimer_sql=dict(updates=sql["updates"], compose=sql["compose"], compose_nested=(") tn" in sql["compose"])),
               protocol=dict(warmup=False, ingestion="untimed (EIMER, GDYN)",
                             query_schedule=("after every batch" if c["pct"] == 100 else f"{c['pct']} % of the batches, evenly spaced, the last included"),
                             server_warmed_by_untimed_first_workload=(os.environ.get("EIMER_REPRO_SERVER_WARMUP") == "1")),
               paper_matches=(ref or {}).get("matches"))
    print(f"[{name}] {c['cls']} N={n} B={B} k={k} sigma={sigma} types={skew} queries={len(qb)}/{B} | pick {toks} ({selection['executed']}; this run selects {pick_live}) | "
          f"{rec['engine']} {rec['catalog']}.{rec['schema']} | limit {T} s", flush=True)

    rec["eimer"], e_tuples = tl.eimer_streaming(client, dep, ptoks, all_vars, wl, spec, table, n, B, frozenset(qb), key_cols, T, measure_storage=True)
    e = rec["eimer"]
    print(f"   eimer : {e['status']} total={e.get('total_ms')} ms (maintenance {e.get('update_ms')} / queries {e.get('compose_ms')}) matches={e.get('final_count')} "
          f"state={e.get('state')}", flush=True)

    cen = gdyn_censored_by_smaller(c, rec_path.parent)
    if cen is not None:
        rec["gdyn"] = dict(status="dnf", censored="smaller_value_did_not_finish", notes=f"not run: GDYN did not finish at {cen[0]} = {cen[1]}")
        g_keys = None
    else:
        q = gdyn.ChainQuery(k, c["w_ms"], (common.HALF_WIDTH, common.HALF_WIDTH))
        rec["gdyn"], g_keys = gdyn.GdynTrino(client, q, table, n, B, T, key_cols, query_batches=qb, state_fn=tl.storage_state).run()
    g = rec["gdyn"]
    gate = None
    if g_keys is not None and e_tuples is not None:
        gate = dict(n=len(g_keys), n_eimer=len(e_tuples), missing=len(e_tuples - g_keys), extra=len(g_keys - e_tuples), identical=(g_keys == e_tuples))
    g["gate"] = gate
    print(f"   gdyn  : {g['status']} total={g.get('total_ms')} ms (maintenance {g.get('update_ms')} / queries {g.get('query_ms')}) matches={g.get('final_count')} "
          f"state={g.get('state')} {g.get('censored') or ''} | gdyn == eimer {gate and gate['identical']}", flush=True)
    cnt = e.get("final_count")
    rec["gates"] = dict(results_identical=(gate["identical"] if gate else None), n_compared=(1 if gate else 0),
                        matches_as_in_paper=((cnt == ref["matches"]) if (ref is not None and cnt is not None) else None))
    print(f"   gates : {rec['gates']}", flush=True)
    rec["_recorded"] = tl.write_record(out_root, name, rec)
    return rec


def main():
    tl.run_main(__doc__, cells, cell_name, run_cell, HERE / "results_fig10")


if __name__ == "__main__":
    main()
