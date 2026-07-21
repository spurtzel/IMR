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
    """dialect-specific SQL forms. the base class is the trino implementation, so
    routing shared code through the interface is byte-identical on trino."""

    dialect = SqlDialect.TRINO

    # --- per-form helpers ---

    def type_name(self, canonical: str) -> str:
        return canonical

    def set_diff_keyword(self) -> str:
        return "EXCEPT"

    def statement_terminator(self) -> str:
        """Trino CLI wants ';'; single-statement execution elsewhere may want none."""
        return ";"

    def approx_distinct(self, column: str) -> str:
        return f"approx_distinct({column})"

    def relation_alias(self, table_expr: str, alias: str) -> str:
        """Relation/derived-table alias. Trino accepts 'AS'."""
        return f"{table_expr} AS {alias}"

    def ds_interval(self, value: object, unit: str) -> str:
        return f"INTERVAL '{value}' {unit}"

    def date_diff_ms(self, start_ts: str, end_ts: str) -> str:
        """Millisecond difference (end - start)."""
        return f"date_diff('millisecond', {start_ts}, {end_ts})"

    def ordered_join_hint(self, sql: str) -> str:
        """No-op on Trino: join order is pinned session-wide via
        ,,SET SESSION join_reordering_strategy='NONE''', not per-statement hints."""
        return sql

    def rewrite_sql(self, sql: str) -> str:
        """strict no-op on trino: the trino render is the canonical render."""
        return sql

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
        """,,rows'' is not reserved on Trino, so it is emitted bare."""
        return "rows"

    # --- the in-DB full-tuple multiset gate ---

    def full_tuple_gate_counts(self, cur, key_cols: Sequence[str], eimer_table: str,
                               mr_table: "str | None") -> tuple:
        """(n_eimer, n_mr, missing, extra) via an in-DB TRUE MULTISET diff on a raw
        cursor. Implemented per backend; the Trino path computes its verdicts in-Python
        (full_tuple_gate_sets) from fetched keys, never through a cursor gate."""
        raise NotImplementedError(
            "no in-DB full-tuple gate for this backend; use "
            "correctness_utils.full_tuple_gate_sets on fetched keys instead")


class _BackendUnavailable(RuntimeError):
    """Raised when a dialect's backend package is not present in this tree."""


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
            else:
                raise ImportError(f"no backend package for dialect {d.value!r}")
        except ImportError as exc:
            raise _BackendUnavailable(
                f"backend {d.value!r} is not available in this tree "
                f"(backends/{d.value}/ missing or not importable)") from exc
        backend = _BACKENDS[d] = _cls()
    return backend


_BACKENDS: "dict[SqlDialect, SqlBackend]" = {}
