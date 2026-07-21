"""One-pass per-column summary-stat gatherer for the mode_b_v2 estimator.

Builds the single ,,SELECT'' that materializes four numbers per column
(,,low, high, ndv, nulls_fraction'') plus the table ,,row_count'', and parses
the one result row into the ,,TableStats'' shape from ,,contract.py''.
(,,averageRowSize'' is never used for selectivity, so it is not gathered.)

Timestamp columns (,,time'', ,,ts'', TIMESTAMP(6)) are projected through
,,to_unixtime(col)'' so ,,low/high'' come out as doubles in seconds. That keeps
the estimator's ,,span = high - low'' in the same unit as the window
predicate's half-width ,,w'' (seconds), so no rescaling is needed. Temporal
ordering (,,a.ts < b.ts'') is invariant under this strictly monotone cast.
"""
from __future__ import annotations

from math import nan
from typing import Dict, List, Sequence

from .contract import ColumnStats, TableStats

# Columns whose stored type is TIMESTAMP(6); they must be projected through a
# numeric cast so low/high are doubles comparable to second-valued literals.
_TIMESTAMP_COLUMNS = frozenset({"time", "ts"})

ROW_COUNT_ALIAS = "row_count"


def _safe_alias(prefix: str, column: str) -> str:
    """Deterministic, SQL-safe alias for a per-column aggregate.

    Sanitizes the column name so an arbitrary column list cannot inject SQL via
    an alias, and so the parser can reconstruct the alias from (prefix, column).
    """
    sanitized = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in column)
    return f"{prefix}__{sanitized}"


def _numeric_expr(column: str, timestamp_columns=_TIMESTAMP_COLUMNS) -> str:
    """SQL expression projecting ,,column'' into a numeric (double) domain.

    Timestamp columns go through ,,to_unixtime'' (seconds-since-epoch double);
    every other column is referenced directly. ,,timestamp_columns'' defaults to
    the synthetic schema's {time, ts}; callers on other schemas pass a
    data-driven timestamp set so arbitrarily-named TIMESTAMP columns still cast.
    """
    if column in timestamp_columns:
        return f"to_unixtime({column})"
    return column


def build_stats_sql(columns: Sequence[str], table: str, *, timestamp_columns=_TIMESTAMP_COLUMNS) -> str:
    """Single SELECT computing min/max/approx_distinct/null-fraction per column.

    Result is one row: ,,row_count'' (count(*)), ,,min__<col>''/,,max__<col>''
    (numeric min/max, timestamps cast), ,,ndv__<col>'' (approx_distinct), and
    ,,nf__<col>'' = (count(*) - count(col)) * 1.0 / count(*), a double in [0, 1]
    (,,count(col)'' excludes NULLs by SQL semantics).
    """
    ordered = list(dict.fromkeys(columns))  # de-dup, preserve order
    if not ordered:
        raise ValueError("build_stats_sql requires at least one column")

    select_items: List[str] = [f"count(*) AS {ROW_COUNT_ALIAS}"]
    for column in ordered:
        numeric = _numeric_expr(column, timestamp_columns)
        select_items.append(f"min({numeric}) AS {_safe_alias('min', column)}")
        select_items.append(f"max({numeric}) AS {_safe_alias('max', column)}")
        select_items.append(f"approx_distinct({column}) AS {_safe_alias('ndv', column)}")
        select_items.append(
            f"(count(*) - count({column})) * 1.0 / count(*) AS {_safe_alias('nf', column)}"
        )

    body = ",\n  ".join(select_items)
    return f"SELECT\n  {body}\nFROM {table}"


def _as_float_or_nan(value: object) -> float:
    """Cast a Trino scalar to float; NULL/None -> NaN (the 'unknown' value)."""
    if value is None:
        return nan
    try:
        return float(value)
    except (TypeError, ValueError):
        return nan


def _row_to_mapping(columns: Sequence[str], rows: Sequence[Sequence[object]]) -> Dict[str, object]:
    if not rows:
        raise ValueError("stats query returned no rows")
    if len(rows) != 1:
        raise ValueError(f"stats query must return exactly one row, got {len(rows)}")
    row = rows[0]
    return {str(name): row[idx] for idx, name in enumerate(columns)}


