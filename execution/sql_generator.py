from __future__ import annotations

import bootstrap  # noqa: F401

import re
from typing import Collection, Sequence

from canonical_query import canonical_query_spec
from sql_compiler import indent_sql, replace_watermark_placeholder, strip_trailing_semicolon
from benchmark_types import BenchmarkStrategy, CanonicalDependentConfig
from sql_op_id import PRE_BATCH_MARKER, prepend_op_id_marker
from eimer.query.query_schedule import normalize_query_batches as _normalize_core_query_batches
from eimer.query.query_spec import (
    QuerySpec,
    query_spec_result_column_types,
    query_spec_result_key_columns,
    query_spec_to_match_recognize_sql,
)
from eimer.sql.backend import backend_for
from eimer.sql.sql_dialect import (
    SqlDialect,
    ordered_join_hint,
    rewrite_sql,
    set_diff_keyword,
)

WORKLOAD_PROFILES = (
    "deterministic_grid_default",
    "iid_uniform",
    "high_match",
    "clustered_spatial",
    "temporal_bursty",
)
DEFAULT_WORKLOAD_PROFILE = "deterministic_grid_default"
DEFAULT_WORKLOAD_SEED = 0


def _extract_created_table_name(ddl: str) -> str:
    match = re.search(r"CREATE\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)", ddl, re.IGNORECASE)
    if not match:
        raise ValueError(f"Could not extract table name from DDL: {ddl!r}")
    return match.group(1)


def _validate_benchmark_params(
    *,
    initial_history: int,
    num_updates: int,
    update_size: int,
    scale_factor: float | None,
    selectivity_r: float | None,
    selectivity_b: float | None,
    selectivity_m: float | None,
) -> None:
    if initial_history <= 0:
        raise ValueError("initial_history must be > 0")
    if num_updates < 0:
        raise ValueError("num_updates must be >= 0")
    if update_size <= 0:
        raise ValueError("update_size must be > 0")
    if scale_factor is not None and scale_factor <= 0:
        raise ValueError("scale_factor must be > 0")

    any_sel = any(v is not None for v in (selectivity_r, selectivity_b, selectivity_m))
    all_sel = all(v is not None for v in (selectivity_r, selectivity_b, selectivity_m))
    if any_sel and not all_sel:
        raise ValueError("selectivity_r, selectivity_b, and selectivity_m must be provided together")

    if all_sel:
        assert selectivity_r is not None and selectivity_b is not None and selectivity_m is not None
        for value in (selectivity_r, selectivity_b, selectivity_m):
            if value < 0 or value > 1:
                raise ValueError("selectivity values must be within [0, 1]")
        if (selectivity_r + selectivity_b + selectivity_m) > 1:
            raise ValueError("selectivity_r + selectivity_b + selectivity_m must be <= 1")


def validate_workload_profile(workload_profile: str) -> None:
    if workload_profile not in WORKLOAD_PROFILES:
        raise ValueError(f"unknown workload profile {workload_profile!r}; expected one of {WORKLOAD_PROFILES!r}")


def _batch_sizes(
    *,
    initial_history: int,
    num_updates: int,
    update_size: int,
    scale_factor: float | None,
) -> list[int]:
    if scale_factor is None:
        return [initial_history] + [update_size] * num_updates

    sizes = [initial_history]
    for idx in range(1, num_updates + 1):
        size = int(round(update_size * (scale_factor ** idx)))
        sizes.append(max(1, size))
    return sizes


def _bucket_expr(id_expr: str, *, seed: int, salt: int, cycle: int = 1_000_000) -> str:
    # Deterministic integer hash stream; salt varies the coefficients so event type and coordinates draw from different streams.
    multiplier = 1_103_515_245 + (salt * 2_654_435_761 % 1_000_000_007)
    increment = 12_345 + salt * 97_531 + seed * 214_013
    return f"mod(({id_expr}) * {multiplier} + {increment}, {cycle})"


def _profile_selectivities(
    *,
    profile: str,
    selectivity_r: float | None,
    selectivity_b: float | None,
    selectivity_m: float | None,
) -> tuple[float, float, float]:
    if all(v is not None for v in (selectivity_r, selectivity_b, selectivity_m)):
        assert selectivity_r is not None and selectivity_b is not None and selectivity_m is not None
        return selectivity_r, selectivity_b, selectivity_m
    if profile == "high_match":
        return 0.10, 0.10, 0.10
    if profile == "temporal_bursty":
        return 0.02, 0.02, 0.02
    return 1.0 / 97.0, 1.0 / 89.0, 1.0 / 83.0


