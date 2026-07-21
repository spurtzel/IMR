"""schema helpers for restricted-model QuerySpec sql generation.

the QuerySpec event schema is the source of truth for columns carried through eimer
view tables.
"""

from __future__ import annotations

from collections.abc import Mapping

from eimer.models import View
from eimer.query.query_spec import ColumnSpec, QuerySpec


class QuerySchemaError(ValueError):
    """Raised when a QuerySpec column type cannot be resolved."""


def _event_schema_map(spec: QuerySpec) -> dict[str, str]:
    """lower-cased event column name -> sql type."""
    return {column.name.lower(): column.sql_type for column in spec.event_schema}


def event_column_type(spec: QuerySpec, column_name: str) -> str:
    """Return the SQL type for an event column declared by ,,spec''."""

    schema = _event_schema_map(spec)
    try:
        return schema[column_name.lower()]
    except KeyError as exc:
        raise QuerySchemaError(
            f"QuerySpec {spec.name!r} event_schema has no column {column_name!r}"
        ) from exc


def _variable_for_name(spec: QuerySpec, variable: str):
    """the VariableSpec named ,,variable'', or raise."""
    for item in spec.variables:
        if item.name == variable:
            return item
    raise QuerySchemaError(f"QuerySpec {spec.name!r} has no variable {variable!r}")


def _strip_carried_column_decorators(column_name: str) -> str:
    """strip first_/last_ (and count) decorators back to the underlying event column."""
    if column_name in {"first_id", "last_id"}:
        return "id"
    if column_name in {"first_ts", "last_ts"}:
        return "ts"
    if column_name in {"count", "first_count", "last_count"}:
        return "count"
    for marker in ("first_", "last_"):
        if column_name.startswith(marker):
            return column_name[len(marker):]
    return column_name


def variable_output_column_type(spec: QuerySpec, variable: str, output_column: str) -> str:
    """Resolve a variable-carried output column to a SQL type.

    ,,output_column'' may be an event column name (,,time''), a view-style
    variable-prefixed name (,,A_time''), or a result-alias-style output-prefix
    name (,,a_time'').
    """

    variable_spec = _variable_for_name(spec, variable)
    raw_column = output_column
    for prefix in (f"{variable_spec.name}_", f"{variable_spec.output_prefix}_"):
        if raw_column.startswith(prefix):
            raw_column = raw_column[len(prefix):]
            break

    event_column = _strip_carried_column_decorators(raw_column)
    if event_column == "count":
        return "BIGINT"
    return event_column_type(spec, event_column)


def variable_prefixed_column_type(spec: QuerySpec, prefixed_column: str) -> str:
    """Resolve a view/result prefixed column such as ,,A_time'' or ,,a_id''."""

    measure_types = {measure.alias: measure.sql_type for measure in spec.measures if measure.sql_type}
    if prefixed_column in measure_types:
        return str(measure_types[prefixed_column])

    candidates: list[tuple[int, str, str]] = []
    for variable in spec.variables:
        candidates.append((len(variable.name), variable.name, f"{variable.name}_"))
        candidates.append((len(variable.output_prefix), variable.name, f"{variable.output_prefix}_"))

    for _, variable_name, prefix in sorted(candidates, reverse=True):
        if prefixed_column.startswith(prefix):
            return variable_output_column_type(spec, variable_name, prefixed_column)

    raise QuerySchemaError(
        f"Could not resolve QuerySpec {spec.name!r} prefixed column {prefixed_column!r}"
    )


def referenced_event_columns_by_variable(spec: QuerySpec) -> dict[str, tuple[str, ...]]:
    """Return event columns that QuerySpec predicates require per variable."""

    refs: dict[str, set[str]] = {variable.name: {"id", "ts"} for variable in spec.variables}
    for predicate in spec.independent_predicates:
        for variable in predicate.variables:
            refs.setdefault(variable, {"id", "ts"}).update(predicate.referenced_columns)
    for predicate in spec.dependent_predicates:
        for variable in predicate.variables:
            refs.setdefault(variable, {"id", "ts"}).update(predicate.referenced_columns)
    return {variable: tuple(sorted(columns)) for variable, columns in refs.items()}


def cache_column_schema_for_view(
    spec: QuerySpec, view: View, *,
    referenced_columns_by_var: Mapping[str, tuple[str, ...] | list[str] | set[str]] | None = None,
) -> tuple[ColumnSpec, ...]:
    """Return QuerySpec-typed view columns for a non-Kleene materialized view."""

    columns_by_var = referenced_columns_by_var or referenced_event_columns_by_variable(spec)
    order = {variable.name: idx for idx, variable in enumerate(spec.variables)}
    schema: list[ColumnSpec] = []
    for variable in sorted(view, key=lambda item: order[item]):
        schema.append(ColumnSpec(f"{variable}_id", variable_output_column_type(spec, variable, "id")))
        schema.append(ColumnSpec(f"{variable}_ts", variable_output_column_type(spec, variable, "ts")))
        for column in sorted(columns_by_var.get(variable, ())):
            if column in {"id", "ts"}:
                continue
            schema.append(
                ColumnSpec(
                    f"{variable}_{column}",
                    variable_output_column_type(spec, variable, column),
                )
            )
    return tuple(schema)


# fixed-width payload bytes per sql type, for the memory-aware selector's row-width estimate
# (analysis-only; changes no emitted sql). VARCHAR/CHAR carry no precision in the event_schema, so a
# live VARCHAR column makes the row width underivable (flagged, never guessed).
_SQL_TYPE_BYTES = {
    "BIGINT": 8, "LONG": 8, "INTEGER": 4, "INT": 4, "SMALLINT": 2, "TINYINT": 1,
    "DOUBLE": 8, "REAL": 4, "FLOAT": 8, "DECIMAL": 8, "NUMERIC": 8, "NUMBER": 8,
    "BOOLEAN": 1, "BOOL": 1, "BIT": 1,
    "TIMESTAMP": 8, "DATE": 4, "TIME": 8,
}


