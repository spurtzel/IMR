#!/usr/bin/env python3
"""JVM baseline per cell: the Esper jar with ,,--arm none'' loads the cell's data set exactly as every timed arm does and
exits; measured like the arms (GNU time %M = peak RSS, -Xlog:gc = live heap after each collection). The per-arm working
memory of the figure = the arm's peak live heap minus this baseline ("peak above the loaded table", as for DuckDB).
Reads the cell directories of a finished memory run (data.csv + plan_pick.json).

  run_jvm_baseline.py --cells-from <memory run> --out <dir> [--only a,b]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cells  # noqa: E402

JAR = Path(os.environ.get("ESPER_JAR", cells.EIMER_REPO / "backends" / "esper" / "java" / "target" / "esper-eimer.jar"))
JVM = [os.environ.get("JAVA", "java"), "-Xmx12g", "-XX:+UseSerialGC", "-XX:ActiveProcessorCount=1"]
GNU_TIME = "/usr/bin/time"


def parse_gc(path: Path) -> dict:
    rx = re.compile(r"(\d+)([KMG])->(\d+)([KMG])\((\d+)([KMG])\)"); unit = {"K": 1 / 1024.0, "M": 1.0, "G": 1024.0}
    before, after, n = 0.0, 0.0, 0
    for line in path.read_text(errors="replace").splitlines() if path.exists() else []:
        m = rx.search(line)
        if m and "Pause" in line:
            n += 1; before = max(before, float(m.group(1)) * unit[m.group(2)]); after = max(after, float(m.group(3)) * unit[m.group(4)])
    return dict(peak_live_heap_mb=round(after, 1), peak_heap_before_gc_mb=round(before, 1), gc_count=n)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cells-from", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--only", default=""); ap.add_argument("--reps", type=int, default=3, help="JVM launches per cell; the record keeps all and the median")
    a = ap.parse_args(); only = {x for x in a.only.split(",") if x}
    src = HERE / a.cells_from / "cells"; out = HERE / a.out; (out / "records").mkdir(parents=True, exist_ok=True); (out / "tmp").mkdir(exist_ok=True)
    for c in cells.repro_cells():
        name = cells.cell_name(c)
        if only and name not in only: continue
        rec_path = out / "records" / f"{name}.json"
        if rec_path.exists(): print(f"skip {name}"); continue
        cell_dir = src / name
        if not (cell_dir / "data.csv").exists():
            print(f"skip {name} (no data.csv under {cell_dir})"); continue
        plan = next(iter(sorted(cell_dir.glob("plan_*.json"))), None)
        if plan is None: print(f"skip {name} (no plan file)"); continue
        runs = []
        for i in range(a.reps):
            o = out / "tmp" / f"{name}.{i}.json"; rss = o.with_suffix(".rss"); gc = o.with_suffix(".gc.log")
            for q in (o, rss, gc): q.unlink(missing_ok=True)
            cmd = [GNU_TIME, "-f", "%M", "-o", str(rss)] + JVM + [f"-Xlog:gc:file={gc}", "-jar", str(JAR), "--data", str(cell_dir), "--plan", str(plan),
                                                                   "--arm", "none", "--out", str(o), "--wall-s", "600", "--total-wall-s", "600", "--final-gc", "1"]
            t0 = time.time(); p = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
            r = json.loads(o.read_text()) if o.exists() else {"status": "error", "notes": p.stderr[-800:]}
            r.update(parse_gc(gc)); r["wall_clock_s"] = round(time.time() - t0, 1); r["rc"] = p.returncode
            r["peak_live_heap_mb"] = round(max(r.get("peak_live_heap_sampled_mb") or 0.0, r.get("final_live_heap_mb") or 0.0), 1)   # the data set's live size: forced GC with the rows held
            r["peak_rss_mb"] = round(int(rss.read_text().strip().splitlines()[-1]) / 1024.0, 1) if rss.exists() else None
            runs.append(r)
        ok = [r for r in runs if r.get("status") == "ok" and r.get("peak_rss_mb")]
        med = lambda k: (sorted(r[k] for r in ok)[len(ok) // 2] if ok else None)
        rec = dict(cell=name, params=c, arm="none", runs=runs, peak_rss_mb=med("peak_rss_mb"), peak_live_heap_mb=med("peak_live_heap_mb"), final_live_heap_mb=med("final_live_heap_mb"),
                   n_ok=len(ok), data_rows=c["n"], jar=JAR.name, jvm=JVM, measured="GNU time %M + -Xlog:gc, median of the launches")
        rec_path.write_text(json.dumps(rec))
        print(f"[{name}] baseline JVM: peak RSS {rec['peak_rss_mb']} MB, live heap {rec['peak_live_heap_mb']} MB ({len(ok)}/{a.reps} ok)", flush=True)


if __name__ == "__main__":
    main()
