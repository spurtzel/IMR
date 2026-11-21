#!/usr/bin/env python3
"""Summary and checks of a run of a Trino experiment:  summarize_trino.py fig8|fig9|fig10|kleene|budget_shift|topology_memory|realworld <out dir>

Prints one row per workload, writes <out dir>/summary.md and <out dir>/summary.json, and exits with 0 only if every
check passes:
  correctness    the approaches of a workload return the same result, compared on full tuples
  configuration  Trino 479, Iceberg or in-memory tables, single-threaded, join reordering off, native MR with the
                 window on the gap variable, EIMER's composition emitted over the plan's join tree
  paper          the executed strategy is the paper's pick, the selection of this run arrives at the same pick, and
                 the number of matches is the paper's (reproducibility/reference/paper_trino.json)
fig8 shows the paper's Trino values next to the measured ones.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPRO = Path(__file__).resolve().parents[2]
REF = json.loads((REPRO / "reference" / "paper_trino.json").read_text())


class Checks:
    def __init__(self):
        self.failed, self.passed = [], 0

    def ok(self, cond, what: str) -> bool:
        if cond:
            self.passed += 1
        else:
            self.failed.append(what)
        return bool(cond)


def load(out: Path) -> list:
    d = out / "records"
    return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))] if d.is_dir() else []


def cell(arm) -> str:
    if not arm:
        return "-"
    if arm.get("status") == "ok":
        v = arm["total_ms"] / 1000
        return f"{v:.2f}" if v < 100 else f"{v:,.0f}"
    if arm.get("status") == "dnf":
        return "DNF" + (f" (>{arm['spent_ms'] / 1000:,.0f})" if arm.get("spent_ms") else "")
    return str(arm.get("status"))


def paper_cell(p) -> str:
    if not p:
        return "-"
    return f"{p['s']:g}" if p.get("status") == "ok" else "DNF"


def ratio(a, b) -> str:
    if a and b and a.get("status") == "ok" and b.get("status") == "ok" and b["total_ms"]:
        return f"{a['total_ms'] / b['total_ms']:.1f}x"
    return "-"


def common_checks(ck: Checks, r: dict, arms: tuple):
    c = r["cell"]
    ck.ok((r.get("eimer") or {}).get("status") == "ok", f"{c}: EIMER did not finish ({(r.get('eimer') or {}).get('status')})")
    arms = tuple(a for a in arms if a in r)
    for a in arms:
        ck.ok((r.get(a) or {}).get("status") in ("ok", "dnf"), f"{c}: {a} ended with {(r.get(a) or {}).get('status')}: {(r.get(a) or {}).get('notes', '')[:120]}")
    g = r.get("gates", {})
    ck.ok(g.get("results_identical") is not False, f"{c}: results differ between the approaches")
    ck.ok(g.get("n_compared", 0) >= 1 or all((r.get(a) or {}).get("status") != "ok" for a in arms if a != "eimer"),
          f"{c}: no result comparison although a baseline finished")
    ck.ok(r.get("engine") == "Trino 479", f"{c}: engine {r.get('engine')} (paper: Trino 479)")
    ck.ok(r.get("catalog") in ("iceberg", "memory"), f"{c}: catalog {r.get('catalog')} (paper: Iceberg tables)")
    ck.ok(r.get("session") == {"join_reordering_strategy": "NONE", "task_concurrency": 1}, f"{c}: session {r.get('session')}")
    if "mr" in arms:
        ck.ok(r.get("mr_form", {}).get("gap_bounded_by_window") is True and "<=" in r.get("mr_form", {}).get("gap_define", ""),
              f"{c}: native MR without the window on the gap variable")
    ck.ok(r.get("protocol", {}).get("warmup") is False, f"{c}: warm-up runs")
    ck.ok(r.get("eimer_sql", {}).get("compose_nested") is True or len(r.get("pick", [])) < 2, f"{c}: composition not emitted over the join tree")
    ck.ok(r.get("selection", {}).get("kappa") == 150.0, f"{c}: kappa {r.get('selection', {}).get('kappa')} (paper: 150)")
    sel = r.get("selection", {})
    if "this_run_selects_paper_pick" in sel and sel.get("selector_reproduces_paper_pick", True):
        ck.ok(sel["this_run_selects_paper_pick"] is True, f"{c}: this run's selection {sel.get('pick_this_run')} differs from the paper's pick {sel.get('pick')}")
    if g.get("matches_as_in_paper") is not None:
        ck.ok(g["matches_as_in_paper"] is True, f"{c}: {(r.get('eimer') or {}).get('final_count')} matches (paper: {r.get('paper_matches')})")


def as_paper(r: dict) -> str:
    sel, g = r.get("selection", {}), r.get("gates", {})
    if "this_run_selects_paper_pick" not in sel and g.get("matches_as_in_paper") is None:
        return "not in the paper"
    ok = sel.get("this_run_selects_paper_pick", True) is True and g.get("matches_as_in_paper") in (True, None)
    return "yes" if ok else "NO"


def fig8(out: Path):
    recs = sorted(load(out), key=lambda r: (0 if "N" in r["params"]["axes"] else 1, r["params"]["n"], r["params"]["k"]))
    ck = Checks()
    ck.ok(bool(recs), "no records")
    head = "| panel | N | B | k | matches | pick | T: MR [s] | T: GDYN [s] | T: EIMER [s] | MR / EIMER | GDYN / EIMER | results identical | as in the paper | paper: MR / GDYN / EIMER [s] | peak query memory [MB]: MR / GDYN / EIMER |"
    rows = []
    for r in recs:
        p = r["params"]
        common_checks(ck, r, ("mr", "gdyn", "eimer"))
        if r.get("paper") is not None:
            ck.ok(r["selection"].get("executed", "").startswith("the paper's pick") and r["pick"] == REF["fig8"][r["cell"]]["pick"], f"{r['cell']}: not the paper's pick")
            ck.ok(r["selection"].get("selector_reproduces_paper_pick") is True, f"{r['cell']}: the selector does not reproduce the paper's pick from the paper's estimates")
        pp = r.get("paper") or {}
        rows.append(f"| {'+'.join(p['axes'])} | {p['n']:,} | {p['batches']} | {p['k']} | {(r.get('eimer') or {}).get('final_count')} | {{{', '.join(t.replace('_', '') for t in r['pick'])}}} | "
                    f"{cell(r.get('mr'))} | {cell(r.get('gdyn'))} | {cell(r.get('eimer'))} | {ratio(r.get('mr'), r.get('eimer'))} | {ratio(r.get('gdyn'), r.get('eimer'))} | "
                    f"{r['gates'].get('results_identical')} | {as_paper(r)} | {paper_cell(pp.get('mr'))} / {paper_cell(pp.get('gdyn'))} / {paper_cell(pp.get('eimer'))} | {pm(r.get('mr'))} / {pm(r.get('gdyn'))} / {pm(r.get('eimer'))} |")
    title = "# Trino experiment (Fig. 8a, Trino series): native MR, GDYN and EIMER on Trino"
    notes = ("Notes: runtimes are totals over all batches (EIMER: view updates + queries; GDYN: maintenance + queries; MR: one query per batch over "
             "the accumulated table). DNF: the approach hit a time limit (in brackets: seconds spent) or was not run because it did not finish at a "
             "smaller value. \"as in the paper\": this run's selection arrives at the paper's pick and the number of matches is the paper's. "
             "The paper's values were measured on other hardware (GDYN: median of three runs).")
    return title, head, rows, notes, ck, recs


def fig9(out: Path):
    recs = sorted(load(out), key=lambda r: (0 if "W" in r["params"]["axes"] else 1, r["params"]["n"], -r["params"]["sigma"], -(r.get("type_rates") or {}).get("last", 0.01),
                                              r["params"]["w_ms"], r["params"]["batches"]))
    ck = Checks()
    ck.ok(bool(recs), "no records")
    head = "| panel | N | B | W [ms] | sigma | first / last type | matches | pick | this run's selection | MR [s] | prefilter [s] | prefilter subsequence | EIMER [s] | MR / EIMER | prefilter / EIMER | results identical | as in the paper | peak query memory [MB]: MR / prefilter / EIMER |"
    rows = []
    for r in recs:
        p, tr = r["params"], r.get("type_rates") or {}
        common_checks(ck, r, ("mr", "prefilter", "eimer"))
        ref = REF["fig9"]["workloads"].get(r["cell"])
        if ref is not None:
            ck.ok(r["selection"].get("executed", "").startswith("the paper's pick") and r["pick"] == ref["pick"], f"{r['cell']}: not the paper's pick")
            ck.ok(r["selection"].get("selector_reproduces_paper_pick") is True, f"{r['cell']}: the selector does not reproduce the paper's pick from the paper's estimates")
        ck.ok(r.get("protocol", {}).get("ingestion") == "charged to EIMER", f"{r['cell']}: ingestion not charged to EIMER")
        pfr = r.get("prefilter") or {}
        for sr in pfr.get("subsets", []):
            ck.ok(sr.get("status") in ("ok", "dnf", "dominated"), f"{r['cell']}: prefilter {sr.get('subset')} ended with {sr.get('status')}")
        rows.append(f"| {'+'.join(p['axes'])} | {p['n']:,} | {p['batches']} | {p['w_ms']} | {p['sigma']} | {tr.get('first', 0.01) * 100:g} % / {tr.get('last', 0.01) * 100:g} % | {(r.get('eimer') or {}).get('final_count')} | {{{', '.join(t.replace('_', ' ') for t in r['pick'])}}} | "
                    f"{{{', '.join(t.replace('_', ' ') for t in r['selection'].get('pick_this_run', []))}}} | {cell(r.get('mr'))} | {cell(pfr)} | {' '.join(pfr.get('subset') or []) or '-'} | {cell(r.get('eimer'))} | {ratio(r.get('mr'), r.get('eimer'))} | {ratio(pfr, r.get('eimer'))} | "
                    f"{r['gates'].get('results_identical')} | {as_paper(r) if (r.get('type_rates') or {}).get('first', 0.01) == (r.get('type_rates') or {}).get('last', 0.01) == 0.01 else '-'} | {pm(r.get('mr'))} / "
                    f"{pm(next((x for x in pfr.get('subsets', []) if x.get('subset') == pfr.get('subset')), None))} / {pm(r.get('eimer'))} |")
    title = "# Trino experiment (Fig. 9 on Trino): native MR, the join-based prefilter and EIMER on Trino"
    notes = ("Notes: runtimes are totals over all batches (EIMER: writing the batch + view updates + queries; MR and prefilter: one query per batch "
             "over the accumulated table). prefilter: the fastest of all subsequences that contain the first and last variable. DNF: the approach hit a "
             "time limit or was not run because it did not finish at a smaller B. pick: the paper's strategy for the paper's workloads, else this run's "
             "selection. \"as in the paper\": the number of matches is the paper's, where the paper has this table.")
    return title, head, rows, notes, ck, recs


def mb(arm) -> str:
    st = (arm or {}).get("state")
    if not st:
        return "-"
    if st.get("bytes") is not None:
        return f"{st['bytes'] / 1e6:.2f}"
    return f"{st['uncompressed_bytes'] / 1e6:.2f} uncompressed" if st.get("uncompressed_bytes") is not None else f"{st['rows']:,} rows"


def pm(arm) -> str:
    """an approach's peak query memory in MB."""
    v = (arm or {}).get("peak_query_memory_bytes")
    return "-" if v is None else f"{v / 1e6:.1f}"


