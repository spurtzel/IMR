#!/usr/bin/env python3
"""Cells of the DuckDB experiment (EIMER on DuckDB vs. the transpiled MATCH_RECOGNIZE re-execution).

Center: windowed chain, k = 4, N = 25,000, 25 batches, W = 60 ms, five event types at rate 0.2, sigma 0.2, seed 42.
The figure of the paper varies two axes around it:
  N1000  the table size at a constant batch size of 1,000 events (B = N / 1,000)
  k      the pattern length at N = 25,000, B = 25

Environment variables:
  EIMER_REPRO_DUCKDB_NS=5000,10000,25000      table sizes of the N1000 axis (multiples of 1,000)
  EIMER_REPRO_DUCKDB_KS=3,4,5                 pattern lengths of the k axis (3..8)
  EIMER_REPRO_WALL=600                        time limit in seconds per statement and per approach (default: tiers below)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EIMER_REPO = Path(os.environ.get("EIMER_REPO", HERE.parents[2]))
for _p in (str(EIMER_REPO), str(EIMER_REPO / "execution"), str(EIMER_REPO / "demo")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

HALF_WIDTH = 0.02
SPECS = Path(os.environ.get("EIMER_REPRO_SPECS_DIR", HERE / "specs"))
CENTER = dict(k=4, n=25_000, batches=25, w_ms=60, sigma=0.2, ftype=0.2, ntypes=5, seed=42)
PAPER_NS = (5_000, 10_000, 25_000, 50_000, 100_000, 250_000, 500_000, 1_000_000)
PAPER_KS = (3, 4, 5, 6, 7, 8)


def _ints(env: str, default: tuple) -> list:
    raw = os.environ.get(env, "").strip()
    return [int(x) for x in raw.replace(" ", "").split(",") if x] if raw else list(default)


_NS = _ints("EIMER_REPRO_DUCKDB_NS", PAPER_NS)
_KS = _ints("EIMER_REPRO_DUCKDB_KS", PAPER_KS)
assert all(n % 1000 == 0 and n >= 1000 for n in _NS), f"N1000 axis: multiples of 1,000 expected, got {_NS}"
assert all(3 <= k <= 8 for k in _KS), f"k axis: 3..8 (the cached transpiler output), got {_KS}"

AXES = {
    "N": ("n", [5_000, 10_000, 25_000, 50_000, 100_000, 250_000, 500_000, 1_000_000]),
    "W": ("w_ms", [20, 40, 60]),
    "B": ("batches", [5, 25, 100, 250, 500, 1000]),
    "k": ("k", _KS),
    "N1000": ("n", [dict(n=n, batches=n // 1000) for n in _NS]),
}
REPRO_AXES = ("N1000", "k")
CHAIN_SPEC = {k: f"q{k}_chain_rare_spatial" for k in (3, 4, 5, 6, 7, 8)}


def cell_name(c: dict) -> str:
    return f"k{c['k']}_n{c['n']}_b{c['batches']}_w{c['w_ms']}"


def cells(axes: tuple | None = None) -> list[dict]:
    """every cell with the axes it belongs to."""
    out: dict[str, dict] = {}
    for axis, (param, values) in AXES.items():
        if axes and axis not in axes:
            continue
        for v in values:
            c = {**CENTER, **v} if isinstance(v, dict) else {**CENTER, param: v}
            rec = out.setdefault(cell_name(c), {**c, "axes": []})
            rec["axes"].append(axis)
    return sorted(out.values(), key=lambda c: (c["n"], c["batches"], c["k"], c["w_ms"]))


def wall_seconds(c: dict) -> int:
    """per-statement and per-approach time limit: 1,800 s below 100k rows, 3,600 s to 250k, 7,200 s beyond and for 500 and 1,000 batches. EIMER_REPRO_WALL overrides."""
    if os.environ.get("EIMER_REPRO_WALL"):
        return int(os.environ["EIMER_REPRO_WALL"])
    if c["batches"] >= 500:
        return 7200
    return 7200 if c["n"] >= 500_000 else 3600 if c["n"] >= 100_000 else 1800


def spec_name(k: int, w_ms: int) -> str:
    return f"chain_k{k}_w{w_ms}"


def build_spec(k: int, w_ms: int) -> Path:
    src = EIMER_REPO / "execution" / "query_specs" / "synthetic" / f"{CHAIN_SPEC[k]}.json"
    spec = json.loads(src.read_text())
    var_order = list(spec["pattern"]["variables"])
    assert len(var_order) == k, (src, var_order)
    type_for = {v: f"T{i % CENTER['ntypes']}" for i, v in enumerate(var_order)}
    for p in spec["independent_predicates"]:
        v = p["variables"][0]
        p["sql_template"] = f"{v}.primary_type = '{type_for[v]}'"
    import re
    for p in spec["dependent_predicates"]:
        m = re.search(r"(\w+)\.lon\s+BETWEEN\s+(\w+)\.lon", p["sql_template"])
        tgt, ref = m.group(1), m.group(2)
        p["sql_template"] = (f"{tgt}.lon BETWEEN {ref}.lon - {HALF_WIDTH} AND {ref}.lon + {HALF_WIDTH} "
                             f"AND {tgt}.lat BETWEEN {ref}.lat - {HALF_WIDTH} AND {ref}.lat + {HALF_WIDTH}")
        p["referenced_columns"] = ["lon", "lat"]
    first, last = var_order[0], var_order[-1]
    spec["dependent_predicates"].append({
        "predicate_id": f"{first}_{last}_window", "kind": "dependent", "variables": [first, last],
        "sql_template": f"abs(date_diff('millisecond', {first}.ts, {last}.ts)) <= {w_ms}",
        "referenced_columns": ["ts"], "selectivity_key": f"{first}|{last}"})
    spec["name"] = spec_name(k, w_ms)
    spec["suite_metadata"] = {**spec.get("suite_metadata", {}), "window_ms": w_ms, "source": CHAIN_SPEC[k]}
    out = SPECS / f"{spec_name(k, w_ms)}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(spec, indent=2) + "\n"); os.replace(tmp, out)
    return out


def load_spec(k: int, w_ms: int):
    from eimer.query.query_spec import load_query_spec_file, query_spec_to_dependency_graph
    path = build_spec(k, w_ms)
    spec = load_query_spec_file(str(path))
    return spec, query_spec_to_dependency_graph(spec), path


def generate_frame(c: dict):
    """the repository's generator at the cell's parameters."""
    import common
    from workload.data.datagen import generate_events
    cfg = common.make_datagen_config(c["n"], c["sigma"], ftype=c["ftype"], ntypes=c["ntypes"], seed=c["seed"])
    return generate_events(cfg).frame


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--list":
        for c in sorted(cells(REPRO_AXES), key=lambda c: -(c["n"] * c["batches"] * c["k"])):
            print(cell_name(c))
        raise SystemExit(0)
    for c in cells(REPRO_AXES):
        print(cell_name(c), c["axes"], "wall", wall_seconds(c))
