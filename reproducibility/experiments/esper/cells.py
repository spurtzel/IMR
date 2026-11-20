#!/usr/bin/env python3
"""Cells of the Esper experiment (EIMER's strategies on Esper vs. Esper's native match_recognize).

The workload is the windowed chain of the paper's cross-engine comparison: eight event types at rate 0.05, band
sigma 0.05, window W = 1 s, 1,000 rows per second, seed 42. The figure of the paper has two panels from two centers:
  ESPER_CENTER=fig8s005       the N panel: table size at a constant batch size of 200 events (axis N200, B = N / 200), k = 4
  ESPER_CENTER=fig8s005n25k   the k panel: pattern length at N = 25,000 and B = 25

The axis values can be reduced or extended without editing this file:
  EIMER_REPRO_ESPER_NS=2000,5000,10000        table sizes of the N200 axis (multiples of 200)
  EIMER_REPRO_ESPER_KS=3,4,5                  pattern lengths of the k axis (3..8)
  EIMER_REPRO_WALL=600                        time limit in seconds per batch and per arm (default: tiers below)
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EIMER_REPO = Path(os.environ.get("EIMER_REPO", HERE.parents[2]))   # the repository root (eimer/, demo/, execution/, workload/)
for _p in (str(EIMER_REPO), str(EIMER_REPO / "execution"), str(EIMER_REPO / "demo")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

HALF_WIDTH = 0.02
# the generated query specifications (one per pattern length); EIMER_REPRO_SPECS_DIR points them at a writable directory
SPECS = Path(os.environ.get("EIMER_REPRO_SPECS_DIR", HERE / "specs"))
PAPER_NS = (2_000, 5_000, 10_000, 25_000, 50_000, 100_000, 250_000, 500_000)
PAPER_KS = (3, 4, 5, 6, 7, 8)


def _ints(env: str, default: tuple) -> list:
    raw = os.environ.get(env, "").strip()
    return [int(x) for x in raw.replace(" ", "").split(",") if x] if raw else list(default)


_NS = _ints("EIMER_REPRO_ESPER_NS", PAPER_NS)
_KS = _ints("EIMER_REPRO_ESPER_KS", PAPER_KS)
assert all(n % 200 == 0 and n >= 200 for n in _NS), f"N200 axis: multiples of 200 expected, got {_NS}"
assert all(3 <= k <= 8 for k in _KS), f"k axis: 3..8, got {_KS}"

# the cross-engine regime: eight types T0..T7 at rate 0.05, W = 1 s, B = 25
CENTER_FIG8 = dict(k=4, n=5_000, batches=25, w_ms=1000, sigma=0.02, ftype=0.05, ntypes=8, seed=42)
AXES_FIG8 = {"N": ("n", [2_000, 5_000, 10_000, 25_000, 50_000]),
             "W": ("w_ms", [500, 1000, 2000, 3000, 5000]),
             "B": ("batches", [5, 25, 100, 250, 1000]),
             "k": ("k", [3, 4, 5, 6, 7, 8])}
# the same regime at band sigma 0.05, where the chain yields matches for every pattern length; its N200 axis varies the
# table size at a constant batch size of 200 events (the center's 5k / 25), B = N / 200
CENTER_FIG8_S005 = dict(CENTER_FIG8, sigma=0.05)
AXES_FIG8_S005 = dict(AXES_FIG8, N=("n", [2_000, 5_000, 10_000, 25_000, 50_000, 100_000, 250_000, 500_000]),
                      N200=("n", [dict(n=n, batches=n // 200) for n in _NS]))
CENTER_FIG8_S005_N25K = dict(CENTER_FIG8_S005, n=25_000)
AXES_FIG8_S005_N25K = {"W": ("w_ms", [200, 500, 1000]),
                       "B": ("batches", [5, 25, 100, 250, 1000]),
                       "k": ("k", _KS)}
CENTERS = {"fig8": (CENTER_FIG8, AXES_FIG8), "fig8s005": (CENTER_FIG8_S005, AXES_FIG8_S005),
           "fig8s005n25k": (CENTER_FIG8_S005_N25K, AXES_FIG8_S005_N25K)}
REPRO_AXES = {"fig8s005": ("N200",), "fig8s005n25k": ("k",)}   # the two panels of the paper's figure


def active_center(name: str | None = None):
    """(center, axes) for ESPER_CENTER (or the given name)."""
    return CENTERS[name or os.environ.get("ESPER_CENTER", "fig8s005")]


CHAIN_SPEC = {k: f"q{k}_chain_rare_spatial" for k in (3, 4, 5, 6, 7, 8)}


def cell_name(c: dict) -> str:
    return f"k{c['k']}_n{c['n']}_b{c['batches']}_w{c['w_ms']}"


def cells(center: str | None = None, axes: tuple | None = None) -> list[dict]:
    out: dict[str, dict] = {}
    ctr, all_axes = active_center(center)
    for axis, (param, values) in all_axes.items():
        if axes and axis not in axes:
            continue
        for v in values:
            c = {**ctr, **v} if isinstance(v, dict) else {**ctr, param: v}   # a dict value sets several parameters (N200 axis)
            out.setdefault(cell_name(c), {**c, "axes": []})["axes"].append(axis)
    return sorted(out.values(), key=lambda c: (c["n"], c["batches"], c["k"], c["w_ms"]))


def repro_cells(center: str | None = None) -> list[dict]:
    """the cells of the paper's figure for the active (or given) center."""
    name = center or os.environ.get("ESPER_CENTER", "fig8s005")
    return cells(name, REPRO_AXES[name])


