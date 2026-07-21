"""Exact achieved-selectivity measurement, mode_c-equivalent.

Per edge, counts ordered pairs over the whole table INCLUDING the diagonal
i=j whenever a row satisfies both endpoints' independent predicates: exactly
what compute_selectivities.py's
_mode_c_pair_count_sql cross join counts. Band bounds are computed the way the
SQL computes them (anchor.lon - w and anchor.lon + w in float64, then inclusive
comparisons), so the counts agree with Trino bit-for-bit.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Predicates (independent conditions)


@dataclass(frozen=True)
class TypeEquals:
    value: str
    column: str = "primary_type"

    def mask(self, frame: pd.DataFrame) -> np.ndarray:
        return (frame[self.column] == self.value).to_numpy()

    def label(self) -> str:
        return f"{self.column} = '{self.value}'"


@dataclass(frozen=True)
class ColumnThreshold:
    column: str
    op: str  # one of < <= > >=
    literal: float

    def mask(self, frame: pd.DataFrame) -> np.ndarray:
        values = frame[self.column].to_numpy()
        if self.op == "<":
            return values < self.literal
        if self.op == "<=":
            return values <= self.literal
        if self.op == ">":
            return values > self.literal
        if self.op == ">=":
            return values >= self.literal
        raise ValueError(f"unsupported operator {self.op!r}")

    def label(self) -> str:
        return f"{self.column} {self.op} {self.literal}"


Predicate = TypeEquals | ColumnThreshold


# ---------------------------------------------------------------------------
# Edges (dependent conditions); anchor = the variable whose columns appear on
# the right-hand side of BETWEEN (A in "B.lon BETWEEN A.lon - w AND A.lon + w").


@dataclass(frozen=True)
class SpatialBandEdge:
    name: str
    anchor: Predicate
    subject: Predicate
    half_width_lon: float | None
    half_width_lat: float | None
    target_sigma: float | None = None

    def __post_init__(self) -> None:
        if self.half_width_lon is None and self.half_width_lat is None:
            raise ValueError("a spatial band needs at least one axis")


@dataclass(frozen=True)
class TemporalWindowEdge:
    """subject.col - anchor.col in [lower_micros, upper_micros], inclusive.

    For ,,B.time BETWEEN A.time AND A.time + INTERVAL d'': [0, d_micros].
    For ,,abs(date_diff('millisecond', A.ts, B.ts)) <= m'' (truncation toward
    zero): [-(m*1000 + 999), m*1000 + 999].
    """

    name: str
    anchor: Predicate
    subject: Predicate
    lower_micros: int
    upper_micros: int
    column: str = "ts"
    target_sigma: float | None = None


@dataclass(frozen=True)
class IdWindowEdge:
    """subject.id - anchor.id in [lower, upper], inclusive."""

    name: str
    anchor: Predicate
    subject: Predicate
    lower: int
    upper: int
    target_sigma: float | None = None


Edge = SpatialBandEdge | TemporalWindowEdge | IdWindowEdge


# ---------------------------------------------------------------------------
# Exact pair counting


class _Fenwick:
    def __init__(self, size: int) -> None:
        self._tree = np.zeros(size + 1, dtype=np.int64)

    def add(self, index: int, delta: int) -> None:
        i = index + 1
        tree = self._tree
        while i < len(tree):
            tree[i] += delta
            i += i & (-i)

    def prefix(self, count: int) -> int:
        """Sum of the first ,,count'' positions."""
        total = 0
        tree = self._tree
        i = count
        while i > 0:
            total += tree[i]
            i -= i & (-i)
        return int(total)


def count_band_pairs(
    anchor_lon: np.ndarray,
    anchor_lat: np.ndarray,
    subject_lon: np.ndarray,
    subject_lat: np.ndarray,
    half_width_lon: float | None,
    half_width_lat: float | None,
) -> tuple[int, np.ndarray]:
    """Exact ordered-pair band count and per-anchor partner counts.

    Replicates ,,subject.lon BETWEEN anchor.lon - w AND anchor.lon + w'' (and the
    lat conjunct) with float64 bounds derived from the anchor, inclusive. A
    lon-sorted sliding window over subjects with a Fenwick tree over lat ranks:
    O((n_a + n_s) log n_s).
    """
    n_anchor, n_subject = len(anchor_lon), len(subject_lon)
    per_anchor = np.zeros(n_anchor, dtype=np.int64)
    if n_anchor == 0 or n_subject == 0:
        return 0, per_anchor

    if half_width_lon is None:
        # lat-only band: same sweep with axes swapped.
        return count_band_pairs(anchor_lat, anchor_lon, subject_lat, subject_lon, half_width_lat, None)

    lon_only = half_width_lat is None

    anchor_order = np.argsort(anchor_lon, kind="stable")
    subject_order = np.argsort(subject_lon, kind="stable")
    s_lon = subject_lon[subject_order]
    s_lat = subject_lat[subject_order]

    if not lon_only:
        # Stable rank over subject lats: duplicates get consecutive unique
        # Fenwick slots, and range queries by value remain exact.
        lat_argsort = np.argsort(s_lat, kind="stable")
        lat_sorted = s_lat[lat_argsort]
        unique_ranks = np.empty(n_subject, dtype=np.int64)
        unique_ranks[lat_argsort] = np.arange(n_subject, dtype=np.int64)
        fenwick = _Fenwick(n_subject)

    total = 0
    lo_ptr = 0
    hi_ptr = 0
    active = 0
    for a_idx in anchor_order:
        lo = anchor_lon[a_idx] - half_width_lon
        hi = anchor_lon[a_idx] + half_width_lon
        while hi_ptr < n_subject and s_lon[hi_ptr] <= hi:
            if not lon_only:
                fenwick.add(int(unique_ranks[hi_ptr]), 1)
            active += 1
            hi_ptr += 1
        while lo_ptr < hi_ptr and s_lon[lo_ptr] < lo:
            if not lon_only:
                fenwick.add(int(unique_ranks[lo_ptr]), -1)
            active -= 1
            lo_ptr += 1
        if lon_only:
            count = active
        else:
            lat_lo = anchor_lat[a_idx] - half_width_lat
            lat_hi = anchor_lat[a_idx] + half_width_lat
            rank_lo = int(np.searchsorted(lat_sorted, lat_lo, side="left"))
            rank_hi = int(np.searchsorted(lat_sorted, lat_hi, side="right"))
            count = fenwick.prefix(rank_hi) - fenwick.prefix(rank_lo)
        per_anchor[a_idx] = count
        total += count
    return total, per_anchor


def count_window_pairs(
    anchor_values: np.ndarray,
    subject_values: np.ndarray,
    lower: int,
    upper: int,
) -> tuple[int, np.ndarray]:
    """Exact one-dimensional inclusive-window count on integer values:
    subject - anchor in [lower, upper]."""
    subject_sorted = np.sort(subject_values, kind="stable")
    lo_idx = np.searchsorted(subject_sorted, anchor_values + lower, side="left")
    hi_idx = np.searchsorted(subject_sorted, anchor_values + upper, side="right")
    per_anchor = (hi_idx - lo_idx).astype(np.int64)
    return int(per_anchor.sum()), per_anchor


# ---------------------------------------------------------------------------
# Reports


@dataclass(frozen=True)
class IndependentReport:
    label: str
    count: int
    rate: float
    target_rate: float | None = None


@dataclass(frozen=True)
class EdgeReport:
    name: str
    kind: str
    n_anchor: int
    n_subject: int
    n_overlap: int
    total_pairs: int
    matching_pairs: int
    sigma: float
    off_diagonal_sigma: float | None
    sigma_sd_estimate: float
    target_sigma: float | None


@dataclass(frozen=True)
class Path2Report:
    """Joint statistic for two edges sharing a variable: the exact count of
    (left-partner, shared, right-partner) triples over n_l * n_shared * n_r,
    next to the independence product of the two pair sigmas; their ratio
    measures the higher-order dependence between the two edges."""

    name: str
    triples: int
    joint: float
    pair_product: float


@dataclass(frozen=True)
class VerificationReport:
    total_rows: int
    independents: tuple[IndependentReport, ...] = ()
    edges: tuple[EdgeReport, ...] = ()
    path2: tuple[Path2Report, ...] = ()


def _one_sided_variance(per_row: np.ndarray, n_other: int) -> float:
    if len(per_row) <= 1 or n_other == 0:
        return float("nan")
    proportions = per_row / n_other
    return float(np.var(proportions, ddof=1) / len(per_row))


def _count_edge(frame: pd.DataFrame, edge: Edge) -> tuple[int, np.ndarray, np.ndarray, np.ndarray, bool]:
    """(matching_pairs, per_anchor_counts, anchor_mask, subject_mask,
    diagonal_satisfies)."""
    anchor_mask = edge.anchor.mask(frame)
    subject_mask = edge.subject.mask(frame)
    anchors = frame.loc[anchor_mask]
    subjects = frame.loc[subject_mask]

    if isinstance(edge, SpatialBandEdge):
        matching, per_anchor = count_band_pairs(
            anchors["lon"].to_numpy(),
            anchors["lat"].to_numpy(),
            subjects["lon"].to_numpy(),
            subjects["lat"].to_numpy(),
            edge.half_width_lon,
            edge.half_width_lat,
        )
        diagonal_satisfies = True  # |delta| = 0 always within a band
    elif isinstance(edge, TemporalWindowEdge):
        # Bounds are in microseconds; normalize the column's resolution before
        # the int64 cast (a datetime64[ns] frame would otherwise yield
        # nanosecond ticks and shrink the window 1000x).
        anchor_values = anchors[edge.column].to_numpy().astype("datetime64[us]").astype("int64")
        subject_values = subjects[edge.column].to_numpy().astype("datetime64[us]").astype("int64")
        matching, per_anchor = count_window_pairs(anchor_values, subject_values, edge.lower_micros, edge.upper_micros)
        diagonal_satisfies = edge.lower_micros <= 0 <= edge.upper_micros
    elif isinstance(edge, IdWindowEdge):
        matching, per_anchor = count_window_pairs(
            anchors["id"].to_numpy(), subjects["id"].to_numpy(), edge.lower, edge.upper
        )
        diagonal_satisfies = edge.lower <= 0 <= edge.upper
    else:
        raise TypeError(f"unsupported edge type {type(edge)!r}")
    return matching, per_anchor, anchor_mask, subject_mask, diagonal_satisfies


def verify_edge(frame: pd.DataFrame, edge: Edge) -> EdgeReport:
    matching, per_anchor, anchor_mask, subject_mask, diagonal_satisfies = _count_edge(frame, edge)
    n_anchor = int(anchor_mask.sum())
    n_subject = int(subject_mask.sum())
    n_overlap = int((anchor_mask & subject_mask).sum())
    total_pairs = n_anchor * n_subject

    sigma = matching / total_pairs if total_pairs else float("nan")
    off_diagonal_sigma: float | None = None
    if n_overlap and total_pairs > n_overlap:
        off_matching = matching - (n_overlap if diagonal_satisfies else 0)
        off_diagonal_sigma = off_matching / (total_pairs - n_overlap)

    # Two-sample U-statistic SD: anchor-side + subject-side conditional-
    # proportion variances (the subject side needs the role-swapped count).
    per_subject, _, _ = _per_row_counts(frame, edge, role="subject")
    sd = float(np.sqrt(_one_sided_variance(per_anchor, n_subject) + _one_sided_variance(per_subject, n_anchor)))

    return EdgeReport(
        name=edge.name,
        kind=type(edge).__name__,
        n_anchor=n_anchor,
        n_subject=n_subject,
        n_overlap=n_overlap,
        total_pairs=total_pairs,
        matching_pairs=matching,
        sigma=sigma,
        off_diagonal_sigma=off_diagonal_sigma,
        sigma_sd_estimate=sd,
        target_sigma=edge.target_sigma,
    )


def _per_row_counts(frame: pd.DataFrame, edge: Edge, *, role: str) -> tuple[np.ndarray, np.ndarray, int]:
    """Partner counts per row of the given endpoint ('anchor' or 'subject').

    Subject-side counts swap the endpoint roles; for spatial bands the swapped
    bounds differ from the SQL's anchor-derived bounds by at most 1-ulp
    boundary events: acceptable for the path-2 dependence report (not used by
    the bit-for-bit edge gate).
    """
    if role == "anchor":
        _, per_anchor, anchor_mask, subject_mask, _ = _count_edge(frame, edge)
        return per_anchor, anchor_mask, int(subject_mask.sum())
    swapped: Edge
    if isinstance(edge, SpatialBandEdge):
        swapped = SpatialBandEdge(
            name=edge.name,
            anchor=edge.subject,
            subject=edge.anchor,
            half_width_lon=edge.half_width_lon,
            half_width_lat=edge.half_width_lat,
        )
    elif isinstance(edge, TemporalWindowEdge):
        swapped = TemporalWindowEdge(
            name=edge.name,
            anchor=edge.subject,
            subject=edge.anchor,
            lower_micros=-edge.upper_micros,
            upper_micros=-edge.lower_micros,
            column=edge.column,
        )
    elif isinstance(edge, IdWindowEdge):
        swapped = IdWindowEdge(
            name=edge.name, anchor=edge.subject, subject=edge.anchor, lower=-edge.upper, upper=-edge.lower
        )
    else:
        raise TypeError(f"unsupported edge type {type(edge)!r}")
    _, per_anchor, anchor_mask, subject_mask, _ = _count_edge(frame, swapped)
    return per_anchor, anchor_mask, int(subject_mask.sum())


def verify_path2(frame: pd.DataFrame, left: Edge, right: Edge, shared_role: tuple[str, str]) -> Path2Report:
    """Exact triple count for two edges sharing a variable.

    shared_role gives the shared variable's role in (left, right), each
    'anchor' or 'subject'. joint = sum over shared rows of
    cnt_left * cnt_right / (n_left_other * n_shared * n_right_other).
    """
    left_counts, left_mask, n_left_other = _per_row_counts(frame, left, role=shared_role[0])
    right_counts, right_mask, n_right_other = _per_row_counts(frame, right, role=shared_role[1])
    if not np.array_equal(left_mask, right_mask):
        raise ValueError("path-2 edges do not share the same variable predicate")
    triples = int(np.dot(left_counts, right_counts))
    n_shared = int(left_mask.sum())
    denom = n_left_other * n_shared * n_right_other
    joint = triples / denom if denom else float("nan")

    left_total = int(left_counts.sum())
    right_total = int(right_counts.sum())
    left_sigma = left_total / (n_shared * n_left_other) if n_shared and n_left_other else float("nan")
    right_sigma = right_total / (n_shared * n_right_other) if n_shared and n_right_other else float("nan")
    return Path2Report(
        name=f"{left.name} & {right.name}",
        triples=triples,
        joint=joint,
        pair_product=left_sigma * right_sigma,
    )


def shared_variable_pairs(edges):
    """Edge pairs sharing an endpoint predicate, tagged with the shared
    variable's role per side, for the path-2 joint statistics the manifest
    reports."""
    pairs = []
    for i, left in enumerate(edges):
        for right in edges[i + 1 :]:
            for left_role, right_role in (
                ("anchor", "anchor"),
                ("anchor", "subject"),
                ("subject", "anchor"),
                ("subject", "subject"),
            ):
                if getattr(left, left_role) == getattr(right, right_role):
                    pairs.append((left, right, (left_role, right_role)))
                    break
    return tuple(pairs)


def verify_dataframe(
    frame: pd.DataFrame,
    *,
    independents: dict[str, Predicate] | None = None,
    edges: tuple[Edge, ...] = (),
    path2_pairs: tuple[tuple[Edge, Edge, tuple[str, str]], ...] = (),
    independent_targets: dict[str, float] | None = None,
) -> VerificationReport:
    total_rows = len(frame)
    independent_reports = []
    for name, predicate in (independents or {}).items():
        count = int(predicate.mask(frame).sum())
        independent_reports.append(
            IndependentReport(
                label=f"{name}: {predicate.label()}",
                count=count,
                rate=count / total_rows if total_rows else float("nan"),
                target_rate=(independent_targets or {}).get(name),
            )
        )
    edge_reports = tuple(verify_edge(frame, edge) for edge in edges)
    path2_reports = tuple(verify_path2(frame, left, right, roles) for left, right, roles in path2_pairs)
    return VerificationReport(
        total_rows=total_rows,
        independents=tuple(independent_reports),
        edges=edge_reports,
        path2=path2_reports,
    )
