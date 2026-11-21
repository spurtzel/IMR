#!/usr/bin/env python3
"""Peak resident memory per approach, each approach in its own process.

The parent spawns one child per (cell, approach); the child loads the table, runs that one approach as run_cells.py
does (baseline: DuckDB's join-order optimizer; EIMER: the pinned join order on the recorded strategy) and reports its
own ru_maxrss after the data load and at the end.
Transient = peak - after-load: the figure's memory bars. The runtime is recorded as a cross-check. The check: the
approach's final tuples equal the stored tuples of that approach in the runtime record.

  run_peak_memory.py --runtime results --out results_peakmem [--only k4_n25000_b25_w60,...]
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cells  # noqa: E402

ARMS = ("transpiled", "eimer")


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def child(arm: str, name: str, runtime: Path, out_json: Path) -> None:
    import duckdb  # noqa
    import duckdb_exec  # noqa
    from transpile import transpiled_sql_for  # noqa
    import run_cells  # noqa
    rec = json.loads((runtime / "records" / f"{name}.json").read_text()); c = rec["params"]
    T = cells.wall_seconds(c)
    spec, dep, _ = cells.load_spec(c["k"], c["w_ms"]); pos = dep.positions
    vs = sorted(dep.variables, key=lambda v: pos[v]); key_cols = [f"{v}_id" for v in vs]; all_vars = set(dep.variables)
    qb = frozenset(range(1, c["batches"] + 1))
    rss_start = _rss_mb()
    frame = cells.generate_frame(c)
    if arm == "transpiled":
        from duckdb_client import DuckDBClient, DuckDBConnectionConfig
        client = DuckDBClient(DuckDBConnectionConfig(database=":memory:", threads=1, memory_limit=run_cells.MEMORY_LIMIT,
                                                     pin_join_order=not run_cells.BASELINE_OPTIMIZER)); client.set_wall(T)
    else:
        client = duckdb_exec.connect(timeout_s=T, memory_limit=run_cells.MEMORY_LIMIT, pin_join_order=not run_cells.EIMER_OPTIMIZER)
    tmp = out_json.parent / f"{name}.{arm}.{os.getpid()}"; tmp.mkdir(parents=True, exist_ok=True)
    client.execute(f"SET temp_directory='{tmp}'")
    loaded = duckdb_exec.load_frame(client, frame, table="events"); assert loaded == c["n"], (loaded, c["n"])
    del frame
    settings = client.settings()
    if arm == "transpiled":
        baseline_sql, _ = transpiled_sql_for(spec, vs[0], c["w_ms"])
        rss_load = _rss_mb(); t0 = time.time()
        ms, n, tuples, _meta = duckdb_exec.baseline_streaming(client, baseline_sql, "events", c["n"], c["batches"], qb, key_cols, T, warmup=False, total_wall_s=T)
        cover = None
    else:
        wl, _payload = duckdb_exec.b_unified_workload(client, dep, table="events", total_events=c["n"], batches=c["batches"])
        cover = rec["eimer"]["cover"]
        rss_load = _rss_mb(); t0 = time.time()
        ms, n, tuples, _state = duckdb_exec.eimer_streaming(client, dep, cover, all_vars, wl, spec, "events", c["n"], c["batches"], qb, key_cols, T,
                                                            measure_state=False, warmup=False, total_wall_s=T)
    peak = _rss_mb(); wall = time.time() - t0
    new_t = sorted(map(list, tuples)) if tuples is not None else None; old_t = (rec.get("tuples") or {}).get(arm)
    out = dict(arm=arm, cover=cover, status="ok" if ms is not None else "dnf", total_ms=ms, wall_clock_s=round(wall, 1), final_count=n,
               runtime_total_ms=rec[arm].get("total_ms"), runtime_final_count=rec[arm].get("final_count"),
               identity_vs_runtime=(None if (new_t is None or old_t is None) else new_t == old_t),
               rss_start_mb=round(rss_start, 1), rss_after_load_mb=round(rss_load, 1), peak_rss_mb=round(peak, 1),
               transient_mb=round(peak - rss_load, 1), settings=settings, engine=f"duckdb {duckdb.__version__}")
    client.close(); out_json.write_text(json.dumps(out))
    print(f"   {arm:10s}: {out['status']} total={ms} ms (runtime record {out['runtime_total_ms']}) matches={n} identity={out['identity_vs_runtime']} "
          f"rss after load {rss_load:.0f} MB -> peak {peak:.0f} MB = transient {peak - rss_load:.0f} MB", flush=True)


def parent(runtime: Path, out: Path, only: set[str]) -> None:
    records = out / "records"; records.mkdir(parents=True, exist_ok=True); (out / "tmp").mkdir(exist_ok=True)
    import run_cells  # noqa
    for c in cells.cells(cells.REPRO_AXES):
        name = cells.cell_name(c)
        if only and name not in only:
            continue
        rec_path = records / f"{name}.json"
        if rec_path.exists():
            print(f"skip {name} (record exists)"); continue
        src = runtime / "records" / f"{name}.json"
        if not src.exists():
            print(f"skip {name} (no runtime record)"); continue
        src_rec = json.loads(src.read_text())
        print(f"[{name}] N={c['n']} B={c['batches']} W={c['w_ms']}ms k={c['k']} wall={cells.wall_seconds(c)}s: one fresh process per arm", flush=True)
        rec = dict(cell=name, params=c, wall_s=cells.wall_seconds(c), mode="peak memory, one process per arm",
                   runtime_record=f"{runtime.name}/records/{name}.json", memory_limit=run_cells.MEMORY_LIMIT)
        for arm in ARMS:
            if (src_rec.get(arm) or {}).get("status") != "ok":
                rec[arm] = dict(arm=arm, status=(src_rec.get(arm) or {}).get("status", "not_measured"), notes="did not finish in the runtime run; not re-executed")
                print(f"   {arm}: {rec[arm]['status']} in the runtime run, not re-executed", flush=True); continue
            tmp_json = out / "tmp" / f"{name}.{arm}.json"
            if tmp_json.exists(): tmp_json.unlink()
            r = subprocess.run([sys.executable, "-u", str(HERE / "run_peak_memory.py"), "--child", arm, "--cell", name, "--runtime", str(runtime), "--tmp-json", str(tmp_json)],
                               cwd=str(HERE), text=True)
            if r.returncode != 0 or not tmp_json.exists():
                rec[arm] = dict(arm=arm, status="error", returncode=r.returncode); print(f"   {arm}: child failed (rc {r.returncode})", flush=True)
            else:
                rec[arm] = json.loads(tmp_json.read_text())
        t, e = rec["transpiled"], rec["eimer"]
        rec["ratio_transient_transpiled_vs_eimer"] = (round(t["transient_mb"] / e["transient_mb"], 2) if (t.get("transient_mb") and e.get("transient_mb")) else None)
        rec_path.write_text(json.dumps(rec)); print(f"   -> {rec_path.name}: transient transpiled {t.get('transient_mb')} MB vs EIMER {e.get('transient_mb')} MB", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime", default="results"); ap.add_argument("--out", default="results_peakmem")
    ap.add_argument("--only", default="")
    ap.add_argument("--child", default=""); ap.add_argument("--cell", default=""); ap.add_argument("--tmp-json", default="")
    a = ap.parse_args()
    if a.child:
        child(a.child, a.cell, Path(a.runtime) if os.path.isabs(a.runtime) else HERE / a.runtime, Path(a.tmp_json))
    else:
        parent(HERE / a.runtime, HERE / a.out, {x for x in a.only.split(",") if x})
