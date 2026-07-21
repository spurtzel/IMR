"""mode_b_v2: stats-only predicate-selectivity estimator.

A port of the Trino CBO closed forms, plus EIMER's principled departures. It
consumes ONLY ColumnStats {low, high, ndv, nulls_fraction} + TableStats.row_count;
it never touches data.

Two profiles:
  profile='trino' -> faithful Trino port.
  profile='eimer' -> departures where they differ:
                       * Family-B '=' : NDVs scoped to the overlap (n_ov = ndv*w/s).
                       * Spatial band : symmetric 2d/s-(d/s)^2 (not Trino's anchor artifact).
                       * Family-B '<' : the exact uniform-integral P(A<B).
                     For ops with no departure, 'eimer' == 'trino'.

NaN semantics: math.nan == 'unknown'; +/-inf == an unbounded range bound.
Estimable-but-unknown results are returned as float('nan') so the caller can mark
them. estimate() returns the [0,1]-CLAMPED selectivity by default (Trino itself
does NOT clamp; the raw value is available via estimate_raw).
"""
from __future__ import annotations

from math import isnan, isinf

from .contract import (
    ColumnStats,
    Predicate,
    TableStats,
    OVERLAPPING_RANGE_INEQUALITY_FILTER_COEFFICIENT,  # 0.5
    UNKNOWN_FILTER_COEFFICIENT,                        # 0.9
    FILTER_CONJUNCTION_INDEPENDENCE_FACTOR,            # 0.75
    INFINITE_TO_FINITE,                                # 0.25
    INFINITE_TO_INFINITE,                              # 0.5
)

NAN = float("nan")

# ---------------------------------------------------------------------------
# Low-level numeric helpers
# ---------------------------------------------------------------------------


def _ndv_floor(ndv: float) -> float:
    """max(ndv, 1); Trino's universal NDV floor."""
    if isnan(ndv):
        return NAN
    return max(ndv, 1.0)


def _clamp01(x: float) -> float:
    """Clip to [0,1]; only the public estimate() path clamps (Trino itself does not)."""
    if isnan(x):
        return x
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _length(lo: float, hi: float) -> float:
    """Range length: high - low."""
    return hi - lo


def _overlap_percent_with(rlo: float, rhi: float, olo: float, ohi: float) -> float:
    """Port of StatisticRange.overlapPercentWith.

    Returns lengthOfIntersect([rlo,rhi] cap [olo,ohi]) / length([rlo,rhi]), i.e. the
    intersect divided by the CALLER's OWN span: the asymmetric anchored ratio
    (c-low)/(high-low), each side dividing the same intersect by its own span.

    Branches that matter for our predicate set:
      * equal ranges                            -> 1.0
      * point intersect (len == 0)              -> 0.0 here (NDV floor applied by the caller that holds ndv)
      * infinite caller span, finite intersect  -> INFINITE_TO_FINITE (0.25)
      * infinite intersect                      -> INFINITE_TO_INFINITE (0.5)

    Trino's sparse DENSITY_HEURISTIC_THRESHOLD branch is intentionally NOT applied
    (it needs the other range's ndv and only perturbs join ordering).
    """
    if isnan(rlo) or isnan(rhi) or isnan(olo) or isnan(ohi):
        return NAN
    # Equal ranges -> full overlap.
    if rlo == olo and rhi == ohi:
        return 1.0
    ilo = max(rlo, olo)
    ihi = min(rhi, ohi)
    intersect = ihi - ilo
    if intersect <= 0.0:
        return 0.0  # disjoint / point-or-less intersect (NDV floor applied by caller)
    caller_span = _length(rlo, rhi)
    # Infinite intersect -> INFINITE_TO_INFINITE.
    if isinf(intersect):
        return INFINITE_TO_INFINITE
    # Finite intersect but infinite caller span -> INFINITE_TO_FINITE.
    if isinf(caller_span):
        return INFINITE_TO_FINITE
    if caller_span <= 0.0:
        return 0.0
    return intersect / caller_span


# ---------------------------------------------------------------------------
# Family A  (col OP literal)
# ---------------------------------------------------------------------------