def gather(columns: Sequence[str], client, table: str, *, timestamp_columns=_TIMESTAMP_COLUMNS) -> TableStats:
    """Run ,,build_stats_sql'' and parse its single row into ,,TableStats''.

    ,,client'' must expose ,,execute(sql) -> (column_names, rows)'' like
    execution/trino_client.TrinoClient. min/max/ndv are cast to float (NaN when
    NULL); ,,timestamp_columns'' selects which columns get the to_unixtime cast.
    """
    ordered = list(dict.fromkeys(columns))
    sql = build_stats_sql(ordered, table, timestamp_columns=timestamp_columns)
    result_columns, rows = client.execute(sql)
    mapping = _row_to_mapping(result_columns, rows)

    raw_row_count = mapping.get(ROW_COUNT_ALIAS)
    if raw_row_count is None:
        raise ValueError("stats query did not return a row_count")
    row_count = int(raw_row_count)

    column_stats: Dict[str, ColumnStats] = {}
    for column in ordered:
        low = _as_float_or_nan(mapping.get(_safe_alias("min", column)))
        high = _as_float_or_nan(mapping.get(_safe_alias("max", column)))
        ndv = _as_float_or_nan(mapping.get(_safe_alias("ndv", column)))
        nf = _as_float_or_nan(mapping.get(_safe_alias("nf", column)))
        if nf != nf:  # NaN guard: an all-NULL or empty column -> treat as 0.0
            nf = 0.0
        column_stats[column] = ColumnStats(low=low, high=high, ndv=ndv, nulls_fraction=nf)

    return TableStats(row_count=row_count, columns=column_stats)


# Offline check (no Trino access): run this module directly.
def _self_test() -> None:
    from . import contract  # local import keeps the module import-cheap

    # The numeric-relevant subset of EVENT_COLUMNS plus a timestamp column.
    test_columns = ["id", "lon", "lat", "ts"]
    sql = build_stats_sql(test_columns, "generated_events")

    # Structural assertions: every column gets all four aggregates + count(*).
    assert "count(*) AS row_count" in sql, sql
    for column in test_columns:
        assert f"approx_distinct({column}) AS ndv__{column}" in sql, (column, sql)
        assert f"AS nf__{column}" in sql, (column, sql)
        if column in _TIMESTAMP_COLUMNS:
            # ts must be cast to a numeric domain via to_unixtime.
            assert f"min(to_unixtime({column})) AS min__{column}" in sql, (column, sql)
            assert f"max(to_unixtime({column})) AS max__{column}" in sql, (column, sql)
        else:
            assert f"min({column}) AS min__{column}" in sql, (column, sql)
            assert f"max({column}) AS max__{column}" in sql, (column, sql)
    # null-fraction must be a double-producing expression.
    assert "* 1.0 / count(*)" in sql, sql

    # gather() must parse a single mocked row into TableStats correctly,
    # including NULL -> NaN for min/max and a real null fraction.
    result_cols = ["row_count"]
    for column in test_columns:
        result_cols += [
            _safe_alias("min", column),
            _safe_alias("max", column),
            _safe_alias("ndv", column),
            _safe_alias("nf", column),
        ]
    # id: min=1 max=1000 ndv=1000 nf=0; lon: min=-87.9 max=-87.7 ndv=500 nf=0.1
    # lat: NULL min/max (all-NULL) ndv=0 nf computed-NaN->0; ts: cast seconds.
    one_row = [
        1000,                       # row_count
        1.0, 1000.0, 1000.0, 0.0,   # id
        -87.9, -87.7, 500.0, 0.1,   # lon
        None, None, 0.0, None,      # lat (all NULL -> NaN min/max, nf None->0)
        1.5778e9, 1.5778e9 + 1.0, 1000.0, 0.0,  # ts (to_unixtime seconds)
    ]

    class _FakeClient:
        def execute(self, _sql):
            return result_cols, [one_row]

    stats = gather(test_columns, _FakeClient(), "generated_events")
    assert isinstance(stats, TableStats)
    assert stats.row_count == 1000
    assert set(stats.columns) == set(test_columns)

    lon = stats.columns["lon"]
    assert lon.low == -87.9 and lon.high == -87.7
    assert lon.ndv == 500.0 and abs(lon.nulls_fraction - 0.1) < 1e-12
    assert lon.span == lon.high - lon.low
    assert lon.known

    lat = stats.columns["lat"]
    assert lat.low != lat.low and lat.high != lat.high  # NaN min/max
    assert lat.nulls_fraction == 0.0                    # None -> 0.0 guard
    assert not lat.known                                 # NaN bound => not known

    ts = stats.columns["ts"]
    assert ts.known and ts.span > 0.0  # seconds-domain span is positive

    # contract constants are importable and unchanged.
    assert contract.EVENT_COLUMNS[0] == "id"

    print("stats_gather self-test OK")
    print("---- build_stats_sql(['id','lon','lat','ts'], 'generated_events') ----")
    print(sql)


if __name__ == "__main__":
    _self_test()