def _threshold_case_for_bucket(
    *,
    bucket_expr: str,
    config: CanonicalDependentConfig,
    selectivity_r: float,
    selectivity_b: float,
    selectivity_m: float,
) -> list[str]:
    cycle = 1_000_000
    r_threshold = int(round(cycle * selectivity_r))
    b_threshold = r_threshold + int(round(cycle * selectivity_b))
    m_threshold = b_threshold + int(round(cycle * selectivity_m))
    return [
        f"    WHEN {bucket_expr} < {r_threshold} THEN '{config.r_type}'",
        f"    WHEN {bucket_expr} < {b_threshold} THEN '{config.b_type}'",
        f"    WHEN {bucket_expr} < {m_threshold} THEN '{config.m_type}'",
    ]


def _seeded_primary_type_case(
    *,
    config: CanonicalDependentConfig,
    id_expr: str,
    workload_profile: str,
    workload_seed: int,
    selectivity_r: float | None,
    selectivity_b: float | None,
    selectivity_m: float | None,
) -> str:
    r_sel, b_sel, m_sel = _profile_selectivities(
        profile=workload_profile,
        selectivity_r=selectivity_r,
        selectivity_b=selectivity_b,
        selectivity_m=selectivity_m,
    )
    type_bucket = _bucket_expr(id_expr, seed=workload_seed, salt=11)
    other_bucket = _bucket_expr(id_expr, seed=workload_seed, salt=17)

    primary_type_case: list[str] = ["CASE"]
    if workload_profile == "temporal_bursty":
        burst = f"mod(CAST(floor((({id_expr}) - 1) / 1000.0) AS BIGINT) + {workload_seed}, 5)"
        primary_type_case.extend(
            [
                f"    WHEN {burst} = 0 AND {type_bucket} < 180000 THEN '{config.r_type}'",
                f"    WHEN {burst} = 1 AND {type_bucket} < 180000 THEN '{config.b_type}'",
                f"    WHEN {burst} = 2 AND {type_bucket} < 180000 THEN '{config.m_type}'",
            ]
        )
    primary_type_case.extend(
        _threshold_case_for_bucket(
            bucket_expr=type_bucket,
            config=config,
            selectivity_r=r_sel,
            selectivity_b=b_sel,
            selectivity_m=m_sel,
        )
    )
    primary_type_case.extend(
        [
            f"    WHEN {other_bucket} < 50000 THEN 'ASSAULT'",
            f"    WHEN {other_bucket} < 100000 THEN 'NARCOTICS'",
            f"    WHEN {other_bucket} < 150000 THEN 'BURGLARY'",
            "    ELSE 'OTHER'",
            "END",
        ]
    )
    return "\n".join(primary_type_case)


def _seeded_coordinate_exprs(
    *,
    id_expr: str,
    workload_profile: str,
    workload_seed: int,
) -> tuple[str, str]:
    lon_bucket = _bucket_expr(id_expr, seed=workload_seed, salt=23)
    lat_bucket = _bucket_expr(id_expr, seed=workload_seed, salt=29)
    if workload_profile in {"clustered_spatial", "high_match"}:
        cluster_bucket = _bucket_expr(id_expr, seed=workload_seed, salt=31, cycle=4)
        lon = (
            "CAST(-87.86 + "
            f"({cluster_bucket}) * 0.035 + ({lon_bucket}) / 1000000.0 * 0.010 AS DOUBLE)"
        )
        lat = (
            "CAST(41.66 + "
            f"mod({cluster_bucket}, 2) * 0.030 + ({lat_bucket}) / 1000000.0 * 0.010 AS DOUBLE)"
        )
        return lon, lat
    return (
        f"CAST(-87.90 + ({lon_bucket}) / 1000000.0 * 0.20 AS DOUBLE)",
        f"CAST(41.60 + ({lat_bucket}) / 1000000.0 * 0.20 AS DOUBLE)",
    )