def estimate_family_a(pred: Predicate, stats: TableStats) -> float:
    """Family-A single-row filter selectivity (raw, unclamped).

    ,,<='' folds to ,,<'', ,,>='' folds to ,,>'' with no boundary adjustment.
    """
    c = stats.col(pred.col)
    nf = c.nulls_fraction
    ndv = c.ndv
    lo, hi = c.low, c.high
    s = _length(lo, hi)
    kind = pred.kind

    if kind == "eq":
        # (1-nf)/max(ndv,1); OOR -> 0
        # EQUAL NaN-guard: ndv NaN AND both bounds infinite -> unknown().
        if isnan(ndv) and isinf(lo) and isinf(hi):
            return NAN
        if isnan(ndv):
            return NAN
        lit = _as_float(pred.literal)
        if lit is not None and not (isnan(lo) or isnan(hi)):
            # out-of-range -> overlap 0 -> selectivity 0
            if lit < lo or lit > hi:
                return 0.0
        return (1.0 - nf) / _ndv_floor(ndv)

    if kind == "neq":
        # (1 - 1/max(ndv,1)) * (1-nf); OOR -> (1-nf)
        if isnan(ndv):
            return NAN
        lit = _as_float(pred.literal)
        if lit is not None and not (isnan(lo) or isnan(hi)):
            if lit < lo or lit > hi:
                return 1.0 - nf  # overlap 0 -> complement is full pass
        return (1.0 - 1.0 / _ndv_floor(ndv)) * (1.0 - nf)

    if kind in ("lt", "le"):
        # (1-nf)*(min(hi,c)-lo)^+/s ; c<=lo -> (1-nf)/max(ndv,1)
        return _family_a_less(lo, hi, ndv, nf, _as_float(pred.literal))

    if kind in ("gt", "ge"):
        # (1-nf)*(hi-max(lo,c))^+/s ; c>=hi -> (1-nf)/max(ndv,1)
        return _family_a_greater(lo, hi, ndv, nf, _as_float(pred.literal))

    if kind == "between":
        # rewritten to AND(col>=c1, col<=c2); same-symbol so applied sequentially with
        # NO independence damping -> (1-nf)*(min(hi,c2)-max(lo,c1))^+/s
        c1 = _as_float(pred.lo)
        c2 = _as_float(pred.hi)
        if c1 is None or c2 is None or isnan(lo) or isnan(hi):
            return NAN
        if s <= 0.0 or isinf(s):
            # degenerate / unbounded base range -> unknown for BETWEEN
            return NAN
        inter = min(hi, c2) - max(lo, c1)
        if inter <= 0.0:
            return 0.0
        return (1.0 - nf) * inter / s

    return NAN


def _family_a_less(lo: float, hi: float, ndv: float, nf: float, c) -> float:
    if c is None or isnan(lo) or isnan(hi):
        return NAN
    s = _length(lo, hi)
    if c < lo:
        # STRICT below the range: [-inf, c] intersect [lo,hi] is empty -> overlap 0.0,
        # so selectivity 0, NOT the point floor.
        return 0.0
    if c == lo:
        # point branch: c exactly at low -> point intersect {low} -> overlap 1/max(ndv,1).
        if isnan(ndv):
            return NAN
        return (1.0 - nf) / _ndv_floor(ndv)
    if isinf(s):
        # infinite caller range, finite intersect -> INFINITE_TO_FINITE (0.25)
        return (1.0 - nf) * INFINITE_TO_FINITE
    if s <= 0.0:
        return 0.0  # empty/degenerate range -> 0
    inter = min(hi, c) - lo
    if inter <= 0.0:
        return 0.0
    return (1.0 - nf) * inter / s


def _family_a_greater(lo: float, hi: float, ndv: float, nf: float, c) -> float:
    if c is None or isnan(lo) or isnan(hi):
        return NAN
    s = _length(lo, hi)
    if c > hi:
        # STRICT above the range: [c, +inf] intersect [lo,hi] is empty -> overlap 0 -> selectivity 0.
        return 0.0
    if c == hi:
        # point branch: c exactly at high -> point intersect {high} -> 1/max(ndv,1).
        if isnan(ndv):
            return NAN
        return (1.0 - nf) / _ndv_floor(ndv)
    if isinf(s):
        return (1.0 - nf) * INFINITE_TO_FINITE
    if s <= 0.0:
        return 0.0
    inter = hi - max(lo, c)
    if inter <= 0.0:
        return 0.0
    return (1.0 - nf) * inter / s


# ---------------------------------------------------------------------------
# Family B  (col OP col)
# ---------------------------------------------------------------------------