def share(arm, maint: str, query: str) -> str:
    if not arm or arm.get("status") != "ok":
        return "-"
    m, q = float(arm.get(maint) or 0), float(arm.get(query) or 0)
    return f"{100 * m / (m + q):.0f} %" if m + q > 0 else "-"


def fig10(out: Path):
    order = {"acyclic": 0, "cyclic": 1}
    skews = {"uniform": (0, ""), "rare_ends": (1, ", first and last type rare"), "rare_interior": (2, ", interior types rare")}
    recs = sorted(load(out), key=lambda r: (r["params"]["part"], order[r["params"]["cls"]], 0 if "N" in r["params"]["axes"] else 1,
                                            r["params"]["n"], r["params"]["k"], -float(r["params"]["pct"]), skews[r["params"].get("skew", "uniform")][0]))
    ck = Checks()
    ck.ok(bool(recs), "no records")
    head = ("| part | query | N | B | k | queried | matches | pick | GDYN [s] | EIMER [s] | GDYN / EIMER | maintenance share GDYN / EIMER | state [MB] GDYN / EIMER | "
            "results identical | as in the paper | peak query memory [MB] GDYN / EIMER |")
    rows = []
    ref = REF.get("fig10", {})
    for r in recs:
        p = r["params"]
        common_checks(ck, r, ("gdyn", "eimer"))
        note = ""
        if r["cell"] in ref:
            ck.ok(r["selection"].get("executed", "").startswith("the paper's pick") and r["pick"] == ref[r["cell"]]["pick"], f"{r['cell']}: not the paper's pick")
            if not r["selection"].get("selector_reproduces_paper_pick"):
                note = f" (selector on the paper's estimates: {{{', '.join(t.replace('_', '') for t in r['selection'].get('selector_on_paper_estimates', []))}}})"
        ck.ok((r["eimer"].get("state") or {}).get("rows") is not None, f"{r['cell']}: no state size for EIMER")
        g, e = r.get("gdyn"), r.get("eimer")
        parts = p["part"] + ("" if "skew" not in p["axes"] or p["part"] == "s" else ", s")
        rows.append(f"| {parts} | {p['cls']}{skews[p.get('skew', 'uniform')][1]} | {p['n']:,} | {p['batches']} | {p['k']} | {p['pct']:g} % | {e.get('final_count')} | {{{', '.join(t.replace('_', '') for t in r['pick'])}}}{note} | "
                    f"{cell(g)} | {cell(e)} | {ratio(g, e)} | {share(g, 'update_ms', 'query_ms')} / {share(e, 'update_ms', 'compose_ms')} | {mb(g)} / {mb(e)} | "
                    f"{r['gates'].get('results_identical')} | {'-' if p.get('skew', 'uniform') != 'uniform' else as_paper(r)} | {pm(g)} / {pm(e)} |")
    title = ("# Trino experiment (Fig. 10 on Trino): EIMER and GDYN on acyclic and cyclic queries (a), under sparser query schedules (b), "
             "and on skewed type frequencies (s)")
    notes = ("Notes: runtimes are totals over all batches (EIMER: view updates + queries on the queried batches; GDYN: maintenance of the reduced sets + "
             "queries); ingestion is untimed for both. maintenance share: the part of the total spent on view / reduced-set updates. state: the Iceberg "
             "data files of EIMER's views and of GDYN's reduced sets at the end of the run. DNF: GDYN hit its time limit or was not run because it did "
             "not finish at a smaller value. \"as in the paper\": this run's selection arrives at the paper's pick and the number of matches is the "
             "paper's, where the paper has this workload. Part s: the rare types are at 1 % of the rows, all other types at 10 %; its rows without a "
             "remark are the uniform table (every type at 5 %); for the skewed tables no values of the paper's run are stored (-).")
    return title, head, rows, notes, ck, recs