def _generated_events_sql(
    *,
    config: CanonicalDependentConfig,
    generated_events_table: str,
    total_events: int,
    selectivity_r: float | None,
    selectivity_b: float | None,
    selectivity_m: float | None,
    workload_profile: str = DEFAULT_WORKLOAD_PROFILE,
    workload_seed: int = DEFAULT_WORKLOAD_SEED,
    dialect: SqlDialect = SqlDialect.TRINO,
) -> str:
    validate_workload_profile(workload_profile)
    block_size = 10_000
    blocks = (total_events + block_size - 1) // block_size
    if blocks > 10_000:
        raise ValueError(
            f"TOTAL_EVENTS={total_events} is too large; would require {blocks} blocks."
        )

    all_sel = all(v is not None for v in (selectivity_r, selectivity_b, selectivity_m))
    selectivity_cycle = 1_000_000

    if all_sel:
        assert selectivity_r is not None and selectivity_b is not None and selectivity_m is not None
        r_threshold = int(round(selectivity_cycle * selectivity_r))
        b_threshold = r_threshold + int(round(selectivity_cycle * selectivity_b))
        m_threshold = b_threshold + int(round(selectivity_cycle * selectivity_m))
    else:
        r_threshold = b_threshold = m_threshold = 0

    id_expr = f"CAST(b AS BIGINT) * {block_size} + x"
    time_expr = backend_for(dialect).generated_event_time_expr(id_expr)
    bucket_expr = f"mod(({id_expr}) * 1103515245 + 12345, {selectivity_cycle})"

    if workload_profile == "deterministic_grid_default":
        primary_type_case: list[str] = ["CASE"]
        if all_sel:
            primary_type_case.append(f"    WHEN {bucket_expr} < {r_threshold} THEN '{config.r_type}'")
            primary_type_case.append(f"    WHEN {bucket_expr} < {b_threshold} THEN '{config.b_type}'")
            primary_type_case.append(f"    WHEN {bucket_expr} < {m_threshold} THEN '{config.m_type}'")
        else:
            primary_type_case.append(f"    WHEN mod({id_expr}, 97) = 0 THEN '{config.r_type}'")
            primary_type_case.append(f"    WHEN mod({id_expr}, 89) = 0 THEN '{config.b_type}'")
            primary_type_case.append(f"    WHEN mod({id_expr}, 83) = 0 THEN '{config.m_type}'")

        primary_type_case.extend(
            [
                f"    WHEN mod({id_expr}, 19) = 0 THEN 'ASSAULT'",
                f"    WHEN mod({id_expr}, 17) = 0 THEN 'NARCOTICS'",
                f"    WHEN mod({id_expr}, 13) = 0 THEN 'BURGLARY'",
                "    ELSE 'OTHER'",
                "END",
            ]
        )
        primary_type_expr = "\n".join(primary_type_case)
        lon_expr = f"CAST(-87.90 + mod(({id_expr}) * 37, 2000) / 10000.0 AS DOUBLE)"
        lat_expr = f"CAST(41.60 + mod(({id_expr}) * 29, 2000) / 10000.0 AS DOUBLE)"
    else:
        primary_type_expr = _seeded_primary_type_case(
            config=config,
            id_expr=id_expr,
            workload_profile=workload_profile,
            workload_seed=workload_seed,
            selectivity_r=selectivity_r,
            selectivity_b=selectivity_b,
            selectivity_m=selectivity_m,
        )
        lon_expr, lat_expr = _seeded_coordinate_exprs(
            id_expr=id_expr,
            workload_profile=workload_profile,
            workload_seed=workload_seed,
        )

    xs_cte = backend_for(dialect).series_cte("xs", "x", block_size)
    bs_cte = backend_for(dialect).series_cte("bs", "b", blocks, zero_based=True)

    return f"""CREATE TABLE {generated_events_table} AS
WITH
    {xs_cte},
    {bs_cte},
    base AS (
        SELECT
            ({id_expr}) AS id,
            {time_expr} AS event_time,
            {primary_type_expr} AS primary_type,
            {lon_expr} AS lon,
            {lat_expr} AS lat
        FROM xs
        CROSS JOIN bs
        WHERE ({id_expr}) <= {total_events}
    )
SELECT
    id,
    event_time AS time,
    event_time AS ts,
    primary_type,
    CASE
        WHEN primary_type = '{config.r_type}' THEN 'R'
        WHEN primary_type = '{config.b_type}' THEN 'B'
        WHEN primary_type = '{config.m_type}' THEN 'M'
        ELSE 'Z'
    END AS etype,
    lon,
    lat
FROM base
"""


