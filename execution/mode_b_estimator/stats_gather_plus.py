"""Enriched per-column stats gatherer for the mode_b_v3 selectivity estimator.

,,stats_gather.gather'' PLUS two cheap single-pass O(N) marginal sketches that
mode_b_v3 consumes to fix two weak spots of the stats-only mode_b_v2 baseline:

  (i)  skewed categorical equality: a top-K MCV frequency stat
       (,,contract.Frequencies'') recovers the true per-value mass and the exact
       collision ,,Sum f_i^2'', vs the baseline's uniform ,,1/ndv''.
  (ii) range/band on clustered numeric columns: an equi-depth quantile histogram
       (,,contract.Histogram'', via ,,approx_percentile'') recovers the empirical
       CDF, vs the baseline's uniform ,,(c-lo)/span''.

Both are one ,,GROUP BY'' / one ,,approx_percentile'' call, the same single-pass
O(N) cost class as the base stats (NOT the O(N^2) pairwise oracle). On
independent row pairs each such selectivity is a functional of the marginal
alone (e.g. ,,P(A==B) = Sum_v f_v^2''), so a richer marginal suffices with no
joint stat.

Timestamp cast contract: the histogram domain must equal the predicate domain.
,,ts''/,,time'' are ,,TIMESTAMP(6)''; ,,stats_gather'' maps their min/max through
,,to_unixtime'' (seconds) and window predicates carry seconds, so
,,build_hist_sql'' wraps these columns in ,,to_unixtime'' too and the histogram
edges land in the same seconds domain.
"""
from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

from . import stats_gather
from .contract import ColumnStats, Frequencies, Histogram, TableStats

# --------------------------------------------------------------------------- #
# Public column policy: which columns get which cheap extension.              #
# --------------------------------------------------------------------------- #
# Categorical (low-NDV string) columns get a top-K MCV frequency stat.
CATEGORICAL_COLS: Tuple[str, ...] = ("etype", "primary_type")
# Numeric (and timestamp) columns get an equi-depth quantile histogram.
# ts/time are projected through to_unixtime so the histogram domain == seconds.
NUMERIC_COLS: Tuple[str, ...] = ("id", "lon", "lat", "ts", "time")

# Columns whose stored type is TIMESTAMP(6); reuse stats_gather's policy so the
# numeric cast (to_unixtime -> seconds double) is identical on both code paths.
_TIMESTAMP_COLUMNS = stats_gather._TIMESTAMP_COLUMNS  # frozenset({"time", "ts"})

# Result-column aliases for the enrichment queries.
FREQ_VALUE_ALIAS = "v"
FREQ_COUNT_ALIAS = "c"
FREQ_TOTAL_ALIAS = "n"
HIST_ALIAS = "pcts"


# --------------------------------------------------------------------------- #
# SQL builders                                                                 #
# --------------------------------------------------------------------------- #
def build_freq_sql(col: str, table: str, *, k: int = 16) -> str:
    """Top-K most-common-value query for a categorical column.

    Emits the top-K values by descending count plus the grand total ,,count(*)''
    so the parser can turn counts into fractions and size the residual tail:

        SELECT <col> AS v, count(*) AS c, (SELECT count(*) FROM t) AS n
        FROM t
        GROUP BY <col>
        ORDER BY c DESC
        LIMIT <k>

    NULLs form their own GROUP BY bucket; the parser drops the NULL value bucket
    (never an equality target) but its mass stays in the grand total, so it
    folds into the residual.
    """
    if k <= 0:
        raise ValueError(f"build_freq_sql requires k >= 1, got {k}")
    return (
        f"SELECT {col} AS {FREQ_VALUE_ALIAS}, count(*) AS {FREQ_COUNT_ALIAS}, "
        f"(SELECT count(*) FROM {table}) AS {FREQ_TOTAL_ALIAS}\n"
        f"FROM {table}\n"
        f"GROUP BY {col}\n"
        f"ORDER BY {FREQ_COUNT_ALIAS} DESC\n"
        f"LIMIT {k}"
    )


def _percentile_points(bins: int) -> List[float]:
    """The B+1 cumulative probabilities 0, 1/B, ..., 1 for an equi-depth sketch."""
    if bins <= 0:
        raise ValueError(f"bins must be >= 1, got {bins}")
    pts = [i / bins for i in range(bins + 1)]
    pts[-1] = 1.0  # guard against float drift on the top edge
    return pts


