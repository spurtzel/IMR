"""Shared helpers for the demo experiment drivers.

Everything here is engine-free: query-spec patching to the synthetic-data schema,
DatagenConfig construction, and constant-selectivity workloads. The live Trino
side lives in demo/trino_exec.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for _p in (str(REPO), str(REPO / "execution")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from workload.data.datagen import (  # noqa: E402
    BandTarget, DatagenConfig, EquiTarget, SpatialConfig, TemporalConfig, generate_events)
from eimer.query.query_spec import load_query_spec_file, query_spec_to_dependency_graph  # noqa: E402
from eimer.workload import ConstantSelectivity, Selectivities, make_workload  # noqa: E402

# Synthetic-data model: NTYPES event types T0..T7 so any pattern length k <= 8 runs on
# one table; each type covers FTYPE of the rows. Band half-width is fixed and the QUERY
# band must reuse it. Selectivity is calibrated at generation time: BandTarget(sigma,
# half_width) makes two i.i.d. rows fall inside the band with probability sigma;
# EquiTarget(sigma) adds a categorical ekey column balanced over K = round(1/sigma)
# values (equality selectivity 1/K, independent of the spatial clusters).
NTYPES = 8
FTYPE = 0.05
SEED = 42
HALF_WIDTH = 0.02
EQUI_SIGMA = 0.25  # band_equi cells: fixed K=4 key; per-edge sigma = band sigma * EQUI_SIGMA
EVENT_COLS = ("id", "time", "ts", "primary_type", "etype", "lon", "lat")

SPEC_DIR = REPO / "execution" / "query_specs" / "overnight_suite"
TOPOLOGY_SPECS = {
    (3, "chain"): "q3_chain_rare_spatial", (3, "star"): "q3_star_rare_spatial",
    (3, "triangle"): "q3_triangle_rare_spatial", (3, "long_edge"): "q3_long_edge_rare_spatial",
    (4, "chain"): "q4_chain_rare_spatial", (4, "star"): "q4_star_anchor_spatial",
    (4, "triangle"): "q4_triangle_rare_spatial", (4, "long_edge"): "q4_long_edge_rare_spatial",
    (4, "cycle"): "q4_cycle_rare_spatial", (4, "diamond"): "q4_diamond_rare_spatial",
    (4, "complete"): "q4_complete_rare_spatial",
    (5, "chain"): "q5_chain_rare_spatial", (5, "star"): "q5_star_rare_spatial",
    (5, "cycle"): "q5_cycle_rare_spatial",
    (6, "chain"): "q6_chain_rare_spatial", (6, "star"): "q6_star_rare_spatial",
    (6, "cycle"): "q6_cycle_rare_spatial",
    (7, "chain"): "q7_chain_rare_spatial", (7, "star"): "q7_star_rare_spatial",
    (8, "chain"): "q8_chain_rare_spatial",
}


def type_names(ntypes: int = NTYPES) -> list[str]:
    return [f"T{i}" for i in range(ntypes)]


def patch_spec_for_datagen(k: int, topology: str, predicates: str = "band") -> str:
    """Patch a stored query spec to the synthetic-data schema: variable i matches
    primary_type='T{i}'; every dependent predicate body is rewritten per ,,predicates'':
    band = the 2-D lon/lat band with HALF_WIDTH, equi = ekey equality, band_equi =
    both conjoined. Structure (variables + edge topology) is kept. Returns the
    patched path."""
    if predicates not in ("band", "equi", "band_equi"):
        raise SystemExit(f"unknown predicate class {predicates!r}")
    name = TOPOLOGY_SPECS[(k, topology)]
    spec = json.loads((SPEC_DIR / f"{name}.json").read_text())
    var_order = list(spec["pattern"]["variables"])
    if len(var_order) != k:
        raise SystemExit(f"spec {name} has {len(var_order)} vars, need k={k}")
    type_for = {v: f"T{i}" for i, v in enumerate(var_order)}
    for p in spec["independent_predicates"]:
        v = p["variables"][0]
        p["sql_template"] = f"{v}.primary_type = '{type_for[v]}'"
    for p in spec.get("dependent_predicates", []):
        body = p["sql_template"]
        m = re.search(r"(\w+)\.lon\s+BETWEEN\s+(\w+)\.lon", body)
        if not m:
            raise SystemExit(f"cannot parse dependent band: {body}")
        tgt, ref = m.group(1), m.group(2)
        band = (f"{tgt}.lon BETWEEN {ref}.lon - {HALF_WIDTH} AND {ref}.lon + {HALF_WIDTH} "
                f"AND {tgt}.lat BETWEEN {ref}.lat - {HALF_WIDTH} AND {ref}.lat + {HALF_WIDTH}")
        equi = f"{tgt}.ekey = {ref}.ekey"
        p["sql_template"] = {"band": band, "equi": equi,
                             "band_equi": f"{band} AND {equi}"}[predicates]
        p["referenced_columns"] = {"band": ["lon", "lat"], "equi": ["ekey"],
                                   "band_equi": ["lon", "lat", "ekey"]}[predicates]
    if predicates != "band":
        schema = list(spec.get("event_schema") or [])
        if not any(c.get("name") == "ekey" for c in schema):
            schema.append({"name": "ekey", "sql_type": "BIGINT"})
        spec["event_schema"] = schema
    out_path = Path(tempfile.gettempdir()) / f"{name}_demo_{predicates}.{os.getpid()}.json"
    out_path.write_text(json.dumps(spec, indent=2))
    return str(out_path)


def make_datagen_config(scale: int, sigma: float, *, ftype: float = FTYPE,
                        ntypes: int = NTYPES, seed: int = SEED,
                        predicates: str = "band") -> DatagenConfig:
    """The calibrated synthetic generator config: uniform per-type rate, Chicago-like
    spatial mixture, 1-ms temporal grid. sigma calibrates the band (predicates=band),
    the equi key (equi: realized as 1/round(1/sigma)), or the band with the fixed
    EQUI_SIGMA key on top (band_equi: per-edge sigma = sigma * EQUI_SIGMA)."""
    band = BandTarget(sigma=sigma, half_width_lon=HALF_WIDTH, half_width_lat=HALF_WIDTH)
    spatial_band, equi = {"band": (band, None),
                          "equi": (None, EquiTarget(sigma)),
                          "band_equi": (band, EquiTarget(EQUI_SIGMA))}[predicates]
    return DatagenConfig(
        types=tuple((nm, ftype) for nm in type_names(ntypes)),
        batch_sizes=(scale,),
        seed=seed,
        spatial=SpatialConfig(),
        temporal=TemporalConfig(rho=1000.0),
        spatial_band=spatial_band,
        equi=equi,
    )


def generate_frame(scale: int, sigma: float, *, ftype: float = FTYPE, seed: int = SEED,
                   predicates: str = "band"):
    """Deterministic synthetic event frame (pandas DataFrame) for (scale, sigma)."""
    return generate_events(make_datagen_config(scale, sigma, ftype=ftype, seed=seed,
                                               predicates=predicates)).frame


def const_sigma_workload(dep, *, total_events: int, batches: int,
                         sigma: float, ftype: float = FTYPE):
    """Workload using the generation-target selectivities (constant per edge). The
    memory-budget demo uses these calibrated targets so its budget descent stays
    engine-free; the other drivers select from measured b_unified estimates
    (trino_exec.b_unified_workload)."""
    sels = Selectivities(
        independent={v: ftype for v in dep.variables},
        dependent={(e.var_left, e.var_right): (ConstantSelectivity(sigma),)
                   for e in dep.edges},
    )
    return make_workload(dep, total_events=total_events, batches=batches,
                         selectivities=sels)


def load_spec_and_dep(k: int, topology: str, predicates: str = "band"):
    spec_path = patch_spec_for_datagen(k, topology, predicates)
    spec = load_query_spec_file(spec_path)
    dep = query_spec_to_dependency_graph(spec)
    return spec, dep, spec_path


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")
