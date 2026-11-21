"""The backend interface: every dialect-specific SQL form lives on a backend object;
shared logic calls `backend_for(dialect)`.

Holds the dialect enum, the `SqlBackend` base class (the trino implementation), and
the lazy `backend_for` registry. Per-dialect subclasses live in
backends/<dialect>/dialect.py and are imported on first request.
"""

from __future__ import annotations

from enum import Enum
from typing import Sequence


class SqlDialect(str, Enum):
    TRINO = "trino"
    DUCKDB = "duckdb"


def resolve(value: "str | SqlDialect | None") -> SqlDialect:
    """Resolves a CLI string / enum / None to a SqlDialect (default TRINO)."""
    if value is None:
        return SqlDialect.TRINO
    if isinstance(value, SqlDialect):
        return value
    try:
        return SqlDialect(str(value).strip().lower())
    except ValueError as exc:
        valid = ", ".join(d.value for d in SqlDialect)
        raise ValueError(f"unknown target dialect {value!r}; expected one of: {valid}") from exc


class SqlBackend:
    """Dialect-specific SQL forms. The base class is the trino implementation."""

    dialect = SqlDialect.TRINO
    supports_native_match_recognize = True
    kappa = 150.0
    batch_window_prefix_prune = False
    window_rule = "pairwise"


    def approx_distinct(self, column: str) -> str:
        return f"approx_distinct({column})"


    def unix_seconds(self, expr: str) -> str:
        """Seconds-since-epoch as a double."""
        return f"to_unixtime({expr})"

    def approx_percentiles(self, numeric_expr: str, probs: Sequence[float]) -> str:
        """One array of approximate quantiles of `numeric_expr` at the cumulative
        probabilities `probs` (the equi-depth histogram sketch)."""
        arr = ", ".join(repr(p) for p in probs)
        return f"approx_percentile(CAST({numeric_expr} AS double), ARRAY[{arr}])"

    def column_types_sql(self, table: str) -> str:
        """A statement whose result rows carry (column name, type name) in the first two columns."""
        return f"SHOW COLUMNS FROM {table}"

    def fail_int_expr(self, message: str) -> str:
        """An INTEGER-typed expression that raises `message` when evaluated."""
        return f"CAST(fail('{message}') AS INTEGER)"


    def generated_event_time_expr(self, id_expr: str) -> str:
        """The synthetic event timestamp for generated-events SQL: the 1-based id on a millisecond grid."""
        return (
            f"CAST(date_add('millisecond', ({id_expr}) - 1, "
            "TIMESTAMP '2020-01-01 00:00:00.000000') AS TIMESTAMP(6))"
        )

    def series_cte(self, name: str, col: str, count: int, *, zero_based: bool = False) -> str:
        """A `name AS (SELECT ... AS col)` integer-series CTE: 1..count, or 0..count-1
        when zero_based."""
        lo, hi = (0, count - 1) if zero_based else (1, count)
        return f"{name} AS (SELECT {col} FROM UNNEST(sequence({lo}, {hi})) AS t({col}))"


class _BackendUnavailable(RuntimeError):
    """Raised when a dialect's backend package is not installed."""


def backend_for(dialect: "SqlDialect | str | None") -> SqlBackend:
    """The registry: dialect -> backend implementation (default TRINO).

        The implementations live in backends/<dialect>/dialect.py and are imported on first request.
    """
    d = resolve(dialect)
    backend = _BACKENDS.get(d)
    if backend is None:
        try:
            if d is SqlDialect.TRINO:
                from backends.trino.dialect import TrinoBackend as _cls
            elif d is SqlDialect.DUCKDB:
                from backends.duckdb.dialect import DuckDBBackend as _cls
            else:
                raise ImportError(f"no backend package for dialect {d.value!r}")
        except ImportError as exc:
            raise _BackendUnavailable(
                f"backend {d.value!r} is not installed "
                f"(backends/{d.value}/ missing or not importable)") from exc
        backend = _BACKENDS[d] = _cls()
    return backend


_BACKENDS: "dict[SqlDialect, SqlBackend]" = {}
