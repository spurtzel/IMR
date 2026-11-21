#!/usr/bin/env python3
"""Fig. 8a on Trino: native MATCH_RECOGNIZE, GDYN and EIMER on the recurring windowed chain query.

Workload (the paper's cross-engine comparison): chain A Z* B Z* ... with the lon/lat band between consecutive variables
and the window last.ts <= first.ts + 1 s; eight event types at rate 0.05, band selectivity 0.05, 1,000 events per
second, seed 42; the query runs after every batch. Two axes:
  N   table size at 200 events per batch (B = N / 200), k = 4      paper: 2k, 5k, 10k, 25k, 50k, 100k, 250k
  k   pattern length at N = 5,000, B = 25                          paper: 3 .. 8

Approaches (each on the same Iceberg table, single-threaded, join reordering off):
  mr      native MATCH_RECOGNIZE over the accumulated table, the window also stated on the gap variable
  eimer   clique-grow's pick: per-batch view updates + the composed query (ingestion untimed)
  gdyn    reduced sets per variable, maintained per batch, result enumerated per query (gdyn.py; ingestion untimed)

Checks: mr == eimer and gdyn == eimer on full tuples; the number of matches equals the paper's where recorded.
An approach that did not finish at a smaller value of an axis is not run at larger values (reported as censored).

Axis values and limits (environment):
  EIMER_REPRO_TRINO_NS=2000,5000        EIMER_REPRO_TRINO_KS=3,4,5
  EIMER_REPRO_WALL=<s>                  limit per statement (default 1,800 s) and for GDYN's total (1,800 s; 3,600 s from N 100k)
  EIMER_REPRO_MR_CELL_BOUND=<s>         limit for the total of the native-MR approach (default 7,200 s)
  EIMER_REPRO_ARMS=mr,eimer,gdyn        the approaches to run

  python3 fig8.py --list                         the workloads, in execution order
  python3 fig8.py --only k4_n2000_b10_w1000 --results-dir <dir>
"""
from __future__ import annotations

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

CENTER = dict(k=4, n=5_000, batches=25, w_ms=1000, sigma=0.05, ftype=0.05, ntypes=8, seed=42)
PAPER_NS = (2_000, 5_000, 10_000, 25_000, 50_000, 100_000, 250_000)
PAPER_KS = (3, 4, 5, 6, 7, 8)
SPECS = Path(os.environ.get("EIMER_REPRO_SPECS_DIR", HERE / "specs"))
REFERENCE = json.loads((tl.REPRO / "reference" / "paper_trino.json").read_text())["fig8"]
ARMS = tuple(a for a in os.environ.get("EIMER_REPRO_ARMS", "mr,eimer,gdyn").split(",") if a)
BACKEND = backend_for(SqlDialect.TRINO)


NS = tl.env_list("EIMER_REPRO_TRINO_NS", PAPER_NS)
KS = tl.env_list("EIMER_REPRO_TRINO_KS", PAPER_KS)
assert all(n % 200 == 0 and n >= 200 for n in NS), f"N axis: multiples of 200 expected, got {NS}"
assert all(3 <= k <= 8 for k in KS), f"k axis: 3..8, got {KS}"


def cell_name(c: dict) -> str:
    return f"k{c['k']}_n{c['n']}_b{c['batches']}_w{c['w_ms']}"


