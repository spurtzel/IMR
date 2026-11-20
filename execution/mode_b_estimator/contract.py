"""Shared contract for the mode_b selectivity estimator.

Every module in execution/mode_b_estimator/ codes against the dataclasses, constants,
and conventions defined here; keep field names and signatures in sync with all consumers.

Two predicate families:
- Family 'A'  -> single-row filter:    ,,col OP literal''   (e.g. ,,etype = 'A''', ,,id < 500'')
- Family 'B'  -> cross-row comparison: ,,col OP col''        (e.g. ,,a.id < b.id'', band, ordering)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import isnan
from typing import Dict, Optional

# --- Trino CBO constants (source class:line noted per value) -------
# Per-pair match probability on the overlap x overlap region of a cross-column inequality.
OVERLAPPING_RANGE_INEQUALITY_FILTER_COEFFICIENT = 0.5   # ComparisonStatsCalculator.java:39
# Multiplier applied to the known part of an AND when >=1 conjunct is unestimable.
UNKNOWN_FILTER_COEFFICIENT = 0.9                         # FilterStatsCalculator.java:75
# Cross-group AND exponential-backoff base (most-selective term keeps exponent 1).
FILTER_CONJUNCTION_INDEPENDENCE_FACTOR = 0.75           # OptimizerConfig:59
# overlapPercentWith infinite-range heuristics.
INFINITE_TO_FINITE = 0.25                               # StatisticRange.java:33
INFINITE_TO_INFINITE = 0.5                              # StatisticRange.java:34
# Sparse-column density branch (local heuristic, not in stock Trino).
DENSITY_HEURISTIC_THRESHOLD = 1e-3                      # StatisticRange.java:35

# Generated events schema (workload/data/datagen/config.py EVENT_COLUMNS).
EVENT_COLUMNS = ("id", "time", "ts", "primary_type", "etype", "lon", "lat")

RESULT_COLUMNS = (
    "config_id", "size", "regime", "ndv_variant", "band_variant", "temporal_variant",
    "operator", "family", "col", "other", "predicate_sql",
    "estimate", "exact", "abs_err", "signed_err", "rel_err", "notes",
)


def _num_or_none(value) -> Optional[float]:
    """float(value) for numbers, numeric strings and booleans ('true' / 'false', True / False -> 1.0 / 0.0); else None."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return 1.0 if value.strip().lower() == "true" else 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Frequencies:
    """Top-K most-common-value frequencies for a categorical column: a cheap single-pass
    O(N) GROUP BY statistic. Used to price equality without assuming uniformity; the tail
    (values not in ,,top'') is assumed uniform.
    """
    top: tuple               # tuple[tuple[str, float], ...]: (value_repr, fraction), desc by fraction
    residual_fraction: float # total mass of values NOT in ,,top''  (>= 0)
    residual_ndv: float      # number of distinct values NOT in ,,top'' (>= 0), tail assumed uniform

    def freq_of(self, value_repr: str) -> float:
        """f(value): P(a row's value == value_repr). Values are compared as strings; when both sides parse as
        numbers they are compared numerically (numeric and boolean equality columns are sketched too, so a
        literal 1.0 / True must meet the sketch's "1" / "true"; booleans count as 1 / 0)."""
        want = _num_or_none(value_repr)
        for v, f in self.top:
            if v == value_repr or (want is not None and _num_or_none(v) == want):
                return f
        return (self.residual_fraction / self.residual_ndv) if self.residual_ndv > 0 else 0.0

    def collision(self) -> float:
        """Sum_i f_i^2 = P(two independent rows share the value); the exact family-B '=' selectivity."""
        s = sum(f * f for _, f in self.top)
        if self.residual_ndv > 0:
            s += (self.residual_fraction ** 2) / self.residual_ndv  # tail: residual_ndv uniform buckets
        return s


@dataclass(frozen=True)
class Histogram:
    """Equi-depth quantile sketch: ,,edges'' are boundary values at cumulative probabilities
    ,,probs'' (e.g. approx_percentile at 0, 1/B, ..., 1). The empirical CDF is the piecewise-
    linear interpolant of (edges[i], probs[i]). A CHEAP single-pass O(N) statistic.
    """
    edges: tuple   # sorted boundary values, length B+1 (edges[0]=min ... edges[-1]=max)
    probs: tuple   # cumulative prob at each edge, length B+1 (probs[0]=0.0 ... probs[-1]=1.0)

    def cdf(self, x: float) -> float:
        e = self.edges
        if x <= e[0]:
            return 0.0
        if x >= e[-1]:
            return 1.0
        for i in range(1, len(e)):
            if x <= e[i]:
                lo, hi = e[i - 1], e[i]
                plo, phi = self.probs[i - 1], self.probs[i]
                return phi if hi == lo else plo + (phi - plo) * (x - lo) / (hi - lo)
        return 1.0

    def bins(self):
        """Yield (lo, hi, p) per bin; p = mass in [lo, hi]. For band/window convolution."""
        for i in range(1, len(self.edges)):
            yield self.edges[i - 1], self.edges[i], self.probs[i] - self.probs[i - 1]


