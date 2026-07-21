from __future__ import annotations

import bootstrap  # noqa: F401

from canonical_query import spatial_lat_predicate, spatial_lon_predicate
from benchmark_types import CanonicalDependentConfig
from eimer.query.query_spec import QuerySpec


def strip_trailing_semicolon(sql: str) -> str:
    stripped = sql.strip()
    if stripped.endswith(";"):
        stripped = stripped[:-1]
    return stripped.rstrip()


def indent_sql(sql: str, spaces: int = 4) -> str:
    prefix = " " * spaces
    return "\n".join(prefix + line if line else "" for line in sql.splitlines())


def replace_watermark_placeholder(sql: str, watermark_table: str) -> str:
    watermark_expr = f"(SELECT max(watermark) FROM {watermark_table})"
    return sql.replace(":watermark", watermark_expr)


def normalize_n_composition_sql(
    composition_sql: str,
    variables: tuple[str, str, str] = ("R", "B", "M"),
    query_spec: QuerySpec | None = None,
) -> str:
    inner = strip_trailing_semicolon(composition_sql)
    if query_spec is not None:
        projections = _result_projection_for_query_spec(query_spec)
        select_lines = ",\n".join(f"    {source} AS {alias}" for source, alias in projections)
        return (
            "SELECT\n"
            f"{select_lines}\n"
            "FROM (\n"
            + indent_sql(inner, 4)
            + "\n) eimer_composed"
        )

    left, middle, right = variables
    return (
        "SELECT\n"
        f"    {left}_id AS r_id,\n"
        f"    {left}_ts AS r_time,\n"
        f"    {middle}_id AS b_id,\n"
        f"    {middle}_ts AS b_time,\n"
        f"    {right}_id AS m_id,\n"
        f"    {right}_ts AS m_time\n"
        "FROM (\n"
        + indent_sql(inner, 4)
        + "\n) eimer_composed"
    )


def _measure_source_column(measure_expression: str) -> str:
    expression = measure_expression.strip()
    if "." not in expression:
        raise ValueError(f"QuerySpec measure expression must be qualified: {measure_expression!r}")
    variable, attribute = (part.strip() for part in expression.split(".", 1))
    if attribute == "id":
        return f"{variable}_id"
    if attribute in {"time", "ts"}:
        return f"{variable}_ts"
    return f"{variable}_{attribute}"


def _result_projection_for_query_spec(query_spec: QuerySpec) -> tuple[tuple[str, str], ...]:
    return tuple((_measure_source_column(measure.expression), measure.alias) for measure in query_spec.measures)


def _measure_alias(query_spec: QuerySpec, variable: str, attributes: tuple[str, ...]) -> str:
    wanted = {f"{variable}.{attribute}" for attribute in attributes}
    for measure in query_spec.measures:
        if measure.expression.strip() in wanted:
            return measure.alias
    raise ValueError(
        f"QuerySpec {query_spec.name!r} has no measure for variable {variable!r} attributes {attributes!r}"
    )


def build_path_a_sql(
    composed_table: str = "composed",
    *,
    query_spec: QuerySpec | None = None,
) -> str:
    if query_spec is not None:
        columns = tuple(measure.alias for measure in query_spec.measures)
        anchor_id = _measure_alias(query_spec, query_spec.variable_names[0], ("id",))
        order_columns = tuple(
            _measure_alias(query_spec, variable, ("time", "ts"))
            for variable in query_spec.variable_names[1:]
        )
        order_by = ", ".join(f"{column} DESC" for column in order_columns)
        return f"""SELECT {', '.join(columns)}
FROM (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY {anchor_id}
            ORDER BY {order_by}
        ) AS rn
    FROM {composed_table}
) ranked
WHERE rn = 1
"""

    return f"""SELECT r_id, r_time, b_id, b_time, m_id, m_time
FROM (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY r_id
            ORDER BY b_time DESC, m_time DESC
        ) AS rn
    FROM {composed_table}
) ranked
WHERE rn = 1
"""


