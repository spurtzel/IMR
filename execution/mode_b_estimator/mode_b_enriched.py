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

NaN means 'unknown' (see contract.py) and is preserved. The final enriched
result is clamped to [0,1].
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

_RANGE_KINDS = ("lt", "le", "gt", "ge", "between")


def _delegate(pred: Predicate, stats: TableStats) -> float:
    """Delegates to the analytic estimator (EIMER profile)."""
    return mode_b_analytic.estimate(pred, stats, profile="eimer")


def _clamp01(x: float) -> float:
    """Clips to [0,1]; preserves NaN (unknown)."""
    if isnan(x):
        return x
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _eq_family_a(c: ColumnStats, literal) -> float:
    """(1 - nf) * f(literal). Requires c.freq."""
    return (1.0 - c.nulls_fraction) * c.freq.freq_of(str(literal))


def _neq_family_a(c: ColumnStats, literal) -> float:
    """(1 - nf) * (1 - f(literal))."""
    return (1.0 - c.nulls_fraction) * (1.0 - c.freq.freq_of(str(literal)))


def _eq_family_b_same(cl: ColumnStats, cr: ColumnStats) -> float:
    """(1 - nf_L)(1 - nf_R) * Sum_i f_i^2  (collision). Same categorical column."""
    return (1.0 - cl.nulls_fraction) * (1.0 - cr.nulls_fraction) * cl.freq.collision()


def _eq_family_b_cross(cl: ColumnStats, cr: ColumnStats) -> float:
    """P(L = R) for independent draws L ~ column l, R ~ column r (different columns, both sketched):
        sum_v f_l(v) f_r(v). The two top-K sketches give the heavy values exactly; each column's residual
        mass is spread uniformly over its residual distinct values, and the two residual value sets are
        taken as nested, so residual x residual contributes res_l * res_r / max(ndv_res).
        Null factor (1 - nf_l)(1 - nf_r) as the same-column collision.
    """
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
    """Histogram-backed family-A range/inequality. Requires c.hist."""
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
            mass = 0.0
        return (1.0 - nf) * mass

    return NAN


