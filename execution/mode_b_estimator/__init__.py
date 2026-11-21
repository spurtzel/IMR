"""Unified mode_b selectivity estimator: the entry point for the stats-only
estimators, used by the selectivity producer (`compute_selectivities.py`, `b_unified` mode).

It wraps:
  * `mode_b_analytic`: the analytic Trino-CBO port (profiles 'trino' and 'eimer'). Needs only
    {low, high, ndv, nf}.
  * `mode_b_enriched`: the enriched estimator (profile 'eimer_plus') that uses an equi-depth
    histogram / top-K frequency sketch where present and delegates to the analytic 'eimer' profile otherwise.
plus the `contract.Predicate` / `ColumnStats` / `TableStats` types and the stats gatherers.

Profiles (passed to `estimate`):
  'eimer'      -> analytic (coarse stats only).
  'eimer_plus' -> enriched (falls back to the analytic 'eimer' per operator when a column lacks
                  the enriched stat).
  'trino'      -> analytic, Trino port.
  'auto'       -> 'eimer_plus' iff any gathered column carries a freq/hist sketch, else 'eimer'.

The text->Predicate adapter lives in `compute_selectivities.py`.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from . import mode_b_analytic, mode_b_enriched, stats_gather, stats_gather_plus
from .contract import (
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
    """Resolves 'auto' to 'eimer_plus' iff any column carries an enriched sketch, else 'eimer'.

        Non-'auto' profiles pass through unchanged.
    """
    if profile != "auto":
        return profile
    for c in stats.columns.values():
        if c.freq is not None or c.hist is not None:
            return "eimer_plus"
    return "eimer"


def estimate(pred: Predicate, stats: TableStats, *, profile: str = "auto") -> float:
    """Unified [0,1]-clamped selectivity for one predicate.

        Routes to the enriched estimator for 'eimer_plus', else the analytic estimator
        for 'eimer'/'trino'. 'auto' picks 'eimer_plus' when enriched stats are present.
        NaN is preserved (unknown).
    """
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; expected one of {PROFILES}")
    resolved = resolve_profile(profile, stats)
    if resolved == "eimer_plus":
        return mode_b_enriched.estimate(pred, stats, profile="eimer_plus")
    return mode_b_analytic.estimate(pred, stats, profile=resolved)


_TIMESTAMP_TYPE_PREFIXES = ("timestamp",)
_STRING_TYPE_PREFIXES = ("varchar", "char")
_NUMERIC_TYPE_PREFIXES = ("bigint", "integer", "smallint", "tinyint", "double", "real", "decimal")
_BOOLEAN_TYPE_PREFIXES = ("boolean",)


def _column_types(client, table: str) -> dict:
    """{column: lower-cased type} from the backend's column listing (Trino: `SHOW COLUMNS FROM <table>`,
        DuckDB: `DESCRIBE <table>`; (name, type) in the first two result columns).
    """
    from eimer.sql.backend import backend_for
    _cols, rows = client.execute(backend_for(stats_gather.client_dialect(client)).column_types_sql(table))
    return {str(r[0]): str(r[1]).strip().lower() for r in rows}


def _classify(types: dict, columns=None):
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


def classify_column_types(client, table: str, columns=None):
    """Data-driven column classification via the backend's column listing.

        Returns (string_cols, numeric_cols, timestamp_cols) as sets. Restricted to `columns`
        when given, else covers every column. Unrecognized types (including
        boolean) fall into none of the sets. `client.execute(sql) -> (col_names, rows)`.
    """
    return _classify(_column_types(client, table), columns)


def gather_stats(columns, client, table: str, *, enriched: bool = False,
                 eq_columns=(), hist_columns=()) -> TableStats:
    """Gathers the per-column summary stats the estimator consumes, as a `TableStats`.

        enriched=False: coarse {low, high, ndv, nulls_fraction} via one aggregate SELECT.

        enriched=True: additionally gathers the top-K frequency sketch for equality columns
        (string, numeric and boolean) and the equi-depth histogram for numeric/timestamp range
        columns, used by the 'eimer_plus' estimator. `eq_columns`/`hist_columns` are the predicate
        roles. `client.execute(sql) -> (col_names, rows)`.
    """
    columns = list(dict.fromkeys(columns))
    if not enriched:
        return stats_gather.gather(columns, client, table)
    types = _column_types(client, table)
    string_cols, numeric_cols, timestamp_cols = _classify(types, columns)
    boolean_cols = {c for c in columns if types.get(c, "").startswith(_BOOLEAN_TYPE_PREFIXES)}
    eq_set, hist_set = set(eq_columns), set(hist_columns)
    freq = [c for c in columns if c in eq_set and (c in string_cols or c in numeric_cols or c in boolean_cols)]
    hist = [c for c in columns if c in hist_set and (c in numeric_cols or c in timestamp_cols)]
    return stats_gather_plus.gather_plus(
        columns, client, table,
        categorical=freq, numeric=hist, timestamp_columns=frozenset(timestamp_cols),
        k=16, bins=32,
    )


def _selftest() -> None:
    from math import isnan

    def approx(a, b, tol=1e-12):
        return (isnan(a) and isnan(b)) or abs(a - b) <= tol

    coarse = TableStats(row_count=1000, columns={
        "id": ColumnStats(0.0, 8000.0, 8000.0, 0.0),
        "etype": ColumnStats(float("nan"), float("nan"), 4.0, 0.0),
    })
    assert resolve_profile("auto", coarse) == "eimer", "auto must be eimer on coarse stats"

    pa = Predicate(kind="eq", family="A", col="etype", literal="R")
    assert approx(estimate(pa, coarse, profile="eimer"),
                  mode_b_analytic.estimate(pa, coarse, profile="eimer"))
    assert approx(estimate(pa, coarse, profile="auto"),
                  mode_b_analytic.estimate(pa, coarse, profile="eimer")), "auto==eimer on coarse"

    pb = Predicate(kind="offset_band", family="B", col="id", other="id",
                   lo_offset=0.0, hi_offset=500.0)
    got = estimate(pb, coarse, profile="eimer")
    want = mode_b_analytic.estimate(pb, coarse, profile="eimer")
    assert approx(got, want) and not isnan(got), (got, want)

    assert isnan(estimate(pb, coarse, profile="trino"))

    uhist = Histogram(edges=tuple(i * 80.0 for i in range(101)),
                      probs=tuple(i / 100 for i in range(101)))
    enriched = TableStats(row_count=1000, columns={
        "id": ColumnStats(0.0, 8000.0, 8000.0, 0.0, hist=uhist),
    })
    assert resolve_profile("auto", enriched) == "eimer_plus"
    pbe = Predicate(kind="offset_band", family="B", col="id", other="id",
                    lo_offset=0.0, hi_offset=2000.0)
    got_e = estimate(pbe, enriched, profile="auto")
    want_e = mode_b_enriched.estimate(pbe, enriched, profile="eimer_plus")
    assert approx(got_e, want_e), (got_e, want_e)

    cols = ["row_count",
            stats_gather._safe_alias("min", "id"), stats_gather._safe_alias("max", "id"),
            stats_gather._safe_alias("ndv", "id"), stats_gather._safe_alias("nf", "id")]
    row = [1000, 1.0, 8000.0, 8000.0, 0.0]

    class _FakeClient:
        def execute(self, _sql):
            return cols, [row]

    ts = gather_stats(["id"], _FakeClient(), "events")
    assert ts.row_count == 1000 and ts.col("id").low == 1.0 and ts.col("id").high == 8000.0

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
    pz = Predicate(kind="eq", family="A", col="etype", literal="Z")
    assert abs(estimate(pz, es, profile="auto") - 0.70) < 1e-9, estimate(pz, es, profile="auto")

    try:
        estimate(pa, coarse, profile="nonsense")
        raise AssertionError("expected ValueError on bad profile")
    except ValueError:
        pass

    print("mode_b_estimator self-test: ALL PASS")
    print(f"  profiles={PROFILES}; auto(coarse)=eimer, auto(enriched)=eimer_plus")


if __name__ == "__main__":
    _selftest()