def build_hist_sql(col: str, table: str, *, bins: int = 32, timestamp_columns=_TIMESTAMP_COLUMNS) -> str:
    """Equi-depth quantile-sketch query for a numeric (or timestamp) column.

        SELECT approx_percentile(<numeric col>, ARRAY[0.0, 1/B, ..., 1.0]) AS pcts
        FROM t

    Returns one array of boundary values at those cumulative probs, single-pass
    O(N). Timestamp columns are wrapped in ,,to_unixtime'' so the edges land in
    the seconds domain (same as base low/high and the window literal).
    """
    numeric = stats_gather._numeric_expr(col, timestamp_columns)  # to_unixtime(col) for timestamps
    pts = _percentile_points(bins)
    arr = ", ".join(repr(p) for p in pts)
    # CAST to double: approx_percentile rejects decimal, so a DECIMAL column would fail
    # without this; harmless for the already-double cases (incl. to_unixtime).
    return (
        f"SELECT approx_percentile(CAST({numeric} AS double), ARRAY[{arr}]) AS {HIST_ALIAS}\n"
        f"FROM {table}"
    )


# --------------------------------------------------------------------------- #
# Parsers (pure: rows -> contract stat objects)                                #
# --------------------------------------------------------------------------- #
def _to_float(x) -> float:
    return float(x)


def parse_frequencies(rows: Sequence[Sequence[object]], base_ndv: float) -> Frequencies:
    """Turn the top-K GROUP BY result into a ,,contract.Frequencies''.

    Each row is ,,(value, count, grand_total)''. Reads N from the first row, drops
    the NULL-value bucket (equality never matches NULL) while keeping its mass in
    N, and builds ,,top = [(str(value), count/N), ...]'' in descending order. The
    residual is the tail: residual_fraction = max(0, 1 - sum top), spread over
    residual_ndv = max(0, base_ndv - len(top)) distinct non-top values.
    """
    if not rows:
        return Frequencies(top=(), residual_fraction=0.0, residual_ndv=max(0.0, base_ndv))

    total = _to_float(rows[0][2])
    if total <= 0.0:
        return Frequencies(top=(), residual_fraction=0.0, residual_ndv=max(0.0, base_ndv))

    top: List[Tuple[str, float]] = []
    for value, count, _n in rows:
        if value is None:
            # NULL bucket: not an equality target; its mass stays in ,,total'' and
            # folds into residual_fraction below.
            continue
        top.append((str(value), _to_float(count) / total))

    sum_top = sum(f for _, f in top)
    residual_fraction = max(0.0, 1.0 - sum_top)
    residual_ndv = max(0.0, base_ndv - len(top))
    return Frequencies(
        top=tuple(top),
        residual_fraction=residual_fraction,
        residual_ndv=residual_ndv,
    )


def _monotonize(
    raw_edges: Sequence[float], raw_probs: Sequence[float]
) -> Tuple[Tuple[float, ...], Tuple[float, ...]]:
    """Collapse duplicate edges so ,,edges'' is STRICTLY increasing.

    ,,approx_percentile'' returns equal values at consecutive probability points on
    a spike (many rows share one value); ,,Histogram.cdf''/,,bins'' need strictly
    increasing edges to interpolate without divide-by-zero. Merge each run of equal
    edges into one boundary, keeping the LAST (largest) cumulative prob so the
    spike's full mass is attributed to ,,<= value''. First edge keeps prob 0.0,
    last keeps 1.0.
    """
    edges: List[float] = []
    probs: List[float] = []
    for e, p in zip(raw_edges, raw_probs):
        ef, pf = float(e), float(p)
        if edges and ef == edges[-1]:
            probs[-1] = pf  # duplicate boundary: keep the LAST (highest) cumulative prob
            continue
        edges.append(ef)
        probs.append(pf)
    return tuple(edges), tuple(probs)


