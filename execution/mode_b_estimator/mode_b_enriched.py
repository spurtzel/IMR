"""Enriched stats-only predicate-selectivity estimator.

  (i)  skewed categorical equality, priced from the frequency sketch:
           family A:  P(row = v)               = (1-nf) * f(v)
           family B:  P(rowA = rowB), same col = (1-nf_L)(1-nf_R) * Sum_i f_i^2
  (ii) range / band on a clustered, non-uniform numeric column, priced from the
       empirical CDF in the histogram:
           family A '<'      -> (1-nf) * cdf(c)
           family A between  -> (1-nf) * (cdf(c2) - cdf(c1))
           family B band |a.col-b.col|<=d -> Sum_i p_i ( cdf(m_i+d) - cdf(m_i-d) )

Everything else (family-B col-col inequality, temporal order, unique-key '=' with no
freq, any column lacking the enriched stat) delegates to mode_b_analytic.estimate(..., 'eimer').

NaN == 'unknown' (from contract.py) is preserved, not clamped to 0. The final enriched
result is clamped to [0,1] to match the public mode_b_analytic.estimate.
"""
from __future__ import annotations

from math import isnan

from . import mode_b_analytic
from .contract import (
    ColumnStats,
    Predicate,
    TableStats,
)

NAN = float("nan")

# Operators whose family-A form is a numeric range / inequality served by the histogram.
_RANGE_KINDS = ("lt", "le", "gt", "ge", "between")


def _delegate(pred: Predicate, stats: TableStats) -> float:
    """Verbatim fall-back to the baseline (always the EIMER profile)."""
    return mode_b_analytic.estimate(pred, stats, profile="eimer")


def _clamp01(x: float) -> float:
    """Clip to [0,1]; preserve NaN (unknown)."""
    if isnan(x):
        return x
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


# ---------------------------------------------------------------------------
# Enriched operators
# ---------------------------------------------------------------------------


def _eq_family_a(c: ColumnStats, literal) -> float:
    """(1 - nf) * f(literal). Caller guarantees c.freq is present."""
    return (1.0 - c.nulls_fraction) * c.freq.freq_of(str(literal))


def _neq_family_a(c: ColumnStats, literal) -> float:
    """(1 - nf) * (1 - f(literal))."""
    return (1.0 - c.nulls_fraction) * (1.0 - c.freq.freq_of(str(literal)))


def _eq_family_b_same(cl: ColumnStats, cr: ColumnStats) -> float:
    """(1 - nf_L)(1 - nf_R) * Sum_i f_i^2  (collision). Same categorical column."""
    return (1.0 - cl.nulls_fraction) * (1.0 - cr.nulls_fraction) * cl.freq.collision()


def _eq_family_b_cross(cl: ColumnStats, cr: ColumnStats) -> float:
    """P(L = R) for independent draws L ~ column l, R ~ column r (DIFFERENT columns, both sketched):
    sum_v f_l(v) f_r(v). The two top-K sketches give the heavy values exactly; each column's residual
    mass is spread uniformly over its residual distinct values, and the two residual value sets are
    taken as nested (containment), so residual x residual contributes res_l * res_r / max(ndv_res).
    Null factor (1 - nf_l)(1 - nf_r) as the same-column collision."""
    fl, fr = cl.freq, cr.freq
    if fl is None or fr is None:
        return NAN
    top_l, top_r = dict(fl.top), dict(fr.top)
    unif_l = (fl.residual_fraction / fl.residual_ndv) if fl.residual_ndv > 0 else 0.0
    unif_r = (fr.residual_fraction / fr.residual_ndv) if fr.residual_ndv > 0 else 0.0
    acc = sum(p * top_r[v] for v, p in top_l.items() if v in top_r)
    acc += sum(p * unif_r for v, p in top_l.items() if v not in top_r)
    acc += sum(p * unif_l for v, p in top_r.items() if v not in top_l)
    nd = max(fl.residual_ndv, fr.residual_ndv)
    if nd > 0:
        acc += fl.residual_fraction * fr.residual_fraction / nd
    return (1.0 - cl.nulls_fraction) * (1.0 - cr.nulls_fraction) * acc