def estimate_family_b(pred: Predicate, stats: TableStats, *, profile: str = "trino") -> float:
    """Family-B cross-row comparison selectivity (raw, unclamped)."""
    kind = pred.kind

    if kind == "band":
        d = _as_float(pred.delta)
        if d is None:
            return NAN
        if profile == "eimer":
            return eimer_band_selectivity(stats, pred.col, d)
        return trino_band_selectivity(stats, pred.col, d)

    if kind == "offset_band":
        # a.col + lo <= b.col <= a.col + hi  (asymmetric offset band; e.g.
        # ,,b.col BETWEEN a.col AND a.col + 500'' is lo=0, hi=500).
        lo = _as_float(pred.lo_offset)
        hi = _as_float(pred.hi_offset)
        if lo is None or hi is None:
            return NAN
        if profile == "eimer":
            return eimer_offset_band_selectivity(stats, pred.col, lo, hi)
        # Trino profile: a column-bounded BETWEEN is NOT estimated; visitBetween bails
        # to unknown() when either bound is not a single value. Report unknown rather than fabricate.
        return NAN

    if kind == "window":
        # |a.ts - b.ts| <= w  is a numeric band on ts with half-width w.
        wv = _as_float(pred.w)
        if wv is None:
            return NAN
        if profile == "eimer":
            return eimer_band_selectivity(stats, pred.col, wv)
        return trino_band_selectivity(stats, pred.col, wv)

    if kind == "order":
        # a.col < b.col -> family-B '<' path.
        return temporal_order_selectivity(stats, pred.col, pred.other, profile=profile)

    # col OP col  (eq/neq/lt/le/gt/ge)
    cl = stats.col(pred.col)
    cr = stats.col(pred.other)

    if kind == "eq":
        return _family_b_eq(cl, cr, profile=profile)
    if kind == "neq":
        return _family_b_neq(cl, cr)
    if kind in ("lt", "le"):
        return _family_b_less(cl, cr, profile=profile)
    if kind in ("gt", "ge"):
        # GREATER_THAN(a,b) computed as LESS_THAN(b,a) by swapping.
        return _family_b_less(cr, cl, profile=profile)
    return NAN


def _family_b_eq(cl: ColumnStats, cr: ColumnStats, *, profile: str = "trino") -> float:
    """col = col.

    Trino base: (1-nf_L)(1-nf_R)/max(ndv_L,ndv_R,1) using FULL-column NDVs.
    EIMER departure: scope NDVs to the overlap n_ov = ndv*w/s -> 1/max(n_L^ov,n_R^ov);
    Trino never narrows the NDV feeding max(...).
    """
    if isnan(cl.ndv) or isnan(cr.ndv):
        return NAN  # either NDV NaN -> unknown()
    nulls_ff = (1.0 - cl.nulls_fraction) * (1.0 - cr.nulls_fraction)

    if profile == "eimer":
        ndv_l = _overlap_scoped_ndv(cl, cr)
        ndv_r = _overlap_scoped_ndv(cr, cl)
        if isnan(ndv_l) or isnan(ndv_r):
            return NAN
        # Empty overlap (disjoint ranges) -> NO shared distinct values -> P(=) = 0.
        # _overlap_scoped_ndv returns 0.0 on a disjoint overlap; the max(...,1) floor below
        # is a divide-by-zero guard for the IN-overlap case, NOT a row-count floor, so it
        # must NOT resurrect mass on a disjoint pair. Zero it explicitly before the floor.
        if ndv_l <= 0.0 or ndv_r <= 0.0:
            return 0.0
        denom = max(ndv_l, ndv_r, 1.0)
        return nulls_ff / denom

    # trino: full-column NDVs
    denom = max(cl.ndv, cr.ndv, 1.0)
    return nulls_ff / denom


def _overlap_scoped_ndv(c: ColumnStats, other: ColumnStats) -> float:
    """n_ov = ndv * w / s  (EIMER departure).

    w = overlap width, s = c's own span. Degenerate span (point column) keeps the
    full ndv (no narrowing possible). Floored at 1 implicitly by the max(...,1) caller.
    """
    s = _length(c.low, c.high)
    if isnan(s):
        # Unknown range (e.g. unordered categorical): overlap notion does not apply,
        # so keep the full-column NDV (eimer == trino here).
        return c.ndv
    if s <= 0.0 or isinf(s):
        return c.ndv  # degenerate / unbounded span -> cannot scope, keep full ndv
    lo = max(c.low, other.low)
    hi = min(c.high, other.high)
    w = hi - lo
    if w <= 0.0:
        return 0.0  # disjoint overlap -> no shared distinct values
    return c.ndv * w / s


def _family_b_neq(cl: ColumnStats, cr: ColumnStats) -> float:
    """col <> col.

    sel = (1-nf_L)(1-nf_R)*(1 - eqFF), eqFF = 1/max(ndv_L,ndv_R,1).
    Non-finite eqFF -> 0.0.
    """
    if isnan(cl.ndv) or isnan(cr.ndv):
        return NAN  # inherits EQUAL unknown()
    nulls_ff = (1.0 - cl.nulls_fraction) * (1.0 - cr.nulls_fraction)
    denom = max(cl.ndv, cr.ndv, 1.0)
    eq_ff = 1.0 / denom
    if isnan(eq_ff) or isinf(eq_ff):
        eq_ff = 0.0
    return nulls_ff * (1.0 - eq_ff)


