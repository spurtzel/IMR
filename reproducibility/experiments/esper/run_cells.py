#!/usr/bin/env python3
"""The Esper experiment per cell: generate the table, select EIMER's materialization strategy on DuckDB (selectivity
estimates on the loaded table, kappa of the paper's runs for Esper, window propagation), measure the reference arm
EIMER-on-DuckDB, render the strategies as EPL (backends/esper/epl_emitter.py), then run the JVM arms through the Java program
of backends/esper, one JVM per arm:
  esper_native            Esper's on-demand match_recognize: all matches + the SQL standard's selection
                          (NATIVE_FORM=bounded: the window is also stated on the gap variable, the form of the paper's figure)
  eimer_esper             EIMER on clique-grow's pick
  eimer_esper_edge        EIMER on the edge cover (one view per dependent condition)
  eimer_esper_prefix      EIMER on all pattern prefixes
  eimer_esper_singletons  EIMER without multi-variable views
An arm whose cover equals the pick is recorded as same_as_pick, not run twice.
Gates: every arm's final full-tuple set and per-batch match counts against EIMER-on-DuckDB (an independent engine), and
the Esper arms against each other. Resume-safe.
Usage: run_cells.py [--only a,b] [--force] [--results-dir results]

Memory stage: ESPER_MEASURE_MEMORY=1 measures, per arm, the JVM's peak live heap (forced collections at batch
boundaries and at the end) and peak RSS; ESPER_SKIP_DNF_FROM=<runtime results> copies the outcome of arms that did not
finish in the runtime run instead of re-executing them.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cells  # noqa: E402  (puts the repository on sys.path)
from backends.esper import epl_emitter  # noqa: E402
import duckdb  # noqa: E402
import duckdb_exec  # noqa: E402
import eimer_arm  # noqa: E402
from eimer.selection.plan_selection_clique_grow import select_clique_grow_plan  # noqa: E402
from eimer.selection.plan_selection_edge_cover import select_edge_cover_plan  # noqa: E402
from eimer.sql.backend import SqlDialect, backend_for  # noqa: E402

JAR = Path(os.environ.get("ESPER_JAR", cells.EIMER_REPO / "backends" / "esper" / "java" / "target" / "esper-eimer.jar"))
JVM = [os.environ.get("JAVA", "java"), "-Xmx12g", "-XX:+UseSerialGC", "-XX:ActiveProcessorCount=1"]
MEMORY_LIMIT = "12GB"
# kappa: the engine constant of the paper's runs for Esper (kappa_esper.json).
# NATIVE_FORM: "bounded" (the window also on the gap variable; the paper's figure) or "paper" (declared predicates only).
KAPPA_FILE = Path(os.environ.get("KAPPA_FILE", HERE / "kappa_esper.json"))
NATIVE_FORM = os.environ.get("NATIVE_FORM", "bounded")
EIMER_RENDER = os.environ.get("EIMER_RENDER", "diff")   # EPL rendering of the EIMER statements (epl_emitter.render_plan)
UPDATE_FORM = os.environ.get("UPDATE_FORM", "chain")   # multi-source view updates as a chain of two-way joins
# EIMER arms on Esper besides the pick: "edge" = the edge-cover selector, "prefix" = all pattern prefixes, "singletons" = no
# multi-variable view (the compose chain over the history singletons).
ESPER_ARMS = [a for a in os.environ.get("ESPER_ARMS", "pick,edge,prefix,singletons").split(",") if a]
assert ESPER_ARMS[0] == "pick" and all(a in ("pick", "star", "edge", "prefix", "singletons") for a in ESPER_ARMS), ESPER_ARMS
def kappa() -> float:
    return float(json.loads(KAPPA_FILE.read_text())["kappa"])
COLUMNS = {"id": "long", "ts": "long", "primary_type": "string", "lon": "double", "lat": "double"}
RESULTS = HERE / "results"


def export_data(frame, c: dict, d: Path) -> None:
    import pandas as pd
    d.mkdir(parents=True, exist_ok=True)
    f = frame.sort_values("id")
    ms = (f["ts"] - pd.Timestamp("1970-01-01")) // pd.Timedelta(milliseconds=1)
    f.assign(ts=ms)[list(COLUMNS)].to_csv(d / "data.csv", index=False)
    N, B = c["n"], c["batches"]
    (d / "batches.json").write_text(json.dumps([{"b": b, "lo": ((b - 1) * N) // B, "hi": (b * N) // B} for b in range(1, B + 1)]))


def reference_counts(client, dep, toks, wl, spec, N, B) -> list[int]:
    """per-batch match counts of a cover on DuckDB (untimed): the compose over the prefixes."""
    st = eimer_arm.cover_statements(dep, toks, set(dep.variables), wl, dialect=SqlDialect.DUCKDB)
    result_q = (getattr(st, "post_filter_sql", "") or st.composition_sql).rstrip().rstrip(";")
    cache_sel = {re.search(r"INSERT\s+INTO\s+(cache_\w+)", u, re.I).group(1): re.sub(r"INSERT\s+INTO\s+cache_\w+\s*", "", u, flags=re.I).rstrip().rstrip(";")
                 for u in st.update_statements}
    for t in ("hist_bp", "bat_bp"):
        client.execute(f"DROP TABLE IF EXISTS {t}"); client.execute(f"CREATE TABLE {t} AS SELECT * FROM events WITH NO DATA")
    for cache, sel in cache_sel.items():
        client.execute(f"DROP TABLE IF EXISTS {cache}"); client.execute(f"CREATE TABLE {cache} AS {sel}")
    counts = []
    for b in range(1, B + 1):
        lo, hi = ((b - 1) * N) // B, (b * N) // B
        client.execute("DROP TABLE bat_bp"); client.execute(f"CREATE TABLE bat_bp AS SELECT * FROM events WHERE id > {lo} AND id <= {hi}")
        client.execute("INSERT INTO hist_bp SELECT * FROM bat_bp")
        for cache, sel in cache_sel.items():
            client.execute(f"INSERT INTO {cache} {sel}")
        counts.append(int(client.execute(f"SELECT count(*) FROM ({result_q})")[1][0][0]))
    for t in ["hist_bp", "bat_bp"] + list(cache_sel):
        client.execute(f"DROP TABLE IF EXISTS {t}")
    return counts


MEASURE_MEMORY = os.environ.get("ESPER_MEASURE_MEMORY", "0") == "1"   # per-arm JVM peak live heap + peak RSS
GNU_TIME = "/usr/bin/time"


def _parse_gc_log(path: Path) -> dict:
    """-Xlog:gc lines 'GC(n) Pause ... 310M->45M(1024M) 12.3ms': peak heap used after a collection (the live set), peak
    before one, and the number of collections."""
    import re
    rx = re.compile(r"(\d+)([KMG])->(\d+)([KMG])\((\d+)([KMG])\)")
    unit = {"K": 1 / 1024.0, "M": 1.0, "G": 1024.0}
    before, after, n = 0.0, 0.0, 0
    for line in path.read_text(errors="replace").splitlines() if path.exists() else []:
        m = rx.search(line)
        if m and "Pause" in line:
            n += 1; before = max(before, float(m.group(1)) * unit[m.group(2)]); after = max(after, float(m.group(3)) * unit[m.group(4)])
    return dict(peak_live_heap_mb=round(after, 1), peak_heap_before_gc_mb=round(before, 1), gc_count=n)


def run_jvm(data_dir: Path, plan_path: Path, arm: str, out: Path, T: int) -> dict:
    cmd = JVM + ["-jar", str(JAR), "--data", str(data_dir), "--plan", str(plan_path), "--arm", arm, "--out", str(out),
                 "--wall-s", str(T), "--total-wall-s", str(T)]
    rss_file, gc_log = out.with_suffix(".rss"), out.with_suffix(".gc.log")
    if MEASURE_MEMORY:   # one JVM per arm already: GNU time's %M = the JVM's peak RSS (KB); -Xlog:gc = heap occupancy after every GC
        cmd = [GNU_TIME, "-f", "%M", "-o", str(rss_file)] + JVM + [f"-Xlog:gc:file={gc_log}"] + cmd[len(JVM):] + ["--final-gc", "1", "--gc-samples", os.environ.get("ESPER_GC_SAMPLES", "25")]   # live set at ~25 batch boundaries + the end
        rss_file.unlink(missing_ok=True); gc_log.unlink(missing_ok=True)
    t0 = time.time()
    out.unlink(missing_ok=True)  # never read a record of an earlier attempt as this run's result
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=T + 900)
        rc, err = p.returncode, p.stderr
    except subprocess.TimeoutExpired as e:  # the watchdog should have written a dnf record; censor regardless
        se = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        rc, err = -9, f"subprocess timeout after {T + 900} s: {se[-1000:]}"
    rec = json.loads(out.read_text()) if out.exists() else {"status": "dnf" if rc == -9 else "error", "total_ms": None, "per_batch": [], "notes": ""}
    rec["wall_clock_s"] = round(time.time() - t0, 1)
    if MEASURE_MEMORY:
        try:
            rec["peak_rss_mb"] = round(int(rss_file.read_text().strip().splitlines()[-1]) / 1024.0, 1) if rss_file.exists() else None
        except ValueError:
            rec["peak_rss_mb"] = None
        rec.update(_parse_gc_log(gc_log))
        rec["peak_live_heap_after_gc_log_mb"] = rec.get("peak_live_heap_mb")   # after any natural collection (young collections include promoted garbage: an upper bound)
        rec["peak_live_heap_mb"] = round(max(rec.get("peak_live_heap_sampled_mb") or 0.0, rec.get("final_live_heap_mb") or 0.0), 1)   # peak over the forced full collections (batch samples + end)
        rec["memory_measured"] = "live heap = max over the forced full collections (batch samples, --gc-samples, and the final one, --final-gc); peak RSS = GNU time %M (JVM footprint, initial heap 2 GB: not a working-set measure)"
    if rc not in (0, 3):  # 3 = the watchdog halted the JVM after writing the dnf record
        rec["notes"] = (rec.get("notes") or "") + f"\njvm exit {rc}: {err[-1500:]}"
    if rec.get("status") == "error" and "OutOfMemoryError" in (rec.get("notes") or ""):
        # the 12 GB heap is the memory cap: exhausting it is a resource DNF censored like the time limit (as the DuckDB
        # experiment's memory limit), not an error
        rec["status"] = "dnf"; rec["notes"] = "resource DNF: Java heap (12 GB) exhausted. " + (rec.get("notes") or "")
    if rec.get("status") == "error":
        print(f"   !! {arm} arm ERROR (not a DNF): {(rec.get('notes') or '')[:300]}", flush=True)
    return rec


def run_cell(c: dict, force: bool = False) -> dict:
    name = cells.cell_name(c)
    rec_path = RESULTS / "records" / f"{name}.json"
    if rec_path.exists() and not force:
        print(f"skip {name} (record exists)"); return json.loads(rec_path.read_text())
    T = cells.wall_seconds(c); KAPPA_ = kappa()
    spec, dep, spec_path = cells.load_spec(c["k"], c["w_ms"])
    all_vars, pos = set(dep.variables), dep.positions
    vs = sorted(dep.variables, key=lambda v: pos[v]); key_cols = [f"{v}_id" for v in vs]
    qb = frozenset(range(1, c["batches"] + 1))
    frame = cells.generate_frame(c)
    cell_dir = RESULTS / "cells" / name
    export_data(frame, c, cell_dir)
    import hashlib
    _ts = frame.sort_values("id")["ts"]
    assert _ts.is_monotonic_increasing and _ts.is_unique, "ts must be strictly increasing in id (the EPL orders by ts, MR by row)"
    data_sha = hashlib.sha256((cell_dir / "data.csv").read_bytes()).hexdigest()[:16]
    client = duckdb_exec.connect(timeout_s=T, memory_limit=MEMORY_LIMIT)
    # DuckDB spills to ".tmp" in the working directory: every process gets its own temp directory
    _tmp = RESULTS / "tmp" / f"{name}.{os.getpid()}"; _tmp.mkdir(parents=True, exist_ok=True)
    client.execute(f"SET temp_directory='{_tmp}'")
    assert duckdb_exec.load_frame(client, frame, table="events") == c["n"]
    t_sel = time.time()  # one-time costs, outside every timed region, reported separately
    wl, payload = duckdb_exec.b_unified_workload(client, dep, table="events", total_events=c["n"], batches=c["batches"])
    t_est = time.time() - t_sel
    pick = select_clique_grow_plan(dep, wl, query_batches=qb, kappa=KAPPA_)
    t_pick = time.time() - t_sel - t_est
    toks = sorted("_".join(sorted(v, key=lambda x: pos[x])) for v in pick.compose)
    star = [f"{vs[0]}_{v}" for v in vs[1:]]
    tok = lambda cover: sorted("_".join(sorted(v, key=lambda x: pos[x])) for v in cover)
    covers = {"pick": toks, "star": star,
              "edge": tok(select_edge_cover_plan(dep, wl, query_batches=qb).compose),
              "prefix": ["_".join(vs[:i]) for i in range(2, len(vs) + 1)],
              "singletons": list(vs)}
    covers = {a: covers[a] for a in ESPER_ARMS}
    rec = dict(cell=name, params=c, center=os.environ.get('ESPER_CENTER', 'fig8s005'), wall_s=T, spec=spec_path.name, engine_duckdb=f"duckdb {duckdb.__version__}",
               warmup=False, kappa=KAPPA_, kappa_provenance=str(KAPPA_FILE),
               estimation_s=round(t_est, 3), plan_selection_s=round(t_pick, 3), native_form=NATIVE_FORM, eimer_render=EIMER_RENDER, update_form=UPDATE_FORM, jvm=JVM,
               window_rule=backend_for(SqlDialect.DUCKDB).window_rule, prefix_prune=backend_for(SqlDialect.DUCKDB).batch_window_prefix_prune,
               selector=dict(algorithm="clique-grow", rule=("propagated graph" if os.environ.get("EIMER_SELECT_ON_DERIVED") else "explicit graph")),
               selectivities=payload, pick=dict(cover=toks, certificate={k: (round(v, 6) if isinstance(v, float) else v) for k, v in pick.certificate.items()}),
               star=star, covers=covers, arms=ESPER_ARMS, data_sha256_16=data_sha)
    print(f"[{name}] N={c['n']} B={c['batches']} W={c['w_ms']}ms k={c['k']} pick={toks} (kappa={KAPPA_}) wall={T}s", flush=True)

    # reference arm: EIMER on DuckDB (timed as in the DuckDB experiment) + per-batch counts (untimed)
    ms, n, tuples, state = duckdb_exec.eimer_streaming(client, dep, toks, all_vars, wl, spec, "events", c["n"], c["batches"], qb, key_cols, T,
                                                       measure_state=True, warmup=False, total_wall_s=T)
    ref_tuples = sorted(map(list, tuples)) if tuples is not None else None
    try:
        ref_counts = reference_counts(client, dep, toks, wl, spec, c["n"], c["batches"]) if ms is not None else None
    except Exception as e:  # untimed cross-check only: without it the count gate is tri-state None, the cell goes on
        print(f"   reference_counts failed ({str(e)[:120]}): per-batch count gate unavailable", flush=True); ref_counts = None
    rec["eimer_duckdb"] = dict(status="ok" if ms is not None else "dnf", total_ms=ms, final_count=n, cover=toks,
                               max_resident_rows=(state or {}).get("max_resident_rows"), per_batch_ms=(state or {}).get("per_batch_ms"),
                               per_batch_counts=ref_counts)
    print(f"   eimer_duckdb : {rec['eimer_duckdb']['status']} total={ms} ms matches={n}", flush=True)
    rec["reference"] = {"final_tuples": ref_tuples, "per_batch_counts": ref_counts}

    # plans as EPL
    types = cells.types_for(c["k"])
    plans = {}
    for label, cover in covers.items():
        if label != "pick" and sorted(cover) == sorted(toks):
            continue  # same cover as the pick: recorded as same_as_pick below
        o, _plan, nodes = eimer_arm.build_cover_plan(dep, cover, all_vars, wl)
        plans[label] = epl_emitter.render_plan(dep, cover, o, nodes, w_ms=c["w_ms"], types=types, columns=COLUMNS, native_form=NATIVE_FORM, eimer_render=EIMER_RENDER, update_form=UPDATE_FORM)
        (cell_dir / f"plan_{label}.json").write_text(json.dumps(plans[label], indent=1))
    client.close()

    # the JVM arms
    arms = [("esper_native", "native", "pick"), ("eimer_esper", "eimer", "pick")]
    for label, cover in covers.items():
        if label == "pick":
            continue
        if sorted(cover) != sorted(toks):
            arms.append((f"eimer_esper_{label}", "eimer", label))
        else:
            rec[f"eimer_esper_{label}"] = dict(status="same_as_pick", cover=cover)
    eimer_arms = ["eimer_esper"] + [f"eimer_esper_{a}" for a in ESPER_ARMS if a != "pick"]
    skip_from = os.environ.get("ESPER_SKIP_DNF_FROM", "")   # memory stage: arms that did not finish in the runtime run are not re-executed
    reg = json.loads((HERE / skip_from / "records" / f"{name}.json").read_text()) if skip_from and (HERE / skip_from / "records" / f"{name}.json").exists() else {}
    for arm_name, mode, label in arms:
        out = cell_dir / f"record_{arm_name}.json"
        if reg and (reg.get(arm_name) or {}).get("status") == "dnf":   # no figure or table reports memory for a censored arm: copy the censored outcome
            rec[arm_name] = dict(status="dnf", total_ms=reg[arm_name].get("total_ms"), cover=reg[arm_name].get("cover"), identity_final=None, identity_counts=None,
                                 copied_from=f"{skip_from}/records/{name}.json", notes="did not finish in the runtime run; not re-executed for the memory measurement")
            print(f"   {arm_name:16s}: dnf (copied from {skip_from}; not re-executed)", flush=True); continue
        r = run_jvm(cell_dir, cell_dir / f"plan_{label}.json", mode, out, T)
        r["cover"] = plans[label]["cover"] if mode == "eimer" else None
        if r.get("data_sha256_16") is not None:  # the JVM read exactly the exported table
            assert r["data_sha256_16"] == data_sha, (arm_name, r["data_sha256_16"], data_sha)
        rec.setdefault("engine_esper", r.get("esper_version"))
        ft = sorted(map(list, r.get("final_tuples") or [])) if r.get("status") == "ok" else None
        r["identity_final"] = (None if (ft is None or ref_tuples is None) else ft == ref_tuples)
        pc = [b.get("count") for b in r.get("per_batch", [])] if r.get("status") == "ok" else None
        r["identity_counts"] = (None if (pc is None or ref_counts is None) else pc == ref_counts)
        r.pop("final_tuples", None)  # kept in the per-arm record file
        rec[arm_name] = r
        print(f"   {arm_name:16s}: {r.get('status')} total={r.get('total_ms')} ms matches={r.get('final_count')} "
              f"identity={r['identity_final']}/{r['identity_counts']}", flush=True)
    # gates: (1) each Esper arm against the DuckDB reference (independent engine), (2) the Esper arms against each
    # other (always available); (3) non-vacuity. speed-ups only within Esper and only when every gate passed.
    nat = rec["esper_native"]
    ok_arms = [a for a in ["esper_native"] + eimer_arms if rec.get(a, {}).get("status") == "ok"]
    ok_arms = [a for a in ok_arms if (cell_dir / f"record_{a}.json").exists()]
    finals = {a: sorted(map(list, json.loads((cell_dir / f"record_{a}.json").read_text()).get("final_tuples") or [])) for a in ok_arms}
    counts = {a: [b.get("count") for b in rec[a].get("per_batch", [])] for a in ok_arms}
    rec["identity_arms"] = (len({json.dumps(finals[a]) for a in ok_arms}) <= 1 and len({json.dumps(counts[a]) for a in ok_arms}) <= 1) if ok_arms else None
    rec["vacuous"] = (rec["eimer_duckdb"].get("final_count") == 0)
    def gated(a):
        x = rec.get(a, {})
        return x.get("status") == "ok" and x.get("identity_final") is True and x.get("identity_counts") is True
    for a in eimer_arms:
        x = rec.get(a, {})
        if gated("esper_native") and gated(a) and rec["identity_arms"] and not rec["vacuous"] and x.get("total_ms"):
            rec[f"speedup_{a}_vs_native"] = round(nat["total_ms"] / x["total_ms"], 3)
            # the same speed-up with the ingestion time of both arms added
            ing = lambda r: sum(b.get("ingest_ms") or 0 for b in r.get("per_batch", []))
            if all("ingest_ms" in b for b in nat.get("per_batch", [])) and all("ingest_ms" in b for b in x.get("per_batch", [])):
                rec[f"speedup_{a}_vs_native_incl_ingest"] = round((nat["total_ms"] + ing(nat)) / (x["total_ms"] + ing(x)), 3)
                rec[f"ingest_ms_{a}"] = round(ing(x), 1); rec["ingest_ms_esper_native"] = round(ing(nat), 1)
    print(f"   gates: arms identical={rec['identity_arms']} vacuous={rec['vacuous']}", flush=True)
    (RESULTS / "records").mkdir(parents=True, exist_ok=True)
    rec_path.write_text(json.dumps(rec))
    return rec


def main() -> None:
    import argparse
    global RESULTS
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=""); ap.add_argument("--force", action="store_true")
    ap.add_argument("--results-dir", default=os.environ.get("RESULTS_DIR", "results"))
    a = ap.parse_args()
    RESULTS = HERE / a.results_dir
    only = {x for x in a.only.split(",") if x}
    todo = cells.repro_cells()
    for c in todo:
        if only and cells.cell_name(c) not in only:
            continue
        run_cell(c, force=a.force)


if __name__ == "__main__":
    main()
