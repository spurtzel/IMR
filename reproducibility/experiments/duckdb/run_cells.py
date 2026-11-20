#!/usr/bin/env python3
"""The DuckDB arms per cell: generate + load the table, estimate the selectivities on the loaded table, select
EIMER's materialization strategy (clique-grow), then
  (1) the transpiled MATCH_RECOGNIZE baseline, re-executed after every batch over the table so far, and
  (2) EIMER on the selected strategy: view updates per batch + composition and selection at every query.
Both arms run single-shot (no warm-up) in one thread under the cell's time limit, per statement AND per arm; an arm
that exceeds it is recorded as DNF. Writes <results>/records/<cell>.json.

Configuration of the paper's runs, all recorded in every record:
  * baseline: DuckDB's join-order optimizer ON (BASELINE_OPTIMIZER=1)
  * EIMER: the composition is emitted over the plan's (bushy) join tree and DuckDB's join reordering is disabled, so
    the engine executes the selected plan (EIMER_OPTIMIZER=0)
  * selection: kappa from KAPPA_FILE (the engine constant of the paper's runs)
  * correctness: the final result sets of both arms are compared on full tuples (in_engine_identity)

Resume-safe: a cell with a record is skipped unless --force.
Usage: run_cells.py [--only k4_n25000_b25_w60,...] [--force] [--results-dir results]
"""
from __future__ import annotations

import json
import os
import resource
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cells  # noqa: E402  (puts the repository on sys.path)
from transpile import bounded_gap_in_input, transpiled_sql_for  # noqa: E402

import duckdb  # noqa: E402
import duckdb_exec  # noqa: E402
import eimer_arm  # noqa: E402
from eimer.selection.plan_selection_clique_grow import select_clique_grow_plan  # noqa: E402
from eimer.sql import sql_emitter  # noqa: E402
from eimer.sql.backend import SqlDialect, backend_for  # noqa: E402