def parse_histogram(percentiles: Sequence[float], bins: int) -> Histogram:
    """Turn an ,,approx_percentile'' array into a monotone ,,contract.Histogram''.

    ,,percentiles'' are the boundary values at cumulative probs 0, 1/B, ..., 1;
    pair them with those probs and monotonize. approx_percentile is already sorted,
    so after collapsing duplicate edges ,,edges'' is strictly increasing and
    ,,cdf''/,,bins'' interpolate cleanly.
    """
    probs = _percentile_points(bins)
    if len(percentiles) != len(probs):
        raise ValueError(
            f"approx_percentile returned {len(percentiles)} values, expected {len(probs)}"
        )
    edges, mono_probs = _monotonize([float(v) for v in percentiles], probs)
    return Histogram(edges=edges, probs=mono_probs)


# --------------------------------------------------------------------------- #
# Driver                                                                        #
# --------------------------------------------------------------------------- #
def _first_row(rows: Sequence[Sequence[object]], what: str) -> Sequence[object]:
    if not rows:
        raise ValueError(f"{what} query returned no rows")
    return rows[0]


def gather_plus(
    columns: Sequence[str],
    client,
    table: str,
    *,
    categorical: Sequence[str] = CATEGORICAL_COLS,
    numeric: Sequence[str] = NUMERIC_COLS,
    timestamp_columns=_TIMESTAMP_COLUMNS,
    k: int = 16,
    bins: int = 32,
) -> TableStats:
    """Gather the base 4 stats PLUS freq (categorical) / hist (numeric) extensions.

    Runs ,,stats_gather.gather'' for the base low/high/ndv/nf + row_count, then
    adds a ,,Frequencies'' (top-K MCV, sized against base ndv) for each present
    categorical column and a monotone ,,Histogram'' (equi-depth) for each present
    numeric column. Returns a new ,,TableStats'' carrying ,,freq''/,,hist'' where
    gathered, other fields copied from base.

    ,,client'' must expose ,,execute(sql) -> (column_names, rows)'' like
    execution/trino_client.TrinoClient.
    """
    ordered = list(dict.fromkeys(columns))  # de-dup, preserve order
    base = stats_gather.gather(ordered, client, table, timestamp_columns=timestamp_columns)

    present = set(ordered)
    cat_targets = [c for c in categorical if c in present]
    num_targets = [c for c in numeric if c in present]

    freqs: Dict[str, Frequencies] = {}
    for col in cat_targets:
        _cols, rows = client.execute(build_freq_sql(col, table, k=k))
        freqs[col] = parse_frequencies(rows, base.col(col).ndv)

    hists: Dict[str, Histogram] = {}
    for col in num_targets:
        _cols, rows = client.execute(build_hist_sql(col, table, bins=bins, timestamp_columns=timestamp_columns))
        row = _first_row(rows, f"histogram({col})")
        percentiles = row[0]  # the ARRAY value (single projected column)
        hists[col] = parse_histogram(percentiles, bins)

    enriched: Dict[str, ColumnStats] = {}
    for col, cs in base.columns.items():
        enriched[col] = ColumnStats(
            low=cs.low,
            high=cs.high,
            ndv=cs.ndv,
            nulls_fraction=cs.nulls_fraction,
            freq=freqs.get(col),
            hist=hists.get(col),
        )
    return TableStats(row_count=base.row_count, columns=enriched)


