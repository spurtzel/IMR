"""`DuckDBBackend`: the DuckDB dialect implementation.

DuckDB accepts the emitted Trino SQL with the overrides below. DuckDB has no native
MATCH_RECOGNIZE, so Kleene run caches and the in-script correctness baseline are
unavailable (`supports_native_match_recognize`).
"""

from __future__ import annotations

from typing import Sequence

from eimer.sql.backend import SqlBackend, SqlDialect


class DuckDBBackend(SqlBackend):
    dialect = SqlDialect.DUCKDB
    supports_native_match_recognize = False
    kappa = 49.0
    batch_window_prefix_prune = True
    window_rule = "chain"

    def approx_distinct(self, column: str) -> str:
        return f"approx_count_distinct({column})"

    def generated_event_time_expr(self, id_expr: str) -> str:
        """Timestamp plus n * INTERVAL arithmetic."""
        return (
            f"CAST(TIMESTAMP '2020-01-01 00:00:00.000000' + "
            f"(({id_expr}) - 1) * INTERVAL 1 MILLISECOND AS TIMESTAMP(6))"
        )

    def series_cte(self, name: str, col: str, count: int, *, zero_based: bool = False) -> str:
        """DuckDB's `range(lo, hi)` table function is half-open."""
        lo, hi = (0, count - 1) if zero_based else (1, count)
        return f"{name} AS (SELECT range AS {col} FROM range({lo}, {hi + 1}))"

    def unix_seconds(self, expr: str) -> str:
        return f"epoch({expr})"

    def approx_percentiles(self, numeric_expr: str, probs: Sequence[float]) -> str:
        arr = ", ".join(repr(p) for p in probs)
        return f"approx_quantile(CAST({numeric_expr} AS DOUBLE), [{arr}])"

    def column_types_sql(self, table: str) -> str:
        """DESCRIBE yields (column_name, column_type, ...)."""
        return f"DESCRIBE {table}"

    def fail_int_expr(self, message: str) -> str:
        return f"CAST(error('{message}') AS INTEGER)"
