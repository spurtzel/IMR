"""DuckDB runtime layer for the demo experiment drivers (the DuckDB twin of trino_exec).

Loads a synthetic datagen frame into a DuckDB table and runs the two arms:
  * baseline_streaming -- a caller-supplied stateless baseline (the SQL a
                          MATCH_RECOGNIZE transpiler generated for the query; DuckDB
                          has no native MATCH_RECOGNIZE), re-executed over the
                          cumulative window per queried batch.
  * eimer_streaming    -- an EIMER cover, the shared per-batch protocol of
                          demo/eimer_arm.py, rendered for the DuckDB dialect.

Measurement discipline pinned per connection in ,,connect'': single-threaded
(SET threads = 1), join order pinned (SET disabled_optimizers = 'join_order': the
cost-model-selected join order runs verbatim), and a client-side per-statement wall.
No index structures exist on any table. Every arm runs an untimed warm-up pass
(identical statements, result discarded) before the timed pass.

Both arms return full-tuple key sets on the final batch.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for _p in (str(REPO), str(REPO / "execution")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from duckdb_client import DuckDBClient, DuckDBClientError, DuckDBConnectionConfig, is_dnf  # noqa: E402
from eimer.sql.backend import SqlDialect  # noqa: E402
import eimer_arm  # noqa: E402
from eimer_arm import b_unified_workload, build_cover_plan  # noqa: E402,F401  (re-exported)

# Loadable column types; ekey (BIGINT equi key, DatagenConfig.equi) is optional and
# frame-driven: present in the frame -> present in the table.
_COL_TYPES = {"id": "BIGINT", "time": "TIMESTAMP(6)", "ts": "TIMESTAMP(6)",
              "primary_type": "VARCHAR", "etype": "VARCHAR", "lon": "DOUBLE",
              "lat": "DOUBLE", "ekey": "BIGINT"}
# the relation name a baseline SQL must read: the arm re-points it at each cumulative window
BASELINE_INPUT_VIEW = "events_window"


class DuckDBEngine(eimer_arm.Engine):
    dialect = SqlDialect.DUCKDB
    error_type = DuckDBClientError

    def set_wall(self, seconds: float) -> None:
        self.client.set_wall(seconds)

    def is_dnf(self, err) -> bool:
        return is_dnf(err)


def connect(database: str = ":memory:", threads: int = 1, timeout_s: int = 1800,
            memory_limit: str | None = None, pin_join_order: bool = True) -> DuckDBClient:
    """Open a DuckDB client and pin the measurement discipline: single-threaded, join
    order pinned, per-statement wall T (client-side interrupt), optionally a memory limit
    (a statement outgrowing it is a DNF, not a killed process). ,,pin_join_order=False''
    leaves DuckDB's join-order optimizer on (the emitted nesting is then advisory)."""
    client = DuckDBClient(DuckDBConnectionConfig(database=database, threads=threads, memory_limit=memory_limit,
                                                 pin_join_order=pin_join_order))
    client.set_wall(timeout_s)
    return client


def drop_table(client, table: str) -> None:
    try:
        client.execute(f"DROP TABLE IF EXISTS {table}")
    except DuckDBClientError:
        pass


def load_frame(client, frame, table: str = "events") -> int:
    """CREATE TABLE AS from the in-memory frame (DuckDB reads pandas frames natively, so
    no literal rendering: bit-exact doubles and microsecond timestamps); the column set
    is frame-driven (the 7 canonical columns, plus ekey when the equi predicate is on).
    Returns the loaded row count (callers assert it equals len(frame))."""
    cols = [c for c in frame.columns if c in _COL_TYPES]
    drop_table(client, table)
    client.register_frame("_eimer_load_frame", frame[cols])
    try:
        client.execute(f"CREATE TABLE {table} AS SELECT "
                       + ", ".join(f"CAST({c} AS {_COL_TYPES[c]}) AS {c}" for c in cols)
                       + " FROM _eimer_load_frame")
    finally:
        client.unregister_frame("_eimer_load_frame")
    _, rows = client.execute(f"SELECT count(*) FROM {table}")
    return int(rows[0][0])


def baseline_streaming(client, baseline_sql: str, table, N, B, qb, key_cols, T, warmup=True,
                       total_wall_s: float | None = None):
    """Stateless baseline arm: ,,baseline_sql'' (a SELECT over the relation
    ,,events_window'', e.g. the transpiled MATCH_RECOGNIZE of the query) is re-executed
    over the cumulative window id <= (b*N)//B on each queried batch (count(*) forces
    full evaluation); the final batch pulls the full-tuple keys for the identity gate.
    The wall T applies PER RE-EXECUTION; ,,total_wall_s'' (optional) bounds the arm's timed
    total the same way (a DNF once the re-executions so far exceed it). With warmup (the
    default), the whole workload first runs once untimed. Returns (total_ms, final_count,
    final_tuple_set, meta), or (None, None, None, meta) on a wall DNF."""
    if warmup:
        warm = baseline_streaming(client, baseline_sql, table, N, B, qb, key_cols, T, warmup=False,
                                  total_wall_s=total_wall_s)
        if warm[0] is None:
            return warm
        return baseline_streaming(client, baseline_sql, table, N, B, qb, key_cols, T, warmup=False,
                                  total_wall_s=total_wall_s)
    sql = baseline_sql.strip().rstrip(";")
    tot = 0.0
    final = None
    tuples = None
    per_scan = []  # (window_rows, ms) per re-execution
    qbs = sorted(qb)
    keysel = ", ".join(c.lower() for c in key_cols)
    client.set_wall(T)
    try:
        for b in qbs:
            win_hi = (b * N) // B
            client.execute(f"CREATE OR REPLACE VIEW {BASELINE_INPUT_VIEW} AS "
                           f"SELECT * FROM {table} WHERE id <= {win_hi}")
            last = (b == qbs[-1])
            t0 = time.perf_counter()
            try:
                _, rows = client.execute(f"SELECT {keysel if last else 'count(*)'} FROM ({sql})")
            except DuckDBClientError as e:
                if is_dnf(e):
                    return None, None, None, dict(
                        spent_ms=round(tot + (time.perf_counter() - t0) * 1000.0, 1),
                        per_scan_ms=per_scan)
                raise
            ms = (time.perf_counter() - t0) * 1000.0
            tot += ms
            per_scan.append([win_hi, round(ms, 1)])
            if last:
                tuples = frozenset(tuple(int(x) for x in r) for r in rows)
                final = len(rows)
            else:
                final = int(rows[0][0]) if rows else 0
            if total_wall_s is not None and tot > total_wall_s * 1000.0:
                return None, None, None, dict(spent_ms=round(tot, 1), per_scan_ms=per_scan)
    finally:
        client.execute(f"DROP VIEW IF EXISTS {BASELINE_INPUT_VIEW}")
    return round(tot, 1), final, tuples, dict(spent_ms=round(tot, 1), per_scan_ms=per_scan)


def eimer_streaming(client, dep, toks, all_vars, wl, spec, table, N, B, qb, key_cols, T,
                    measure_state: bool = False, warmup: bool = True, total_wall_s: float | None = None):
    """the shared EIMER arm (demo/eimer_arm.py) on this DuckDB client."""
    return eimer_arm.eimer_streaming(DuckDBEngine(client), dep, toks, all_vars, wl, spec, table,
                                     N, B, qb, key_cols, T, measure_state=measure_state, warmup=warmup,
                                     total_wall_s=total_wall_s)