def _neq_family_b_same(cl: ColumnStats, cr: ColumnStats) -> float:
    """(1 - nf_L)(1 - nf_R) * (1 - collision)."""
    return (1.0 - cl.nulls_fraction) * (1.0 - cr.nulls_fraction) * (1.0 - cl.freq.collision())


def _range_family_a(pred: Predicate, c: ColumnStats) -> float:
    """Histogram-backed family-A range/inequality. Caller guarantees c.hist present."""
    nf = c.nulls_fraction
    h = c.hist
    kind = pred.kind

    if kind in ("lt", "le"):
        cc = _as_float(pred.literal)
        if cc is None:
            return NAN
        return (1.0 - nf) * h.cdf(cc)

    if kind in ("gt", "ge"):
        cc = _as_float(pred.literal)
        if cc is None:
            return NAN
        return (1.0 - nf) * (1.0 - h.cdf(cc))

    if kind == "between":
        c1 = _as_float(pred.lo)
        c2 = _as_float(pred.hi)
        if c1 is None or c2 is None:
            return NAN
        mass = h.cdf(c2) - h.cdf(c1)
        if mass < 0.0:
            mass = 0.0  # clamp the (c2 < c1) degenerate case to >= 0
        return (1.0 - nf) * mass

    return NAN


def _band_family_b_same(c: ColumnStats, d: float) -> float:
    """P(|X - Y| <= d) for X,Y iid ~ histogram marginal, same column.

    Marginal-convolution identity for two independent draws:
        P(|X-Y| <= d) = E_X[ cdf(X+d) - cdf(X-d) ]
                      ~= Sum_i p_i ( cdf(m_i + d) - cdf(m_i - d) )
    with m_i = bin midpoint (lo+hi)/2 and p_i = bin mass.

    No (1-nf) factor: the band is treated as a pure geometric/marginal probability,
    matching mode_b_analytic.eimer_band_selectivity.
    """
    if d is None or isnan(d):
        return NAN
    h = c.hist
    # Degenerate/point-mass column collapses to a single edge after monotonisation, so
    # bins() is empty and the convolution would wrongly be 0.0. The true marginal is a
    # Dirac (|X-Y| == 0 for every pair), so the band passes iff d >= 0.
    edges = h.edges
    if len(edges) < 2 or edges[-1] == edges[0]:
        return 1.0 if d >= 0.0 else 0.0
    acc = 0.0
    for lo, hi, p in h.bins():
        m = (lo + hi) / 2.0
        acc += p * (h.cdf(m + d) - h.cdf(m - d))
    return acc


def _offset_band_family_b_same(c: ColumnStats, lo: float, hi: float) -> float:
    """P(lo <= B - A <= hi) for A, B iid ~ histogram marginal, same column (directed band).

    ,,b.col BETWEEN a.col + lo AND a.col + hi'' is exactly {lo <= B - A <= hi}. Conditioning
    on the anchor draw A = m gives P(lo <= B - m <= hi) = cdf(m+hi) - cdf(m+lo), so:
        P(lo <= B-A <= hi) = E_A[ cdf(A+hi) - cdf(A+lo) ]
                           ~= Sum_i p_i ( cdf(m_i + hi) - cdf(m_i + lo) )
    with m_i = bin midpoint, p_i = bin mass. No (1-nf) factor (pure geometric band);
    the symmetric case (lo=-d, hi=+d) reduces to _band_family_b_same.
    """
    if lo is None or hi is None or isnan(lo) or isnan(hi):
        return NAN
    if hi < lo:
        return 0.0  # empty band
    h = c.hist
    # Degenerate/point-mass column -> Dirac at 0: B - A == 0 for every pair, so the
    # band passes iff lo <= 0 <= hi.
    edges = h.edges
    if len(edges) < 2 or edges[-1] == edges[0]:
        return 1.0 if (lo <= 0.0 <= hi) else 0.0
    acc = 0.0
    for blo, bhi, p in h.bins():
        m = (blo + bhi) / 2.0
        acc += p * (h.cdf(m + hi) - h.cdf(m + lo))
    return acc