def engine_checks(ck: Checks, r: dict):
    c = r["cell"]
    ck.ok(r.get("engine") == "Trino 479", f"{c}: engine {r.get('engine')} (paper: Trino 479)")
    ck.ok(r.get("catalog") in ("iceberg", "memory"), f"{c}: catalog {r.get('catalog')}")
    ck.ok(r.get("session") == {"join_reordering_strategy": "NONE", "task_concurrency": 1}, f"{c}: session {r.get('session')}")
    ck.ok(r.get("protocol", {}).get("warmup") is False, f"{c}: warm-up runs")


def kleene(out: Path):
    recs = sorted(load(out), key=lambda r: (r["params"]["part"], r["params"]["l_run"], r["params"].get("seed", 0)))
    ck = Checks()
    ck.ok(bool(recs), "no records")
    head = "| part | N | batches | L | seed | matches | pick | MR [s] | EIMER, no stitch [s] | EIMER, stitch [s] | MR / stitch | no stitch / stitch | Kleene view maintenance [s]: no stitch / stitch | results identical | peak query memory [MB]: MR / no stitch / stitch |"
    rows = []
    for r in recs:
        p, c = r["params"], r["cell"]
        engine_checks(ck, r)
        g = r.get("gates", {})
        ck.ok(g.get("results_identical") is True, f"{c}: results differ ({g})")
        if p["part"] == "payoff":
            t = r["totals"]
            rows.append(f"| one view | {p['n']:,} | {p['n'] // p['batch']} | {p['l_run']} | {p['seed']} | {g.get('n_runs')} runs | - | - | - | - | - | {t['rescan_over_stitch']}x | "
                        f"{t['rescan_maint_ms'] / 1000:.2f} / {t['stitch_maint_ms'] / 1000:.2f} | {g.get('results_identical')} | - |")
            continue
        for a in ("mr", "eimer_nostitch", "eimer_stitch"):
            ck.ok((r.get(a) or {}).get("status") in ("ok", "dnf"), f"{c}: {a} ended with {(r.get(a) or {}).get('status')}: {(r.get(a) or {}).get('notes', '')[:120]}")
        for a in ("eimer_nostitch", "eimer_stitch"):
            ck.ok(r[a].get("status") == "ok", f"{c}: {a} did not finish")
        gf = r.get("gate_first_events", {})
        ck.ok(gf.get("nostitch_eq_mr") is True and gf.get("stitch_eq_mr") is True, f"{c}: the gate on the first events failed ({gf})")
        ck.ok(g.get("matches_as_expected") is not False, f"{c}: {r['eimer_stitch'].get('final_count')} matches (expected {r.get('expected_matches')})")
        if c in REF["kleene"]:
            ck.ok(r["selection"].get("executed", "").startswith("the paper's pick") and r["selection"].get("pick") == REF["kleene"][c]["pick"], f"{c}: not the paper's pick")
            ck.ok(r["selection"].get("selector_reproduces_paper_pick") is True, f"{c}: the selector does not reproduce the paper's pick from the paper's estimates")
        ck.ok(r["selection"].get("kappa") == 150.0, f"{c}: kappa {r['selection'].get('kappa')}")
        ns, st = r["eimer_nostitch"], r["eimer_stitch"]
        rows.append(f"| query | {p['n']:,} | {p['batches']} | {p['l_run']} | - | {st.get('final_count')} | {{{', '.join(t.replace('_', '') for t in r['pick'])}}} | {cell(r.get('mr'))} | "
                    f"{cell(ns)} | {cell(st)} | {ratio(r.get('mr'), st)} | {ratio(ns, st)} | {(ns.get('kleene_view_ms') or 0) / 1000:.2f} / {(st.get('kleene_view_ms') or 0) / 1000:.2f} | "
                    f"{g.get('results_identical')} | {pm(r.get('mr'))} / {pm(ns)} / {pm(st)} |")
    title = "# Trino experiment (Fig. 11, Kleene panels): Kleene view maintenance and the Kleene query on Trino"
    notes = ("Notes: \"one view\": the maximal runs of PATTERN (C+) maintained per batch by rescan (no stitch) and by the incremental stitch on the same "
             "table; the last columns give the total maintenance time of both and their ratio. \"query\": A Z* B Z*? C+ Z* D queried after every batch; "
             "runtimes are totals over all batches (EIMER: view updates + queries; MR: one query per batch over the accumulated table). DNF: native MR hit "
             "a time limit. Results are compared on full tuples: both maintenance forms against a single MATCH_RECOGNIZE (one view); MR and both EIMER "
             "arms on the first 1,000 events and on the full run where MR finishes (query).")
    return title, head, rows, notes, ck, recs