def wall_seconds(c: dict) -> int:
    """per batch and per arm, every arm of a cell alike: 1,800 s; 3,600 s from N 50k or 500 batches. EIMER_REPRO_WALL overrides."""
    if os.environ.get("EIMER_REPRO_WALL"):
        return int(os.environ["EIMER_REPRO_WALL"])
    return 3600 if (c["n"] >= 50_000 or c["batches"] >= 500) else 1800


def types_for(k: int, ntypes: int | None = None) -> dict:
    """variable -> event type: T0, T1, ... (the identity T0..T7 for the eight types of this regime)."""
    nt = ntypes or active_center()[0]["ntypes"]
    return {v: f"T{i % nt}" for i, v in enumerate("ABCDEFGH"[:k])}


def build_spec(k: int, w_ms: int, ntypes: int | None = None) -> Path:
    nt = ntypes or active_center()[0]["ntypes"]
    src = EIMER_REPO / "execution" / "query_specs" / "overnight_suite" / f"{CHAIN_SPEC[k]}.json"
    spec = json.loads(src.read_text())
    var_order = list(spec["pattern"]["variables"]); assert len(var_order) == k
    type_for = types_for(k, nt)
    for p in spec["independent_predicates"]:
        v = p["variables"][0]; p["sql_template"] = f"{v}.primary_type = '{type_for[v]}'"
    for p in spec["dependent_predicates"]:
        m = re.search(r"(\w+)\.lon\s+BETWEEN\s+(\w+)\.lon", p["sql_template"]); tgt, ref = m.group(1), m.group(2)
        p["sql_template"] = (f"{tgt}.lon BETWEEN {ref}.lon - {HALF_WIDTH} AND {ref}.lon + {HALF_WIDTH} "
                             f"AND {tgt}.lat BETWEEN {ref}.lat - {HALF_WIDTH} AND {ref}.lat + {HALF_WIDTH}")
        p["referenced_columns"] = ["lon", "lat"]
    first, last = var_order[0], var_order[-1]
    spec["dependent_predicates"].append({"predicate_id": f"{first}_{last}_window", "kind": "dependent", "variables": [first, last],
        "sql_template": f"abs(date_diff('millisecond', {first}.ts, {last}.ts)) <= {w_ms}", "referenced_columns": ["ts"], "selectivity_key": f"{first}|{last}"})
    spec["name"] = f"chain_k{k}_w{w_ms}" + ("" if nt == 5 else f"_t{nt}")
    out = SPECS / f"{spec['name']}.json"; out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(f".{os.getpid()}.tmp")  # several parallel cells write the same spec: atomic replace
    tmp.write_text(json.dumps(spec, indent=2) + "\n"); os.replace(tmp, out)
    return out


def load_spec(k: int, w_ms: int, ntypes: int | None = None):
    from eimer.query.query_spec import load_query_spec_file, query_spec_to_dependency_graph
    path = build_spec(k, w_ms, ntypes); spec = load_query_spec_file(str(path))
    return spec, query_spec_to_dependency_graph(spec), path


def generate_frame(c: dict):
    import common
    from workload.data.datagen import generate_events
    cfg = common.make_datagen_config(c["n"], c["sigma"], ftype=c["ftype"], ntypes=c["ntypes"], seed=c["seed"])
    return generate_events(cfg).frame


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--list":  # the reproduction's cells of the active center, largest first
        for c in sorted(repro_cells(), key=lambda c: -(wall_seconds(c) * c["n"] * c["batches"] * c["k"])):
            print(cell_name(c))
        raise SystemExit(0)
    for c in repro_cells():
        print(cell_name(c), c["axes"], "wall", wall_seconds(c))