def _batch_range(batch_sizes: Sequence[int], batch_idx: int) -> tuple[int, int]:
    start = 1 + sum(batch_sizes[: batch_idx - 1])
    end = sum(batch_sizes[:batch_idx])
    return start, end


def normalize_query_batches(
    query_batches: Collection[int] | None,
    batch_count: int,
) -> tuple[int, ...]:
    """Normalize 1-based query batches for benchmark SQL rendering."""

    normalized = _normalize_core_query_batches(query_batches, batch_count)
    if normalized is None:
        return tuple(range(1, batch_count + 1))
    return tuple(sorted(normalized))


def _correctness_sql(
    *,
    strategy_id: str,
    batch: int,
    baseline_agg_table: str,
    results_table: str,
    result_key_columns: Sequence[str],
    dialect: SqlDialect = SqlDialect.TRINO,
) -> str:
    columns = ", ".join(result_key_columns)
    group_by = ", ".join(str(idx) for idx in range(1, len(result_key_columns) + 1))
    return (
        f"INSERT INTO {results_table}\n"
        "WITH strategy_result AS (\n"
        "    SELECT * FROM result\n"
        "),\n"
        "strat_agg AS (\n"
            f"    SELECT {columns}, count(*) AS c\n"
            "    FROM strategy_result\n"
            f"    GROUP BY {group_by}\n"
        ")\n"
        f"SELECT '{strategy_id}', {batch}, 'eimer_row_count', count(*)\n"
        "FROM strategy_result\n"
        "UNION ALL\n"
        f"SELECT '{strategy_id}', {batch}, 'baseline_row_count', COALESCE(sum(c), 0)\n"
        f"FROM {baseline_agg_table}\n"
        f"WHERE batch = {batch}\n"
        "UNION ALL\n"
        f"SELECT '{strategy_id}', {batch}, 'diff_strat_minus_base', count(*)\n"
        "FROM (\n"
        "    SELECT * FROM strat_agg\n"
        f"    {set_diff_keyword(dialect)}\n"
        f"    SELECT {columns}, c\n"
        f"    FROM {baseline_agg_table}\n"
        f"    WHERE batch = {batch}\n"
        ") d\n"
        "UNION ALL\n"
        f"SELECT '{strategy_id}', {batch}, 'diff_base_minus_strat', count(*)\n"
        "FROM (\n"
        f"    SELECT {columns}, c\n"
        f"    FROM {baseline_agg_table}\n"
        f"    WHERE batch = {batch}\n"
        f"    {set_diff_keyword(dialect)}\n"
        "    SELECT * FROM strat_agg\n"
        ") d\n"
    )


