"""Load an external dataset (datagen manifest + parquet) into Trino.

Loads into a staging table via chunked INSERT ... VALUES (statements are
character-budgeted: Trino's query.max-length defaults to 1,000,000 chars),
then finalizes with a single CTAS so the benchmark table has one snapshot and
engine-sized files (hundreds of small INSERT files would confound scan costs
vs the CTAS-built corpus). Idempotent: a matching fingerprint in
generated_events_meta skips the load unless --force.

Literal fidelity: timestamps as TIMESTAMP '... .ffffff' (microseconds), floats
as typed DOUBLE '<repr>' literals; bare decimals would parse as DECIMAL and
the DECIMAL->DOUBLE coercion is not guaranteed correctly rounded.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Iterable, Sequence

import bootstrap  # noqa: F401

from trino_client import TrinoClient, TrinoClientError, TrinoConnectionConfig

EVENTS_TABLE = "generated_events"
META_TABLE = "generated_events_meta"
STAGING_TABLE = "generated_events_staging"
DEFAULT_CHUNK_CHARS = 700_000

_DDL_COLUMNS = (
    "id BIGINT, time TIMESTAMP(6), ts TIMESTAMP(6), "
    "primary_type VARCHAR, etype VARCHAR, lon DOUBLE, lat DOUBLE"
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--trino-server", default=None)
    parser.add_argument("--trino-catalog", default=None)
    parser.add_argument("--trino-schema", default=None)
    parser.add_argument("--trino-user", default=None)
    parser.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS)
    parser.add_argument("--force", action="store_true", help="reload even if the fingerprint already matches")
    return parser.parse_args(argv)


def _escape(value: str) -> str:
    return value.replace("'", "''")


def render_row(row: tuple) -> str:
    """One VALUES tuple for an event row (id, time, ts, primary_type, etype, lon, lat)."""
    id_value, time_value, ts_value, primary_type, etype, lon, lat = row
    time_literal = time_value.strftime("%Y-%m-%d %H:%M:%S.%f")
    ts_literal = ts_value.strftime("%Y-%m-%d %H:%M:%S.%f")
    return (
        f"({id_value}, TIMESTAMP '{time_literal}', TIMESTAMP '{ts_literal}', "
        f"'{_escape(primary_type)}', '{_escape(etype)}', DOUBLE '{lon!r}', DOUBLE '{lat!r}')"
    )


def iter_insert_statements(rows: Iterable[tuple], *, table: str, chunk_chars: int) -> Iterable[str]:
    prefix = f"INSERT INTO {table} VALUES "
    pending: list[str] = []
    pending_chars = len(prefix)
    for row in rows:
        rendered = render_row(row)
        if pending and pending_chars + len(rendered) + 2 > chunk_chars:
            yield prefix + ", ".join(pending)
            pending = []
            pending_chars = len(prefix)
        pending.append(rendered)
        pending_chars += len(rendered) + 2
    if pending:
        yield prefix + ", ".join(pending)


def _existing_fingerprint(client: TrinoClient) -> tuple[str, int] | None:
    """(fingerprint, total_rows) from the meta marker, but only if the events
    table itself still matches: an in-Trino CTAS run drops the meta marker,
    yet a half-torn state must not let the fast path skip a needed reload."""
    try:
        _, rows = client.execute(f"SELECT manifest_fingerprint, total_rows FROM {META_TABLE}")
    except TrinoClientError:
        return None
    if len(rows) != 1:
        return None
    fingerprint, total_rows = str(rows[0][0]), int(rows[0][1])
    try:
        _, count_rows = client.execute(f"SELECT count(*) FROM {EVENTS_TABLE}")
    except TrinoClientError:
        return None
    if int(count_rows[0][0]) != total_rows:
        return None
    return fingerprint, total_rows


def load(manifest_path: Path, client: TrinoClient, *, chunk_chars: int, force: bool) -> dict:
    # Heavy deps only when actually loading.
    from workload.data.datagen.writer import content_fingerprint, load_manifest, read_dataset_frames
    import pandas as pd

    manifest = load_manifest(manifest_path)
    fingerprint = manifest["fingerprint"]
    total_rows = int(manifest["total_rows"])

    existing = _existing_fingerprint(client)
    if existing == (fingerprint, total_rows) and not force:
        return {"skipped": True, "fingerprint": fingerprint, "total_rows": total_rows}

    frames = [frame for _, frame in read_dataset_frames(manifest_path)]
    full = pd.concat(frames, ignore_index=True)
    actual_fingerprint = content_fingerprint(full)
    if actual_fingerprint != fingerprint:
        raise ValueError(
            f"parquet contents do not match the manifest fingerprint "
            f"(manifest {fingerprint}, files {actual_fingerprint})"
        )
    if len(full) != total_rows:
        raise ValueError(f"parquet row count {len(full)} != manifest total_rows {total_rows}")

    started = time.monotonic()
    client.execute(f"DROP TABLE IF EXISTS {STAGING_TABLE}")
    client.execute(f"DROP TABLE IF EXISTS {EVENTS_TABLE}")
    client.execute(f"DROP TABLE IF EXISTS {META_TABLE}")
    client.execute(f"CREATE TABLE {STAGING_TABLE} ({_DDL_COLUMNS})")

    statements = 0
    for statement in iter_insert_statements(
        full.itertuples(index=False, name=None), table=STAGING_TABLE, chunk_chars=chunk_chars
    ):
        if len(statement) > 1_000_000:
            raise ValueError(
                f"rendered INSERT statement is {len(statement)} chars (> Trino query.max-length); "
                "lower --chunk-chars"
            )
        client.execute(statement)
        statements += 1

    client.execute(f"CREATE TABLE {EVENTS_TABLE} AS SELECT * FROM {STAGING_TABLE}")
    client.execute(f"DROP TABLE {STAGING_TABLE}")

    _, rows = client.execute(
        f"SELECT count(*), count(DISTINCT id), min(id), max(id) FROM {EVENTS_TABLE}"
    )
    count, distinct, min_id, max_id = (int(value) for value in rows[0])
    if not (count == distinct == max_id == total_rows and min_id == 1):
        raise ValueError(
            f"loaded table failed the contiguity guard: count={count}, distinct={distinct}, "
            f"min={min_id}, max={max_id}, expected exactly ids 1..{total_rows}"
        )

    client.execute(f"CREATE TABLE {META_TABLE} (manifest_fingerprint VARCHAR, total_rows BIGINT)")
    client.execute(f"INSERT INTO {META_TABLE} VALUES ('{_escape(fingerprint)}', {total_rows})")

    return {
        "skipped": False,
        "fingerprint": fingerprint,
        "total_rows": total_rows,
        "insert_statements": statements,
        "seconds": round(time.monotonic() - started, 2),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config = TrinoConnectionConfig.from_env(
        server=args.trino_server,
        catalog=args.trino_catalog,
        schema=args.trino_schema,
        user=args.trino_user,
        source="external_events_loader",
    )
    client = TrinoClient(config)
    result = load(args.manifest, client, chunk_chars=args.chunk_chars, force=args.force)
    if result["skipped"]:
        print(
            f"generated_events already holds manifest {result['fingerprint'][:16]}… "
            f"({result['total_rows']} rows); skipping (use --force to reload)"
        )
    else:
        print(
            f"loaded {result['total_rows']} rows into {config.catalog}.{config.schema}.{EVENTS_TABLE} "
            f"({result['insert_statements']} insert statements, {result['seconds']}s)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
