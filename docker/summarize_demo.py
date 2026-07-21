#!/usr/bin/env python3
"""Demo verdict: read every phase's artifacts, print a human summary, exit 0 iff
every gate passed. Stdlib only."""
import json
import sys
from pathlib import Path

out = Path(sys.argv[1] if len(sys.argv) > 1 else "docker/out")
failures = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))
    if not ok:
        failures.append(name)


print("=" * 62)
print("EIMER demo summary")
print("=" * 62)

# phase exit codes
status = {}
status_file = out / "phase_status.txt"
if status_file.exists():
    for line in status_file.read_text().splitlines():
        name, _, verdict = line.strip().rpartition(" ")
        status[name] = verdict
for name in ("frontend", "pipeline", "eimer_vs_mr", "memory_budget",
             "predicates_equi", "predicates_band_equi"):
    check(f"phase {name} exit", status.get(name) == "PASS", status.get(name, "missing"))

# pipeline: the per-batch native-MR full-tuple correctness capture
manifest = out / "pipeline" / "experiment_manifest.json"
if manifest.exists():
    m = json.loads(manifest.read_text())
    corr = (m.get("captures") or {}).get("correctness") or {}
    check("pipeline correctness capture", corr.get("status") == "PASSED",
          corr.get("status", "absent"))
    rows_csv = out / "pipeline" / "candidate_rows.csv"
    check("pipeline candidate record written", rows_csv.exists())
else:
    check("pipeline experiment_manifest.json", False, "missing")

# EIMER vs MR + the predicate-class cells: every arm's identity gate + speedups
for phase_dir, label in (("eimer_vs_mr", "EIMER full-tuple identity gates"),
                         ("predicates_equi", "equi-predicate identity gates"),
                         ("predicates_band_equi", "band+equi identity gates")):
    res = out / phase_dir / "results.json"
    if not res.exists():
        check(f"{phase_dir} results.json", False, "missing")
        continue
    r = json.loads(res.read_text())["results"]
    arms = {k: v for k, v in r.items() if k != "MATCH_RECOGNIZE"}
    bad = [k for k, v in arms.items() if v.get("gate") not in ("PASSED",)]
    check(label, not bad, ", ".join(bad) or "all arms")
    mr = r.get("MATCH_RECOGNIZE", {}).get("total_ms")
    print(f"       native MATCH_RECOGNIZE: {mr} ms")
    for k, v in arms.items():
        if v.get("total_ms") is not None:
            print(f"       {k:<18}: {v['total_ms']} ms  "
                  f"(speedup {v.get('speedup_vs_mr')}x, cover {v.get('cover')})")

# memory budget: the three gates
comp = out / "memory_budget" / "live_compliance.json"
if comp.exists():
    g = json.loads(comp.read_text())["gates"]
    check("budget: predicted peak <= M each step", g.get("predicted_within_budget"))
    check("budget: identical results under every M", g.get("identical_results"))
    check("budget: monotone shrinking state", g.get("monotone_state"))
    desc = json.loads((out / "memory_budget" / "descent.json").read_text())
    print(f"       descent plans: "
          f"{[s['cover'] or ['(empty)'] for s in desc['steps']]}")
else:
    check("memory_budget live_compliance.json", False, "missing")

print("-" * 62)
if failures:
    print(f"VERDICT: FAILED ({len(failures)} gate(s)): {', '.join(failures)}")
    sys.exit(1)
print("VERDICT: ALL GATES PASSED: the pipeline runs end-to-end, every EIMER result "
      "(band, equi, and band+equi predicates) is tuple-identical to native "
      "MATCH_RECOGNIZE, and the storage budget M behaves as specified.")