def lower_bound(arm) -> str:
    """an approach's runtime in seconds."""
    if not arm:
        return "-"
    if arm.get("status") == "ok":
        return cell(arm)
    if arm.get("status") == "dnf":
        return f">{arm['spent_ms'] / 1000:,.0f}" if arm.get("spent_ms") else "DNF"
    return str(arm.get("status"))


def speedup(mr, eimer) -> str:
    if not mr or not eimer or eimer.get("status") != "ok" or not eimer.get("total_ms"):
        return "-"
    if mr.get("status") == "ok":
        return f"{mr['total_ms'] / eimer['total_ms']:,.1f}x"
    if mr.get("status") == "dnf" and mr.get("spent_ms"):
        return f">{mr['spent_ms'] / eimer['total_ms']:,.1f}x"
    return "-"


def topology_memory(out: Path):
    strategy = {"singletons": "all singletons", "pick": "clique-grow's pick", "full_pattern": "full pattern", "prefixes": "all prefixes"}
    recs = sorted(load(out), key=lambda r: (0 if r["params"]["part"] == "topology" else 1, r["params"]["topo"], r["params"]["k"], r["params"]["sigma"]))
    ck = Checks()
    ck.ok(bool(recs), "no records")
    head = ("| part | workload | N | batches | strategy | views | native MR [s] | EIMER [s] | MR / EIMER | state [MB] | peak query memory [MB] | "
            "matches | results identical |")
    rows = []
    for r in recs:
        p, c, g = r["params"], r["cell"], r.get("gates", {})
        engine_checks(ck, r)
        ck.ok(g.get("results_identical") is not False, f"{c}: results differ ({g})")
        ck.ok(r["selection"].get("kappa") == 150.0, f"{c}: kappa {r['selection'].get('kappa')}")
        wl = f"{p['topo']} k={p['k']}, sigma={p['sigma']}, type rate {p['ftype']}"
        views = lambda vs: "{" + ", ".join(v.replace("_", "") for v in vs) + "}"
        if p["part"] == "topology":
            mr, e = r["mr"], r["eimer"]
            ck.ok(e.get("status") == "ok", f"{c}: EIMER did not finish ({e.get('status')})")
            ck.ok(mr.get("status") in ("ok", "dnf"), f"{c}: native MR ended with {mr.get('status')}: {mr.get('notes', '')[:120]}")
            ck.ok(g.get("n_compared", 0) >= 1 or mr.get("status") != "ok", f"{c}: no result comparison although native MR finished")
            ck.ok(r.get("eimer_sql", {}).get("compose_nested") is True or len(r.get("pick", [])) < 2, f"{c}: composition not emitted over the join tree")
            rows.append(f"| topology | {wl} | {p['n']:,} | {p['batches']} | {strategy['pick']} | {views(r['pick'])} | {cell(mr)} | {cell(e)} | {ratio(mr, e)} | "
                        f"{mb(e)} | {pm(e)} | {e.get('final_count')} | {g.get('results_identical')} |")
            continue
        ck.ok(g.get("singletons_finished") is True, f"{c}: the all-singleton strategy did not finish")
        ck.ok(g.get("n_compared", 0) >= 1, f"{c}: no result comparison")
        for s in r["strategies"]:
            ck.ok(s.get("status") in ("ok", "dnf"), f"{c}: {s['strategy']} ended with {s.get('status')}: {(s.get('notes') or '')[:120]}")
            if s.get("status") == "ok":
                ck.ok((s.get("state") or {}).get("rows") is not None, f"{c}: no state size for {s['strategy']}")
            rows.append(f"| memory | {wl} | {p['n']:,} | {p['batches']} | {strategy[s['strategy']]} | {views(s['views'])} | - | {cell(s)} | - | "
                        f"{mb(s) if s.get('status') == 'ok' else '-'} | {pm(s) if s.get('status') == 'ok' else '-'} | {s.get('final_count')} | "
                        f"{(s.get('gate') or {}).get('identical', '-')} |")
    title = "# Trino experiment (Section 7.4, query topology and memory footprint) on Trino"
    notes = ("Notes: topology: native MATCH_RECOGNIZE (the statement as written; the queries have no time window) against clique-grow's pick, "
             "totals over all batches. memory: four strategies on the chain; state = the Iceberg data files of the strategy's views at the end of the "
             "run, results compared with the all-singleton strategy; native MR is not run (it keeps no state and does not finish on Trino at table "
             "sizes at which the strategies differ). DNF (>s): the approach hit its limit after that many seconds. The paper's topology tables are larger "
             "(50,000 rows at type rate 0.01). Its state sizes are those of a row store, so the megabytes of the memory part are not comparable with "
             "the paper's; the order of the strategies is.")
    return title, head, rows, notes, ck, recs


