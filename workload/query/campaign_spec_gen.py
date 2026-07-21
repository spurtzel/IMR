"""Parametric MATCH_RECOGNIZE query-spec generator.

Emits overnight_suite-format specs for any pattern length n, topology shape, and band
tightness.

CLI: ,,python3 workload/query/campaign_spec_gen.py --emit-n5-probe''  -> q5_{chain,star,cycle}.
"""
import json, argparse
from pathlib import Path

# Types produced by execution/sql_generator.py. Index = variable.
RARE_TYPES = ["ROBBERY", "BATTERY", "MOTOR VEHICLE THEFT", "BURGLARY", "NARCOTICS", "ASSAULT"]
SPECDIR = Path("execution/query_specs/overnight_suite")


def var_name(i: int) -> str:
    return chr(65 + i)  # A, B, C, ...


def edges(n: int, shape: str):
    """Dependent-predicate edge set (i<j index pairs) for a topology on n variables."""
    if shape == "chain":        return [(i, i + 1) for i in range(n - 1)]            # path P_n
    if shape == "star":         return [(0, j) for j in range(1, n)]                 # A central
    if shape == "cycle":        return [(i, i + 1) for i in range(n - 1)] + [(0, n - 1)]
    if shape in ("clique", "complete"): return [(i, j) for i in range(n) for j in range(i + 1, n)]
    if shape == "triangle":     return [(0, 1), (1, 2), (0, 2)] + [(i, i + 1) for i in range(2, n - 1)]
    #                             K3 on the first three vars plus a pendant chain; edge order
    #                             (chain first, then the closing (0,2)) is fixed for byte-stable output.
    if shape == "diamond":      return [(0, 1), (0, 2), (0, 3), (1, 2), (2, 3)] if n == 4 else _diamond_err(n)
    #                             K4 minus the (1,3) edge.
    if shape == "long_edge":    return [(0, n - 1)]                                  # single far edge
    if shape == "independent":  return []
    raise ValueError(f"unknown shape {shape!r}")


def _diamond_err(n):
    raise ValueError(f"diamond is a 4-variable topology (got n={n})")


def edge_classes(n, shape, predicate_classes="band"):
    """Per-edge dependent-predicate class list. 'band' (default) / 'equi' uniform;
    'mixed' = DETERMINISTIC alternation by edge index (band, equi, band, ...), never
    random (a random per-edge mix makes single cells structurally high-variance)."""
    es = edges(n, shape)
    if predicate_classes == "mixed":
        return ["band" if i % 2 == 0 else "equi" for i in range(len(es))]
    if predicate_classes in ("band", "equi"):
        return [predicate_classes] * len(es)
    raise ValueError(f"unknown predicate_classes {predicate_classes!r}")


def make_spec(n, shape, band_lon=0.05, band_lat=0.02, sel_class="rare", types=None, name=None,
              predicate_classes="band") -> dict:
    types = types or RARE_TYPES
    if n > len(types):
        raise ValueError(f"need >= {n} distinct types, have {len(types)}")
    V = [var_name(i) for i in range(n)]
    classes = edge_classes(n, shape, predicate_classes)
    name = name or f"q{n}_{shape}_{sel_class}_spatial"
    spec = {
        "name": name,
        "suite_metadata": {"pattern_length": n, "shape": shape, "independent_selectivity_class": sel_class,
                           "dependent_predicate_strength": "canonical", "band_lon": band_lon, "band_lat": band_lat,
                           **({"predicate_classes": predicate_classes} if predicate_classes != "band" else {})},
        "variables": [{"name": V[i], "output_prefix": V[i].lower(), "independent_predicate_id": f"{V[i]}_type"}
                      for i in range(n)],
        "pattern": {"variables": V, "wildcard": "Z", "wildcard_quantifier": "*"},
        "independent_predicates": [
            {"predicate_id": f"{V[i]}_type", "kind": "independent", "variables": [V[i]],
             "sql_template": f"{V[i]}.primary_type = '{types[i]}'", "referenced_columns": ["primary_type"],
             "selectivity_key": V[i]} for i in range(n)],
        "dependent_predicates": [
            ({"predicate_id": f"{V[i]}_{V[j]}_spatial", "kind": "dependent", "variables": [V[i], V[j]],
              "sql_template": (f"{V[j]}.lon BETWEEN {V[i]}.lon - {band_lon} AND {V[i]}.lon + {band_lon} "
                               f"AND {V[j]}.lat BETWEEN {V[i]}.lat - {band_lat} AND {V[i]}.lat + {band_lat}"),
              "referenced_columns": ["lon", "lat"], "selectivity_key": f"{V[i]}|{V[j]}"}
             if cls == "band" else
             {"predicate_id": f"{V[i]}_{V[j]}_equi", "kind": "dependent", "variables": [V[i], V[j]],
              "sql_template": f"{V[j]}.ekey = {V[i]}.ekey",
              "referenced_columns": ["ekey"], "selectivity_key": f"{V[i]}|{V[j]}"})
            for (i, j), cls in zip(edges(n, shape), classes)],
        "measures": [m for i in range(n) for m in (
            {"expression": f"{V[i]}.id", "alias": f"{V[i].lower()}_id", "sql_type": "BIGINT"},
            {"expression": f"{V[i]}.time", "alias": f"{V[i].lower()}_time", "sql_type": "TIMESTAMP(6)"})],
        "result_key_columns": [c for i in range(n) for c in (f"{V[i].lower()}_id", f"{V[i].lower()}_time")],
        "event_schema": [
            {"name": "id", "sql_type": "BIGINT"}, {"name": "time", "sql_type": "TIMESTAMP(6)"},
            {"name": "ts", "sql_type": "TIMESTAMP(6)"}, {"name": "primary_type", "sql_type": "VARCHAR"},
            {"name": "etype", "sql_type": "VARCHAR"}, {"name": "lon", "sql_type": "DOUBLE"},
            {"name": "lat", "sql_type": "DOUBLE"}]
        + ([{"name": "ekey", "sql_type": "BIGINT"}] if "equi" in classes else []),
        "id_column": "id", "time_column": "time", "ts_column": "ts", "order_by": ["time"],
        "row_output": "ONE ROW PER MATCH", "after_match": "AFTER MATCH SKIP TO NEXT ROW",
        "partition_by": [], "window": None, "kleene_annotations": [],
    }
    return spec


def write_spec(spec) -> Path:
    p = SPECDIR / f"{spec['name']}.json"
    p.write_text(json.dumps(spec, indent=2))
    return p


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit-n5-probe", action="store_true", help="emit q5_{chain,star,cycle} for the feasibility probe")
    a = ap.parse_args()
    if a.emit_n5_probe:
        for shape in ("chain", "star", "cycle"):
            sp = make_spec(5, shape)
            p = write_spec(sp)
            print(f"wrote {p}  edges={[d['variables'] for d in sp['dependent_predicates']]}")