# ---------------------------------------------------------------------------
# Public dispatch
# ---------------------------------------------------------------------------


def estimate(pred: Predicate, stats: TableStats, *, profile: str = "eimer_plus") -> float:
    """Enriched selectivity in [0,1].

    profile is accepted for parity with mode_b_analytic; only 'eimer_plus' is meaningful.
    Uses freq/hist where present, delegates every other operator to
    mode_b_analytic.estimate(pred, stats, 'eimer').
    """
    if profile != "eimer_plus":
        raise ValueError(f"unknown profile {profile!r}; mode_b_enriched expects 'eimer_plus'")

    kind = pred.kind
    fam = pred.family

    # ---- EQUALITY ----------------------------------------------------------
    if kind == "eq":
        if fam == "A":
            c = stats.col(pred.col)
            if c.freq is not None:
                return _clamp01(_eq_family_a(c, pred.literal))
            return _delegate(pred, stats)  # unique-key '=' w/o freq -> baseline
        if fam == "B":
            cl = stats.col(pred.col)
            cr = stats.col(pred.other)
            # Same column AND freq present -> collision. Different columns, both sketched ->
            # frequency overlap; otherwise delegate to the analytic 1/max(ndv) form.
            if pred.col == pred.other and cl.freq is not None:
                return _clamp01(_eq_family_b_same(cl, cr))
            if pred.col != pred.other and cl.freq is not None and cr.freq is not None:
                return _clamp01(_eq_family_b_cross(cl, cr))
            return _delegate(pred, stats)

    # ---- NOT EQUAL ---------------------------------------------------------
    if kind == "neq":
        if fam == "A":
            c = stats.col(pred.col)
            if c.freq is not None:
                return _clamp01(_neq_family_a(c, pred.literal))
            return _delegate(pred, stats)
        if fam == "B":
            cl = stats.col(pred.col)
            cr = stats.col(pred.other)
            if pred.col == pred.other and cl.freq is not None:
                return _clamp01(_neq_family_b_same(cl, cr))
            return _delegate(pred, stats)

    # ---- RANGE / INEQUALITY ------------------------------------------------
    if kind in _RANGE_KINDS:
        if fam == "A":
            c = stats.col(pred.col)
            if c.hist is not None:
                return _clamp01(_range_family_a(pred, c))
            return _delegate(pred, stats)
        # family-B col-col inequality -> delegate (no marginal-only form).
        return _delegate(pred, stats)

    # ---- SPATIAL BAND / TEMPORAL WINDOW (family B, |a.col - b.col| <= d) ----
    if kind in ("band", "window"):
        cl = stats.col(pred.col)
        # SAME column + hist present -> marginal-convolution band; else delegate.
        same_col = (pred.other is None) or (pred.col == pred.other)
        if same_col and cl.hist is not None:
            d = pred.delta if kind == "band" else pred.w
            return _clamp01(_band_family_b_same(cl, _as_float(d)))
        return _delegate(pred, stats)

    # ---- OFFSET / ASYMMETRIC BAND (family B, a.col+lo <= b.col <= a.col+hi) ----
    if kind == "offset_band":
        cl = stats.col(pred.col)
        same_col = (pred.other is None) or (pred.col == pred.other)
        if same_col and cl.hist is not None:
            return _clamp01(_offset_band_family_b_same(cl, _as_float(pred.lo_offset), _as_float(pred.hi_offset)))
        # No histogram -> fall back to the analytic 'eimer' form (triangular-difference CDF).
        return _delegate(pred, stats)

    # ---- EVERYTHING ELSE (temporal order, etc.) -> delegate ----------------
    return _delegate(pred, stats)