def realworld(out: Path):
    order = {"taxi": 0, "chicago": 1, "ecommerce": 2}
    recs = sorted(load(out), key=lambda r: (order.get(r["dataset"], 9), r["query"]))
    ck = Checks()
    ck.ok(bool(recs), "no records")
    head = ("| table | query | pattern | N | batches | matches | pick | MR [s] | EIMER [s] | MR / EIMER | Kleene view: stitch / rescan [s] | window: rows, matches, MR == EIMER | "
            "MR == EIMER (full) | rescan == stitch | reference == EIMER | matches as in the paper | this run selects the paper's pick | peak query memory [MB]: MR / EIMER |")
    rows = []
    for r in recs:
        c, p, g, w = r["cell"], r["params"], r.get("gates", {}), r.get("window", {})
        engine_checks(ck, r)
        kl = bool(r.get("kleene_variables"))
        arms = ("mr", "eimer") + (("eimer_rescan",) if kl else ())
        for a in arms:
            ck.ok((r.get(a) or {}).get("status") in ("ok", "dnf"), f"{c}: {a} ended with {(r.get(a) or {}).get('status')}: {(r.get(a) or {}).get('notes', '')[:120]}")
        e = r.get("eimer") or {}
        if e.get("status") != "ok":
            ck.ok(e.get("status") == "dnf" and (e.get("spent_ms") or 0) >= r["limits"]["total_s"] * 1000.0, f"{c}: EIMER ended with {e.get('status')} before its limit")
        ck.ok(w.get("mr_status") == "ok" and w.get("eimer_status") == "ok", f"{c}: window: MR {w.get('mr_status')}, EIMER {w.get('eimer_status')} (no comparison)")
        ck.ok(w.get("mr_eq_eimer") is True, f"{c}: window: native MR and EIMER differ ({w.get('n_mr')} vs. {w.get('n_eimer')} matches)")
        ck.ok((w.get("n_eimer") or 0) >= 1, f"{c}: window: no match to compare")
        if kl:
            ck.ok(w.get("rescan_eq_eimer") is True, f"{c}: window: the Kleene view by rescan and by the stitch give different results")
            ck.ok(w.get("reference_eq_eimer") is True, f"{c}: window: EIMER differs from the reference computed from the table")
            if e.get("status") == "ok":
                ck.ok(g.get("rescan_eq_eimer") is True, f"{c}: the Kleene view by rescan and by the stitch give different results")
                ck.ok(g.get("reference_eq_eimer") is True, f"{c}: EIMER differs from the reference computed from the table")
        ck.ok(g.get("mr_eq_eimer") is not False, f"{c}: native MR and EIMER differ on the full table")
        ck.ok(g.get("results_identical") is True, f"{c}: results differ ({g})")
        ck.ok(g.get("matches_as_in_paper") is not False, f"{c}: {e.get('final_count')} matches (paper: {r.get('paper_matches')})")
        ck.ok(r.get("pick") == REF["realworld"][f"{r['dataset']}_{r['query']}"]["pick"], f"{c}: not the paper's pick")
        ck.ok(r["selection"].get("kappa") == 150.0, f"{c}: kappa {r['selection'].get('kappa')} (paper: 150)")
        if REF["realworld"][f"{r['dataset']}_{r['query']}"].get("selector_reproduces_pick", True):
            ck.ok(r["selection"].get("selector_reproduces_paper_pick") is True,
                  f"{c}: the selector gives {r['selection'].get('selector_on_paper_estimates')} from the paper's estimates (paper: {r['pick']})")
        ck.ok(r["selection"].get("pick_this_run") is not None, f"{c}: no selection of this run ({(r['selection'].get('estimates_this_run') or {}).get('error')})")
        if p["n"] == 100_000:
            ck.ok(r["selection"].get("this_run_selects_paper_pick") is True,
                  f"{c}: this run's selection {r['selection'].get('pick_this_run')} differs from the paper's pick {r['pick']}")
        mf = r.get("mr_form", {})
        ck.ok((mf.get("gap_bounded_by_window") is True and "<=" in (mf.get("gap_define") or "")) or r["dataset"] + r["query"] == "taxiQ2",
              f"{c}: native MR without the window on the gap variable")
        ck.ok(r.get("eimer_sql", {}).get("compose_nested") is True or len(r.get("pick", [])) < 2 or kl, f"{c}: composition not emitted over the join tree")
        rs = r.get("eimer_rescan") or {}
        rows.append(f"| {r['dataset']} | {r['query']} | `{r.get('pattern', '')}` | {p['n']:,} | {p['batches']} | {e.get('final_count')} | {{{', '.join(t.replace('_', '') for t in r['pick'])}}} | "
                    f"{lower_bound(r.get('mr'))} | {lower_bound(e)} | {speedup(r.get('mr'), e)} | "
                    + (f"{(e.get('kleene_view_ms') or 0) / 1000:.2f} / {(rs.get('kleene_view_ms') or 0) / 1000:.2f}" if kl else "-")
                    + f" | {w.get('rows', ['?', '?'])[0]:,}-{w.get('rows', ['?', '?'])[1]:,}, {w.get('n_eimer')}, {w.get('mr_eq_eimer')} | {g.get('mr_eq_eimer')} | "
                    f"{g.get('rescan_eq_eimer') if kl else '-'} | {g.get('reference_eq_eimer') if kl else '-'} | {g.get('matches_as_in_paper')} | "
                    f"{r['selection'].get('this_run_selects_paper_pick') if p['n'] == 100_000 else 'n/a below the paper size'} | "
                    f"{pm(r.get('mr'))} / {pm(e)} |")
    title = "# Trino experiment (Fig. 12): native MR and EIMER on real-world tables"
    notes = ("Notes: runtimes are totals over all batches (EIMER: view updates + queries; MR: one query per batch over the accumulated table). \">\": the "
             "approach hit its limit; the value is the time it had spent, a lower bound. EIMER runs the strategy the paper's run selected, built from the "
             "paper's selectivity estimates. Results are compared on full tuples (the ids of all variables; for a Kleene variable the first and last id "
             "and the count of its run): on a small window of the table, where native MR finishes, and on the full table where native MR finishes. The two "
             "queries with a Kleene variable (taxi Q2, e-commerce Q4) maintain its view incrementally (stitch); the same strategy with the view maintained "
             "by rescan must return the same result, and both are compared with a reference computed directly from the table in Python. \"matches as in "
             "the paper\": the number of matches equals the paper's (None below the paper's table size). "
             "\"this run selects the paper's pick\": the selectivities estimated on the loaded table "
             "(at the paper's table size) lead clique-grow to the strategy of the paper's run; the executed strategy is the paper's in every case.")
    return title, head, rows, notes, ck, recs


