"""the backend interface: every dialect-specific SQL form lives on a backend object,
so shared logic asks ,,backend_for(dialect)'' instead of branching on the dialect.

holds the dialect enum, the ,,SqlBackend'' base class (the trino implementation), and
the lazy ,,backend_for'' registry. per-dialect subclasses live in
backends/<dialect>/dialect.py and are imported on first request.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Sequence


class SqlDialect(str, Enum):
    TRINO = "trino"      # default
    DUCKDB = "duckdb"


def resolve(value: "str | SqlDialect | None") -> SqlDialect:
    """Resolve a CLI string / enum / None to a SqlDialect (default TRINO)."""
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
    """dialect-specific SQL forms. the base class is the trino implementation."""

    dialect = SqlDialect.TRINO
    # engines without a native MATCH_RECOGNIZE cannot run the Kleene run-cache statements
    # (PATTERN (v+) re-detection) nor the in-script correctness baseline; callers fail loud.
    supports_native_match_recognize = True
    # the clique-grow selector's maintenance-to-compose unit ratio on this engine (kappa in
    # TotalCost = Q*compose + kappa*B*sum|prefix(V)|): the price of maintaining one prefix row
    # for one batch in units of composition work. an engine constant, calibrated once per engine;
    # the base value is the selector's default.
    kappa = 150.0
    # the batch-window prefix prune (sql_emitter.generate_update_sql_statements): restrict every
    # non-batch source of a view update to the rows the target's window can still reach from the
    # batch. off by default; an engine that joins the batch against the whole history without an
    # index (duckdb) turns it on.
    batch_window_prefix_prune = False
    window_rule = "pairwise"

    # --- per-form helpers ---

    def type_name(self, canonical: str) -> str:
        return canonical

    def set_diff_keyword(self) -> str:
        return "EXCEPT"

    def statement_terminator(self) -> str:
        """Terminator of an emitted statement."""
        return ";"

    def approx_distinct(self, column: str) -> str:
        return f"approx_distinct({column})"

    def relation_alias(self, table_expr: str, alias: str) -> str:
        """A relation or derived table with its alias."""
        return f"{table_expr} AS {alias}"

    def ds_interval(self, value: object, unit: str) -> str:
        return f"INTERVAL '{value}' {unit}"

    def date_diff_ms(self, start_ts: str, end_ts: str) -> str:
        """Millisecond difference (end - start)."""
        return f"date_diff('millisecond', {start_ts}, {end_ts})"

    def ordered_join_hint(self, sql: str) -> str:
        """The statement with its join order pinned. On Trino the order is pinned for the session
        (,,SET SESSION join_reordering_strategy='NONE'''), so the statement is returned as it is."""
        return sql

    def rewrite_sql(self, sql: str) -> str:
        """strict no-op on trino: the trino render is the canonical render."""
        return sql

    # --- statistics-gathering forms (the b_unified estimator) ---

    def unix_seconds(self, expr: str) -> str:
        """seconds-since-epoch as a double (timestamp columns are projected through this so
        low/high land in the same unit as the window literal)."""
        return f"to_unixtime({expr})"

    def approx_percentiles(self, numeric_expr: str, probs: Sequence[float]) -> str:
        """one array of approximate quantiles of ,,numeric_expr'' at the cumulative
        probabilities ,,probs'' (the equi-depth histogram sketch)."""
        arr = ", ".join(repr(p) for p in probs)
        # CAST to double: approx_percentile rejects decimal, so a DECIMAL column would fail
        # without this; harmless for the already-double cases (incl. the seconds cast).
        return f"approx_percentile(CAST({numeric_expr} AS double), ARRAY[{arr}])"

    def column_types_sql(self, table: str) -> str:
        """a statement whose result rows carry (column name, type name) in the first two
        columns, for the data-driven column classification."""
        return f"SHOW COLUMNS FROM {table}"

    def fail_int_expr(self, message: str) -> str:
        """an INTEGER-typed expression that raises ,,message'' when evaluated (the
        preloaded-dataset guard of the benchmark script)."""
        return f"CAST(fail('{message}') AS INTEGER)"

    # --- benchmark-generator forms ---

    def generated_event_time_expr(self, id_expr: str) -> str:
        """The synthetic event timestamp for generated-events SQL: 1-based id on the
        millisecond grid from the fixed 2020-01-01 epoch."""
        return (
            f"CAST(date_add('millisecond', ({id_expr}) - 1, "
            "TIMESTAMP '2020-01-01 00:00:00.000000') AS TIMESTAMP(6))"
        )

    def series_cte(self, name: str, col: str, count: int, *, zero_based: bool = False) -> str:
        """A ,,name AS (SELECT ... AS col)'' integer-series CTE: 1..count, or 0..count-1
        when zero_based (the generated-events row generator)."""
        lo, hi = (0, count - 1) if zero_based else (1, count)
        return f"{name} AS (SELECT {col} FROM UNNEST(sequence({lo}, {hi})) AS t({col}))"

    def rows_column_name(self) -> str:
        """The name of the ,,rows'' column as it is emitted."""
        return "rows"

    # --- the in-DB full-tuple multiset gate ---

    def full_tuple_gate_counts(self, cur, key_cols: Sequence[str], eimer_table: str,
                               mr_table: "str | None") -> tuple:
        """(n_eimer, n_mr, missing, extra) of a multiset comparison of the two result tables inside the engine.
        The backends of this repository do not implement it; their callers compare the fetched keys in Python."""
        raise NotImplementedError(
            "no multiset comparison inside the engine for this backend; compare the fetched keys instead")


class _BackendUnavailable(RuntimeError):
    """Raised when a dialect's backend package is not installed."""


def backend_for(dialect: "SqlDialect | str | None") -> SqlBackend:
    """The single registry: dialect -> backend implementation (default TRINO).

    The implementations live in backends/<dialect>/dialect.py and are
    imported LAZILY on first request, so an unavailable backend fails loud with the
    missing-directory explanation instead of an ImportError at module load."""
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