def _as_float(x):
    """Numeric coercion mirroring mode_b_analytic._as_float (categorical -> None)."""
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


# ---------------------------------------------------------------------------
# Self-test: synthetic stats, no Trino.
# ---------------------------------------------------------------------------


def _approx(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


def _uniform_hist(lo: float, hi: float, bins: int) -> "Histogram":
    """Equi-spaced edges over [lo,hi] with LINEAR cumulative probs -> a uniform marginal."""
    from .contract import Histogram
    edges = tuple(lo + (hi - lo) * i / bins for i in range(bins + 1))
    probs = tuple(i / bins for i in range(bins + 1))
    return Histogram(edges=edges, probs=probs)


def _selftest() -> None:
    from .contract import Frequencies, Histogram

    results = []

    def check(label, got, want, tol=1e-9):
        ok = (isnan(got) and isnan(want)) or _approx(got, want, tol)
        results.append((label, got, want, ok))
        assert ok, f"{label}: got {got!r}, want {want!r}"

    # ---- Frequencies: Z:0.7, R:0.1, B:0.1, M:0.1 (covers full mass, no residual) ----
    freq = Frequencies(top=(("Z", 0.7), ("R", 0.1), ("B", 0.1), ("M", 0.1)),
                       residual_fraction=0.0, residual_ndv=0.0)
    # collision = 0.49 + 0.01 + 0.01 + 0.01 = 0.52
    assert _approx(freq.collision(), 0.52, 1e-12), freq.collision()

    # Family-B '=' same col with that freq, nf=0 -> collision == 0.52 (NOT 1/4=0.25).
    sB = TableStats(row_count=1000, columns={
        "etype": ColumnStats(low=NAN, high=NAN, ndv=4.0, nulls_fraction=0.0, freq=freq),
    })
    check("B '=' same col collision -> 0.52",
          estimate(Predicate(kind="eq", family="B", col="etype", other="etype"), sB), 0.52)

    # Family-A '=' literal 'R' -> 0.10 (NOT 1/ndv = 0.25).
    sA = TableStats(row_count=1000, columns={
        "etype": ColumnStats(low=NAN, high=NAN, ndv=4.0, nulls_fraction=0.0, freq=freq),
    })
    check("A '=' literal R -> 0.10",
          estimate(Predicate(kind="eq", family="A", col="etype", literal="R"), sA), 0.10)
    # Family-A '<>' literal 'R' -> 1 - 0.10 = 0.90.
    check("A '<>' literal R -> 0.90",
          estimate(Predicate(kind="neq", family="A", col="etype", literal="R"), sA), 0.90)
    # Skew check: '=' on the heavy value Z -> 0.70 (uniform 1/ndv would say 0.25).
    check("A '=' literal Z (heavy) -> 0.70",
          estimate(Predicate(kind="eq", family="A", col="etype", literal="Z"), sA), 0.70)
    # Family-B '<>' same col -> 1 - collision = 0.48.
    check("B '<>' same col -> 0.48",
          estimate(Predicate(kind="neq", family="B", col="etype", other="etype"), sB), 0.48)

    # ---- Range over a UNIFORM histogram [0,100], 100 bins ----
    uhist = _uniform_hist(0.0, 100.0, 100)
    sR = TableStats(row_count=1000, columns={
        "id": ColumnStats(low=0.0, high=100.0, ndv=1000.0, nulls_fraction=0.0, hist=uhist),
    })
    # between 25..75 -> cdf(75)-cdf(25) = 0.50.
    check("between 25..75 uniform -> 0.50",
          estimate(Predicate(kind="between", family="A", col="id", lo=25.0, hi=75.0), sR), 0.50, tol=1e-9)
    # '<' 25 -> cdf(25) = 0.25.
    check("'<' 25 uniform -> 0.25",
          estimate(Predicate(kind="lt", family="A", col="id", literal=25.0), sR), 0.25, tol=1e-9)
    # '>' 25 -> 1 - cdf(25) = 0.75.
    check("'>' 25 uniform -> 0.75",
          estimate(Predicate(kind="gt", family="A", col="id", literal=25.0), sR), 0.75, tol=1e-9)
    # between with c2 < c1 -> clamped to 0.
    check("between 75..25 (inverted) -> 0",
          estimate(Predicate(kind="between", family="A", col="id", lo=75.0, hi=25.0), sR), 0.0, tol=1e-9)

    # ---- BAND over a UNIFORM histogram, span s=100, d=0.25*s=25 -> ~0.4375 ----
    sBand = TableStats(row_count=1000, columns={
        "lon": ColumnStats(low=0.0, high=100.0, ndv=1000.0, nulls_fraction=0.0, hist=uhist),
    })
    band_uniform = estimate(Predicate(kind="band", family="B", col="lon", other="lon", delta=25.0), sBand)
    check("band uniform d=0.25s -> ~0.4375", band_uniform, 0.4375, tol=0.01)
    # d >= s -> ~1.0.
    check("band uniform d>=s -> ~1.0",
          estimate(Predicate(kind="band", family="B", col="lon", other="lon", delta=100.0), sBand), 1.0, tol=1e-9)

    # ---- OFFSET BAND over the UNIFORM histogram: must match the analytic special cases ----
    # lo=-25,hi=+25 (symmetric) -> ~0.4375 (== band uniform).
    check("offset band uniform lo=-25,hi=+25 -> ~0.4375",
          estimate(Predicate(kind="offset_band", family="B", col="lon", other="lon",
                             lo_offset=-25.0, hi_offset=25.0), sBand), 0.4375, tol=0.01)
    # lo=0,hi=25 (one-sided) -> ~0.21875.
    check("offset band uniform lo=0,hi=25 -> ~0.21875",
          estimate(Predicate(kind="offset_band", family="B", col="lon", other="lon",
                             lo_offset=0.0, hi_offset=25.0), sBand), 0.21875, tol=0.01)
    # lo=0,hi=s (full positive half) -> ~0.5.
    check("offset band uniform lo=0,hi=s -> ~0.5",
          estimate(Predicate(kind="offset_band", family="B", col="lon", other="lon",
                             lo_offset=0.0, hi_offset=100.0), sBand), 0.5, tol=0.01)
    # No histogram -> delegate to the analytic 'eimer' form.
    sOffNoHist = TableStats(row_count=1000, columns={
        "id": ColumnStats(low=0.0, high=100.0, ndv=1000.0, nulls_fraction=0.0),
    })
    pred_off = Predicate(kind="offset_band", family="B", col="id", other="id",
                         lo_offset=0.0, hi_offset=25.0)
    check("offset band no-hist == analytic eimer",
          estimate(pred_off, sOffNoHist), mode_b_analytic.estimate(pred_off, sOffNoHist, profile="eimer"))

    # ---- SKEWED histogram: mass concentrated in a narrow band -> HIGHER band sel ----
    # Edges 0..100 but cumulative probs jump steeply in [40,60] (80% of mass there).
    skew_edges = (0.0, 40.0, 50.0, 60.0, 100.0)
    skew_probs = (0.0, 0.10, 0.50, 0.90, 1.0)  # 10% / 40% / 40% / 10%
    shist = Histogram(edges=skew_edges, probs=skew_probs)
    sSkew = TableStats(row_count=1000, columns={
        "lon": ColumnStats(low=0.0, high=100.0, ndv=1000.0, nulls_fraction=0.0, hist=shist),
    })
    band_skew = estimate(Predicate(kind="band", family="B", col="lon", other="lon", delta=25.0), sSkew)
    assert band_skew > band_uniform, (band_skew, band_uniform)
    results.append(("band skewed > band uniform (clustering)", band_skew, band_uniform, True))

    # ---- TEMPORAL WINDOW (uses pred.w, same uniform hist) reduces to band form ----
    sWin = TableStats(row_count=1000, columns={
        "ts": ColumnStats(low=0.0, high=100.0, ndv=1000.0, nulls_fraction=0.0, hist=uhist),
    })
    check("window uniform w=0.25s -> ~0.4375",
          estimate(Predicate(kind="window", family="B", col="ts", other="ts", w=25.0), sWin), 0.4375, tol=0.01)

    # ---- DELEGATION: family-B inequality a.id < b.id with NO freq/hist == analytic 'eimer' ----
    sDel = TableStats(row_count=1000, columns={
        "id": ColumnStats(low=0.0, high=20.0, ndv=21.0, nulls_fraction=0.0),
    })
    pred_del = Predicate(kind="lt", family="B", col="id", other="id")
    got_del = estimate(pred_del, sDel)
    want_del = mode_b_analytic.estimate(pred_del, sDel, profile="eimer")
    check("delegate B '<' a.id<b.id == analytic eimer", got_del, want_del)

    # ---- DELEGATION: family-A '=' with NO freq (unique-key id) == analytic 'eimer' ----
    sDelEq = TableStats(row_count=1000, columns={
        "id": ColumnStats(low=0.0, high=100.0, ndv=10.0, nulls_fraction=0.0),
    })
    pred_eq = Predicate(kind="eq", family="A", col="id", literal=50.0)
    got_eq = estimate(pred_eq, sDelEq)
    want_eq = mode_b_analytic.estimate(pred_eq, sDelEq, profile="eimer")
    check("delegate A '=' no-freq == analytic eimer (0.1)", got_eq, want_eq)
    check("delegate A '=' no-freq value == 0.1", got_eq, 0.1)

    # ---- family-B '=' DIFFERENT cols: both sketched -> frequency overlap; else delegate ----
    # two constant-"X" columns -> 1.0
    sDiff = TableStats(row_count=1000, columns={
        "a": ColumnStats(low=0.0, high=100.0, ndv=10.0, nulls_fraction=0.0,
                         freq=Frequencies(top=(("X", 1.0),), residual_fraction=0.0, residual_ndv=0.0)),
        "b": ColumnStats(low=0.0, high=100.0, ndv=20.0, nulls_fraction=0.0,
                         freq=Frequencies(top=(("X", 1.0),), residual_fraction=0.0, residual_ndv=0.0)),
    })
    pred_diff = Predicate(kind="eq", family="B", col="a", other="b")
    check("B '=' different cols, both sketched constant X -> 1.0", estimate(pred_diff, sDiff), 1.0)
    # with a sketch missing on either side the analytic form is delegated to
    sDiffNo = TableStats(row_count=1000, columns={
        "a": ColumnStats(low=0.0, high=100.0, ndv=10.0, nulls_fraction=0.0,
                         freq=Frequencies(top=(("X", 1.0),), residual_fraction=0.0, residual_ndv=0.0)),
        "b": ColumnStats(low=0.0, high=100.0, ndv=20.0, nulls_fraction=0.0),
    })
    got_diff = estimate(pred_diff, sDiffNo)
    want_diff = mode_b_analytic.estimate(pred_diff, sDiffNo, profile="eimer")
    check("delegate B '=' different cols (one sketch missing) == analytic eimer", got_diff, want_diff)

    # ---- numeric / boolean sketch matching: literal True / 1.0 meets the sketch value "1" ----
    fBool = Frequencies(top=(("1", 0.3), ("0", 0.7)), residual_fraction=0.0, residual_ndv=0.0)
    sBool = TableStats(row_count=100, columns={"arrest": ColumnStats(low=0.0, high=1.0, ndv=2.0, nulls_fraction=0.0, freq=fBool)})
    check("A '=' boolean literal vs numeric sketch", estimate(Predicate(kind="eq", family="A", col="arrest", literal=True), sBool), 0.3)
    check("A '=' float literal vs integer sketch", estimate(Predicate(kind="eq", family="A", col="arrest", literal=1.0), sBool), 0.3)
    check("A '<>' boolean literal vs numeric sketch", estimate(Predicate(kind="neq", family="A", col="arrest", literal=False), sBool), 0.3)
    # a boolean column sketched by the engine: the values arrive as True / False
    fBool2 = Frequencies(top=(("False", 0.7), ("True", 0.3)), residual_fraction=0.0, residual_ndv=0.0)
    sBool2 = TableStats(row_count=100, columns={"arrest": ColumnStats(low=0.0, high=1.0, ndv=2.0, nulls_fraction=0.0, freq=fBool2)})
    check("A '=' boolean literal vs boolean sketch", estimate(Predicate(kind="eq", family="A", col="arrest", literal=True), sBool2), 0.3)

    # ---- CROSS-COLUMN '=' with BOTH sketches: sum_v f_l(v) f_r(v) ----
    # l: {x: 0.5, y: 0.3} + residual 0.2 over 2 values; r: {y: 0.6, z: 0.2} + residual 0.2 over 4 values
    fL = Frequencies(top=(("x", 0.5), ("y", 0.3)), residual_fraction=0.2, residual_ndv=2.0)
    fR = Frequencies(top=(("y", 0.6), ("z", 0.2)), residual_fraction=0.2, residual_ndv=4.0)
    sCross = TableStats(row_count=100, columns={
        "l": ColumnStats(low=0.0, high=10.0, ndv=4.0, nulls_fraction=0.0, freq=fL),
        "r": ColumnStats(low=0.0, high=10.0, ndv=6.0, nulls_fraction=0.0, freq=fR)})
    # heavy x heavy: y: 0.3*0.6 = 0.18; x (heavy in l, residual in r): 0.5 * 0.2/4 = 0.025;
    # z (heavy in r, residual in l): 0.2 * 0.2/2 = 0.02; residual x residual: 0.2*0.2/4 = 0.01 -> 0.235
    check("B '=' cross columns, both sketched", estimate(Predicate(kind="eq", family="B", col="l", other="r"), sCross), 0.235, tol=1e-12)
    check("B '=' cross columns symmetric", estimate(Predicate(kind="eq", family="B", col="r", other="l"), sCross), 0.235, tol=1e-12)
    # one-sided offset_band (open upper side) on a uniform histogram: P(B - A > 25) on U[0,100] = (75/100)^2/2 = 0.28125
    check("offset_band open side P(B-A > 25) on U[0,100]",
          estimate(Predicate(kind="offset_band", family="B", col="lon", other="lon", lo_offset=25.0, hi_offset=float("inf")), sBand), 0.28125, tol=0.02)

    # ---- DELEGATION: range family-A with NO hist == analytic 'eimer' ----
    sNoHist = TableStats(row_count=1000, columns={
        "lon": ColumnStats(low=0.0, high=100.0, ndv=100.0, nulls_fraction=0.0),
    })
    pred_lt = Predicate(kind="lt", family="A", col="lon", literal=25.0)
    got_lt = estimate(pred_lt, sNoHist)
    want_lt = mode_b_analytic.estimate(pred_lt, sNoHist, profile="eimer")
    check("delegate A '<' no-hist == analytic eimer", got_lt, want_lt)

    # ---- DELEGATION: temporal order -> always delegate ----
    sOrd = TableStats(row_count=1000, columns={
        "ts": ColumnStats(low=0.0, high=20.0, ndv=21.0, nulls_fraction=0.0, hist=uhist),
    })
    pred_ord = Predicate(kind="order", family="B", col="ts", other="ts")
    got_ord = estimate(pred_ord, sOrd)
    want_ord = mode_b_analytic.estimate(pred_ord, sOrd, profile="eimer")
    check("delegate temporal order == analytic eimer", got_ord, want_ord)

    print("mode_b_enriched self-test: ALL PASS")
    width = max(len(lbl) for lbl, *_ in results)
    for lbl, got, want, ok in results:
        print(f"  [{'ok' if ok else 'XX'}] {lbl:<{width}}  got={got!r:<22} want={want!r}")


if __name__ == "__main__":
    _selftest()
