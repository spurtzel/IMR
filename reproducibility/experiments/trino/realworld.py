#!/usr/bin/env python3
"""The real-world experiment on Trino (the paper's Fig. 12): native MATCH_RECOGNIZE vs. EIMER on 15 queries over three
real-world tables (NYC taxi trips, Chicago crimes, e-commerce events; five queries each).

Per query, on the first N rows of the table (paper: N = 100,000) in batches of 200 rows, queried after every batch:
  mr      native MATCH_RECOGNIZE over the accumulated table, once per batch, with the query's time window also stated on
          the gap variable (taxi Q2 has no time condition and runs as stated)
  eimer   EIMER in the delta form: the views without the pattern's last variable
          are kept and updated per batch; of the view with the last variable only the batch's delta is built; the
          composed query joins it with the other views and appends the matches that complete in the batch; a final
          dedup leaves one row per match (trino_lib.eimer_delta_streaming)
  Two queries have a Kleene variable (taxi Q2: B+ Z* C; e-commerce Q4: A Z*? B+ Z* C). Their Kleene view is maintained
  incrementally (the stitch, as in the paper's runs); a second EIMER arm, eimer_rescan, maintains it by the per-batch
  rescan and is recorded for comparison.
Each approach has a limit per statement and a limit for its total; past it, its time is a lower bound.

Scale and limits (environment):
  EIMER_REPRO_RW_DATASETS=taxi,chicago,ecommerce   EIMER_REPRO_RW_QUERIES=1,2,3,4,5
  EIMER_REPRO_RW_N=100000            rows of each table (a multiple of the batch size, at most 100,000)
  EIMER_REPRO_RW_BATCH=200           rows per batch
  EIMER_REPRO_WALL=<s>               limit per statement (default 1,800 s)
  EIMER_REPRO_RW_TOTAL=<s>           limit for the total of each approach (default 14,400 s)
  EIMER_REPRO_RW_GATE_TOTAL=<s>      limit for the total of native MR on the window (default 1,800 s)

  python3 realworld.py --list
  python3 realworld.py --only taxi_Q2_n100000_b200 --results-dir <dir>
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import trino_lib as tl  # noqa: E402  (wires the repository onto sys.path)
import realworld_reference as RR  # noqa: E402
import pandas as pd  # noqa: E402
from trino_client import TrinoClientError  # noqa: E402
from selectivity_payload import parse_selectivities_payload  # noqa: E402
from eimer.query.query_spec import load_query_spec_file, query_spec_to_dependency_graph, query_spec_to_match_recognize_sql  # noqa: E402
from eimer.selection.plan_selection_clique_grow import select_clique_grow_plan  # noqa: E402
from eimer.sql.backend import SqlDialect, backend_for  # noqa: E402
from eimer.workload import make_workload  # noqa: E402
from eimer.models import make_view  # noqa: E402

DATA = tl.REPRO / "data" / "realworld"
SPECS = DATA / "specs"
REFERENCE = json.loads((tl.REPRO / "reference" / "paper_trino.json").read_text()).get("realworld", {})
BACKEND = backend_for(SqlDialect.TRINO)
ORDER = ("taxi", "chicago", "ecommerce")   # the order of the paper's figure
PAPER_N, PAPER_BATCH, CARVE_ROWS = 100_000, 200, 100_000

DATASETS = [d for d in os.environ.get("EIMER_REPRO_RW_DATASETS", ",".join(ORDER)).replace(" ", "").split(",") if d]
QUERIES = [int(q) for q in os.environ.get("EIMER_REPRO_RW_QUERIES", "1,2,3,4,5").replace(" ", "").split(",") if q]
N = int(os.environ.get("EIMER_REPRO_RW_N", PAPER_N))
BATCH = int(os.environ.get("EIMER_REPRO_RW_BATCH", 200))
assert all(d in ORDER for d in DATASETS) and all(1 <= q <= 5 for q in QUERIES), "datasets: taxi, chicago, ecommerce; queries: 1..5"
assert 0 < N <= CARVE_ROWS and N % BATCH == 0, "N is a multiple of the batch size and at most 100,000"


def cells() -> list:
    return [dict(dataset=d, query=q, n=N, batch_rows=BATCH) for d in ORDER if d in DATASETS for q in sorted(QUERIES)]


def cell_name(c: dict) -> str:
    return f"{c['dataset']}_Q{c['query']}_n{c['n']}_b{c['batch_rows']}"


def wall_s() -> int:
    return int(os.environ.get("EIMER_REPRO_WALL", 1800))


def total_s() -> float:
    return float(os.environ.get("EIMER_REPRO_RW_TOTAL", 14400))


def gate_total_s() -> float:
    return float(os.environ.get("EIMER_REPRO_RW_GATE_TOTAL", 1800))


_FRAMES: dict = {}


def frame(dataset: str) -> pd.DataFrame:
    """the shipped table (its file checked against the recorded SHA-256), rows in id order 1 .. 100,000."""
    if dataset not in _FRAMES:
        man = json.loads((DATA / "manifest.json").read_text())[dataset]
        path = DATA / man["file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == man["sha256"], f"{path.name}: not the shipped file"
        df = pd.read_parquet(path)
        assert len(df) == man["rows"] == CARVE_ROWS and list(df["id"]) == list(range(1, CARVE_ROWS + 1)) and df["time"].is_monotonic_increasing
        _FRAMES[dataset] = df
    return _FRAMES[dataset]


def rows_of(dataset: str, lo: int, n: int) -> pd.DataFrame:
    """rows lo .. lo + n - 1 of the table with ids renumbered 1 .. n (the arms cut batches by id; the order is kept)."""
    df = frame(dataset).iloc[lo - 1:lo - 1 + n].reset_index(drop=True).copy()
    assert len(df) == n
    df["id"] = range(1, n + 1)
    return df


def ensure_table(client, dataset: str, lo: int, n: int) -> str:
    table = f"events_rw_{dataset}_{lo}_{n}"
    try:
        r = client.execute(f"SELECT count(*), min(id), max(id) FROM {table}")[1][0]
        ok = (int(r[0]), int(r[1]), int(r[2])) == (n, 1, n)
    except (TrinoClientError, TypeError):
        ok = False
    if not ok:
        t0 = time.time()
        tl.load_typed_frame(client, table, rows_of(dataset, lo, n))
        print(f"   [data {table}: {n} rows loaded in {time.time() - t0:.0f} s]", flush=True)
    return table


def select(dep, wl, batches: int) -> list:
    """clique-grow's pick; with a Kleene variable every variable the pick leaves out gets its own view."""
    pos = dep.positions
    toks = tl.toks_of(select_clique_grow_plan(dep, wl, query_batches=frozenset(range(1, batches + 1)), kappa=BACKEND.kappa).compose, pos)
    if any(q in ("PLUS", "RELUCTANT_PLUS") for q in dep.quantifiers.values()):
        covered = {x for t in toks for x in make_view(t.split("_"))}
        toks = sorted(set(toks + [v for v in dep.variables if v not in covered]))
    return toks


