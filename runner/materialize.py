"""Materialize a config into a concrete QuerySpec JSON and a dataset.

Three couplings are each declared once in the config and derived here into both
generators, so a spec cannot disagree with its calibration target:

  1. band_tightness -> BandTarget.sigma (datagen) and the spatial band literal in
                       every dependent predicate (query), at fixed half-widths (0.05, 0.02).
  2. independent_selectivity -> datagen type presence fractions (datagen) and
                       per-variable ,,primary_type = '<type>''' predicates (query).
  3. the type set = the keys of independent_selectivity in declaration order, into
                       both: variable i <-> types[i].
"""

from __future__ import annotations

import dataclasses
import random
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "execution"))

from workload.query.campaign_spec_gen import make_spec, var_name  # noqa: E402
from runner.schema import ConfigError, DataConfig, ExperimentConfig  # noqa: E402

# Fixed band half-widths: band_tightness (sigma) is the single dependent-selectivity
# tunable, realized by datagen calibration at these widths.
BAND_HALF_WIDTH_LON = 0.05
BAND_HALF_WIDTH_LAT = 0.02

_GAP_TOKEN = {"greedy": "*", "reluctant": "*?"}


# --------------------------------------------------------------------------- #
# query materializer
# --------------------------------------------------------------------------- #

def _parse_free_measure(measure: str, variables: list[str]) -> dict:
    expression, sep, alias = measure.rpartition(" AS ")
    if not sep or not expression.strip() or not alias.strip():
        raise ConfigError(f"query.measures entry {measure!r} must have the form "
                          "'<EXPRESSION> AS <alias>'")
    # Reject references to undefined variables here: they pass schema checks and only
    # fail as invalid MR SQL at execution.
    referenced = set(re.findall(r"\b([A-Z][A-Z0-9_]*)\s*\.", expression))
    unknown = referenced - set(variables)
    if unknown:
        raise ConfigError(f"query.measures entry {measure!r} references undefined "
                          f"variable(s) {sorted(unknown)}: this query's variables are "
                          f"{variables} (positional, one per declared type)")
    return {"expression": expression.strip(), "alias": alias.strip(), "sql_type": None}


def materialize_query_spec_dict(exp: ExperimentConfig) -> dict:
    """Config -> spec dict (deterministic for a given config)."""
    q = exp.query
    types = list(exp.data.independent_selectivity.keys())  # declaration order
    n_conj = q.pattern_length
    n_kleene = q.kleene.count if q.kleene else 0
    total = n_conj + n_kleene
    if total > len(types):
        raise ConfigError(f"{total} variables ({n_conj} conjunctive + {n_kleene} Kleene) "
                          f"but only {len(types)} declared types: variables bind types "
                          "positionally (var i -> types[i])")

    hw_lon, hw_lat = exp.data.band_half_widths or (BAND_HALF_WIDTH_LON, BAND_HALF_WIDTH_LAT)
    spec = make_spec(n_conj, q.topology,
                     band_lon=hw_lon, band_lat=hw_lat,
                     types=types, name=f"cfg_q{total}_{q.topology}")

    # Kleene variables: appended after the conjunctive pattern, off the join graph
    # (no dependent predicate may touch a Kleene variable: validate_query_spec gate).
    for k in range(n_kleene):
        idx = n_conj + k
        name = var_name(idx)
        prefix = name.lower()
        spec["variables"].append({
            "name": name, "output_prefix": prefix,
            "independent_predicate_id": f"{name}_type",
            "quantifier": q.kleene.quantifier,
        })
        spec["pattern"]["variables"].append(name)
        spec["independent_predicates"].append({
            "predicate_id": f"{name}_type", "kind": "independent", "variables": [name],
            "sql_template": f"{name}.primary_type = '{types[idx]}'",
            "referenced_columns": ["primary_type"], "selectivity_key": name,
        })
        # Kleene boundary aliases are part of the full-tuple identity, so they go into
        # measures and result_key_columns.
        spec["measures"].extend([
            {"expression": f"FIRST({name}.id)", "alias": f"{prefix}_first_id", "sql_type": "BIGINT"},
            {"expression": f"LAST({name}.id)", "alias": f"{prefix}_last_id", "sql_type": "BIGINT"},
        ])
        spec["result_key_columns"].extend([f"{prefix}_first_id", f"{prefix}_last_id"])

    # Per-gap wildcard quantifiers: conjunctive gaps stay greedy "*"; the gap before
    # each Kleene variable takes the configured mode. Emitted only when some gap differs
    # from the default.
    if n_kleene:
        gaps = ["*"] * (n_conj - 1) + [_GAP_TOKEN[q.kleene.gap]] * n_kleene
        if any(g != "*" for g in gaps):
            spec["pattern"]["gap_quantifiers"] = gaps

    # Free-form extra measures (e.g. COUNT over a Kleene variable), appended last,
    # not part of the identity key set.
    variable_names = [v["name"] for v in spec["variables"]]
    for measure in q.measures:
        spec["measures"].append(_parse_free_measure(measure, variable_names))

    return spec