RECORDS = HERE / "results" / "records"
MEMORY_LIMIT = "12GB"  # a runaway statement is a censored DNF, not a killed process
KAPPA_FILE = Path(os.environ.get("KAPPA_FILE", HERE / "kappa_duckdb.json"))
KAPPA = float(json.loads(KAPPA_FILE.read_text())["kappa"])
FULL_ANCHOR = {"0": False, "1": "always", "edgeless": "edgeless"}[os.environ.get("FULL_ANCHOR", "0")]
BASELINE_OPTIMIZER = os.environ.get("BASELINE_OPTIMIZER", "1") == "1"   # the transpiled baseline with DuckDB's join-order optimizer
EIMER_OPTIMIZER = os.environ.get("EIMER_OPTIMIZER", "0") == "1"         # 0 = EIMER's join order pinned (the paper's runs)
STAR_ARM = os.environ.get("EIMER_REPRO_STAR", "0") == "1"


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def run_cell(c: dict, force: bool = False) -> dict:
    name = cells.cell_name(c)
    rec_path = RECORDS / f"{name}.json"
    if rec_path.exists() and not force:
        print(f"skip {name} (record exists)"); return json.loads(rec_path.read_text())
    assert sql_emitter._compose_tree_emission_enabled(), "the composition-tree emission is switched off (EIMER_DISABLE_COMPOSE_TREE)"
    T = cells.wall_seconds(c)
    spec, dep, spec_path = cells.load_spec(c["k"], c["w_ms"])
    all_vars, pos = set(dep.variables), dep.positions
    vs = sorted(dep.variables, key=lambda v: pos[v])
    key_cols = [f"{v}_id" for v in vs]
    qb = frozenset(range(1, c["batches"] + 1))
    baseline_sql, sql_path = transpiled_sql_for(spec, vs[0], c["w_ms"])

    frame = cells.generate_frame(c)
    client = duckdb_exec.connect(timeout_s=T, memory_limit=MEMORY_LIMIT, pin_join_order=not EIMER_OPTIMIZER)
    # DuckDB spills to ".tmp" in the working directory: every process gets its own temp directory
    _tmp = RECORDS.parent / "tmp" / f"{name}.{os.getpid()}"; _tmp.mkdir(parents=True, exist_ok=True)
    client.execute(f"SET temp_directory='{_tmp}'")
    loaded = duckdb_exec.load_frame(client, frame, table="events")
    assert loaded == len(frame) == c["n"], (loaded, len(frame), c["n"])
    settings_eimer = client.settings()
    assert settings_eimer.get("disabled_optimizers", "") == ("" if EIMER_OPTIMIZER else "join_order"), settings_eimer
    # the transpiled baseline on its own connection, with DuckDB's join-order optimizer
    client_b = client
    if BASELINE_OPTIMIZER:
        from duckdb_client import DuckDBClient, DuckDBConnectionConfig
        client_b = DuckDBClient(DuckDBConnectionConfig(database=":memory:", threads=1, memory_limit=MEMORY_LIMIT, pin_join_order=False)); client_b.set_wall(T)
        client_b.execute(f"SET temp_directory='{_tmp}'")
        assert duckdb_exec.load_frame(client_b, frame, table="events") == c["n"]
        assert client_b.settings().get("disabled_optimizers", "") == "", client_b.settings()
    settings_baseline = client_b.settings()

    t_sel = time.time()
    wl, payload = duckdb_exec.b_unified_workload(client, dep, table="events", total_events=c["n"], batches=c["batches"])
    pick = select_clique_grow_plan(dep, wl, query_batches=qb, kappa=KAPPA, full_anchor=FULL_ANCHOR)
    toks = sorted("_".join(sorted(v, key=lambda x: pos[x])) for v in pick.compose)
    rec = dict(cell=name, params=c, wall_s=T, spec=spec_path.name, transpiled_sql=sql_path.name,
               engine=f"duckdb {duckdb.__version__}", warmup=False, freq="all",
               kappa=KAPPA, kappa_provenance=KAPPA_FILE.name,
               window_rule=backend_for(SqlDialect.DUCKDB).window_rule,
               selector=dict(algorithm="clique-grow", full_anchor=FULL_ANCHOR,
                             rule=("propagated graph" if os.environ.get("EIMER_SELECT_ON_DERIVED") else "explicit graph")),
               execution=dict(compose_tree=True, join_order=("optimizer" if EIMER_OPTIMIZER else "pinned")),
               baseline=dict(join_order=("duckdb optimizer" if BASELINE_OPTIMIZER else "pinned"), form="transpiled SQL, re-executed per batch",
                             gap_bounded_by_window=bounded_gap_in_input(spec.name)),
               memory_limit=MEMORY_LIMIT, prefix_prune=backend_for(SqlDialect.DUCKDB).batch_window_prefix_prune,
               settings=settings_baseline, settings_eimer=settings_eimer, plan_selection_s=round(time.time() - t_sel, 3),
               selectivities=payload, pick=dict(cover=toks, certificate={k: (round(v, 6) if isinstance(v, float) else v)
                                                                         for k, v in pick.certificate.items()}))
    print(f"[{name}] N={c['n']} B={c['batches']} W={c['w_ms']}ms k={c['k']} pick={toks} (kappa={KAPPA}, full_anchor={FULL_ANCHOR}) wall={T}s", flush=True)

    # --- arm 1: transpiled baseline, per-batch re-execution (single-shot) ---
    rss0 = _rss_mb(); t0 = time.time()
    ms, n, tuples, meta = duckdb_exec.baseline_streaming(client_b, baseline_sql, "events", c["n"], c["batches"], qb, key_cols, T,
                                                         warmup=False, total_wall_s=T)
    rec["transpiled"] = dict(status="ok" if ms is not None else "dnf", total_ms=ms, final_count=n,
                             per_scan_ms=meta.get("per_scan_ms"), spent_ms=meta.get("spent_ms"),
                             planner_fallbacks=client_b.planner_fallbacks,
                             rss_peak_delta_mb=round(_rss_mb() - rss0, 1), wall_clock_s=round(time.time() - t0, 1),
                             join_order=rec["baseline"]["join_order"])
    if BASELINE_OPTIMIZER:
        client_b.close()
    tuples_t = tuples
    print(f"   transpiled: {rec['transpiled']['status']} total={ms} ms matches={n}", flush=True)

    # --- arm 2: EIMER on the selector's pick ---
    star = [f"{vs[0]}_{v}" for v in vs[1:]]
    rec["tuples"] = {"transpiled": sorted(map(list, tuples_t)) if tuples_t is not None else None}
    rec["compose_sql"] = {}
    for arm, cover in (("eimer", toks), ("eimer_star", star)):
        if arm == "eimer_star" and not STAR_ARM:
            rec[arm] = dict(status="not_run", cover=star); continue
        if arm == "eimer_star" and sorted(star) == sorted(toks):
            rec[arm] = dict(status="same_as_pick", cover=star)
            print("   eimer_star: same cover as the pick", flush=True)
            continue
        # evidence: the emitted composition statement; "nested" = the plan's join tree is rendered as nested subqueries
        st = eimer_arm.cover_statements(dep, cover, all_vars, wl, dialect=SqlDialect.DUCKDB)
        rec["compose_sql"][arm] = dict(composition_sql=st.composition_sql, nested=("tn1" in st.composition_sql), views=len(cover))
        rss0 = _rss_mb(); t0 = time.time(); fb0 = client.planner_fallbacks
        ms, n, tuples, state = duckdb_exec.eimer_streaming(client, dep, cover, all_vars, wl, spec, "events", c["n"], c["batches"],
                                                           qb, key_cols, T, measure_state=True, warmup=False, total_wall_s=T)
        rec[arm] = dict(status="ok" if ms is not None else "dnf", total_ms=ms, final_count=n, cover=cover,
                        planner_fallbacks=client.planner_fallbacks - fb0,
                        max_resident_rows=(state or {}).get("max_resident_rows"),
                        per_cache_rows=(state or {}).get("per_cache_rows"),
                        per_batch_ms=(state or {}).get("per_batch_ms"),
                        rss_peak_delta_mb=round(_rss_mb() - rss0, 1), wall_clock_s=round(time.time() - t0, 1))
        new_t = sorted(map(list, tuples)) if tuples is not None else None
        rec["tuples"][arm] = new_t
        # in-engine agreement with the transpiled arm (final full-tuple sets)
        rec[f"identity_{arm}"] = (None if (new_t is None or rec["tuples"]["transpiled"] is None) else (new_t == rec["tuples"]["transpiled"]))
        if rec["transpiled"]["total_ms"] and ms:
            rec[f"speedup_{arm}_vs_transpiled"] = round(rec["transpiled"]["total_ms"] / ms, 3)
        print(f"   {arm:10s}: {rec[arm]['status']} total={ms} ms matches={n} resident_rows={rec[arm]['max_resident_rows']} "
              f"identity={rec[f'identity_{arm}']} nested_compose={rec['compose_sql'][arm]['nested']} cover={cover}", flush=True)
    rec["in_engine_identity"] = rec.get("identity_eimer")
    rec["speedup_eimer_vs_transpiled"] = rec.get("speedup_eimer_vs_transpiled")
    if rec["eimer_star"]["status"] == "same_as_pick":
        rec["identity_eimer_star"] = rec.get("identity_eimer")
        rec["speedup_eimer_star_vs_transpiled"] = rec.get("speedup_eimer_vs_transpiled")
    RECORDS.mkdir(parents=True, exist_ok=True)
    rec_path.write_text(json.dumps(rec))
    client.close()
    return rec


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--results-dir", default=os.environ.get("RESULTS_DIR", "results"), help="where records/ go (relative to this directory, or absolute)")
    a = ap.parse_args()
    global RECORDS
    RECORDS = (HERE / a.results_dir) / "records"
    only = {x for x in a.only.split(",") if x}
    for c in cells.cells(cells.REPRO_AXES):
        if only and cells.cell_name(c) not in only:
            continue
        run_cell(c, force=a.force)


if __name__ == "__main__":
    main()