# --------------------------------------------------------------------------- #
# Offline self-test (NO Trino network access).                                 #
# --------------------------------------------------------------------------- #
def _self_test() -> None:
    # ---- (1) SQL SHAPE -----------------------------------------------------
    # Categorical freq SQL.
    fsql = build_freq_sql("etype", "generated_events", k=16)
    assert "GROUP BY etype" in fsql, fsql
    assert "ORDER BY c DESC" in fsql, fsql
    assert "LIMIT 16" in fsql, fsql
    assert "count(*) AS c" in fsql, fsql
    assert "(SELECT count(*) FROM generated_events) AS n" in fsql, fsql

    # Numeric histogram SQL (non-timestamp).
    hsql = build_hist_sql("lon", "generated_events", bins=32)
    assert "approx_percentile(CAST(lon AS double), ARRAY[" in hsql, hsql
    assert hsql.count(",") + 1 >= 33  # at least B+1 = 33 array entries
    assert "0.0" in hsql and "1.0" in hsql, hsql

    # Timestamp histogram SQL MUST cast through to_unixtime (seconds domain).
    tsql = build_hist_sql("ts", "generated_events", bins=8)
    assert "approx_percentile(CAST(to_unixtime(ts) AS double), ARRAY[" in tsql, tsql

    # ---- (2) PARSING via a FAKE client (canned rows, no network) -----------
    # Categorical: etype freqs {Z:0.7, R:0.1, B:0.1, M:0.1}, N=1000, ndv=4.
    # -> top fractions 0.7,0.1,0.1,0.1; residual_fraction=0; residual_ndv=0.
    # collision = 0.7^2 + 3 * 0.1^2 = 0.49 + 0.03 = 0.52.
    N = 1000
    etype_rows = [
        ("Z", 700, N),
        ("R", 100, N),
        ("B", 100, N),
        ("M", 100, N),
    ]
    freq = parse_frequencies(etype_rows, base_ndv=4.0)
    assert freq.top == (("Z", 0.7), ("R", 0.1), ("B", 0.1), ("M", 0.1)), freq.top
    assert abs(freq.residual_fraction - 0.0) < 1e-12, freq.residual_fraction
    assert abs(freq.residual_ndv - 0.0) < 1e-12, freq.residual_ndv
    coll = freq.collision()
    assert abs(coll - 0.52) < 1e-12, coll
    # freq_of recovers true per-value mass (vs uniform 1/ndv = 0.25).
    assert abs(freq.freq_of("Z") - 0.7) < 1e-12
    assert abs(freq.freq_of("absent_value") - 0.0) < 1e-12  # residual_ndv=0 -> 0

    # Categorical with a residual tail: top {A:0.5, B:0.2}, N=1000, ndv=12.
    # residual_fraction = 1 - 0.7 = 0.3 over residual_ndv = 12 - 2 = 10 buckets.
    # collision = 0.5^2 + 0.2^2 + 0.3^2/10 = 0.25 + 0.04 + 0.009 = 0.299.
    tail_rows = [("A", 500, N), ("B", 200, N)]
    ftail = parse_frequencies(tail_rows, base_ndv=12.0)
    assert abs(ftail.residual_fraction - 0.3) < 1e-12, ftail.residual_fraction
    assert abs(ftail.residual_ndv - 10.0) < 1e-12, ftail.residual_ndv
    assert abs(ftail.freq_of("absent") - 0.03) < 1e-12, ftail.freq_of("absent")
    assert abs(ftail.collision() - 0.299) < 1e-12, ftail.collision()

    # NULL bucket: dropped from ,,top'', mass retained in residual via grand total.
    null_rows = [("A", 600, N), (None, 200, N), ("B", 200, N)]
    fnull = parse_frequencies(null_rows, base_ndv=5.0)
    assert fnull.top == (("A", 0.6), ("B", 0.2)), fnull.top
    # residual_fraction = 1 - 0.8 = 0.2 (includes the 0.2 NULL mass).
    assert abs(fnull.residual_fraction - 0.2) < 1e-12, fnull.residual_fraction

    # Numeric: lon percentiles linear 0..1 at probs 0,1/32,...,1 -> uniform CDF.
    bins = 32
    lon_pcts = [i / bins for i in range(bins + 1)]  # values == probs (linear)
    hist = parse_histogram(lon_pcts, bins)
    assert hist.edges[0] == 0.0 and hist.edges[-1] == 1.0
    assert len(hist.edges) == bins + 1  # strictly increasing, none collapsed
    # On a linear marginal the CDF is the identity on [0,1].
    assert abs(hist.cdf(0.5) - 0.5) < 1e-12, hist.cdf(0.5)
    assert abs(hist.cdf(0.25) - 0.25) < 1e-12, hist.cdf(0.25)
    assert hist.cdf(-1.0) == 0.0 and hist.cdf(2.0) == 1.0
    # mass in [0.25, 0.75] == 0.5 (uniform).
    band = hist.cdf(0.75) - hist.cdf(0.25)
    assert abs(band - 0.5) < 1e-12, band

    # Numeric with a SPIKE: bottom half all 0.0 (duplicate edges); monotonize must
    # collapse them so cdf is well-defined. B=4, values 0,0,0,5,10 -> edges
    # (0,5,10), probs (0.5,0.75,1.0) (the merged 0-edge keeps the LAST run prob).
    spike_pcts = [0.0, 0.0, 0.0, 5.0, 10.0]
    hspike = parse_histogram(spike_pcts, 4)
    assert hspike.edges == (0.0, 5.0, 10.0), hspike.edges
    assert hspike.probs == (0.5, 0.75, 1.0), hspike.probs
    # CDF at the spike value 0.0: x <= edges[0] -> 0.0 (mass below the spike).
    assert hspike.cdf(0.0) == 0.0
    # Just above the spike, halfway to 5.0: 0.5 + (0.75-0.5)*0.5 = 0.625.
    assert abs(hspike.cdf(2.5) - 0.625) < 1e-12, hspike.cdf(2.5)
    # Mass attributed to the spike region [0,5] is 0.25 (0.75 - 0.5).
    binmass = list(hspike.bins())
    assert abs(binmass[0][2] - 0.25) < 1e-12, binmass

    # ---- (3) gather_plus wiring via a fake client (routes by SQL) ----------
    base_cols = ["row_count"]
    test_columns = ["id", "lon", "etype"]
    for c in test_columns:
        base_cols += [
            stats_gather._safe_alias("min", c),
            stats_gather._safe_alias("max", c),
            stats_gather._safe_alias("ndv", c),
            stats_gather._safe_alias("nf", c),
        ]
    base_row = [
        N,
        1.0, 1000.0, 1000.0, 0.0,         # id
        0.0, 1.0, 500.0, 0.0,             # lon (range [0,1])
        float("nan"), float("nan"), 4.0, 0.0,  # etype (string -> NaN min/max, ndv=4)
    ]

    class _FakeClient:
        def execute(self, sql):
            if sql.startswith("SELECT\n  count(*) AS row_count"):
                return base_cols, [base_row]
            if "GROUP BY etype" in sql:
                return ["v", "c", "n"], etype_rows
            if "approx_percentile" in sql and "id" in sql:
                # id histogram: linear 1..1000 at the 33 probs.
                vals = [1.0 + (1000.0 - 1.0) * (i / 32) for i in range(33)]
                return ["pcts"], [[vals]]
            if "approx_percentile" in sql and "lon" in sql:
                return ["pcts"], [[lon_pcts]]
            raise AssertionError(f"unexpected SQL routed: {sql!r}")

    stats = gather_plus(test_columns, _FakeClient(), "generated_events",
                        categorical=("etype",), numeric=("id", "lon"), k=16, bins=32)
    assert stats.row_count == N
    # etype carries a Frequencies, no Histogram.
    et = stats.col("etype")
    assert et.freq is not None and et.hist is None
    assert abs(et.freq.collision() - 0.52) < 1e-12, et.freq.collision()
    # lon carries a Histogram, no Frequencies.
    lonc = stats.col("lon")
    assert lonc.hist is not None and lonc.freq is None
    assert abs(lonc.hist.cdf(0.5) - 0.5) < 1e-12
    # id carries a Histogram spanning [1,1000].
    idc = stats.col("id")
    assert idc.hist is not None
    assert idc.hist.edges[0] == 1.0 and idc.hist.edges[-1] == 1000.0
    # base fields preserved from stats_gather.gather.
    assert idc.ndv == 1000.0 and lonc.low == 0.0 and lonc.high == 1.0

    print("stats_gather_plus self-test: ALL PASS")
    print(f"  etype Frequencies.collision()      = {et.freq.collision():.6f}  (skew-aware; uniform 1/ndv would be {1.0/4.0:.6f})")
    print(f"  tail  Frequencies.collision()      = {ftail.collision():.6f}")
    print(f"  lon   Histogram.cdf(0.5)           = {lonc.hist.cdf(0.5):.6f}")
    print(f"  lon   band mass [0.25,0.75]        = {band:.6f}")
    print(f"  spike Histogram edges/probs        = {hspike.edges} / {hspike.probs}")
    print(f"  spike Histogram.cdf(2.5)           = {hspike.cdf(2.5):.6f}")
    print("---- build_freq_sql('etype','generated_events',k=16) ----")
    print(fsql)
    print("---- build_hist_sql('ts','generated_events',bins=8) ----")
    print(tsql)


if __name__ == "__main__":
    _self_test()