def write_query_spec(exp: ExperimentConfig, out_path: Path):
    """Materialize, validate, and dump the spec deterministically; returns the QuerySpec."""
    from eimer.query.query_spec import (
        dump_query_spec_file, query_spec_from_dict, validate_query_spec)
    payload = materialize_query_spec_dict(exp)
    spec = query_spec_from_dict(payload)
    validate_query_spec(spec)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dump_query_spec_file(spec, str(out_path))
    return spec


# --------------------------------------------------------------------------- #
# data materializer
# --------------------------------------------------------------------------- #

def draw_seed() -> int:
    """A fresh datagen seed for seed-less configs; the caller records it."""
    return random.SystemRandom().randrange(2**31)


def materialize_dataset(exp: ExperimentConfig, spec_path: Path, out_dir: Path, seed: int) -> Path:
    """Config -> parquet batches + manifest, via the datagen library.

    batch_sizes is data.stream verbatim. Raises ConfigError on generation-gate
    failures (feasibility floor / calibration reachability).
    """
    import json

    from workload.data.datagen import (
        BandTarget, BurstConfig, DatagenConfig, SpatialConfig, TemporalConfig,
        generate_events, verify_dataframe, write_dataset)
    from workload.data.datagen.spec_edges import edges_from_query_spec
    from workload.data.datagen.verification import shared_variable_pairs

    d: DataConfig = exp.data

    # Idempotent skip: reuse an existing manifest only if it matches this config's
    # identity (seed, batch schedule, type rates, band); a mismatch fails loud.
    existing = Path(out_dir) / "manifest.json"
    if existing.exists():
        manifest = json.loads(existing.read_text(encoding="utf-8"))
        cfg = manifest.get("config", {})
        band_match = (cfg.get("spatial_band") or {}).get("sigma") == exp.band_tightness \
            if exp.band_tightness is not None else cfg.get("spatial_band") is None
        if (manifest.get("seed") == seed
                and list(manifest.get("batch_sizes", [])) == list(d.stream)
                and [tuple(t) for t in cfg.get("types", [])] == list(d.independent_selectivity.items())
                and band_match):
            return existing
        raise ConfigError(f"{out_dir} holds a dataset that does not match this cell's "
                          "config (seed/stream/types/band differ): refusing to reuse; "
                          "use a fresh out-dir")
    burst = None
    if d.burst is not None:
        burst = BurstConfig(burst_rows=d.burst.rows, period_rows=d.burst.period,
                            rate_multiplier=d.burst.multiplier)
    band = None
    if exp.band_tightness is not None:
        hw_lon, hw_lat = d.band_half_widths or (BAND_HALF_WIDTH_LON, BAND_HALF_WIDTH_LAT)
        band = BandTarget(sigma=exp.band_tightness,
                          half_width_lon=hw_lon,
                          half_width_lat=hw_lat)

    config = DatagenConfig(
        types=tuple(d.independent_selectivity.items()),  # type rates, declaration order
        batch_sizes=tuple(d.stream),                     # verbatim
        seed=seed,
        spatial=SpatialConfig(cluster_count=d.cluster_count),
        temporal=TemporalConfig(rho=d.rho, burst=burst),
        spatial_band=band,
    )

    try:
        result = generate_events(config)
    except ValueError as exc:  # feasibility floor / calibration reachability
        raise ConfigError(f"dataset generation rejected the configuration: {exc}") from exc

    independents, edges = edges_from_query_spec(spec_path)
    if band is not None:
        edges = tuple(
            dataclasses.replace(edge, target_sigma=band.sigma)
            if getattr(edge, "half_width_lon", None) == band.half_width_lon
            and getattr(edge, "half_width_lat", None) == band.half_width_lat
            else edge
            for edge in edges
        )
    rate_targets = dict(config.types)
    independent_targets = {
        name: rate_targets[predicate.value]
        for name, predicate in (independents or {}).items()
        if getattr(predicate, "value", None) in rate_targets
    }
    report = verify_dataframe(result.frame, independents=independents, edges=edges,
                              path2_pairs=shared_variable_pairs(edges),
                              independent_targets=independent_targets)
    return write_dataset(out_dir, config, result, report)