def _mb_or_rows(b, rows) -> str:
    return f"{b / 1e6:.3f}" if b is not None else (f"{rows:,} rows" if rows is not None else "-")


def budget_shift(out: Path):
    recs = sorted(load(out), key=lambda r: (r["params"]["part"], r["cell"]))
    ck = Checks()
    ck.ok(bool(recs), "no records")
    head = "| part | workload | N | batches | plan | predicted state [MB] | measured state [MB] | EIMER [s] | native MR [s] | MR / EIMER | results identical | peak query memory [MB]: EIMER / MR |"
    rows = []
    for r in recs:
        p, c, g = r["params"], r["cell"], r.get("gates", {})
        engine_checks(ck, r)
        ck.ok(g.get("results_identical") is not False, f"{c}: results differ ({g})")
        ck.ok(g.get("n_compared", 0) >= 1, f"{c}: no result comparison")
        ck.ok(r["selection"].get("kappa") == 150.0, f"{c}: kappa {r['selection'].get('kappa')}")
        mr = r["mr"]
        ck.ok(mr.get("status") in ("ok", "dnf"), f"{c}: native MR ended with {mr.get('status')}: {mr.get('notes', '')[:120]}")
        if p["part"] == "budget":
            ck.ok(g.get("descent_equals_selector") is True, f"{c}: the descent differs from the selector's decisions under the budgets")
            ck.ok(g.get("all_plans_finished") is True, f"{c}: a plan of the descent did not finish")
            wl = f"{p['topo']} k={p['k']}, sigma={p['sigma']}"
            for s in r["steps"]:
                arm = dict(status=s["status"], total_ms=s.get("measured_ms"))
                rows.append(f"| budget | {wl} | {p['n']:,} | {p['batches']} | {{{', '.join(v.replace('_', '') for v in s['views'])}}}{' = all singletons' if s['plan'] == '(empty)' else ''} | "
                            f"{s['predicted_peak_bytes'] / 1e6:.3f} | {_mb_or_rows(s.get('measured_peak_bytes'), s.get('state_rows'))} | {cell(arm)} | {cell(mr)} | {ratio(mr, arm)} | "
                            f"{(s.get('gate') or {}).get('identical', '-')} | {pm(s)} / {pm(mr)} |")
            continue
        ck.ok(g.get("all_arms_finished") is True, f"{c}: an EIMER arm did not finish")
        wl = f"{p['topo']} k={p['k']}, shift after batch {p['shift_after_batch']}"
        plans = {"stale": r["plan_before_shift"], "instant": r["plan_after_reselection"], "delayed": r["plan_after_reselection"]}
        for a in ("stale", "instant", "delayed"):
            arm = r["arms"][a]
            label = {"stale": "stale", "instant": f"instant switch (batch {arm.get('switch_batch')})", "delayed": f"delayed switch (batch {arm.get('switch_batch')})"}[a]
            rows.append(f"| shift, {label} | {wl} | {p['n']:,} | {p['batches']} | {{{', '.join(v.replace('_', '') for v in plans[a])}}} | - | "
                        f"{_mb_or_rows((arm.get('state') or {}).get('bytes'), (arm.get('state') or {}).get('rows'))} | {cell(arm)} | {cell(mr)} | {ratio(mr, arm)} | {(arm.get('gate') or {}).get('identical', '-')} | {pm(arm)} / {pm(mr)} |")
    title = "# Trino experiment (Fig. 11, storage budget and distribution shift) on Trino"
    notes = ("Notes: budget: per workload the plans of the budget descent (the unconstrained pick first, then one view dropped per step, last the "
             "all-singleton strategy), each executed; predicted state = the selector's estimate that is compared with the budget M, measured state = "
             "the Iceberg data files of the plan's views at the end of the run. shift: the plan selected before the shift kept (stale) and the plan "
             "re-selected after it, switched to right at the shift (instant) or four batches later (delayed); the switch rebuilds the new views over "
             "the history. Runtimes are totals over all batches; DNF (>s): native MR hit its limit after that many seconds, results are then compared "
             "among the EIMER plans. The paper's tables for these workloads are larger (100,000 to 200,000 rows).")
    return title, head, rows, notes, ck, recs


