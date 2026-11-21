#!/usr/bin/env python3
"""Fig. 9 on Trino: EIMER vs. prefiltered and native MATCH_RECOGNIZE on the recurring windowed chain query.

Workload: chain V0 Z* V1 Z* V2 Z* V3 (k = 4) with the lon +-0.05 / lat +-0.02 band between consecutive variables and
the window V3.ts <= V0.ts + W; eight event types, the first and the last pattern type at rate 0.01 each (both can be
set, see below) and all others at 0.10; band selectivity sigma; 1,000 events per second, seed 42; the query runs
after every batch. Two panels:
  W   window size at N = 10,000, B = 10, for sigma 0.05 and 0.02          W = 200, 500, 1,000, 2,000 ms
  B   number of batches at W = 1,000 ms, sigma 0.05, for two table sizes  B = 1 .. 25 at N = 25k and 50k
      (EIMER_REPRO_FIG9_BW sets another window for this panel)

Approaches (same Iceberg table, single-threaded, join reordering off):
  mr         native MATCH_RECOGNIZE over the accumulated table, the window also stated on the gap variable
  prefilter  the join-based prefilter (prefilter.py) in front of the same MATCH_RECOGNIZE, re-run per query; all
             subsequences that contain the first and last variable are executed, the fastest is reported
  eimer      clique-grow's pick: per-batch view updates + the composed query; EIMER is charged for writing each
             batch into its staging tables

Checks: prefilter == mr and eimer == mr on full tuples (against eimer where native MR does not finish); the number of
matches equals the paper's where recorded. A baseline that did not finish at a smaller B of the same table is not
run at larger B.

Axis values and limits (environment):
  EIMER_REPRO_FIG9_NW=10000   EIMER_REPRO_FIG9_SIGMAS=0.05,0.02   EIMER_REPRO_FIG9_WS=200,500,1000,2000
  EIMER_REPRO_FIG9_NBS=25000,50000   EIMER_REPRO_FIG9_BS=1,2,5,10,25   EIMER_REPRO_FIG9_BW=1000
  EIMER_REPRO_FIG9_FIRST_RATE=0.01   EIMER_REPRO_FIG9_LAST_RATE=0.01   rate of the first / the last pattern type
  EIMER_REPRO_FIG9_WB=10             number of batches of the W panel
  EIMER_REPRO_WALL=<s>               limit per statement (default 1,800 s)
  EIMER_REPRO_MR_CELL_BOUND=<s>      limit for the total of native MR and of each prefilter subsequence (default 7,200 s)
  EIMER_REPRO_ARMS=mr,prefilter,eimer
  EIMER_REPRO_FIG9_PANELS=W,B        run only these panels (W: window axis, B: batch axis)

  python3 fig9.py --list
  python3 fig9.py --only k4_n10000_b10_w200_s0.05 --results-dir <dir>
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
import prefilter as pf  # noqa: E402
import common  # noqa: E402
from workload.data.datagen import BandTarget, DatagenConfig, SpatialConfig, TemporalConfig, generate_events  # noqa: E402
from eimer.query.query_spec import load_query_spec_file, query_spec_to_dependency_graph  # noqa: E402
from eimer.selection.plan_selection_clique_grow import select_clique_grow_plan  # noqa: E402
from eimer.sql.backend import SqlDialect, backend_for  # noqa: E402
from eimer.workload import make_workload  # noqa: E402
from eimer.models import make_view  # noqa: E402
from selectivity_payload import parse_selectivities_payload  # noqa: E402

K = 4
HW_LON, HW_LAT = 0.05, 0.02
RARE, COMMON = 0.01, 0.10
FIRST_RATE = tl.env_list("EIMER_REPRO_FIG9_FIRST_RATE", (RARE,), float)[0]
LAST_RATE = tl.env_list("EIMER_REPRO_FIG9_LAST_RATE", (RARE,), float)[0]
RATES_TAG = "" if (FIRST_RATE, LAST_RATE) == (RARE, RARE) else f"_f{FIRST_RATE:g}_l{LAST_RATE:g}"
NTYPES, SEED = 8, 42
SPECS = Path(os.environ.get("EIMER_REPRO_SPECS_DIR", HERE / "specs"))
REFERENCE = json.loads((tl.REPRO / "reference" / "paper_trino.json").read_text())["fig9"]
ARMS = tuple(a for a in os.environ.get("EIMER_REPRO_ARMS", "mr,prefilter,eimer").split(",") if a)
BACKEND = backend_for(SqlDialect.TRINO)


NW = tl.env_list("EIMER_REPRO_FIG9_NW", (10_000,))[0]
SIGMAS = tl.env_list("EIMER_REPRO_FIG9_SIGMAS", (0.05, 0.02), float)
WS = tl.env_list("EIMER_REPRO_FIG9_WS", (200, 500, 1000, 2000))
NBS = tl.env_list("EIMER_REPRO_FIG9_NBS", (25_000, 50_000))
BS = tl.env_list("EIMER_REPRO_FIG9_BS", (1, 2, 5, 10, 25))
B_PANEL = dict(w_ms=tl.env_list("EIMER_REPRO_FIG9_BW", (1000,))[0], sigma=0.05)
W_PANEL = dict(batches=tl.env_list("EIMER_REPRO_FIG9_WB", (10,))[0])
PANELS = [p for p in os.environ.get("EIMER_REPRO_FIG9_PANELS", "W,B").replace(" ", "").split(",") if p]


def cell_name(c: dict) -> str:
    return f"k{c['k']}_n{c['n']}_b{c['batches']}_w{c['w_ms']}_s{c['sigma']}" + RATES_TAG


def check_table(n: int, sigma: float):
    """checks the table size."""
    rarest = min(FIRST_RATE, LAST_RATE, COMMON)
    expected = n * n * rarest * rarest * sigma
    if expected < 100:
        raise SystemExit(f"Fig. 9: N = {n:,} is too small for sigma = {sigma} at type rate {rarest:g}: {expected:.0f} expected band pairs of the "
                         f"rarest type (100 needed); use N >= {int((100 / (rarest * rarest * sigma)) ** 0.5) + 1:,} or a larger sigma")


def cells() -> list:
    """the workloads in execution order."""
    out: dict = {}
    for s in (SIGMAS if "W" in PANELS else []):
        check_table(NW, s)
    for n in (NBS if "B" in PANELS else []):
        check_table(n, B_PANEL["sigma"])
    for s in (SIGMAS if "W" in PANELS else []):
        for w in sorted(WS):
            c = dict(k=K, n=NW, batches=W_PANEL["batches"], w_ms=w, sigma=s)
            out.setdefault(cell_name(c), {**c, "axes": []})["axes"].append("W")
    for n in (sorted(NBS) if "B" in PANELS else []):
        for b in sorted(BS):
            c = dict(k=K, n=n, batches=b, **B_PANEL)
            out.setdefault(cell_name(c), {**c, "axes": []})["axes"].append("B")
    return list(out.values())


def statement_limit(c: dict) -> int:
    return int(os.environ.get("EIMER_REPRO_WALL", 1800))


def type_rates(k: int = K) -> tuple:
    return tuple((f"T{i}", (FIRST_RATE if i == 0 else LAST_RATE if i == k - 1 else COMMON)) for i in range(NTYPES))


def make_frame(n: int, sigma: float):
    cfg = DatagenConfig(types=type_rates(), batch_sizes=(n,), seed=SEED, spatial=SpatialConfig(), temporal=TemporalConfig(rho=1000.0),
                        spatial_band=BandTarget(sigma=sigma, half_width_lon=common.HALF_WIDTH, half_width_lat=common.HALF_WIDTH), equi=None)
    return generate_events(cfg).frame


def chain_windowed_spec(k: int, w_ms: int) -> dict:
    """the windowed chain query."""
    base = json.loads((common.SPEC_DIR / f"q{k}_chain_rare_spatial.json").read_text())
    names = [f"V{i}" for i in range(k)]
    spec = {key: base[key] for key in ("event_schema", "id_column", "time_column", "ts_column", "order_by", "row_output",
                                       "after_match", "partition_by", "window", "kleene_annotations") if key in base}
    spec["name"] = f"chain_k{k}_W{w_ms}"
    spec["suite_metadata"] = {"pattern_length": k, "shape": "chain", "purpose": f"windowed chain k={k}, W={w_ms} ms between the first and last variable"}
    spec["variables"] = [{"name": v, "output_prefix": v.lower(), "independent_predicate_id": f"{v}_type"} for v in names]
    spec["pattern"] = {"variables": names, "wildcard": "Z", "wildcard_quantifier": "*"}
    spec["independent_predicates"] = [
        {"predicate_id": f"{v}_type", "kind": "independent", "variables": [v], "sql_template": f"{v}.primary_type = 'T{i}'",
         "referenced_columns": ["primary_type"], "selectivity_key": v} for i, v in enumerate(names)]
    dep = []
    for ref, tgt in zip(names, names[1:]):
        dep.append({"predicate_id": f"{ref}_{tgt}_spatial", "kind": "dependent", "variables": [ref, tgt],
                    "sql_template": (f"{tgt}.lon BETWEEN {ref}.lon - {HW_LON} AND {ref}.lon + {HW_LON} "
                                     f"AND {tgt}.lat BETWEEN {ref}.lat - {HW_LAT} AND {ref}.lat + {HW_LAT}"),
                    "referenced_columns": ["lon", "lat"], "selectivity_key": f"{ref}|{tgt}"})
    first, last = names[0], names[-1]
    dep.append({"predicate_id": f"{first}_{last}_window", "kind": "dependent", "variables": [first, last],
                "sql_template": f"{last}.ts BETWEEN {first}.ts AND {first}.ts+INTERVAL '{w_ms / 1000.0}' SECOND",
                "referenced_columns": ["ts"], "selectivity_key": f"{first}|{last}"})
    spec["dependent_predicates"] = dep
    measures = []
    for v in names:
        measures += [{"expression": f"{v}.id", "alias": f"{v.lower()}_id", "sql_type": "BIGINT"},
                     {"expression": f"{v}.time", "alias": f"{v.lower()}_time", "sql_type": "TIMESTAMP(6)"}]
    spec["measures"] = measures
    spec["result_key_columns"] = [m["alias"] for m in measures]
    return spec


def build_spec(k: int, w_ms: int) -> Path:
    out = SPECS / f"fig9_chain_k{k}_w{w_ms}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(chain_windowed_spec(k, w_ms), indent=2) + "\n")
    os.replace(tmp, out)
    return out


def censored_by_smaller_b(c: dict, records: Path, arm: str):
    hits = []
    for f in records.glob("*.json"):
        r = json.loads(f.read_text())
        p = r.get("params", {})
        if (r.get(arm) or {}).get("status") == "dnf" and all(p.get(x) == c[x] for x in ("k", "n", "w_ms", "sigma")) and p["batches"] < c["batches"]:
            hits.append(p["batches"])
    return min(hits) if hits else None


def run_cell(c: dict, out_root: Path) -> dict:
    name = cell_name(c)
    rec_path = out_root / "records" / f"{name}.json"
    rec_path.parent.mkdir(parents=True, exist_ok=True)
    n, B, k, W, sigma = c["n"], c["batches"], c["k"], c["w_ms"], c["sigma"]
    T = statement_limit(c)
    client = tl.connect(timeout_s=T)
    spec = load_query_spec_file(str(build_spec(k, W)))
    dep = query_spec_to_dependency_graph(spec)
    pos = dep.positions
    vs = sorted(dep.variables, key=lambda v: pos[v])
    all_vars = set(dep.variables)
    types = {v: f"T{i}" for i, v in enumerate(vs)}
    key_cols = [f"{v}_id" for v in vs]
    qb = tl.query_schedule(B)
    table = tl.ensure_events(client, f"events_fig9_n{n}_s{str(sigma).replace('.', '')}" + RATES_TAG.replace(".", ""), n, lambda: make_frame(n, sigma))

    t0 = time.time()
    wl_live, payload_live = tl.estimated_workload(client, dep, table, n, B, BACKEND.window_rule)
    pick_live = tl.toks_of(select_clique_grow_plan(dep, wl_live, query_batches=frozenset(qb), kappa=BACKEND.kappa).compose, pos)
    sel_s = round(time.time() - t0, 1)
    ref = REFERENCE["workloads"].get(name)
    if ref is not None:
        toks = list(ref["pick"])
        wl = make_workload(dep, total_events=n, batches=B, selectivities=parse_selectivities_payload(ref["selectivities"], dep),
                           window_rule=BACKEND.window_rule)
        pick_paper_estimates = tl.toks_of(select_clique_grow_plan(dep, wl, query_batches=frozenset(qb), kappa=BACKEND.kappa).compose, pos)
        selection = dict(executed="the paper's pick with the paper's estimates", pick=toks, pick_this_run=pick_live,
                         selector_on_paper_estimates=pick_paper_estimates, selector_reproduces_paper_pick=(pick_paper_estimates == toks))
    else:
        toks, wl = pick_live, wl_live
        selection = dict(executed="this run's selection", pick=toks, pick_this_run=pick_live)
    selection.update(algorithm="clique-grow", kappa=BACKEND.kappa, estimator="b_unified on the loaded table, each stated predicate on its own", selection_s=sel_s,
                     estimates_this_run=payload_live)
    ptoks = toks + ([vs[-1]] if not any(vs[-1] in make_view(t.split("_")) for t in toks) else [])
    sql = tl.cover_sql(dep, ptoks, all_vars, wl)
    ref_n = None if RATES_TAG else REFERENCE["matches"].get(f"n{n}_s{sigma}_w{W}")
    rec = dict(cell=name, figure="9 (Trino)", params=dict(c), type_rates=dict(first=FIRST_RATE, last=LAST_RATE, others=COMMON),
               **tl.engine_info(client), table=table, key_cols=key_cols,
               limits=dict(statement_s=T, mr_total_s=tl.MR_CELL_BOUND_S),
               selection=selection, pick=toks, mr_form=dict(gap_bounded_by_window=True, gap_define=tl.gap_define_line(spec)),
               eimer_sql=dict(updates=sql["updates"], compose=sql["compose"], compose_nested=(") tn" in sql["compose"])),
               protocol=dict(warmup=False, ingestion="charged to EIMER", query_schedule="after every batch",
                             server_warmed_by_untimed_first_workload=(os.environ.get("EIMER_REPRO_SERVER_WARMUP") == "1")),
               paper_matches=ref_n)
    print(f"[{name}] N={n} B={B} k={k} W={W} ms sigma={sigma} | pick {toks} (this run selects {pick_live}) | {rec['engine']} "
          f"{rec['catalog']}.{rec['schema']} | limits {rec['limits']}", flush=True)

    e_tuples = m_tuples = None
    if "eimer" in ARMS:
        rec["eimer"], e_tuples = tl.eimer_streaming(client, dep, ptoks, all_vars, wl, spec, table, n, B, qb, key_cols, T, time_ingestion=True)
        print(f"   eimer     : {rec['eimer']['status']} total={rec['eimer'].get('total_ms')} ms (updates {rec['eimer'].get('update_ms')} / queries "
              f"{rec['eimer'].get('compose_ms')} / ingestion {rec['eimer'].get('ingest_ms')}) matches={rec['eimer'].get('final_count')}", flush=True)
    if "mr" in ARMS:
        cen = censored_by_smaller_b(c, rec_path.parent, "mr")
        if cen is not None:
            rec["mr"] = dict(status="dnf", censored="smaller_value_did_not_finish", notes=f"not run: native MR did not finish at B = {cen}")
        else:
            t0 = time.time()
            rec["mr"], m_tuples = tl.mr_streaming(client, spec, table, n, B, qb, key_cols, T)
            rec["mr"]["wall_clock_s"] = round(time.time() - t0, 1)
        print(f"   mr        : {rec['mr']['status']} total={rec['mr'].get('total_ms')} ms matches={rec['mr'].get('final_count')} {rec['mr'].get('censored') or ''}", flush=True)

    truth, truth_name = (m_tuples, "mr") if m_tuples is not None else (e_tuples, "eimer")

    def gate(tuples):
        if tuples is None or truth is None:
            return None
        return dict(reference=truth_name, n=len(tuples), n_reference=len(truth), missing=len(truth - tuples), extra=len(tuples - truth), identical=(tuples == truth))

    if "eimer" in rec:
        rec["eimer"]["gate"] = gate(e_tuples) if truth_name == "mr" else None
    cen = censored_by_smaller_b(c, rec_path.parent, "prefilter") if "prefilter" in ARMS else None
    if cen is not None:
        rec["prefilter"] = dict(status="dnf", censored="smaller_value_did_not_finish", notes=f"not run: no subsequence finished at B = {cen}", subsets=[])
        print("   prefilter : dnf smaller_value_did_not_finish", flush=True)
    elif "prefilter" in ARMS:
        runs, best = [], None
        for subset in pf.enumerate_subsets(vs):
            t0 = time.time()
            r, tuples = pf.prefilter_streaming(client, spec, vs, types, (HW_LON, HW_LAT), subset, table, n, B, qb, W, key_cols, T,
                                               cap_ms=(best["total_ms"] if best else None), total_s=tl.MR_CELL_BOUND_S)
            r.update(subset=list(subset), wall_clock_s=round(time.time() - t0, 1), gate=gate(tuples))
            runs.append(r)
            if r["status"] == "ok" and (best is None or r["total_ms"] < best["total_ms"]):
                best = r
            print(f"   prefilter {'|'.join(subset):<12}: {r['status']} total={r.get('total_ms')} ms {('spent ' + str(r.get('spent_ms'))) if r['status'] != 'ok' else ''} "
                  f"| == {truth_name} {r['gate'] and r['gate']['identical']}", flush=True)
        rec["prefilter"] = (dict(status="ok", total_ms=best["total_ms"], final_count=best["final_count"], subset=best["subset"], gate=best["gate"]) if best
                            else dict(status="dnf", notes="no subsequence finished")) | dict(subsets=runs)
    g = [x["identical"] for x in ([rec.get("eimer", {}).get("gate")] + [r.get("gate") for r in rec.get("prefilter", {}).get("subsets", [])]) if x]
    cnt = (rec.get("eimer") or {}).get("final_count")
    rec["gates"] = dict(results_identical=(all(g) if g else None), n_compared=len(g), reference=truth_name,
                        matches_as_in_paper=((cnt == ref_n) if (ref_n is not None and cnt is not None) else None))
    print(f"   gates     : {rec['gates']}", flush=True)
    rec["_recorded"] = tl.write_record(out_root, name, rec)
    return rec


def main():
    tl.run_main(__doc__, cells, cell_name, run_cell, HERE / "results_fig9")


if __name__ == "__main__":
    main()
