"""Unified mode_b selectivity estimator: the production entry point for the stats-only
estimators, used by the selectivity producer (,,compute_selectivities.py'', ,,b_unified'' mode).

It wraps:
  * ,,mode_b_v2'': the analytic Trino-CBO port (profiles 'trino' and 'eimer'). Needs only
    {low, high, ndv, nf}.
  * ,,mode_b_v3'': the enriched estimator (profile 'eimer_plus') that uses an equi-depth
    histogram / top-K frequency sketch where present and delegates to v2 'eimer' otherwise.
plus the ,,contract.Predicate'' / ,,ColumnStats'' / ,,TableStats'' types and the stats gatherers.

Profiles (passed to ,,estimate''):
  'eimer'      -> v2 analytic (always available; coarse stats only).
  'eimer_plus' -> v3 enriched (falls back to v2 'eimer' per-operator when a column lacks
                  the enriched stat).
  'trino'      -> v2 faithful-Trino port (for comparison; not a EIMER default).
  'auto'       -> 'eimer_plus' iff any gathered column carries a freq/hist sketch, else 'eimer'.

This package deliberately does NOT import ,,compute_selectivities'', so the producer can
import it without an import cycle; the text->Predicate adapter lives in the producer.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Keep the repo root importable regardless of how this package is first imported.
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from . import mode_b_v2, mode_b_v3, stats_gather, stats_gather_plus
from .contract import (  # re-exported: the stable type surface
    ColumnStats,
    Frequencies,
    Histogram,
    Predicate,
    TableStats,
)

__all__ = [
    "ColumnStats", "Frequencies", "Histogram", "Predicate", "TableStats",
    "PROFILES", "estimate", "gather_stats", "resolve_profile", "classify_column_types",
]

PROFILES = ("trino", "eimer", "eimer_plus", "auto")


def resolve_profile(profile: str, stats: TableStats) -> str:
    """Resolve 'auto' to 'eimer_plus' iff any column carries an enriched sketch, else 'eimer'.

    Non-'auto' profiles pass through unchanged (validated by ,,estimate'').
    """
    if profile != "auto":
        return profile
    for c in stats.columns.values():
        if c.freq is not None or c.hist is not None:
            return "eimer_plus"
    return "eimer"


def estimate(pred: Predicate, stats: TableStats, *, profile: str = "auto") -> float:
    """Unified [0,1]-clamped selectivity for one predicate.

    Routes to the enriched estimator (v3) for 'eimer_plus', else the analytic estimator
    (v2) for 'eimer'/'trino'. 'auto' picks 'eimer_plus' when enriched stats are present.
    NaN is preserved (the producer treats it as 'unknown').
    """
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; expected one of {PROFILES}")
    resolved = resolve_profile(profile, stats)
    if resolved == "eimer_plus":
        return mode_b_v3.estimate(pred, stats, profile="eimer_plus")
    return mode_b_v2.estimate(pred, stats, profile=resolved)


# Trino type prefixes -> column class (lower-cased SHOW COLUMNS 'Type' string).
_TIMESTAMP_TYPE_PREFIXES = ("timestamp",)           # to_unixtime cast (seconds domain)
_STRING_TYPE_PREFIXES = ("varchar", "char")          # categorical -> frequency sketch
_NUMERIC_TYPE_PREFIXES = ("bigint", "integer", "smallint", "tinyint", "double", "real", "decimal")


def classify_column_types(client, table: str, columns=None):
    """Data-driven column classification via ,,SHOW COLUMNS FROM <table>''.

    Returns (string_cols, numeric_cols, timestamp_cols) as sets. Restricted to ,,columns''
    when given, else covers every column (used to discover the table's timestamp columns
    for schema-agnostic ordering detection). Works on any schema; unrecognized types fall
    into none of the sets (base stats only). ,,client.execute(sql) -> (col_names, rows)''.
    """
    _cols, rows = client.execute(f"SHOW COLUMNS FROM {table}")
    types = {str(r[0]): str(r[1]).strip().lower() for r in rows}
    want = set(types) if columns is None else set(columns)
    string_cols, numeric_cols, timestamp_cols = set(), set(), set()
    for col in want:
        t = types.get(col, "")
        if t.startswith(_TIMESTAMP_TYPE_PREFIXES):
            timestamp_cols.add(col)
        elif t.startswith(_STRING_TYPE_PREFIXES):
            string_cols.add(col)
        elif t.startswith(_NUMERIC_TYPE_PREFIXES):
            numeric_cols.add(col)
    return string_cols, numeric_cols, timestamp_cols


def gather_stats(columns, client, table: str, *, enriched: bool = False,
                 eq_columns=(), hist_columns=()) -> TableStats:
    """Gather the per-column summary stats the estimator consumes, as a ,,TableStats''.

    enriched=False: coarse {low, high, ndv, nulls_fraction} via one aggregate SELECT.

    enriched=True: additionally gather the top-K frequency sketch for string equality
    columns and the equi-depth histogram for numeric/timestamp range columns, so the
    'eimer_plus' estimator uses the skew-aware (collision) and clustered (convolution)
    forms. Column classes are derived from Trino types (,,classify_column_types''), so this
    works on any schema. ,,eq_columns''/,,hist_columns'' are the predicate roles; the type
    intersection keeps freq to categoricals and hist to numerics.
    ,,client.execute(sql) -> (col_names, rows)''.
    """
    columns = list(dict.fromkeys(columns))
    if not enriched:
        return stats_gather.gather(columns, client, table)
    string_cols, numeric_cols, timestamp_cols = classify_column_types(client, table, columns)
    eq_set, hist_set = set(eq_columns), set(hist_columns)
    freq = [c for c in columns if c in eq_set and c in string_cols]
    hist = [c for c in columns if c in hist_set and (c in numeric_cols or c in timestamp_cols)]
    return stats_gather_plus.gather_plus(
        columns, client, table,
        categorical=freq, numeric=hist, timestamp_columns=frozenset(timestamp_cols),
        k=16, bins=32,
    )


# --------------------------------------------------------------------------- #
# Self-test (no Trino access)
# --------------------------------------------------------------------------- #
def _selftest() -> None:
    from math import isnan

    def approx(a, b, tol=1e-12):
        return (isnan(a) and isnan(b)) or abs(a - b) <= tol

    # Coarse stats only -> 'auto' must resolve to 'eimer' and match v2 'eimer' exactly.
    coarse = TableStats(row_count=1000, columns={
        "id": ColumnStats(0.0, 8000.0, 8000.0, 0.0),
        "etype": ColumnStats(float("nan"), float("nan"), 4.0, 0.0),
    })
    assert resolve_profile("auto", coarse) == "eimer", "auto must be eimer on coarse stats"

    # Family-A eq routes and matches v2 directly.
    pa = Predicate(kind="eq", family="A", col="etype", literal="R")
    assert approx(estimate(pa, coarse, profile="eimer"),
                  mode_b_v2.estimate(pa, coarse, profile="eimer"))
    assert approx(estimate(pa, coarse, profile="auto"),
                  mode_b_v2.estimate(pa, coarse, profile="eimer")), "auto==eimer on coarse"

    # Family-B offset band routes to the analytic on coarse stats.
    pb = Predicate(kind="offset_band", family="B", col="id", other="id",
                   lo_offset=0.0, hi_offset=500.0)
    got = estimate(pb, coarse, profile="eimer")
    want = mode_b_v2.estimate(pb, coarse, profile="eimer")
    assert approx(got, want) and not isnan(got), (got, want)

    # trino profile is selectable and is 'unknown' (NaN) for the offset band.
    assert isnan(estimate(pb, coarse, profile="trino"))

    # With an enriched (hist) column present, 'auto' resolves to 'eimer_plus' and routes
    # to v3 (which here matches the analytic on a uniform histogram).
    uhist = Histogram(edges=tuple(i * 80.0 for i in range(101)),
                      probs=tuple(i / 100 for i in range(101)))
    enriched = TableStats(row_count=1000, columns={
        "id": ColumnStats(0.0, 8000.0, 8000.0, 0.0, hist=uhist),
    })
    assert resolve_profile("auto", enriched) == "eimer_plus"
    pbe = Predicate(kind="offset_band", family="B", col="id", other="id",
                    lo_offset=0.0, hi_offset=2000.0)
    got_e = estimate(pbe, enriched, profile="auto")
    want_e = mode_b_v3.estimate(pbe, enriched, profile="eimer_plus")
    assert approx(got_e, want_e), (got_e, want_e)

    # gather_stats parses a single mocked aggregate row into a TableStats (no network).
    cols = ["row_count",
            stats_gather._safe_alias("min", "id"), stats_gather._safe_alias("max", "id"),
            stats_gather._safe_alias("ndv", "id"), stats_gather._safe_alias("nf", "id")]
    row = [1000, 1.0, 8000.0, 8000.0, 0.0]

    class _FakeClient:
        def execute(self, _sql):
            return cols, [row]

    ts = gather_stats(["id"], _FakeClient(), "events")
    assert ts.row_count == 1000 and ts.col("id").low == 1.0 and ts.col("id").high == 8000.0

    # Enriched gather: data-driven type classification routes freq to the string equality
    # column and hist to the numeric range column; the key gets neither.
    base_cols = ["row_count"]
    base_vals = [1000]
    _per_col = {"etype": (None, None, 4.0, 0.0), "id": (1.0, 1000.0, 1000.0, 0.0),
                "lon": (-87.9, -87.7, 500.0, 0.0)}
    for c in ("etype", "id", "lon"):
        base_cols += [stats_gather._safe_alias(p, c) for p in ("min", "max", "ndv", "nf")]
        base_vals += list(_per_col[c])
    lon_pcts = [-87.9 + 0.2 * (i / 32) for i in range(33)]

    class _EnrichedFake:
        def execute(self, sql):
            if sql.startswith("SHOW COLUMNS"):
                return ["Column", "Type", "Extra", "Comment"], [
                    ["etype", "varchar", "", ""], ["id", "bigint", "", ""],
                    ["lon", "double", "", ""]]
            if sql.startswith("SELECT\n  count(*) AS row_count"):
                return base_cols, [base_vals]
            if "GROUP BY etype" in sql:
                return ["v", "c", "n"], [("Z", 700, 1000), ("R", 100, 1000),
                                         ("B", 100, 1000), ("M", 100, 1000)]
            if "approx_percentile" in sql and "lon" in sql:
                return ["pcts"], [[lon_pcts]]
            raise AssertionError(f"unexpected SQL: {sql!r}")

    es = gather_stats(["etype", "id", "lon"], _EnrichedFake(), "events",
                      enriched=True, eq_columns={"etype"}, hist_columns={"lon"})
    assert es.col("etype").freq is not None and es.col("etype").hist is None, "etype -> freq only"
    assert es.col("lon").hist is not None and es.col("lon").freq is None, "lon -> hist only"
    assert es.col("id").freq is None and es.col("id").hist is None, "id (no role) -> base only"
    assert abs(es.col("etype").freq.collision() - 0.52) < 1e-9, es.col("etype").freq.collision()
    assert resolve_profile("auto", es) == "eimer_plus", "enriched stats -> auto picks eimer_plus"
    # Skew: family-A eq on the heavy value Z must use the frequency (0.70), not 1/ndv (0.25).
    pz = Predicate(kind="eq", family="A", col="etype", literal="Z")
    assert abs(estimate(pz, es, profile="auto") - 0.70) < 1e-9, estimate(pz, es, profile="auto")

    # Bad profile is rejected.
    try:
        estimate(pa, coarse, profile="nonsense")
        raise AssertionError("expected ValueError on bad profile")
    except ValueError:
        pass

    print("mode_b_estimator self-test: ALL PASS")
    print(f"  profiles={PROFILES}; auto(coarse)=eimer, auto(enriched)=eimer_plus")


if __name__ == "__main__":
    _selftest()