def same(a, b):
    return (a == b) if (a is not None and b is not None) else None


def run_cell(c: dict, out_root: Path) -> dict:
    name = cell_name(c)
    ds, qi, n, batch = c["dataset"], c["query"], c["n"], c["batch_rows"]
    B = n // batch
    T, TOT = wall_s(), total_s()
    client = tl.connect(timeout_s=T)
    spec = load_query_spec_file(str(SPECS / ds / f"Q{qi}.json"))
    dep = query_spec_to_dependency_graph(spec)
    pos = dep.positions
    vs = sorted(dep.variables, key=lambda v: pos[v])
    all_vars = set(dep.variables)
    kleene_vars = [v for v in vs if dep.quantifiers.get(v) in ("PLUS", "RELUCTANT_PLUS")]
    key_cols = [m.alias for m in spec.measures if (m.sql_type or "").upper() == "BIGINT"]   # ids, and the count of a Kleene run
    ref = REFERENCE[f"{ds}_Q{qi}"]
    bounded = tl.gap_bound(spec) is not None
    pattern = next(ln.strip() for ln in query_spec_to_match_recognize_sql(spec, events_table="events").splitlines() if ln.strip().startswith("PATTERN"))
    pattern = pattern[len("PATTERN"):].strip().strip("()")
    pyref = RR.REFERENCES.get((ds, qi))
    if pyref is not None:
        assert pyref[1] == key_cols, (pyref[1], key_cols)

    table = ensure_table(client, ds, 1, n)
    toks = list(ref["pick"])
    sels = parse_selectivities_payload(ref["selectivities"], dep)
    wl = make_workload(dep, total_events=n, batches=B, selectivities=sels, window_rule=BACKEND.window_rule)
    pick_paper_estimates = select(dep, make_workload(dep, total_events=PAPER_N, batches=PAPER_N // PAPER_BATCH, selectivities=sels,
                                                     window_rule=BACKEND.window_rule), PAPER_N // PAPER_BATCH)
    t0 = time.time()
    try:
        wl_live, payload_live = tl.estimated_workload(client, dep, table, n, B, BACKEND.window_rule)
        pick_live = select(dep, wl_live, B)
    except Exception as e:  # noqa: BLE001  (an estimate the estimator cannot form on this table)
        payload_live, pick_live = dict(error=f"{type(e).__name__}: {str(e)[:200]}"), None
    selection = dict(executed="the paper's pick with the paper's estimates", pick=toks, algorithm="clique-grow", kappa=BACKEND.kappa,
                     selector_on_paper_estimates=pick_paper_estimates, selector_reproduces_paper_pick=(pick_paper_estimates == sorted(toks)),
                     pick_this_run=pick_live, this_run_selects_paper_pick=same(pick_live, sorted(toks)),
                     estimator="b_unified on the loaded table, each stated predicate on its own", selection_s=round(time.time() - t0, 1),
                     estimates_this_run=payload_live)
    ptoks = toks + ([vs[-1]] if not any(vs[-1] in make_view(t.split("_")) for t in toks) else [])
    sql = tl.cover_sql(dep, ptoks, all_vars, wl)

    rec = dict(cell=name, figure="12 (Trino)", params=dict(c, batches=B, k=len(vs)), dataset=ds, query=f"Q{qi}",
               pattern=pattern, kleene_variables=kleene_vars, **tl.engine_info(client), table=table, key_cols=key_cols,
               limits=dict(statement_s=T, total_s=TOT, window_mr_total_s=gate_total_s()),
               selection=selection, pick=toks,
               mr_form=dict(gap_bounded_by_window=bounded, gap_define=(tl.gap_define_line(spec) if bounded else None)),
               eimer_sql=dict(updates=sql["updates"], compose=sql["compose"], compose_nested=("JOIN (SELECT" in sql["compose"] or ") tn" in sql["compose"])),
               protocol=dict(warmup=False, ingestion="untimed", query_schedule="after every batch",
                             eimer_form="delta: the view with the last variable is built per batch only; new matches are appended",
                             kleene_view=("stitch (eimer), rescan (eimer_rescan)" if kleene_vars else None),
                             server_warmed_by_untimed_first_workload=(os.environ.get("EIMER_REPRO_SERVER_WARMUP") == "1")),
               paper_matches=(ref["matches"] if n == PAPER_N else None))
    print(f"[{name}] {ds} Q{qi} N={n} B={B} k={len(vs)} | pick {toks} (selector on the paper's estimates: {pick_paper_estimates}; this run selects "
          f"{pick_live}) | {rec['engine']} {rec['catalog']}.{rec['schema']} | limits {rec['limits']}", flush=True)

    def eimer(tab, nn, bb, wl_, stitch=True, total=None):
        return tl.eimer_delta_streaming(client, dep, ptoks, all_vars, wl_, tab, nn, bb, key_cols, T, total_wall_s=total,
                                        kleene=("stitch" if stitch else "rescan"), measure_storage=(total is not None))

    # ---- the window: a few hundred to a few thousand rows on which native MR finishes
    lo, ng = ref["gate_window"]
    assert ng % batch == 0, "the window is a multiple of the batch size"
    Bg = ng // batch
    gtab = ensure_table(client, ds, lo, ng)
    wl_g = make_workload(dep, total_events=ng, batches=Bg, selectivities=sels, window_rule=BACKEND.window_rule)
    g_mr, g_mr_t = tl.mr_streaming(client, spec, gtab, ng, Bg, tl.query_schedule(Bg), key_cols, T, total_cap_ms=gate_total_s() * 1000.0, bounded=bounded)
    g_e, g_e_t = eimer(gtab, ng, Bg, wl_g)
    win = dict(rows=[lo, lo + ng - 1], batches=Bg, mr_status=g_mr.get("status"), eimer_status=g_e.get("status"),
               n_mr=g_mr.get("final_count"), n_eimer=g_e.get("final_count"), mr_ms=g_mr.get("total_ms"), eimer_ms=g_e.get("total_ms"),
               mr_eq_eimer=same(g_mr_t, g_e_t))
    if kleene_vars:
        g_r, g_r_t = eimer(gtab, ng, Bg, wl_g, stitch=False)
        win.update(eimer_rescan_status=g_r.get("status"), n_eimer_rescan=g_r.get("final_count"), rescan_eq_eimer=same(g_r_t, g_e_t))
    if pyref is not None:
        p_t = frozenset(pyref[0](rows_of(ds, lo, ng)))
        win.update(n_reference=len(p_t), reference_eq_eimer=same(p_t, g_e_t))
    rec["window"] = win
    print(f"   window (rows {lo}-{lo + ng - 1}, {Bg} batches): {win}", flush=True)

    # ---- the full run. EIMER first: its result is the reference of the comparisons
    rec["eimer"], e_t = eimer(table, n, B, wl, total=TOT)
    x = rec["eimer"]
    print(f"   eimer        : {x['status']} total={x.get('total_ms')} ms matches={x.get('final_count')} "
          f"{('(Kleene view ' + str(x.get('kleene_view_ms')) + ' ms)') if kleene_vars else ''}", flush=True)
    r_t = None
    if kleene_vars:
        rec["eimer_rescan"], r_t = eimer(table, n, B, wl, stitch=False, total=TOT)
        x = rec["eimer_rescan"]
        print(f"   eimer_rescan : {x['status']} total={x.get('total_ms')} ms matches={x.get('final_count')} (Kleene view {x.get('kleene_view_ms')} ms)", flush=True)
    t0 = time.time()
    rec["mr"], m_t = tl.mr_streaming(client, spec, table, n, B, tl.query_schedule(B), key_cols, T, total_cap_ms=TOT * 1000.0, bounded=bounded)
    rec["mr"]["wall_clock_s"] = round(time.time() - t0, 1)
    x = rec["mr"]
    print(f"   mr           : {x['status']} total={x.get('total_ms')} ms spent={x.get('spent_ms')} matches={x.get('final_count')} {x.get('censored') or ''}", flush=True)

    cnt = rec["eimer"].get("final_count") if rec["eimer"].get("status") == "ok" else None
    g = dict(mr_eq_eimer=same(m_t, e_t), rescan_eq_eimer=(same(r_t, e_t) if kleene_vars else None),
             reference_eq_eimer=(same(frozenset(pyref[0](rows_of(ds, 1, n))), e_t) if pyref is not None else None),
             matches_as_in_paper=((cnt == ref["matches"]) if (n == PAPER_N and cnt is not None) else None))
    cmp = [win.get("mr_eq_eimer"), win.get("rescan_eq_eimer"), win.get("reference_eq_eimer"), g["mr_eq_eimer"], g["rescan_eq_eimer"], g["reference_eq_eimer"]]
    g.update(results_identical=(all(v for v in cmp if v is not None) if any(v is not None for v in cmp) else None),
             n_compared=sum(v is not None for v in cmp))
    rec["gates"] = g
    print(f"   gates        : {g}", flush=True)
    rec["_recorded"] = tl.write_record(out_root, name, rec)
    return rec


def main():
    tl.run_main(__doc__, cells, cell_name, run_cell, HERE / "results_realworld")


if __name__ == "__main__":
    main()