def _family_b_less(cl: ColumnStats, cr: ColumnStats, *, profile: str = "trino") -> float:
    """col < col (left=cl, right=cr): Trino 3-term range model, or the eimer integral.

    a = left (cl), b = right (cr).
    """
    # Preamble guards.
    if not cl.known or not cr.known:
        return NAN  # either side unknown -> unknown()
    nf_l, nf_r = cl.nulls_fraction, cr.nulls_fraction
    if isnan(nf_l) and isnan(nf_r):
        return NAN  # both nulls NaN -> unknown()
    # nullsFilterFactor = 1 - maxExcludeNaN(nf_L, nf_R)  (MAX: nulls fully correlated)
    nulls_ff = 1.0 - _max_exclude_nan(nf_l, nf_r)

    a_lo, a_hi = cl.low, cl.high
    b_lo, b_hi = cr.low, cr.high
    span_a = _length(a_lo, a_hi)
    span_b = _length(b_lo, b_hi)
    if span_a <= 0.0 or span_b <= 0.0:
        return 0.0  # either range empty -> 0.0

    if profile == "eimer":
        return nulls_ff * _eimer_p_a_less_b(a_lo, a_hi, b_lo, b_hi)

    # ---- Trino 3-term range model
    # Early exits.
    if a_lo > b_hi:
        return 0.0
    if a_hi < b_lo:
        return nulls_ff  # filterFactor == 1, full pass

    intersect = min(a_hi, b_hi) - max(a_lo, b_lo)
    intersect = max(0.0, intersect)
    o_l = intersect / span_a if span_a > 0.0 else 0.0  # leftOverlappingRangeFraction
    o_r = intersect / span_b if span_b > 0.0 else 0.0  # rightOverlappingRangeFraction

    # leftAlwaysLessRangeFraction
    if a_lo < b_lo:
        a_less = min(_overlap_percent_with(a_lo, a_hi, a_lo, b_lo), 1.0 - o_l)
    else:
        a_less = 0.0
    # rightAlwaysGreaterRangeFraction
    if a_hi < b_hi:
        g_r = min(_overlap_percent_with(b_lo, b_hi, a_hi, b_hi), 1.0 - o_r)
    else:
        g_r = 0.0

    # filterFactor = aLess + 0.5*oL*oR + oL*gR
    filter_factor = (
        a_less
        + OVERLAPPING_RANGE_INEQUALITY_FILTER_COEFFICIENT * o_l * o_r
        + o_l * g_r
    )
    return nulls_ff * filter_factor


def _eimer_p_a_less_b(a_lo: float, a_hi: float, b_lo: float, b_hi: float) -> float:
    """Exact uniform integral P(A<B) for A~U[a_lo,a_hi], B~U[b_lo,b_hi].

    P(A<B) = (b.hi - a.hi)^+/s_B + w*(l + u - 2*a.lo)/(2*s_A*s_B)
    where l = max(a_lo,b_lo), u = min(a_hi,b_hi), w = (u-l)^+.
    Disjoint A-below-B -> 1; A-above-B -> 0.
    """
    s_a = a_hi - a_lo
    s_b = b_hi - b_lo
    # Disjoint cases.
    if a_hi <= b_lo:
        return 1.0  # A strictly below B -> always A < B
    if a_lo >= b_hi:
        return 0.0  # A strictly above B -> never A < B
    l = max(a_lo, b_lo)
    u = min(a_hi, b_hi)
    w = max(0.0, u - l)
    term1 = max(0.0, b_hi - a_hi) / s_b if s_b > 0.0 else 0.0
    if s_a <= 0.0 or s_b <= 0.0:
        return term1
    term2 = w * (l + u - 2.0 * a_lo) / (2.0 * s_a * s_b)
    return term1 + term2


# ---------------------------------------------------------------------------
# Spatial band  |a.col - b.col| <= d
# ---------------------------------------------------------------------------


def trino_band_selectivity(stats: TableStats, col: str, delta: float) -> float:
    """Trino's asymmetric anchored band.

    Trino rewrites |B - A.x| <= d  to  AND(B >= A.x - d, B <= A.x + d): two one-sided
    CROSS-COLUMN range predicates combined by AND, each an anchored fraction
    intersect/own-span = (c - low)/(high - low).

    APPROXIMATION: the anchors A.x +/- d are themselves columns (unknown per-row), so
    Trino uses anchored fractions over the SAME column's [low,high]. The half-band of
    width d contributes an anchored fraction d/s per side; the two sides are AND-combined
    with backoff f=0.75. For equal per-side fractions p = clip(d/s, 0, 1):
        band ~= p * p^0.75.
    This is an anchor-pinned artifact, NOT the principled two-sided band (see
    eimer_band_selectivity).
    """
    c = stats.col(col)
    lo, hi = c.low, c.high
    if isnan(lo) or isnan(hi) or delta is None or isnan(delta):
        return NAN
    s = _length(lo, hi)
    if s <= 0.0:
        # Degenerate span (constant column): band is all-or-nothing on |0| <= d.
        return 1.0 if delta >= 0.0 else 0.0
    if isinf(s):
        return NAN
    p = delta / s
    if p < 0.0:
        p = 0.0
    if p > 1.0:
        p = 1.0
    # AND of two equal one-sided anchored fractions with backoff f=0.75:
    # combined = p * p^0.75.
    return and_composition([[p], [p]])