def main() -> int:
    which, out = sys.argv[1], Path(sys.argv[2])
    title, head, rows, notes, ck, recs = {"fig8": fig8, "fig9": fig9, "fig10": fig10, "kleene": kleene, "budget_shift": budget_shift, "topology_memory": topology_memory,
                                         "realworld": realworld}[which](out)
    have = {r["cell"] for r in recs}
    for c in os.environ.get("EIMER_REPRO_EXPECTED", "").split():
        ck.ok(c in have, f"{c}: no record (the workload failed, see logs/{which}.{c}.log)")
    sep = "|" + "---|" * (head.count("|") - 1)
    verdict = "ALL CHECKS PASSED" if not ck.failed else f"{len(ck.failed)} CHECK(S) FAILED"
    if any(r.get("catalog") == "memory" for r in recs):
        title += " [in-memory tables]"
        notes += (" This run used Trino's in-memory tables instead of the paper's Iceberg tables: no statement pays a storage commit, tables are "
                  "emptied with TRUNCATE (the connector has no DELETE), dynamic filtering is off, and state sizes are given in rows.")
    notes += (" peak query memory: the largest peak memory Trino reported for one statement of the approach (its transient query memory; '-' where Trino "
              "reported none).")
    lines = [title, "", head, sep, *rows, "", f"Checks: {ck.passed} passed, {len(ck.failed)} failed. **{verdict}**", ""]
    lines += [f"- FAILED: {f}" for f in ck.failed] + ([""] if ck.failed else []) + [notes, ""]
    text = "\n".join(lines)
    print(text)
    (out / "summary.md").write_text(text)
    (out / "summary.json").write_text(json.dumps(dict(experiment=f"trino_{which}", passed=ck.passed, failed=ck.failed, n_records=len(recs)), indent=1))
    return 0 if not ck.failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
