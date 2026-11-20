#!/usr/bin/env python3
"""Selection time (Section 7.3 of the paper): the time clique-grow and its seed, the edge cover, need to select a
materialization strategy, over the pattern length k for four topologies (chain, star, cycle, complete).

No engine is involved. The queries are those of evaluation_queries/7.3_plan_selection/selection_time/. The workload is
fixed: N = 100,000 rows in 25 batches, every batch queried, every type at 5 % of the rows and a selectivity of 0.01 per
dependent condition (constants, no table is read). Per query, ,,select_clique_grow_plan'' and
,,select_edge_cover_plan'' run REPS times each and the median is reported. A selection that takes longer than LIMIT is
not repeated, and clique-grow is not run for larger k of that topology; the edge cover is.

  selection_time.py --results-dir <dir>     one record per query, then summary.md and summary.json
  selection_time.py --list                  the queries of the current settings

Settings (environment):
  EIMER_REPRO_SEL_KS=3,4,5,6,7,8            pattern lengths of chain, star and cycle
  EIMER_REPRO_SEL_COMPLETE_KS=3,4,5         pattern lengths of the complete topology
  EIMER_REPRO_SEL_REPS=5                    runs per query and selector
  EIMER_REPRO_SEL_LIMIT=300                 seconds, see above
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPRO = HERE.parents[1]
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))

from eimer.query.query_spec import load_query_spec_file, query_spec_to_dependency_graph
from eimer.selection.plan_selection_clique_grow import KAPPA_DEFAULT, select_clique_grow_plan
from eimer.selection.plan_selection_edge_cover import select_edge_cover_plan
from eimer.workload import ConstantSelectivity, Selectivities, make_workload

QUERIES = REPO / "evaluation_queries" / "7.3_plan_selection" / "selection_time"
REFERENCE = json.loads((REPRO / "reference" / "paper_selection_time.json").read_text())["queries"]
N, BATCHES, TYPE_RATE, SIGMA = 100_000, 25, 0.05, 0.01
TOPOLOGIES = ("chain", "star", "cycle", "complete")


def env_ints(name: str, default: str) -> list:
    return [int(x) for x in os.environ.get(name, default).split(",") if x.strip()]


KS = env_ints("EIMER_REPRO_SEL_KS", "3,4,5,6,7,8")
COMPLETE_KS = env_ints("EIMER_REPRO_SEL_COMPLETE_KS", "3,4,5")
REPS = int(os.environ.get("EIMER_REPRO_SEL_REPS", "5"))
LIMIT = float(os.environ.get("EIMER_REPRO_SEL_LIMIT", "300"))


def cells() -> list:
    out = []
    for topology in TOPOLOGIES:
        for k in sorted(COMPLETE_KS if topology == "complete" else KS):
            name = f"{topology}_k{k}"
            if not (QUERIES / topology / name / f"{name}.json").exists():
                raise SystemExit(f"no query {name} in {QUERIES / topology}")
            out.append(dict(topology=topology, k=k, name=name))
    return out


def tokens(cover, pos) -> list:
    """a strategy as sorted view names (,,A_B''), the variables of a view in pattern order."""
    return sorted("_".join(sorted(v, key=lambda x: pos[x])) for v in cover)


def timed(select, reps: int) -> dict:
    """,,select'' run up to ,,reps'' times: median, minimum and maximum in seconds and the strategy of every run."""
    seconds, picks = [], []
    for _ in range(reps):
        t0 = time.perf_counter()
        picks.append(select())
        seconds.append(time.perf_counter() - t0)
        if seconds[-1] > LIMIT:
            break
    return dict(median_s=round(statistics.median(seconds), 4), min_s=round(min(seconds), 4), max_s=round(max(seconds), 4),
                reps=len(seconds), over_limit=seconds[-1] > LIMIT, picks=picks)


def run_cell(c: dict, with_clique_grow: bool) -> dict:
    dep = query_spec_to_dependency_graph(load_query_spec_file(QUERIES / c["topology"] / c["name"] / f"{c['name']}.json"))
    pos = dep.positions
    sels = Selectivities(independent={v: TYPE_RATE for v in dep.variables},
                         dependent={(e.var_left, e.var_right): (ConstantSelectivity(SIGMA),) for e in dep.edges})
    wl = make_workload(dep, total_events=N, batches=BATCHES, selectivities=sels)
    qb = frozenset(range(1, BATCHES + 1))
    rec = dict(cell=c["name"], params=dict(c, n=N, batches=BATCHES, type_rate=TYPE_RATE, sigma=SIGMA, reps=REPS, limit_s=LIMIT),
               kappa=KAPPA_DEFAULT, python=platform.python_version(), paper=REFERENCE.get(c["name"]))
    ec = timed(lambda: tokens(select_edge_cover_plan(dep, wl, max_views=4, query_batches=qb).compose, pos), REPS)
    rec["edge_cover"] = dict({k: v for k, v in ec.items() if k != "picks"}, pick=ec["picks"][0], same_pick_every_run=len({tuple(p) for p in ec["picks"]}) == 1)
    if with_clique_grow:
        cg = timed(lambda: tokens(select_clique_grow_plan(dep, wl, query_batches=qb).compose, pos), REPS)
        rec["clique_grow"] = dict({k: v for k, v in cg.items() if k != "picks"}, pick=cg["picks"][0], same_pick_every_run=len({tuple(p) for p in cg["picks"]}) == 1)
        covered = {v for view in cg["picks"][0] for v in view.split("_")}
        rec["clique_grow"]["covers_every_variable_or_leaves_it_single"] = covered <= set(dep.variables)
    else:
        rec["clique_grow"] = None
    return rec


def fmt(x) -> str:
    return "-" if x is None else (f"{x:,.0f}" if x >= 100 else f"{x:.2f}" if x >= 1 else f"{x:.4f}")


def summarize(root: Path) -> int:
    recs = [json.loads(p.read_text()) for p in sorted((root / "records").glob("*.json"))]
    order = {t: i for i, t in enumerate(TOPOLOGIES)}
    recs.sort(key=lambda r: (order[r["params"]["topology"]], r["params"]["k"]))
    failed = []
    lines = ["# Selection time (Section 7.3)", "",
             f"N = {N:,} rows in {BATCHES} batches, every batch queried, type rate {TYPE_RATE}, selectivity {SIGMA}; median of up to {REPS} runs.", "",
             "| topology | k | clique-grow [s] | edge cover [s] | paper: clique-grow [s] | paper: edge cover [s] | runs | strategy of clique-grow |",
             "|---|---|---|---|---|---|---|---|"]
    for r in recs:
        cg, ec, ref = r["clique_grow"], r["edge_cover"], r.get("paper") or {}
        if not ec["same_pick_every_run"]:
            failed.append(f"{r['cell']}: the edge cover differs between runs")
        if cg is not None and not cg["same_pick_every_run"]:
            failed.append(f"{r['cell']}: clique-grow's strategy differs between runs")
        if cg is not None and not cg["covers_every_variable_or_leaves_it_single"]:
            failed.append(f"{r['cell']}: the strategy names a variable that is not in the pattern")
        pick = "-" if cg is None else "{" + ", ".join(t.replace("_", "") for t in cg["pick"]) + "}"
        lines.append(f"| {r['params']['topology']} | {r['params']['k']} | {fmt(cg['median_s']) if cg else 'not run'} | {fmt(ec['median_s'])} | "
                     f"{fmt(ref.get('clique_grow_s'))} | {fmt(ref.get('edge_cover_s'))} | {cg['reps'] if cg else 0} | {pick} |")
    n_checks = sum(2 if r["clique_grow"] is None else 3 for r in recs)
    verdict = "ALL CHECKS PASSED" if not failed else f"{len(failed)} CHECK(S) FAILED"
    lines += ["", f"Checks: {n_checks - len(failed)} passed, {len(failed)} failed. **{verdict}**", ""] + [f"- FAILED: {f}" for f in failed]
    lines += ["", "Notes: a selection is checked to return the same strategy in every run. \"not run\": clique-grow exceeded the limit at a "
              "smaller k of this topology. The paper's values were measured on other hardware and are given for orientation; a query "
              "with a single run was not repeated because the run exceeded the limit."]
    (root / "summary.md").write_text("\n".join(lines) + "\n")
    (root / "summary.json").write_text(json.dumps(dict(records=recs, failed=failed), indent=1))
    print("\n".join(lines))
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results-dir", default=str(REPRO / "out" / "selection_time"))
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    cs = cells()
    if a.list:
        print("\n".join(c["name"] for c in cs))
        return 0
    root = Path(a.results_dir)
    (root / "records").mkdir(parents=True, exist_ok=True)
    stopped = set()
    for c in cs:
        path = root / "records" / f"{c['name']}.json"
        if path.exists():
            rec = json.loads(path.read_text())
            print(f"   skip  {c['name']} (record exists)", flush=True)
        else:
            rec = run_cell(c, with_clique_grow=c["topology"] not in stopped)
            path.write_text(json.dumps(rec, indent=1))
            cg = rec["clique_grow"]
            print(f"   done  {c['name']}: clique-grow {fmt(cg['median_s']) + ' s' if cg else 'not run'}, edge cover {fmt(rec['edge_cover']['median_s'])} s", flush=True)
        if rec["clique_grow"] is None or rec["clique_grow"]["over_limit"]:
            stopped.add(c["topology"])
    return summarize(root)


if __name__ == "__main__":
    sys.exit(main())