def eimer_band_selectivity(stats: TableStats, col: str, delta: float) -> float:
    """EIMER symmetric two-sided band.

    For same span s: P(|A-B|<=d) = 2d/s - (d/s)^2 = d(2s-d)/s^2 for 0<=d<=s; 1 for d>=s.
    Degenerate span (s=0, constant column): Dirac at the point, so |a-a|=0<=d => 1 for d>=0.
    """
    c = stats.col(col)
    lo, hi = c.low, c.high
    if isnan(lo) or isnan(hi) or delta is None or isnan(delta):
        return NAN
    s = _length(lo, hi)
    if s <= 0.0:
        # Degenerate span special-case (avoid divide-by-zero): Dirac at the point.
        return 1.0 if delta >= 0.0 else 0.0
    if isinf(s):
        return NAN
    if delta <= 0.0:
        return 0.0
    if delta >= s:
        return 1.0
    r = delta / s
    return 2.0 * r - r * r


def _triangular_diff_cdf(t: float, s: float) -> float:
    """CDF T(t) = P(D <= t) of D = B - A for A, B iid uniform over a span-s range.

    D is symmetric-triangular on [-s, s] (density (s-|t|)/s^2), so:
        t <= -s          -> 0
        -s <= t <= 0      -> (s + t)^2 / (2 s^2)
         0 <= t <= s      -> 1 - (s - t)^2 / (2 s^2)
        t >= s            -> 1
    Requires s > 0 (callers handle the degenerate s<=0 / infinite cases).
    """
    if t <= -s:
        return 0.0
    if t >= s:
        return 1.0
    if t <= 0.0:
        return (s + t) * (s + t) / (2.0 * s * s)
    return 1.0 - (s - t) * (s - t) / (2.0 * s * s)


def eimer_offset_band_selectivity(stats: TableStats, col: str, lo: float, hi: float) -> float:
    """EIMER principled asymmetric / offset cross-column band.

    For ,,b.col BETWEEN a.col + lo AND a.col + hi'' over two INDEPENDENT draws A, B of the
    SAME column (span s = high - low), the gap D = B - A is symmetric-triangular on [-s, s].
    The predicate is exactly {lo <= D <= hi}, so
        P(lo <= D <= hi) = T(hi) - T(lo)
    with T the triangular-difference CDF (_triangular_diff_cdf). This reduces to:
      * the symmetric band eimer_band_selectivity at lo=-d, hi=+d  (=> 2d/s - (d/s)^2), and
      * the one-sided directed window at lo=0                       (=> hi/s - (hi/s)^2/2).
    Guards mirror eimer_band_selectivity.
    """
    c = stats.col(col)
    clo, chi = c.low, c.high
    if lo is None or hi is None or isnan(lo) or isnan(hi) or isnan(clo) or isnan(chi):
        return NAN
    if hi < lo:
        return 0.0  # empty band (BETWEEN min > max)
    s = _length(clo, chi)
    if s <= 0.0:
        # Degenerate span (constant column): D == 0 for every pair -> band passes iff lo<=0<=hi.
        return 1.0 if (lo <= 0.0 <= hi) else 0.0
    if isinf(s):
        return NAN
    return _triangular_diff_cdf(hi, s) - _triangular_diff_cdf(lo, s)


# ---------------------------------------------------------------------------
# Temporal ordering  a.col < b.col
# ---------------------------------------------------------------------------


def temporal_order_selectivity(
    stats: TableStats, col: str, other, *, profile: str = "trino"
) -> float:
    """a.col < b.col -> family-B '<' path.

    Identical range [L,H] (both columns) -> filterFactor 0.5 -> 0.5 * nullsFF.
    """
    if other is None:
        other = col
    cl = stats.col(col)
    cr = stats.col(other)
    return _family_b_less(cl, cr, profile=profile)


# ---------------------------------------------------------------------------
# AND composition
# ---------------------------------------------------------------------------


def and_composition(selectivities_by_group) -> float:
    """Cross-group AND with exponential backoff.

    Input: a list of GROUPS, each group a list of per-conjunct selectivities for a
    correlated symbol-group. WITHIN a group the conjuncts are applied SEQUENTIALLY
    (product here; for independent same-symbol bounds the sequential estimate is the
    product). ACROSS groups: sort known per-group selectivities ascending s1<=..<=sK, then
        combined = s1 * prod_{i>=2} s_i^(0.75^(i-1))
    (most-selective term keeps exponent 0.75^0 = 1). If ANY group is unknown
    (empty or NaN), multiply by UNKNOWN_FILTER_COEFFICIENT = 0.9.

    No [0,1] clamp here (Trino does not clamp).
    """
    group_sels = []
    has_unknown = False
    for group in selectivities_by_group:
        if not group:
            has_unknown = True
            continue
        prod = 1.0
        unknown_in_group = False
        for s in group:
            if isnan(s):
                unknown_in_group = True
                break
            prod *= s
        if unknown_in_group:
            has_unknown = True
            continue
        group_sels.append(prod)

    if not group_sels:
        # all groups unknown -> unknown()
        return NAN

    group_sels.sort()
    combined = group_sels[0]
    for i in range(1, len(group_sels)):
        exponent = FILTER_CONJUNCTION_INDEPENDENCE_FACTOR ** i  # 0.75^(i) for i-th (0-based)
        combined *= group_sels[i] ** exponent

    if has_unknown:
        combined *= UNKNOWN_FILTER_COEFFICIENT
    return combined


