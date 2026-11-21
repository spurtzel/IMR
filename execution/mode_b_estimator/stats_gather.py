"""One-pass per-column summary-stat gatherer for the mode_b_analytic estimator.

Builds the single `SELECT` that computes four numbers per column
(`low, high, ndv, nulls_fraction`) plus the table `row_count`, and parses
the one result row into the `TableStats` shape from `contract.py`.

Timestamp columns (`time`, `ts`, TIMESTAMP(6)) are projected through
`to_unixtime(col)`, so `low/high` are doubles in seconds, the unit of the window
predicate's half-width `w`.
"""
from __future__ import annotations

from math import nan
from typing import Dict, List, Sequence

from .contract import ColumnStats, TableStats
from eimer.sql.backend import SqlDialect, backend_for

_TIMESTAMP_COLUMNS = frozenset({"time", "ts"})

ROW_COUNT_ALIAS = "row_count"


def _safe_alias(prefix: str, column: str) -> str:
    """Deterministic, SQL-safe alias for a per-column aggregate."""
    sanitized = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in column)
    return f"{prefix}__{sanitized}"


def _numeric_expr(column: str, timestamp_columns=_TIMESTAMP_COLUMNS,
                  dialect: SqlDialect = SqlDialect.TRINO) -> str:
    """SQL expression projecting `column` into a numeric (double) domain.

        Timestamp columns go through `to_unixtime` (seconds-since-epoch double);
        every other column is referenced directly. `timestamp_columns` defaults to
        the synthetic schema's {time, ts}.
    """
    if column in timestamp_columns:
        return backend_for(dialect).unix_seconds(column)
    return column


def build_stats_sql(columns: Sequence[str], table: str, *, timestamp_columns=_TIMESTAMP_COLUMNS,
                    dialect: SqlDialect = SqlDialect.TRINO) -> str:
    """Single SELECT computing min/max/approx_distinct/null-fraction per column.

        Result is one row: `row_count` (count(*)), `min__<col>`/`max__<col>`
        (numeric min/max, timestamps cast), `ndv__<col>` (approx_distinct), and
        `nf__<col>` = (count(*) - count(col)) * 1.0 / count(*), a double in [0, 1].
    """
    ordered = list(dict.fromkeys(columns))
    if not ordered:
        raise ValueError("build_stats_sql requires at least one column")

    backend = backend_for(dialect)
    select_items: List[str] = [f"count(*) AS {ROW_COUNT_ALIAS}"]
    for column in ordered:
        numeric = _numeric_expr(column, timestamp_columns, dialect)
        select_items.append(f"min({numeric}) AS {_safe_alias('min', column)}")
        select_items.append(f"max({numeric}) AS {_safe_alias('max', column)}")
        select_items.append(f"{backend.approx_distinct(column)} AS {_safe_alias('ndv', column)}")
        select_items.append(
            f"(count(*) - count({column})) * 1.0 / count(*) AS {_safe_alias('nf', column)}"
        )

    body = ",\n  ".join(select_items)
    return f"SELECT\n  {body}\nFROM {table}"


def _as_float_or_nan(value: object) -> float:
    """Casts a Trino scalar to float; NULL/None -> NaN (unknown)."""
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
    """Runs `build_stats_sql` and parses its single row into `TableStats`.

        `client` must expose `execute(sql) -> (column_names, rows)` like
        execution/trino_client.TrinoClient (and may carry a `dialect`; default TRINO).
        min/max/ndv are cast to float (NaN when NULL); `timestamp_columns` selects which
        columns get the seconds cast.
    """
    ordered = list(dict.fromkeys(columns))
    sql = build_stats_sql(ordered, table, timestamp_columns=timestamp_columns,
                          dialect=client_dialect(client))
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
        if nf != nf:
            nf = 0.0
        column_stats[column] = ColumnStats(low=low, high=high, ndv=ndv, nulls_fraction=nf)

    return TableStats(row_count=row_count, columns=column_stats)


def client_dialect(client) -> SqlDialect:
    """The dialect a client executes (its `dialect` attribute); TRINO when it has none."""
    return getattr(client, "dialect", None) or SqlDialect.TRINO


def _self_test() -> None:
    from . import contract

    test_columns = ["id", "lon", "lat", "ts"]
    sql = build_stats_sql(test_columns, "generated_events")

    assert "count(*) AS row_count" in sql, sql
    for column in test_columns:
        assert f"approx_distinct({column}) AS ndv__{column}" in sql, (column, sql)
        assert f"AS nf__{column}" in sql, (column, sql)
        if column in _TIMESTAMP_COLUMNS:
            assert f"min(to_unixtime({column})) AS min__{column}" in sql, (column, sql)
            assert f"max(to_unixtime({column})) AS max__{column}" in sql, (column, sql)
        else:
            assert f"min({column}) AS min__{column}" in sql, (column, sql)
            assert f"max({column}) AS max__{column}" in sql, (column, sql)
    assert "* 1.0 / count(*)" in sql, sql

    result_cols = ["row_count"]
    for column in test_columns:
        result_cols += [
            _safe_alias("min", column),
            _safe_alias("max", column),
            _safe_alias("ndv", column),
            _safe_alias("nf", column),
        ]
    one_row = [
        1000,
        1.0, 1000.0, 1000.0, 0.0,
        -87.9, -87.7, 500.0, 0.1,
        None, None, 0.0, None,
        1.5778e9, 1.5778e9 + 1.0, 1000.0, 0.0,
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
    assert lat.low != lat.low and lat.high != lat.high
    assert lat.nulls_fraction == 0.0
    assert not lat.known

    ts = stats.columns["ts"]
    assert ts.known and ts.span > 0.0

    assert contract.EVENT_COLUMNS[0] == "id"

    print("stats_gather self-test OK")
    print("---- build_stats_sql(['id','lon','lat','ts'], 'generated_events') ----")
    print(sql)


if __name__ == "__main__":
    _self_test()
