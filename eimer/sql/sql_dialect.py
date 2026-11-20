"""dialect API: thin delegators to the per-dialect backend objects in
,,backend.py''. trino is the default dialect (with ,,dialect=TRINO'' every helper returns
the exact byte-identical literal eimer emits); duckdb is the second dialect.
"""
from __future__ import annotations

from eimer.sql.backend import SqlDialect, backend_for, resolve

__all__ = [
    "SqlDialect", "resolve", "type_name", "set_diff_keyword", "statement_terminator",
    "approx_distinct", "relation_alias", "ds_interval", "date_diff_ms",
    "ordered_join_hint", "rewrite_sql",
]


def type_name(canonical: str, dialect: SqlDialect = SqlDialect.TRINO) -> str:
    """Map a canonical (Trino) type name to the dialect."""
    return backend_for(dialect).type_name(canonical)


def set_diff_keyword(dialect: SqlDialect = SqlDialect.TRINO) -> str:
    """Set-difference operator."""
    return backend_for(dialect).set_diff_keyword()


def statement_terminator(dialect: SqlDialect = SqlDialect.TRINO) -> str:
    """Trino CLI wants ';'."""
    return backend_for(dialect).statement_terminator()


def approx_distinct(column: str, dialect: SqlDialect = SqlDialect.TRINO) -> str:
    """Approximate distinct count."""
    return backend_for(dialect).approx_distinct(column)


def relation_alias(table_expr: str, alias: str, dialect: SqlDialect = SqlDialect.TRINO) -> str:
    """A relation or derived table with its alias."""
    return backend_for(dialect).relation_alias(table_expr, alias)


def ds_interval(value: object, unit: str, dialect: SqlDialect = SqlDialect.TRINO) -> str:
    """Day-second interval literal."""
    return backend_for(dialect).ds_interval(value, unit)


def date_diff_ms(start_ts: str, end_ts: str, dialect: SqlDialect = SqlDialect.TRINO) -> str:
    """Millisecond difference (end - start). Trino date_diff."""
    return backend_for(dialect).date_diff_ms(start_ts, end_ts)


def ordered_join_hint(sql: str, dialect: SqlDialect = SqlDialect.TRINO) -> str:
    """The statement with its join order pinned. On Trino the order is pinned for the session
    (,,join_reordering_strategy='NONE'''), so the statement is returned as it is."""
    return backend_for(dialect).ordered_join_hint(sql)


def rewrite_sql(sql: str, dialect: SqlDialect = SqlDialect.TRINO) -> str:
    """Apply dialect-specific rewrites to a fully-rendered SQL string.
    TRINO = strict no-op."""
    return backend_for(dialect).rewrite_sql(sql)