@dataclass(frozen=True)
class ColumnStats:
    """Summary statistics for ONE column.

    The first four fields mirror Trino's SymbolStatsEstimate {low, high, ndv, nullsFraction}
    (averageRowSize is omitted: never used for selectivity) and are the ONLY inputs the baseline
    estimator may use. math.nan means 'unknown', +/-inf an unbounded range bound.
    """
    low: float            # min(col)  (nan = unknown, -inf = unbounded)
    high: float           # max(col)  (nan = unknown, +inf = unbounded)
    ndv: float            # approx_distinct(col)
    nulls_fraction: float # fraction of NULLs in col  (0.0 if non-null)
    freq: "Optional[Frequencies]" = None  # top-K MCV (categorical); None if not gathered
    hist: "Optional[Histogram]" = None    # equi-depth quantile sketch (numeric); None if not gathered

    @property
    def span(self) -> float:
        return self.high - self.low

    @property
    def known(self) -> bool:
        return not (isnan(self.low) or isnan(self.high) or isnan(self.ndv))


@dataclass(frozen=True)
class TableStats:
    """Per-table summary catalog: row_count + one ColumnStats per column."""
    row_count: int
    columns: Dict[str, ColumnStats]

    def col(self, name: str) -> ColumnStats:
        return self.columns[name]


@dataclass(frozen=True)
class Predicate:
    """A single test predicate.

    Fields by kind:
      kind='eq'|'neq'|'lt'|'le'|'gt'|'ge'  family 'A': col, literal        family 'B': col, other
      kind='between'                       family 'A': col, lo, hi
      kind='band'   (|a.col - b.col| <= d) family 'B': col, other(=col), delta
      kind='offset_band' (a.col+lo <= b.col <= a.col+hi)  family 'B': col, other(=col), lo_offset, hi_offset
      kind='window' (|a.col - b.col| <= w) family 'B': col(ts), other(ts), w   (seconds)
      kind='order'  (a.col < b.col)        family 'B': col(ts), other(ts)
    """
    kind: str
    family: str            # 'A' or 'B'
    col: str               # subject column (left); aliased ,,a'' in SQL
    other: Optional[str] = None     # family B: the right column (aliased ,,b'')
    literal: Optional[object] = None  # family A scalar comparand (float or str)
    lo: Optional[float] = None        # family A 'between' lower bound
    hi: Optional[float] = None        # family A 'between' upper bound
    delta: Optional[float] = None     # family B 'band' half-width d
    w: Optional[float] = None         # family B 'window' half-width (seconds)
    lo_offset: Optional[float] = None  # family B 'offset_band' lower offset relative to anchor a.col
    hi_offset: Optional[float] = None  # family B 'offset_band' upper offset relative to anchor a.col
    sql: str = ""
    label: str = ""        # human label, e.g. 'eq:etype', 'order:ts:shared'

    @property
    def operator(self) -> str:
        """Coarse operator bucket for reporting: equality / inequality / range / temporal / spatial."""
        return {
            "eq": "equality", "neq": "not_equal",
            "lt": "inequality", "le": "inequality", "gt": "inequality", "ge": "inequality",
            "between": "range", "band": "spatial_band", "offset_band": "offset_band",
            "window": "temporal_window", "order": "temporal_order",
        }[self.kind]


@dataclass(frozen=True)
class GenConfig:
    config_id: str
    size: int                       # total rows
    regime: str = "independent"     # 'independent' | 'clustered'
    ndv_variant: str = "default"    # e.g. 'low_ndv' | 'high_ndv'
    band_variant: str = "default"   # band half-width regime label
    temporal_variant: str = "shared"  # 'shared' | 'disjoint'
    seed: int = 0
    extra: Dict[str, object] = field(default_factory=dict)  # generator-specific knobs


def rel_err(estimate: float, exact: float, *, eps: float = 1e-9) -> float:
    return abs(estimate - exact) / max(abs(exact), eps)