def build_path_b_sql(
    config: CanonicalDependentConfig,
    composed_table: str = "composed",
    events_table: str = "events",
) -> str:
    b_lon = spatial_lon_predicate("B", "R", config.b_lon_window)
    b_lat = spatial_lat_predicate("B", "R", config.b_lat_window)
    m_lon = spatial_lon_predicate("M", "R", config.m_lon_window)
    m_lat = spatial_lat_predicate("M", "R", config.m_lat_window)

    return f"""WITH
    composed_enriched AS (
        SELECT
            c.*,
            r.lon AS r_lon,
            r.lat AS r_lat,
            b.lon AS b_lon,
            b.lat AS b_lat,
            m.lon AS m_lon,
            m.lat AS m_lat
        FROM {composed_table} c
        JOIN {events_table} r ON c.r_id = r.id
        JOIN {events_table} b ON c.b_id = b.id
        JOIN {events_table} m ON c.m_id = m.id
    ),
    combined AS (
        SELECT DISTINCT
            r_time AS time,
            r_id,
            CAST(NULL AS BIGINT) AS b_id,
            CAST(NULL AS BIGINT) AS m_id,
            r_lon AS lon,
            r_lat AS lat,
            'R' AS etype
        FROM composed_enriched
        UNION ALL
        SELECT DISTINCT
            b_time AS time,
            CAST(NULL AS BIGINT),
            b_id,
            CAST(NULL AS BIGINT),
            b_lon,
            b_lat,
            'B'
        FROM composed_enriched
        UNION ALL
        SELECT DISTINCT
            m_time AS time,
            CAST(NULL AS BIGINT),
            CAST(NULL AS BIGINT),
            m_id,
            m_lon,
            m_lat,
            'M'
        FROM composed_enriched
    )
SELECT *
FROM combined MATCH_RECOGNIZE (
    ORDER BY time
    MEASURES
        R.r_id AS r_id,
        R.time AS r_time,
        B.b_id AS b_id,
        B.time AS b_time,
        M.m_id AS m_id,
        M.time AS m_time
    ONE ROW PER MATCH
    AFTER MATCH SKIP TO NEXT ROW
    PATTERN (R Z* B Z* M)
    DEFINE
        R AS etype = 'R',
        B AS etype = 'B' AND {b_lon} AND {b_lat},
        M AS etype = 'M' AND {m_lon} AND {m_lat},
        Z AS TRUE
)
"""


def build_path_b_sql_from_composed_coords(
    config: CanonicalDependentConfig,
    composed_table: str = "composed",
) -> str:
    b_lon = spatial_lon_predicate("B", "R", config.b_lon_window)
    b_lat = spatial_lat_predicate("B", "R", config.b_lat_window)
    m_lon = spatial_lon_predicate("M", "R", config.m_lon_window)
    m_lat = spatial_lat_predicate("M", "R", config.m_lat_window)

    return f"""WITH
    combined AS (
        SELECT DISTINCT
            r_time AS time,
            r_id,
            CAST(NULL AS BIGINT) AS b_id,
            CAST(NULL AS BIGINT) AS m_id,
            r_lon AS lon,
            r_lat AS lat,
            'R' AS etype
        FROM {composed_table}
        UNION ALL
        SELECT DISTINCT
            b_time AS time,
            CAST(NULL AS BIGINT),
            b_id,
            CAST(NULL AS BIGINT),
            b_lon,
            b_lat,
            'B'
        FROM {composed_table}
        UNION ALL
        SELECT DISTINCT
            m_time AS time,
            CAST(NULL AS BIGINT),
            CAST(NULL AS BIGINT),
            m_id,
            m_lon,
            m_lat,
            'M'
        FROM {composed_table}
    )
SELECT *
FROM combined MATCH_RECOGNIZE (
    ORDER BY time
    MEASURES
        R.r_id AS r_id,
        R.time AS r_time,
        B.b_id AS b_id,
        B.time AS b_time,
        M.m_id AS m_id,
        M.time AS m_time
    ONE ROW PER MATCH
    AFTER MATCH SKIP TO NEXT ROW
    PATTERN (R Z* B Z* M)
    DEFINE
        R AS etype = 'R',
        B AS etype = 'B' AND {b_lon} AND {b_lat},
        M AS etype = 'M' AND {m_lon} AND {m_lat},
        Z AS TRUE
)
"""