def cells() -> list:
    """the workloads in execution order."""
    out: dict = {}
    for n in sorted(NS):
        c = {**CENTER, "n": n, "batches": n // 200}
        out.setdefault(cell_name(c), {**c, "axes": []})["axes"].append("N")
    for k in sorted(KS):
        c = {**CENTER, "k": k}
        out.setdefault(cell_name(c), {**c, "axes": []})["axes"].append("k")
    return list(out.values())


def statement_limit(c: dict) -> int:
    return int(os.environ.get("EIMER_REPRO_WALL", 1800))


def gdyn_limit(c: dict) -> int:
    return int(os.environ.get("EIMER_REPRO_WALL", 3600 if c["n"] >= 100_000 else 1800))


def build_spec(k: int, w_ms: int) -> Path:
    """the chain query of length k."""
    assert w_ms % 1000 == 0
    spec = json.loads(Path(common.patch_spec_for_datagen(k, "chain")).read_text())
    vs = spec["pattern"]["variables"]
    first, last = vs[0], vs[-1]
    spec["dependent_predicates"].append({
        "predicate_id": f"{first}_{last}_window", "kind": "dependent", "variables": [first, last],
        "sql_template": f"{last}.ts BETWEEN {first}.ts AND {first}.ts + INTERVAL '{w_ms // 1000}' SECOND",
        "referenced_columns": ["ts"], "selectivity_key": f"{first}|{last}"})
    out = SPECS / f"fig8_chain_k{k}_w{w_ms}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(spec, indent=2) + "\n")
    os.replace(tmp, out)
    return out


def censored_by_smaller(c: dict, arm: str, records: Path):
    """the smallest axis value at which `arm` did not finish in an existing record of the same axis, if below this cell."""
    hits = []
    for f in records.glob("*.json"):
        r = json.loads(f.read_text())
        p = r.get("params", {})
        if (r.get(arm) or {}).get("status") != "dnf":
            continue
        for axis in c["axes"]:
            if axis not in p.get("axes", []):
                continue
            key = "n" if axis == "N" else "k"
            if p[key] < c[key]:
                hits.append((axis, p[key]))
    return min(hits, key=lambda h: h[1]) if hits else None


def run_cell(c: dict, out_root: Path) -> dict:
    name = cell_name(c)
    rec_path = out_root / "records" / f"{name}.json"
    rec_path.parent.mkdir(parents=True, exist_ok=True)
    n, B, k = c["n"], c["batches"], c["k"]
    T = statement_limit(c)
    client = tl.connect(timeout_s=max(T, gdyn_limit(c)))
    spec_path = build_spec(k, c["w_ms"])
    spec = load_query_spec_file(str(spec_path))
    dep = query_spec_to_dependency_graph(spec)
    pos = dep.positions
    vs = sorted(dep.variables, key=lambda v: pos[v])
    all_vars = set(dep.variables)
    key_cols = [f"{v}_id" for v in vs]
    qb = tl.query_schedule(B)

    table = tl.ensure_events(client, f"events_fig8_n{n}", n, lambda: generate_events(
        common.make_datagen_config(n, c["sigma"], ftype=c["ftype"], ntypes=c["ntypes"], seed=c["seed"])).frame)

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
    selection.update(algorithm="clique-grow", kappa=BACKEND.kappa, estimator="b_unified on the loaded table, each stated predicate on its own", selection_s=sel_s,
                     estimates_this_run=payload_live)
    ptoks = toks + ([vs[-1]] if not any(vs[-1] in make_view(t.split("_")) for t in toks) else [])
    sql = tl.cover_sql(dep, ptoks, all_vars, wl)

    rec = dict(cell=name, figure="8a (Trino)", params=dict(c), **tl.engine_info(client), table=table, key_cols=key_cols,
               limits=dict(statement_s=T, gdyn_total_s=gdyn_limit(c), mr_total_s=tl.MR_CELL_BOUND_S),
               selection=selection, pick=toks,
               mr_form=dict(gap_bounded_by_window=True, gap_define=tl.gap_define_line(spec)),
               eimer_sql=dict(updates=sql["updates"], compose=sql["compose"], compose_nested=("JOIN (SELECT" in sql["compose"] or ") tn" in sql["compose"])),
               protocol=dict(warmup=False, ingestion="untimed (EIMER, GDYN)", query_schedule="after every batch",
                             server_warmed_by_untimed_first_workload=(os.environ.get("EIMER_REPRO_SERVER_WARMUP") == "1")),
               paper=(ref or {}).get("paper"), paper_matches=(ref or {}).get("matches"))
    print(f"[{name}] N={n} B={B} k={k} W={c['w_ms']} ms | pick {toks} ({selection['executed']}; this run selects {pick_live}) | "
          f"{rec['engine']} {rec['catalog']}.{rec['schema']} | limits {rec['limits']}", flush=True)

    e_tuples = None
    if "eimer" in ARMS:
        rec["eimer"], e_tuples = tl.eimer_streaming(client, dep, ptoks, all_vars, wl, spec, table, n, B, qb, key_cols, T)
        print(f"   eimer : {rec['eimer']['status']} total={rec['eimer'].get('total_ms')} ms (updates {rec['eimer'].get('update_ms')} / queries {rec['eimer'].get('compose_ms')}) "
              f"matches={rec['eimer'].get('final_count')}", flush=True)

    def gate(tuples):
        if tuples is None or e_tuples is None:
            return None
        return dict(n=len(tuples), n_eimer=len(e_tuples), missing=len(e_tuples - tuples), extra=len(tuples - e_tuples), identical=(tuples == e_tuples))

    if "mr" in ARMS:
        cen = censored_by_smaller(c, "mr", rec_path.parent)
        if cen is not None:
            rec["mr"] = dict(status="dnf", censored="smaller_value_did_not_finish", notes=f"not run: native MR did not finish at {cen[0]} = {cen[1]}")
            m_tuples = None
        else:
            t0 = time.time()
            rec["mr"], m_tuples = tl.mr_streaming(client, spec, table, n, B, qb, key_cols, T)
            rec["mr"]["wall_clock_s"] = round(time.time() - t0, 1)
        rec["mr"]["gate"] = gate(m_tuples)
        print(f"   mr    : {rec['mr']['status']} total={rec['mr'].get('total_ms')} ms matches={rec['mr'].get('final_count')} "
              f"{rec['mr'].get('censored') or ''} | mr == eimer {rec['mr']['gate'] and rec['mr']['gate']['identical']}", flush=True)

    if "gdyn" in ARMS:
        cen = censored_by_smaller(c, "gdyn", rec_path.parent)
        if cen is not None:
            rec["gdyn"] = dict(status="dnf", censored="smaller_value_did_not_finish", notes=f"not run: GDYN did not finish at {cen[0]} = {cen[1]}")
            g_keys = None
        else:
            q = gdyn.ChainQuery(k, c["w_ms"], (common.HALF_WIDTH, common.HALF_WIDTH))
            rec["gdyn"], g_keys = gdyn.GdynTrino(client, q, table, n, B, gdyn_limit(c), key_cols).run()
        rec["gdyn"]["gate"] = gate(g_keys)
        print(f"   gdyn  : {rec['gdyn']['status']} total={rec['gdyn'].get('total_ms')} ms (updates {rec['gdyn'].get('update_ms')} / queries {rec['gdyn'].get('query_ms')}) "
              f"matches={rec['gdyn'].get('final_count')} {rec['gdyn'].get('censored') or ''} | gdyn == eimer {rec['gdyn']['gate'] and rec['gdyn']['gate']['identical']}", flush=True)

    g = [rec[a]["gate"]["identical"] for a in ("mr", "gdyn") if a in rec and rec[a].get("gate")]
    cnt = (rec.get("eimer") or {}).get("final_count")
    rec["gates"] = dict(results_identical=(all(g) if g else None), n_compared=len(g),
                        matches_as_in_paper=((cnt == ref["matches"]) if (ref is not None and cnt is not None) else None))
    print(f"   gates : {rec['gates']}", flush=True)
    rec["_recorded"] = tl.write_record(out_root, name, rec)
    return rec


def main():
    tl.run_main(__doc__, cells, cell_name, run_cell, HERE / "results_fig8")


if __name__ == "__main__":
    main()