def render_benchmark_sql(
    *,
    config: CanonicalDependentConfig,
    strategies: Sequence[BenchmarkStrategy],
    initial_history: int,
    num_updates: int,
    update_size: int,
    scale_factor: float | None,
    include_correctness_checks: bool,
    mr_postprocessing: bool,
    selectivity_r: float | None,
    selectivity_b: float | None,
    selectivity_m: float | None,
    cost_model_validation: bool = False,
    watermark_table: str = "watermark_state",
    events_table: str = "events",
    batch_table: str = "events_batch",
    generated_events_table: str = "generated_events",
    results_table: str = "bench_results",
    query_batches: Collection[int] | None = None,
    query_spec: QuerySpec | None = None,
    query_spec_fingerprint: str | None = None,
    workload_profile: str = DEFAULT_WORKLOAD_PROFILE,
    workload_seed: int = DEFAULT_WORKLOAD_SEED,
    external_events_fingerprint: str | None = None,
    dialect: SqlDialect = SqlDialect.TRINO,
    pin_join_order: bool = True,
    batch_sizes: Sequence[int] | None = None,
) -> str:
    if not strategies:
        raise ValueError("At least one strategy must be selected")
    validate_workload_profile(workload_profile)
    query_spec = query_spec or canonical_query_spec(config)
    result_key_columns = query_spec_result_key_columns(query_spec)
    result_column_types = query_spec_result_column_types(query_spec)

    _validate_benchmark_params(
        initial_history=initial_history,
        num_updates=num_updates,
        update_size=update_size,
        scale_factor=scale_factor,
        selectivity_r=selectivity_r,
        selectivity_b=selectivity_b,
        selectivity_m=selectivity_m,
    )

    # batch_sizes gives arbitrary per-batch sizes; the scalar flags are the uniform/geometric shorthand for it. Either way the body below is list-driven.
    if batch_sizes is not None:
        batch_sizes = [int(size) for size in batch_sizes]
        if not batch_sizes or any(size <= 0 for size in batch_sizes):
            raise ValueError("batch_sizes must be a non-empty sequence of positive ints")
    else:
        batch_sizes = _batch_sizes(
            initial_history=initial_history,
            num_updates=num_updates,
            update_size=update_size,
            scale_factor=scale_factor,
        )
    total_events = sum(batch_sizes)
    normalized_query_batches = normalize_query_batches(query_batches, len(batch_sizes))
    query_batch_set = set(normalized_query_batches)

    lines: list[str] = []
    lines.append("-- Trino benchmark SQL generated by execution/cli.py")
    lines.append(f"-- QUERY_SPEC      = {query_spec.name}")
    if query_spec_fingerprint:
        lines.append(f"-- QUERY_SPEC_FINGERPRINT = {query_spec_fingerprint}")
    lines.append(f"-- INITIAL_HISTORY = {initial_history}")
    lines.append(f"-- NUM_UPDATES     = {num_updates}")
    lines.append(f"-- UPDATE_SIZE     = {update_size}")
    lines.append(f"-- BATCH_SIZES     = {batch_sizes}")
    if scale_factor is not None:
        lines.append(f"-- SCALE_FACTOR    = {scale_factor}")
    if all(v is not None for v in (selectivity_r, selectivity_b, selectivity_m)):
        lines.append(f"-- SELECTIVITY_R   = {selectivity_r}")
        lines.append(f"-- SELECTIVITY_B   = {selectivity_b}")
        lines.append(f"-- SELECTIVITY_M   = {selectivity_m}")
    lines.append(f"-- TOTAL_EVENTS    = {total_events}")
    if workload_profile != DEFAULT_WORKLOAD_PROFILE or workload_seed != DEFAULT_WORKLOAD_SEED:
        lines.append(f"-- WORKLOAD_PROFILE = {workload_profile}")
        lines.append(f"-- WORKLOAD_SEED    = {workload_seed}")
    lines.append(f"-- STRATEGIES      = {', '.join(s.strategy_id for s in strategies)}")
    lines.append(f"-- MR_POSTPROCESS  = {mr_postprocessing}")
    lines.append(f"-- COST_MODEL_VALIDATION = {cost_model_validation}")
    if query_batches is None:
        lines.append("-- QUERY_BATCHES  = all")
    else:
        lines.append("-- QUERY_BATCHES  = " + (",".join(str(batch) for batch in normalized_query_batches) or "none"))
    lines.append("")

    lines.append(f"DROP TABLE IF EXISTS {results_table};")
    if external_events_fingerprint is None:
        lines.append(f"DROP TABLE IF EXISTS {generated_events_table};")
        # Invalidate any external-load marker: a stale generated_events_meta would let a later external run skip its load and benchmark the wrong dataset.
        lines.append(f"DROP TABLE IF EXISTS {generated_events_table}_meta;")
    lines.append("")
    # ,,rows'' is a reserved word in some SQL dialects but a legal Trino column name -> the backend decides quoting.
    rows_col = backend_for(dialect).rows_column_name()
    lines.append(
        f"CREATE TABLE {results_table} (\n"
        "    strategy VARCHAR,\n"
        "    batch INTEGER,\n"
        "    phase VARCHAR,\n"
        f"    {rows_col} BIGINT\n"
        ");"
    )
    lines.append("")

    if external_events_fingerprint is None:
        generated_events_sql = _generated_events_sql(
            config=config,
            generated_events_table=generated_events_table,
            total_events=total_events,
            selectivity_r=selectivity_r,
            selectivity_b=selectivity_b,
            selectivity_m=selectivity_m,
            workload_profile=workload_profile,
            workload_seed=workload_seed,
            dialect=dialect,
        )
        lines.append(strip_trailing_semicolon(generated_events_sql) + ";")
        lines.append("")
    else:
        # Table is preloaded by execution/load_external_events.py; fail fast if it does not match the manifest so a stale table cannot silently benchmark the wrong dataset.
        # (The comment format must not match the runner's ,,-- Batch <digits>'' / ,,-- Strategy <token>'' context markers.)
        lines.append(f"-- EXTERNAL_EVENTS manifest_fingerprint={external_events_fingerprint}")
        lines.append(
            "SELECT IF(count(*) = 1, 1, CAST(fail("
            f"'{generated_events_table} does not match external manifest "
            f"{external_events_fingerprint} with {total_events} rows; rerun load_external_events.py'"
            ") AS INTEGER))\n"
            f"FROM {generated_events_table}_meta\n"
            f"WHERE manifest_fingerprint = '{external_events_fingerprint}' AND total_rows = {total_events}\n"
            f"  AND total_rows = (SELECT count(*) FROM {generated_events_table});"
        )
        lines.append("")

    baseline_events_table = "events_baseline"
    baseline_agg_table = "baseline_result_agg"

    if include_correctness_checks and normalized_query_batches:
        baseline_sql = strip_trailing_semicolon(
            query_spec_to_match_recognize_sql(query_spec, events_table=baseline_events_table)
        )

        lines.append("-- ============================================================")
        lines.append("-- Baseline Precompute For Correctness (once per batch)")
        lines.append("-- ============================================================")
        lines.append("")
        lines.append(f"DROP TABLE IF EXISTS {baseline_agg_table};")
        lines.append(f"DROP TABLE IF EXISTS {baseline_events_table};")
        lines.append(
            f"CREATE TABLE {baseline_agg_table} (\n"
            "    batch INTEGER,\n"
            + ",\n".join(
                f"    {column} {result_column_types[column]}" for column in result_key_columns
            )
            + ",\n"
            "    c BIGINT\n"
            ");"
        )
        lines.append(
            f"CREATE TABLE {baseline_events_table} AS "
            f"SELECT * FROM {generated_events_table} WHERE 1 = 0;"
        )
        lines.append("")

        for batch in range(1, len(batch_sizes) + 1):
            start, end = _batch_range(batch_sizes, batch)
            # Standard "-- Batch <k>" marker so run_benchmark.sh tracks baseline precompute under the right batch number.
            lines.append(f"-- Batch {batch} (baseline load)")
            lines.append(
                f"INSERT INTO {baseline_events_table} SELECT * FROM {generated_events_table} "
                f"WHERE id BETWEEN {start} AND {end};"
            )

            if batch not in query_batch_set:
                lines.append("")
                continue

            lines.append(f"-- Batch {batch} (baseline correctness)")
            lines.append(f"INSERT INTO {baseline_agg_table}")
            lines.append("WITH baseline_result AS (")
            lines.append(indent_sql(baseline_sql, 4))
            lines.append(")")
            lines.append(
                "SELECT "
                f"{batch} AS batch, "
                f"{', '.join(result_key_columns)}, count(*) AS c"
            )
            lines.append("FROM baseline_result")
            lines.append(
                "GROUP BY "
                + ", ".join(str(idx) for idx in range(2, len(result_key_columns) + 2))
                + ";"
            )
            lines.append("")

    for strategy in strategies:
        lines.append("-- ============================================================")
        lines.append(f"-- Strategy {strategy.strategy_id}")
        lines.append(f"-- {strategy.description}")
        lines.append("-- ============================================================")
        lines.append("")

        lines.append("DROP TABLE IF EXISTS result;")
        lines.append("DROP TABLE IF EXISTS composed;")

        created_cache_tables = [
            _extract_created_table_name(ddl)
            for ddl in strategy.cache_schemas
        ]
        for table_name in created_cache_tables:
            lines.append(f"DROP TABLE IF EXISTS {table_name};")

        lines.append(f"DROP TABLE IF EXISTS {batch_table};")
        lines.append(f"DROP TABLE IF EXISTS {events_table};")
        lines.append(f"DROP TABLE IF EXISTS {watermark_table};")
        lines.append("")

        lines.append(f"CREATE TABLE {events_table} AS SELECT * FROM {generated_events_table} WHERE 1 = 0;")
        lines.append(f"CREATE TABLE {batch_table} AS SELECT * FROM {generated_events_table} WHERE 1 = 0;")
        lines.append(f"CREATE TABLE {watermark_table} (watermark TIMESTAMP(6));")
        lines.append(
            f"INSERT INTO {watermark_table} VALUES (TIMESTAMP '1970-01-01 00:00:00.000000');"
        )

        for ddl in strategy.cache_schemas:
            lines.append(strip_trailing_semicolon(ddl) + ";")

        lines.append("")

        for batch in range(1, len(batch_sizes) + 1):
            start, end = _batch_range(batch_sizes, batch)
            lines.append(f"-- Batch {batch}")
            lines.append(
                f"INSERT INTO {events_table} SELECT * FROM {generated_events_table} "
                f"WHERE id BETWEEN {start} AND {end};"
            )
            lines.append(f"DROP TABLE IF EXISTS {batch_table};")
            lines.append(
                f"CREATE TABLE {batch_table} AS SELECT * FROM {generated_events_table} "
                f"WHERE id BETWEEN {start} AND {end};"
            )

            if cost_model_validation:
                if len(strategy.cache_statement_op_ids) != len(strategy.cache_updates):
                    raise ValueError(
                        f"Strategy {strategy.strategy_id} is missing aligned cache statement op ids for validation mode"
                    )
                lines.append(PRE_BATCH_MARKER)

            for idx, update_sql in enumerate(strategy.cache_updates):
                rendered = replace_watermark_placeholder(
                    strip_trailing_semicolon(update_sql),
                    watermark_table,
                )
                if pin_join_order:  # per-statement join-order pin where the dialect needs one (no-op on Trino)
                    rendered = ordered_join_hint(rendered, dialect)
                if cost_model_validation:
                    rendered = prepend_op_id_marker(rendered, strategy.cache_statement_op_ids[idx])
                lines.append(rendered + ";")

            if batch in query_batch_set:
                lines.append("DROP TABLE IF EXISTS composed;")
                if cost_model_validation:
                    if not strategy.compose_op_id:
                        raise ValueError(
                            f"Strategy {strategy.strategy_id} is missing a compose op id for validation mode"
                        )
                    lines.append(f"-- op_id: {strategy.compose_op_id}")
                lines.append("CREATE TABLE composed AS")
                _compose_sql = strip_trailing_semicolon(strategy.compose_sql)
                if pin_join_order:
                    _compose_sql = ordered_join_hint(_compose_sql, dialect)
                lines.append(indent_sql(_compose_sql, 4))
                lines.append(";")

                lines.append("DROP TABLE IF EXISTS result;")
                if cost_model_validation:
                    if not strategy.post_filter_op_id:
                        raise ValueError(
                            f"Strategy {strategy.strategy_id} is missing a post-filter op id for validation mode"
                        )
                    lines.append(f"-- op_id: {strategy.post_filter_op_id}")
                lines.append("CREATE TABLE result AS")
                lines.append(indent_sql(strip_trailing_semicolon(strategy.path_a_sql), 4))
                lines.append(";")
                lines.append(
                    f"INSERT INTO {results_table} SELECT '{strategy.strategy_id}', {batch}, "
                    "'result_path_a', count(*) FROM result;"
                )

                if include_correctness_checks and strategy.strategy_id != "S0":
                    lines.append(
                        _correctness_sql(
                            strategy_id=strategy.strategy_id,
                            batch=batch,
                            baseline_agg_table=baseline_agg_table,
                            results_table=results_table,
                            result_key_columns=result_key_columns,
                            dialect=dialect,
                        ).rstrip()
                        + ";"
                    )

                if mr_postprocessing:
                    lines.append("DROP TABLE IF EXISTS result;")
                    lines.append("CREATE TABLE result AS")
                    lines.append(indent_sql(strip_trailing_semicolon(strategy.path_b_sql), 4))
                    lines.append(";")
                    lines.append(
                        f"INSERT INTO {results_table} SELECT '{strategy.strategy_id}', {batch}, "
                        "'result_path_b', count(*) FROM result;"
                    )

                    if include_correctness_checks and strategy.strategy_id != "S0":
                        lines.append(
                            _correctness_sql(
                                strategy_id=strategy.strategy_id,
                                batch=batch,
                                baseline_agg_table=baseline_agg_table,
                                results_table=results_table,
                                result_key_columns=result_key_columns,
                                dialect=dialect,
                            ).rstrip()
                            + ";"
                        )

            lines.append(f"INSERT INTO {watermark_table} SELECT max(time) FROM {events_table};")
            lines.append("")

    lines.append(f"SELECT * FROM {results_table} ORDER BY strategy, batch, phase;")

    # Emit-time dialect rewrites over the assembled SQL (per-strategy SQL is pre-rendered Trino, so rewrites land here). TRINO = no-op.
    return rewrite_sql("\n".join(lines).rstrip() + "\n", dialect)