def _band_family_b_same(c: ColumnStats, d: float) -> float:
    """P(|X - Y| <= d) for X,Y iid ~ histogram marginal, same column.

        P(|X-Y| <= d) = E_X[ cdf(X+d) - cdf(X-d) ]
                      ~= Sum_i p_i ( cdf(m_i + d) - cdf(m_i - d) )
        with m_i = bin midpoint (lo+hi)/2 and p_i = bin mass.

        No (1-nf) factor, as in mode_b_analytic.eimer_band_selectivity.
    """
    if d is None or isnan(d):
        return NAN
    h = c.hist
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

        `b.col BETWEEN a.col + lo AND a.col + hi` is {lo <= B - A <= hi}:
            P(lo <= B-A <= hi) = E_A[ cdf(A+hi) - cdf(A+lo) ]
                               ~= Sum_i p_i ( cdf(m_i + hi) - cdf(m_i + lo) )
        with m_i = bin midpoint, p_i = bin mass. No (1-nf) factor; the symmetric case
        (lo=-d, hi=+d) is `_band_family_b_same`.
    """
    if lo is None or hi is None or isnan(lo) or isnan(hi):
        return NAN
    if hi < lo:
        return 0.0
    h = c.hist
    edges = h.edges
    if len(edges) < 2 or edges[-1] == edges[0]:
        return 1.0 if (lo <= 0.0 <= hi) else 0.0
    acc = 0.0
    for blo, bhi, p in h.bins():
        m = (blo + bhi) / 2.0
        acc += p * (h.cdf(m + hi) - h.cdf(m + lo))
    return acc


def estimate(pred: Predicate, stats: TableStats, *, profile: str = "eimer_plus") -> float:
    """Enriched selectivity in [0,1].

        Only profile 'eimer_plus' is meaningful. Uses freq/hist where present, delegates every
        other operator to mode_b_analytic.estimate(pred, stats, 'eimer').
    """
    if profile != "eimer_plus":
        raise ValueError(f"unknown profile {profile!r}; mode_b_enriched expects 'eimer_plus'")

    kind = pred.kind
    fam = pred.family

    if kind == "eq":
        if fam == "A":
            c = stats.col(pred.col)
            if c.freq is not None:
                return _clamp01(_eq_family_a(c, pred.literal))
            return _delegate(pred, stats)
        if fam == "B":
            cl = stats.col(pred.col)
            cr = stats.col(pred.other)
            if pred.col == pred.other and cl.freq is not None:
                return _clamp01(_eq_family_b_same(cl, cr))
            if pred.col != pred.other and cl.freq is not None and cr.freq is not None:
                return _clamp01(_eq_family_b_cross(cl, cr))
            return _delegate(pred, stats)

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

    if kind in _RANGE_KINDS:
        if fam == "A":
            c = stats.col(pred.col)
            if c.hist is not None:
                return _clamp01(_range_family_a(pred, c))
            return _delegate(pred, stats)
        return _delegate(pred, stats)

    if kind in ("band", "window"):
        cl = stats.col(pred.col)
        same_col = (pred.other is None) or (pred.col == pred.other)
        if same_col and cl.hist is not None:
            d = pred.delta if kind == "band" else pred.w
            return _clamp01(_band_family_b_same(cl, _as_float(d)))
        return _delegate(pred, stats)

    if kind == "offset_band":
        cl = stats.col(pred.col)
        same_col = (pred.other is None) or (pred.col == pred.other)
        if same_col and cl.hist is not None:
            return _clamp01(_offset_band_family_b_same(cl, _as_float(pred.lo_offset), _as_float(pred.hi_offset)))
        return _delegate(pred, stats)

    return _delegate(pred, stats)


def _as_float(x):
    """Numeric coercion as in mode_b_analytic._as_float (categorical -> None)."""
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


def _approx(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


def _uniform_hist(lo: float, hi: float, bins: int) -> "Histogram":
    """Equi-spaced edges over [lo,hi] with linear cumulative probs -> a uniform marginal."""
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

    freq = Frequencies(top=(("Z", 0.7), ("R", 0.1), ("B", 0.1), ("M", 0.1)),
                       residual_fraction=0.0, residual_ndv=0.0)
    assert _approx(freq.collision(), 0.52, 1e-12), freq.collision()

    sB = TableStats(row_count=1000, columns={
        "etype": ColumnStats(low=NAN, high=NAN, ndv=4.0, nulls_fraction=0.0, freq=freq),
    })
    check("B '=' same col collision -> 0.52",
          estimate(Predicate(kind="eq", family="B", col="etype", other="etype"), sB), 0.52)

    sA = TableStats(row_count=1000, columns={
        "etype": ColumnStats(low=NAN, high=NAN, ndv=4.0, nulls_fraction=0.0, freq=freq),
    })
    check("A '=' literal R -> 0.10",
          estimate(Predicate(kind="eq", family="A", col="etype", literal="R"), sA), 0.10)
    check("A '<>' literal R -> 0.90",
          estimate(Predicate(kind="neq", family="A", col="etype", literal="R"), sA), 0.90)
    check("A '=' literal Z (heavy) -> 0.70",
          estimate(Predicate(kind="eq", family="A", col="etype", literal="Z"), sA), 0.70)
    check("B '<>' same col -> 0.48",
          estimate(Predicate(kind="neq", family="B", col="etype", other="etype"), sB), 0.48)

    uhist = _uniform_hist(0.0, 100.0, 100)
    sR = TableStats(row_count=1000, columns={
        "id": ColumnStats(low=0.0, high=100.0, ndv=1000.0, nulls_fraction=0.0, hist=uhist),
    })
    check("between 25..75 uniform -> 0.50",
          estimate(Predicate(kind="between", family="A", col="id", lo=25.0, hi=75.0), sR), 0.50, tol=1e-9)
    check("'<' 25 uniform -> 0.25",
          estimate(Predicate(kind="lt", family="A", col="id", literal=25.0), sR), 0.25, tol=1e-9)
    check("'>' 25 uniform -> 0.75",
          estimate(Predicate(kind="gt", family="A", col="id", literal=25.0), sR), 0.75, tol=1e-9)
    check("between 75..25 (inverted) -> 0",
          estimate(Predicate(kind="between", family="A", col="id", lo=75.0, hi=25.0), sR), 0.0, tol=1e-9)

    sBand = TableStats(row_count=1000, columns={
        "lon": ColumnStats(low=0.0, high=100.0, ndv=1000.0, nulls_fraction=0.0, hist=uhist),
    })
    band_uniform = estimate(Predicate(kind="band", family="B", col="lon", other="lon", delta=25.0), sBand)
    check("band uniform d=0.25s -> ~0.4375", band_uniform, 0.4375, tol=0.01)
    check("band uniform d>=s -> ~1.0",
          estimate(Predicate(kind="band", family="B", col="lon", other="lon", delta=100.0), sBand), 1.0, tol=1e-9)

    check("offset band uniform lo=-25,hi=+25 -> ~0.4375",
          estimate(Predicate(kind="offset_band", family="B", col="lon", other="lon",
                             lo_offset=-25.0, hi_offset=25.0), sBand), 0.4375, tol=0.01)
    check("offset band uniform lo=0,hi=25 -> ~0.21875",
          estimate(Predicate(kind="offset_band", family="B", col="lon", other="lon",
                             lo_offset=0.0, hi_offset=25.0), sBand), 0.21875, tol=0.01)
    check("offset band uniform lo=0,hi=s -> ~0.5",
          estimate(Predicate(kind="offset_band", family="B", col="lon", other="lon",
                             lo_offset=0.0, hi_offset=100.0), sBand), 0.5, tol=0.01)
    sOffNoHist = TableStats(row_count=1000, columns={
        "id": ColumnStats(low=0.0, high=100.0, ndv=1000.0, nulls_fraction=0.0),
    })
    pred_off = Predicate(kind="offset_band", family="B", col="id", other="id",
                         lo_offset=0.0, hi_offset=25.0)
    check("offset band no-hist == analytic eimer",
          estimate(pred_off, sOffNoHist), mode_b_analytic.estimate(pred_off, sOffNoHist, profile="eimer"))

    skew_edges = (0.0, 40.0, 50.0, 60.0, 100.0)
    skew_probs = (0.0, 0.10, 0.50, 0.90, 1.0)
    shist = Histogram(edges=skew_edges, probs=skew_probs)
    sSkew = TableStats(row_count=1000, columns={
        "lon": ColumnStats(low=0.0, high=100.0, ndv=1000.0, nulls_fraction=0.0, hist=shist),
    })
    band_skew = estimate(Predicate(kind="band", family="B", col="lon", other="lon", delta=25.0), sSkew)
    assert band_skew > band_uniform, (band_skew, band_uniform)
    results.append(("band skewed > band uniform (clustering)", band_skew, band_uniform, True))

    sWin = TableStats(row_count=1000, columns={
        "ts": ColumnStats(low=0.0, high=100.0, ndv=1000.0, nulls_fraction=0.0, hist=uhist),
    })
    check("window uniform w=0.25s -> ~0.4375",
          estimate(Predicate(kind="window", family="B", col="ts", other="ts", w=25.0), sWin), 0.4375, tol=0.01)

    sDel = TableStats(row_count=1000, columns={
        "id": ColumnStats(low=0.0, high=20.0, ndv=21.0, nulls_fraction=0.0),
    })
    pred_del = Predicate(kind="lt", family="B", col="id", other="id")
    got_del = estimate(pred_del, sDel)
    want_del = mode_b_analytic.estimate(pred_del, sDel, profile="eimer")
    check("delegate B '<' a.id<b.id == analytic eimer", got_del, want_del)

    sDelEq = TableStats(row_count=1000, columns={
        "id": ColumnStats(low=0.0, high=100.0, ndv=10.0, nulls_fraction=0.0),
    })
    pred_eq = Predicate(kind="eq", family="A", col="id", literal=50.0)
    got_eq = estimate(pred_eq, sDelEq)
    want_eq = mode_b_analytic.estimate(pred_eq, sDelEq, profile="eimer")
    check("delegate A '=' no-freq == analytic eimer (0.1)", got_eq, want_eq)
    check("delegate A '=' no-freq value == 0.1", got_eq, 0.1)

    sDiff = TableStats(row_count=1000, columns={
        "a": ColumnStats(low=0.0, high=100.0, ndv=10.0, nulls_fraction=0.0,
                         freq=Frequencies(top=(("X", 1.0),), residual_fraction=0.0, residual_ndv=0.0)),
        "b": ColumnStats(low=0.0, high=100.0, ndv=20.0, nulls_fraction=0.0,
                         freq=Frequencies(top=(("X", 1.0),), residual_fraction=0.0, residual_ndv=0.0)),
    })
    pred_diff = Predicate(kind="eq", family="B", col="a", other="b")
    check("B '=' different cols, both sketched constant X -> 1.0", estimate(pred_diff, sDiff), 1.0)
    sDiffNo = TableStats(row_count=1000, columns={
        "a": ColumnStats(low=0.0, high=100.0, ndv=10.0, nulls_fraction=0.0,
                         freq=Frequencies(top=(("X", 1.0),), residual_fraction=0.0, residual_ndv=0.0)),
        "b": ColumnStats(low=0.0, high=100.0, ndv=20.0, nulls_fraction=0.0),
    })
    got_diff = estimate(pred_diff, sDiffNo)
    want_diff = mode_b_analytic.estimate(pred_diff, sDiffNo, profile="eimer")
    check("delegate B '=' different cols (one sketch missing) == analytic eimer", got_diff, want_diff)

    fBool = Frequencies(top=(("1", 0.3), ("0", 0.7)), residual_fraction=0.0, residual_ndv=0.0)
    sBool = TableStats(row_count=100, columns={"arrest": ColumnStats(low=0.0, high=1.0, ndv=2.0, nulls_fraction=0.0, freq=fBool)})
    check("A '=' boolean literal vs numeric sketch", estimate(Predicate(kind="eq", family="A", col="arrest", literal=True), sBool), 0.3)
    check("A '=' float literal vs integer sketch", estimate(Predicate(kind="eq", family="A", col="arrest", literal=1.0), sBool), 0.3)
    check("A '<>' boolean literal vs numeric sketch", estimate(Predicate(kind="neq", family="A", col="arrest", literal=False), sBool), 0.3)
    fBool2 = Frequencies(top=(("False", 0.7), ("True", 0.3)), residual_fraction=0.0, residual_ndv=0.0)
    sBool2 = TableStats(row_count=100, columns={"arrest": ColumnStats(low=0.0, high=1.0, ndv=2.0, nulls_fraction=0.0, freq=fBool2)})
    check("A '=' boolean literal vs boolean sketch", estimate(Predicate(kind="eq", family="A", col="arrest", literal=True), sBool2), 0.3)

    fL = Frequencies(top=(("x", 0.5), ("y", 0.3)), residual_fraction=0.2, residual_ndv=2.0)
    fR = Frequencies(top=(("y", 0.6), ("z", 0.2)), residual_fraction=0.2, residual_ndv=4.0)
    sCross = TableStats(row_count=100, columns={
        "l": ColumnStats(low=0.0, high=10.0, ndv=4.0, nulls_fraction=0.0, freq=fL),
        "r": ColumnStats(low=0.0, high=10.0, ndv=6.0, nulls_fraction=0.0, freq=fR)})
    check("B '=' cross columns, both sketched", estimate(Predicate(kind="eq", family="B", col="l", other="r"), sCross), 0.235, tol=1e-12)
    check("B '=' cross columns symmetric", estimate(Predicate(kind="eq", family="B", col="r", other="l"), sCross), 0.235, tol=1e-12)
    check("offset_band open side P(B-A > 25) on U[0,100]",
          estimate(Predicate(kind="offset_band", family="B", col="lon", other="lon", lo_offset=25.0, hi_offset=float("inf")), sBand), 0.28125, tol=0.02)

    sNoHist = TableStats(row_count=1000, columns={
        "lon": ColumnStats(low=0.0, high=100.0, ndv=100.0, nulls_fraction=0.0),
    })
    pred_lt = Predicate(kind="lt", family="A", col="lon", literal=25.0)
    got_lt = estimate(pred_lt, sNoHist)
    want_lt = mode_b_analytic.estimate(pred_lt, sNoHist, profile="eimer")
    check("delegate A '<' no-hist == analytic eimer", got_lt, want_lt)

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
