"""Derive verification predicates/edges from a benchmark query-spec JSON.

The regexes mirror execution/compute_selectivities.py, kept self-contained so the
generator package does not import benchmark modules. Unsupported shapes raise.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from workload.data.datagen.verification import (
    ColumnThreshold,
    Edge,
    IdWindowEdge,
    Predicate,
    SpatialBandEdge,
    TemporalWindowEdge,
    TypeEquals,
)

_FLOAT = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?"

# Predicate-shape matchers: reconstruct a datagen edge from a dependent condition's SQL text.
# ,,var.col = 'value''': a categorical type equality.
_TYPE_EQ_RE = re.compile(r"(?is)^\s*(?P<var>\w+)\s*\.\s*(?P<col>\w+)\s*=\s*'(?P<value>[^']*)'\s*$")
# ,,var.col OP <number>'': an independent numeric threshold.
_THRESHOLD_RE = re.compile(rf"(?is)^\s*(?P<var>\w+)\s*\.\s*(?P<col>\w+)\s*(?P<op><=|>=|<|>)\s*(?P<lit>{_FLOAT})\s*$")
# ,,subj.col BETWEEN anchor.col - d AND anchor.col + d'': a symmetric spatial band of half-width d.
_SPATIAL_BETWEEN_RE = re.compile(
    rf"(?is)(?P<subject>\w+)\s*\.\s*(?P<subject_col>\w+)\s+BETWEEN\s+"
    rf"(?P<anchor>\w+)\s*\.\s*(?P<anchor_col>\w+)\s*-\s*(?P<delta>{_FLOAT})\s+AND\s+"
    rf"(?P=anchor)\s*\.\s*(?P=anchor_col)\s*\+\s*(?P<delta2>{_FLOAT})"
)
# ,,subj.ts BETWEEN anchor.ts AND anchor.ts + INTERVAL 'v' unit'': a forward temporal window.
_TEMPORAL_BETWEEN_RE = re.compile(
    r"(?is)^\s*(?P<subject>\w+)\s*\.\s*(?P<col>time|ts)\s+BETWEEN\s+"
    r"(?P<anchor>\w+)\s*\.\s*(?P=col)\s+AND\s+"
    r"(?P=anchor)\s*\.\s*(?P=col)\s*\+\s*INTERVAL\s*'(?P<value>[^']+)'\s*(?P<unit>\w+)\s*$"
)
# ,,abs(date_diff('millisecond', anchor.ts, subj.ts)) <= millis'': a symmetric temporal window.
_ABS_DATE_DIFF_RE = re.compile(
    r"(?is)^\s*abs\s*\(\s*date_diff\s*\(\s*'millisecond'\s*,\s*"
    r"(?P<anchor>\w+)\s*\.\s*(?P<col>time|ts)\s*,\s*(?P<subject>\w+)\s*\.\s*(?P=col)\s*\)\s*\)\s*<=\s*(?P<millis>\d+)\s*$"
)
# ,,subj.id BETWEEN anchor.id AND anchor.id + width'': a forward id-offset band.
_ID_BETWEEN_RE = re.compile(
    r"(?is)^\s*(?P<subject>\w+)\s*\.\s*id\s+BETWEEN\s+(?P<anchor>\w+)\s*\.\s*id\s+AND\s+"
    r"(?P=anchor)\s*\.\s*id\s*\+\s*(?P<width>\d+)\s*$"
)

_INTERVAL_MICROS = {
    "SECOND": 1_000_000,
    "SECONDS": 1_000_000,
    "MINUTE": 60_000_000,
    "MINUTES": 60_000_000,
    "HOUR": 3_600_000_000,
    "HOURS": 3_600_000_000,
}

# abs(date_diff('millisecond', a, b)) <= m with truncation toward zero admits
# any |delta| < (m+1) ms, i.e. up to m*1000 + 999 whole microseconds.
_TRUNC_SLACK_MICROS = 999


def parse_independent_predicate(sql_template: str, variable: str) -> Predicate:
    match = _TYPE_EQ_RE.match(sql_template)
    if match:
        if match.group("var") != variable:
            raise ValueError(f"independent predicate {sql_template!r} does not reference variable {variable!r}")
        return TypeEquals(value=match.group("value"), column=match.group("col"))
    match = _THRESHOLD_RE.match(sql_template)
    if match:
        if match.group("var") != variable:
            raise ValueError(f"independent predicate {sql_template!r} does not reference variable {variable!r}")
        return ColumnThreshold(column=match.group("col"), op=match.group("op"), literal=float(match.group("lit")))
    raise ValueError(f"unsupported independent predicate shape: {sql_template!r}")


def _parse_dependent(name: str, sql_template: str, predicates: dict[str, Predicate]) -> Edge:
    bands = list(_SPATIAL_BETWEEN_RE.finditer(sql_template))
    if bands:
        remainder = _SPATIAL_BETWEEN_RE.sub("", sql_template)
        if remainder.strip().strip("()").replace("AND", "").strip():
            raise ValueError(f"dependent predicate {name!r} mixes band and non-band conjuncts: {sql_template!r}")
        anchor_var = bands[0].group("anchor")
        subject_var = bands[0].group("subject")
        half_widths: dict[str, float] = {}
        for band in bands:
            if band.group("anchor") != anchor_var or band.group("subject") != subject_var:
                raise ValueError(f"dependent predicate {name!r} mixes variable pairs: {sql_template!r}")
            if band.group("delta") != band.group("delta2"):
                raise ValueError(f"asymmetric band in {name!r}: {sql_template!r}")
            column = band.group("subject_col").lower()
            if band.group("anchor_col").lower() != column or column not in ("lon", "lat"):
                raise ValueError(f"unsupported band columns in {name!r}: {sql_template!r}")
            half_widths[column] = float(band.group("delta"))
        return SpatialBandEdge(
            name=name,
            anchor=predicates[anchor_var],
            subject=predicates[subject_var],
            half_width_lon=half_widths.get("lon"),
            half_width_lat=half_widths.get("lat"),
        )

    match = _TEMPORAL_BETWEEN_RE.match(sql_template)
    if match:
        unit = match.group("unit").upper()
        if unit not in _INTERVAL_MICROS:
            raise ValueError(f"unsupported interval unit in {name!r}: {sql_template!r}")
        width = round(float(match.group("value")) * _INTERVAL_MICROS[unit])
        return TemporalWindowEdge(
            name=name,
            anchor=predicates[match.group("anchor")],
            subject=predicates[match.group("subject")],
            lower_micros=0,
            upper_micros=width,
            column=match.group("col").lower(),
        )

    match = _ABS_DATE_DIFF_RE.match(sql_template)
    if match:
        bound = int(match.group("millis")) * 1000 + _TRUNC_SLACK_MICROS
        return TemporalWindowEdge(
            name=name,
            anchor=predicates[match.group("anchor")],
            subject=predicates[match.group("subject")],
            lower_micros=-bound,
            upper_micros=bound,
            column=match.group("col").lower(),
        )

    match = _ID_BETWEEN_RE.match(sql_template)
    if match:
        return IdWindowEdge(
            name=name,
            anchor=predicates[match.group("anchor")],
            subject=predicates[match.group("subject")],
            lower=0,
            upper=int(match.group("width")),
        )

    raise ValueError(f"unsupported dependent predicate shape: {sql_template!r}")


def edges_from_query_spec(spec_path: Path | str) -> tuple[dict[str, Predicate], tuple[Edge, ...]]:
    """(per-variable independent predicates, dependent edges) from a spec file."""
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))

    independent_by_id = {p["predicate_id"]: p for p in spec.get("independent_predicates", [])}
    predicates: dict[str, Predicate] = {}
    for variable in spec["variables"]:
        predicate_id = variable.get("independent_predicate_id")
        if predicate_id is None:
            raise ValueError(f"variable {variable['name']!r} has no independent predicate")
        entry = independent_by_id[predicate_id]
        predicates[variable["name"]] = parse_independent_predicate(entry["sql_template"], variable["name"])

    edges = tuple(
        _parse_dependent(
            entry.get("selectivity_key") or entry["predicate_id"],
            entry["sql_template"],
            predicates,
        )
        for entry in spec.get("dependent_predicates", [])
    )
    return predicates, edges