# ---------------------------------------------------------------------------
# Public dispatch
# ---------------------------------------------------------------------------


def estimate_raw(pred: Predicate, stats: TableStats, *, profile: str = "trino") -> float:
    """Unclamped selectivity (Trino emits raw; may exceed [0,1] in pathological cases)."""
    if pred.family == "A":
        return estimate_family_a(pred, stats)
    if pred.family == "B":
        return estimate_family_b(pred, stats, profile=profile)
    return NAN


def estimate(pred: Predicate, stats: TableStats, *, profile: str = "trino") -> float:
    """Public entry point. Returns the [0,1]-CLAMPED selectivity.

    profile='trino' -> faithful Trino port.
    profile='eimer' -> EIMER departures where they differ.
    """
    if profile not in ("trino", "eimer"):
        raise ValueError(f"unknown profile {profile!r}; expected 'trino' or 'eimer'")
    return _clamp01(estimate_raw(pred, stats, profile=profile))


# ---------------------------------------------------------------------------
# Internal small utilities
# ---------------------------------------------------------------------------


def _as_float(x):
    """Convert a numeric literal to float; non-numeric (categorical) -> None.

    For categorical equality (string literal) the estimate uses only ndv/nf, so a
    None comparand correctly skips the out-of-range numeric check.
    """
    if x is None:
        return None
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _max_exclude_nan(a: float, b: float) -> float:
    """maxExcludeNaN: MAX of two nulls fractions, ignoring NaN."""
    if isnan(a):
        return b
    if isnan(b):
        return a
    return max(a, b)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _approx(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


def _selftest() -> None:
    results = []

    def check(label, got, want, tol=1e-9):
        ok = (isnan(got) and isnan(want)) or _approx(got, want, tol)
        results.append((label, got, want, ok))
        assert ok, f"{label}: got {got!r}, want {want!r}"

    def t(name, low, high, ndv, nf):
        return TableStats(row_count=1000, columns={name: ColumnStats(low, high, ndv, nf)})

    # ---- Family A anchors ----
    s = t("x", 0.0, 100.0, 10.0, 0.0)
    check("A '=' nf=0 ndv=10",
          estimate(Predicate(kind="eq", family="A", col="x", literal=50.0), s), 0.1)
    check("A '<>' nf=0 ndv=10",
          estimate(Predicate(kind="neq", family="A", col="x", literal=50.0), s), 0.9)
    s2 = t("x", 0.0, 100.0, 10.0, 0.0)
    check("A '<' lo=0 hi=100 c=25 nf=0",
          estimate(Predicate(kind="lt", family="A", col="x", literal=25.0), s2), 0.25)

    # ---- Family B '=' / '<>' anchors ----
    sb = TableStats(row_count=1000, columns={
        "L": ColumnStats(0.0, 100.0, 10.0, 0.0),
        "R": ColumnStats(0.0, 100.0, 20.0, 0.0),
    })
    check("B '=' ndvL=10 ndvR=20",
          estimate(Predicate(kind="eq", family="B", col="L", other="R"), sb), 0.05)
    check("B '<>' ndvL=10 ndvR=20",
          estimate(Predicate(kind="neq", family="B", col="L", other="R"), sb), 0.95)

    # ---- Family B '<' identical ranges [0,20], nfL=0.1 nfR=0 ----
    sb2 = TableStats(row_count=1000, columns={
        "U": ColumnStats(0.0, 20.0, 21.0, 0.1),
        "W": ColumnStats(0.0, 20.0, 21.0, 0.0),
    })
    sel_b_lt = estimate(Predicate(kind="lt", family="B", col="U", other="W"), sb2, profile="trino")
    # nullsFF = 1 - max(0.1,0) = 0.9 ; filterFactor = 0.5 ; sel = 0.45
    check("B '<' identical [0,20] nfL=0.1 -> 0.45", sel_b_lt, 0.45)

    # ---- Temporal order identical range nf=0 -> 0.5 ----
    sb3 = TableStats(row_count=1000, columns={
        "ts_a": ColumnStats(0.0, 20.0, 21.0, 0.0),
        "ts_b": ColumnStats(0.0, 20.0, 21.0, 0.0),
    })
    check("temporal order identical range nf=0 -> 0.5",
          estimate(Predicate(kind="order", family="B", col="ts_a", other="ts_b"), sb3, profile="trino"),
          0.5)

    # ---- EIMER band: same span s, d=0.25*s -> 0.4375 ; d>=s -> 1.0 ----
    sband = TableStats(row_count=1000, columns={"p": ColumnStats(0.0, 100.0, 1000.0, 0.0)})
    check("eimer band d=0.25s -> 0.4375",
          eimer_band_selectivity(sband, "p", 25.0), 0.4375)
    check("eimer band d>=s -> 1.0",
          eimer_band_selectivity(sband, "p", 100.0), 1.0)
    check("eimer band d>s -> 1.0",
          eimer_band_selectivity(sband, "p", 150.0), 1.0)

    # ---- EIMER offset band: must collapse to the symmetric band and the one-sided window ----
    # span s=100. lo=-d,hi=+d (d=25) == eimer_band(25) = 0.4375.
    check("eimer offset band lo=-25,hi=+25 == symmetric band 0.4375",
          eimer_offset_band_selectivity(sband, "p", -25.0, 25.0), 0.4375)
    # lo=0,hi=w (w=25) == one-sided window  w/s - (w/s)^2/2 = 0.25 - 0.03125 = 0.21875.
    check("eimer offset band lo=0,hi=25 == one-sided window 0.21875",
          eimer_offset_band_selectivity(sband, "p", 0.0, 25.0), 0.21875)
    # lo=0,hi=s (full positive half) -> T(s)-T(0) = 1 - 0.5 = 0.5.
    check("eimer offset band lo=0,hi=s -> 0.5",
          eimer_offset_band_selectivity(sband, "p", 0.0, 100.0), 0.5)
    # lo=-s,hi=+s (covers the whole triangle) -> 1.0.
    check("eimer offset band lo=-s,hi=+s -> 1.0",
          eimer_offset_band_selectivity(sband, "p", -100.0, 100.0), 1.0)
    # empty band hi<lo -> 0.
    check("eimer offset band hi<lo -> 0",
          eimer_offset_band_selectivity(sband, "p", 50.0, 10.0), 0.0)
    # degenerate span (constant column): D==0 -> passes iff lo<=0<=hi.
    sconst = TableStats(row_count=1000, columns={"p": ColumnStats(7.0, 7.0, 1.0, 0.0)})
    check("eimer offset band constant col lo=0,hi=500 -> 1.0",
          eimer_offset_band_selectivity(sconst, "p", 0.0, 500.0), 1.0)
    check("eimer offset band constant col lo=10,hi=500 -> 0.0",
          eimer_offset_band_selectivity(sconst, "p", 10.0, 500.0), 0.0)
    # via estimate() dispatch: eimer == analytic ; trino == unknown (NaN).
    pred_off = Predicate(kind="offset_band", family="B", col="p", other="p",
                         lo_offset=0.0, hi_offset=25.0, sql="b.p BETWEEN a.p AND a.p + 25")
    check("estimate offset_band eimer dispatch -> 0.21875",
          estimate(pred_off, sband, profile="eimer"), 0.21875)
    check("estimate offset_band trino dispatch -> NaN (unknown)",
          estimate(pred_off, sband, profile="trino"), NAN)

    # ---- Family B '<' filterFactor: 0.625 and 0.875 ----
    # Construct ranges; nf=0 so sel == filterFactor exactly.
    # 0.625 : a=[0,15], b=[0,20]
    s625 = TableStats(row_count=1000, columns={
        "a": ColumnStats(0.0, 15.0, 16.0, 0.0),
        "b": ColumnStats(0.0, 20.0, 21.0, 0.0),
    })
    ff625 = estimate(Predicate(kind="lt", family="B", col="a", other="b"), s625, profile="trino")
    check("B '<' filterFactor 0.625 (a=[0,15] b=[0,20])", ff625, 0.625)
    # 0.875 : a=[0,5], b=[0,20]
    s875 = TableStats(row_count=1000, columns={
        "a": ColumnStats(0.0, 5.0, 6.0, 0.0),
        "b": ColumnStats(0.0, 20.0, 21.0, 0.0),
    })
    ff875 = estimate(Predicate(kind="lt", family="B", col="a", other="b"), s875, profile="trino")
    check("B '<' filterFactor 0.875 (a=[0,5] b=[0,20])", ff875, 0.875)

    # ---- EIMER Family-B '=' disjoint overlap -> 0 ; trino disjoint EQUAL -> 1/max(ndv) ----
    sb_disj = TableStats(row_count=1000, columns={
        "L": ColumnStats(0.0, 10.0, 10.0, 0.0),
        "R": ColumnStats(100.0, 110.0, 10.0, 0.0),
    })
    check("EIMER B '=' disjoint ranges -> 0 (empty overlap)",
          estimate(Predicate(kind="eq", family="B", col="L", other="R"), sb_disj, profile="eimer"), 0.0)
    check("trino B '=' disjoint ranges -> 0.1 (NDV model, not overlap-zeroed)",
          estimate(Predicate(kind="eq", family="B", col="L", other="R"), sb_disj, profile="trino"), 0.1)

    # ---- EIMER exact integral P(A<B) identical [0,20] -> 0.5 ----
    p_int = estimate(Predicate(kind="lt", family="B", col="U", other="W"),
                     TableStats(row_count=1000, columns={
                         "U": ColumnStats(0.0, 20.0, 21.0, 0.0),
                         "W": ColumnStats(0.0, 20.0, 21.0, 0.0)}),
                     profile="eimer")
    check("eimer P(A<B) identical [0,20] -> 0.5", p_int, 0.5)

    # ---- BETWEEN family A: [0,100], between 25..75 -> 0.5 ----
    check("A BETWEEN 25..75 over [0,100] -> 0.5",
          estimate(Predicate(kind="between", family="A", col="x", lo=25.0, hi=75.0),
                   t("x", 0.0, 100.0, 100.0, 0.0)), 0.5)

    # ---- AND composition: backoff, K=2 sels 0.1 & 0.4 -> 0.1 * 0.4^0.75 ----
    ac = and_composition([[0.1], [0.4]])
    want_ac = 0.1 * (0.4 ** (0.75 ** 1))
    check("AND backoff K=2 (0.1,0.4)", ac, want_ac)
    # AND with one unknown group -> *0.9
    ac_unk = and_composition([[0.1], []])
    check("AND with unknown group -> *0.9", ac_unk, 0.1 * 0.9)

    # ---- Trino band uses backoff product (asymmetric artifact), p=d/s=0.25 ----
    tb = trino_band_selectivity(sband, "p", 25.0)
    want_tb = and_composition([[0.25], [0.25]])
    check("trino band p=0.25 (anchored, backoff)", tb, want_tb)

    # ---- NaN / unknown propagation ----
    s_nan = TableStats(row_count=1000, columns={"y": ColumnStats(float("nan"), float("nan"), float("nan"), 0.0)})
    check("A '=' ndv NaN + inf range -> NaN",
          estimate(Predicate(kind="eq", family="A", col="y", literal=1.0),
                   TableStats(row_count=1000, columns={"y": ColumnStats(float("-inf"), float("inf"), float("nan"), 0.0)})),
          NAN)
    check("B '=' ndv NaN -> NaN",
          estimate(Predicate(kind="eq", family="B", col="y", other="y"), s_nan), NAN)

    # ---- Family A out-of-range '=' -> 0 ; '<>' OOR -> (1-nf) ----
    check("A '=' OOR -> 0",
          estimate(Predicate(kind="eq", family="A", col="x", literal=500.0), t("x", 0.0, 100.0, 10.0, 0.0)), 0.0)
    check("A '<>' OOR -> 1.0 (nf=0)",
          estimate(Predicate(kind="neq", family="A", col="x", literal=500.0), t("x", 0.0, 100.0, 10.0, 0.0)), 1.0)

    # ---- Family A '<' / '>' boundary semantics ----
    # filterRange [-inf,c] intersect [lo,hi]: c<lo strict -> empty -> 0; c==lo -> point floor;
    # c==hi -> full 1.0; c>hi -> 1.0. Mirror for '>'.
    sx = t("x", 0.0, 100.0, 10.0, 0.0)
    check("A '<' strict below lo (c=-5) -> 0",
          estimate(Predicate(kind="lt", family="A", col="x", literal=-5.0), sx), 0.0)
    check("A '<' c==lo -> 1/max(ndv,1)=0.1",
          estimate(Predicate(kind="lt", family="A", col="x", literal=0.0), sx), 0.1)
    check("A '<' c==hi -> 1.0",
          estimate(Predicate(kind="lt", family="A", col="x", literal=100.0), sx), 1.0)
    check("A '<' above hi (c=105) -> 1.0",
          estimate(Predicate(kind="lt", family="A", col="x", literal=105.0), sx), 1.0)
    check("A '>' strict above hi (c=105) -> 0",
          estimate(Predicate(kind="gt", family="A", col="x", literal=105.0), sx), 0.0)
    check("A '>' c==hi -> 1/max(ndv,1)=0.1",
          estimate(Predicate(kind="gt", family="A", col="x", literal=100.0), sx), 0.1)
    check("A '>' c==lo -> 1.0",
          estimate(Predicate(kind="gt", family="A", col="x", literal=0.0), sx), 1.0)
    check("A '>' below lo (c=-5) -> 1.0",
          estimate(Predicate(kind="gt", family="A", col="x", literal=-5.0), sx), 1.0)

    print("mode_b_v2 self-test: ALL PASS")
    width = max(len(lbl) for lbl, *_ in results)
    for lbl, got, want, ok in results:
        print(f"  [{'ok' if ok else 'XX'}] {lbl:<{width}}  got={got!r:<22} want={want!r}")


if __name__ == "__main__":
    _selftest()
