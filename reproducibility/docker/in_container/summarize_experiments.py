#!/usr/bin/env python3
"""Summary and checks of a run of the DuckDB or the Esper experiment:  summarize_experiments.py duckdb|esper <out dir>

Prints one row per cell (runtimes, speed-up, memory, matches, selected strategy), writes <out dir>/summary.md and
<out dir>/summary.json, and exits with 0 only if every check passes:
  correctness    the approaches of a cell return the same result, compared on full tuples
  configuration  the run used the configuration of the paper's runs (kappa, selection rule, bushy composition tree,
                 pinned join order for EIMER, optimizer for the baseline, window stated on the gap variable, ...)
  paper          for cells the paper reports: the same selected strategy and the same number of matches as in the
                 paper's run (reproducibility/reference/paper_duckdb_esper.json). Runtimes and memory are shown
                 next to the paper's values.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPRO = Path(__file__).resolve().parents[2]
REF = json.loads((REPRO / "reference" / "paper_duckdb_esper.json").read_text())


def load(root: Path) -> dict:
    return {p.stem: json.loads(p.read_text()) for p in sorted((root / "records").glob("*.json"))} if (root / "records").is_dir() else {}


def s(ms) -> str:
    return "DNF" if ms is None else (f"{ms / 1000:.2f}" if ms < 100_000 else f"{ms / 1000:,.0f}")


def num(v, nd=1) -> str:
    return "-" if v is None else f"{v:.{nd}f}"


class Checks:
    def __init__(self):
        self.failed, self.passed = [], 0

    def ok(self, cond, what: str) -> bool:
        if cond:
            self.passed += 1
        else:
            self.failed.append(what)
        return bool(cond)


def duckdb(out: Path) -> int:
    rt, pk = load(out / "runtime"), load(out / "peakmem")
    ck = Checks(); rows = []; ref = REF["duckdb"]
    ck.ok(bool(rt), "no runtime records")
    order = sorted(rt.values(), key=lambda r: (0 if "N1000" in r["params"]["axes"] else 1, r["params"]["n"], r["params"]["k"]))
    for r in order:
        c, p = r["cell"], r["params"]; t, e = r["transpiled"], r["eimer"]; m = pk.get(c, {})
        both = t["status"] == "ok" and e["status"] == "ok"
        ck.ok(t["status"] in ("ok", "dnf") and e["status"] in ("ok", "dnf"), f"{c}: an approach ended with an error")
        if both:
            ck.ok(r.get("in_engine_identity") is True, f"{c}: EIMER's result differs from the transpiled query's")
        ck.ok(r.get("kappa") == 49.0, f"{c}: kappa {r.get('kappa')} (paper: 49)")
        sel = r.get("selector", {}); ex = r.get("execution", {})
        ck.ok(not sel.get("full_anchor") and sel.get("rule") == "explicit graph", f"{c}: selector {sel}")
        ck.ok(ex.get("compose_tree") is True and ex.get("join_order") == "pinned", f"{c}: EIMER execution {ex}")
        ck.ok(r.get("settings_eimer", {}).get("disabled_optimizers") == "join_order" and r.get("settings_eimer", {}).get("threads") == "1", f"{c}: EIMER connection settings {r.get('settings_eimer')}")
        ck.ok(r.get("settings", {}).get("disabled_optimizers") == "" and r.get("settings", {}).get("threads") == "1", f"{c}: baseline connection settings {r.get('settings')}")
        ck.ok(r.get("baseline", {}).get("gap_bounded_by_window") is True, f"{c}: the baseline query does not state the window on the gap variable")
        ck.ok(r.get("engine") == "duckdb 1.5.5", f"{c}: engine {r.get('engine')} (paper: duckdb 1.5.5)")
        nested = (r.get("compose_sql") or {}).get("eimer", {}).get("nested")
        for arm in ("transpiled", "eimer"):
            if m.get(arm, {}).get("status") == "ok":
                ck.ok(m[arm].get("identity_vs_runtime") is True, f"{c}: the {arm} result of the memory run differs from the runtime run")
        pr = ref.get(c); same = "-"
        if pr:
            a = ck.ok(r["pick"]["cover"] == pr["pick"], f"{c}: selected strategy {r['pick']['cover']} (paper: {pr['pick']})")
            n_here = e.get("final_count") if e["status"] == "ok" else t.get("final_count")
            b = ck.ok(n_here is None or pr["matches"] is None or n_here == pr["matches"], f"{c}: {n_here} matches (paper: {pr['matches']})")
            d = ck.ok(nested == pr["eimer"]["nested_composition"], f"{c}: nested composition {nested} (paper: {pr['eimer']['nested_composition']})") if e["status"] == "ok" else True
            same = "yes" if (a and b and d) else "NO"
        q = None
        if e.get("per_batch_ms"):
            up = sum(x[1] for x in e["per_batch_ms"]); co = sum(x[2] for x in e["per_batch_ms"]); q = 100.0 * co / (up + co) if (up + co) else None
        rows.append(dict(cell=c, n=p["n"], batches=p["batches"], k=p["k"], axis="N" if "N1000" in p["axes"] else "k",
                         matches=e.get("final_count") if e["status"] == "ok" else t.get("final_count"), strategy="{" + ", ".join(x.replace("_", "") for x in r["pick"]["cover"]) + "}",
                         transpiled_s=s(t.get("total_ms")), eimer_s=s(e.get("total_ms")), speedup=r.get("speedup_eimer_vs_transpiled"), query_share=q,
                         mem_transpiled=(m.get("transpiled") or {}).get("transient_mb"), mem_eimer=(m.get("eimer") or {}).get("transient_mb"),
                         identical=r.get("in_engine_identity"), nested=nested, as_paper=same,
                         paper_transpiled_s=(pr or {}).get("transpiled", {}).get("total_s"), paper_eimer_s=(pr or {}).get("eimer", {}).get("total_s")))
    head = "| panel | N | B | k | matches | strategy | transpiled MR [s] | EIMER [s] | speed-up | query share | mem. transpiled [MB] | mem. EIMER [MB] | results identical | bushy tree nested | as in the paper | paper: transpiled / EIMER [s] |"
    lines = [head, "|" + "---|" * 16]
    for x in rows:
        lines.append(f"| {x['axis']} | {x['n']:,} | {x['batches']} | {x['k']} | {x['matches']} | {x['strategy']} | {x['transpiled_s']} | {x['eimer_s']} | "
                     f"{('%.1fx' % x['speedup']) if x['speedup'] else '-'} | {('%.0f%%' % x['query_share']) if x['query_share'] is not None else '-'} | {num(x['mem_transpiled'])} | {num(x['mem_eimer'])} | "
                     f"{x['identical']} | {x['nested']} | {x['as_paper']} | {x['paper_transpiled_s'] if x['paper_transpiled_s'] is not None else ('DNF' if x['as_paper'] != '-' else '-')} / {x['paper_eimer_s'] if x['paper_eimer_s'] is not None else '-'} |")
    return finish(out, "DuckDB experiment (Fig. 8b): EIMER on DuckDB vs. the transpiled MATCH_RECOGNIZE query", lines, rows, ck)


ESPER_ARMS = ("esper_native", "eimer_esper", "eimer_esper_edge", "eimer_esper_prefix", "eimer_esper_singletons")
ARM_LABEL = {"esper_native": "Esper MR", "eimer_esper": "Clique-Grow", "eimer_esper_edge": "Edge Cover", "eimer_esper_prefix": "Prefixes", "eimer_esper_singletons": "Singletons"}


def esper(out: Path) -> int:
    ck = Checks(); rows = []; lines = []
    for center, root, axis in (("n_panel", out / "n_panel", "N"), ("k_panel", out / "k_panel", "k")):
        rt, mem, base = load(root), load(Path(str(root) + "_mem")), load(Path(str(root) + "_jvmbase"))
        ck.ok(bool(rt), f"{center}: no runtime records")
        ref = REF["esper"][center]
        for r in sorted(rt.values(), key=lambda r: (r["params"]["n"], r["params"]["k"])):
            c, p = r["cell"], r["params"]
            ck.ok(r.get("kappa") == 240.0, f"{c}: kappa {r.get('kappa')} (paper: 240)")
            ck.ok(r.get("native_form") == "bounded", f"{c}: Esper MR form {r.get('native_form')} (paper: bounded)")
            ck.ok(r.get("update_form") == "chain" and r.get("eimer_render") == "diff", f"{c}: update form {r.get('update_form')} / render {r.get('eimer_render')}")
            ck.ok(r.get("arms") == ["pick", "edge", "prefix", "singletons"], f"{c}: EIMER arms {r.get('arms')}")
            ck.ok((r.get("selector") or {}).get("rule") == "explicit graph", f"{c}: selector {r.get('selector')}")
            ck.ok(r.get("engine_esper") in (None, "9.0.0") and r.get("engine_duckdb") == "duckdb 1.5.5", f"{c}: engines {r.get('engine_esper')} / {r.get('engine_duckdb')}")
            ck.ok(r.get("vacuous") is False, f"{c}: the query has no match on this table")
            ok_arms = [a for a in ESPER_ARMS if (r.get(a) or {}).get("status") == "ok"]
            for a in ESPER_ARMS:
                x = r.get(a) or {}
                ck.ok(x.get("status") in ("ok", "dnf", "same_as_pick"), f"{c}: {ARM_LABEL[a]} ended with {x.get('status')}: {(x.get('notes') or '')[:160]}")
                if x.get("status") == "ok":
                    ck.ok(x.get("identity_final") is True and x.get("identity_counts") is True, f"{c}: {ARM_LABEL[a]} differs from the reference result")
            if ok_arms:
                ck.ok(r.get("identity_arms") is True, f"{c}: the Esper approaches differ from each other")
            pr = ref.get(c); same = "-"
            if pr:
                a1 = ck.ok(r["pick"]["cover"] == pr["pick"], f"{c}: selected strategy {r['pick']['cover']} (paper: {pr['pick']})")
                a2 = ck.ok(r.get("data_sha256_16") == pr["data_sha256_16"], f"{c}: the generated table differs from the paper's run")
                a3 = ck.ok((r.get("eimer_duckdb") or {}).get("final_count") == pr["matches"], f"{c}: {(r.get('eimer_duckdb') or {}).get('final_count')} matches (paper: {pr['matches']})")
                a4 = all(ck.ok((r.get(a) or {}).get("cover") == pr["arms"][a]["cover"], f"{c}: {ARM_LABEL[a]} strategy {(r.get(a) or {}).get('cover')} (paper: {pr['arms'][a]['cover']})")
                         for a in ESPER_ARMS[1:] if pr["arms"].get(a, {}).get("cover") is not None and (r.get(a) or {}).get("status") in ("ok", "dnf"))
                same = "yes" if (a1 and a2 and a3 and a4) else "NO"
            b = (base.get(c) or {}).get("peak_live_heap_mb") or 0.0
            row = dict(cell=c, panel=axis, n=p["n"], batches=p["batches"], k=p["k"], matches=(r.get("eimer_duckdb") or {}).get("final_count"),
                       pick="{" + ", ".join(x.replace("_", "") for x in r["pick"]["cover"]) + "}", identical=r.get("identity_arms"), as_paper=same, arms={})
            for a in ESPER_ARMS:
                x = r.get(a) or {}; mm = (mem.get(c) or {}).get(a) or {}
                if x.get("status") == "same_as_pick":
                    x = r["eimer_esper"]; mm = (mem.get(c) or {}).get("eimer_esper") or {}; note = " (= pick)"
                else:
                    note = ""
                live = max(mm.get("peak_live_heap_mb") or 0.0, mm.get("final_live_heap_mb") or 0.0) if mm.get("status") == "ok" else None
                row["arms"][a] = dict(runtime=s(x.get("total_ms")) + note if x.get("status") in ("ok", "dnf") else str(x.get("status")),
                                      memory_mb=(max(live - b, 1.0) if live else None), paper_s=((pr or {}).get("arms", {}).get(a) or {}).get("total_s"))
            rows.append(row)
    head = "| panel | N | B | k | matches | pick | " + " | ".join(f"{ARM_LABEL[a]} [s]" for a in ESPER_ARMS) + " | memory [MB] (same order) | results identical | as in the paper | paper [s] (same order) |"
    lines = [head, "|" + "---|" * (6 + len(ESPER_ARMS) + 4)]
    for x in rows:
        lines.append(f"| {x['panel']} | {x['n']:,} | {x['batches']} | {x['k']} | {x['matches']} | {x['pick']} | " + " | ".join(x["arms"][a]["runtime"] for a in ESPER_ARMS)
                     + " | " + " / ".join(num(x["arms"][a]["memory_mb"]) for a in ESPER_ARMS) + f" | {x['identical']} | {x['as_paper']} | "
                     + " / ".join(("-" if x["arms"][a]["paper_s"] is None else str(x["arms"][a]["paper_s"])) for a in ESPER_ARMS) + " |")
    return finish(out, "Esper experiment (Fig. 8c): EIMER's strategies on Esper vs. Esper's native match_recognize", lines, rows, ck)


def finish(out: Path, title: str, lines: list, rows: list, ck: Checks) -> int:
    verdict = "ALL CHECKS PASSED" if not ck.failed else f"{len(ck.failed)} CHECK(S) FAILED"
    text = [f"# {title}", "", *lines, "", f"Checks: {ck.passed} passed, {len(ck.failed)} failed. **{verdict}**", ""]
    if ck.failed:
        text += ["Failed checks:", *[f"- {f}" for f in ck.failed], ""]
    text += ["Notes: runtimes are totals over all batches (view updates + queries for EIMER; one query per batch for the baseline). "
             "Memory is the measured peak above the loaded table. \"as in the paper\" compares the selected strategy and the number of matches "
             "(DuckDB: also whether the composition is emitted as a nested, bushy join tree; Esper: also the generated table and every strategy) "
             "with the paper's run of the same cell; \"-\" = the paper has no such cell. The paper's runtimes were measured on other hardware and are given for orientation."]
    (out / "summary.md").write_text("\n".join(text) + "\n")
    (out / "summary.json").write_text(json.dumps(dict(title=title, verdict=verdict, passed=ck.passed, failed=ck.failed, rows=rows), indent=1))
    print("\n".join(text))
    return 0 if not ck.failed else 1


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("duckdb", "esper"):
        print(__doc__); raise SystemExit(2)
    raise SystemExit(duckdb(Path(sys.argv[2])) if sys.argv[1] == "duckdb" else esper(Path(sys.argv[2])))
