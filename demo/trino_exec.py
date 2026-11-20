"""Trino runtime layer for the demo experiment drivers.

Loads a synthetic datagen frame into a Trino table and runs the two arms:
  * mr_streaming    -- native MATCH_RECOGNIZE, re-scan of the cumulative window
                       per queried batch (the baseline).
  * eimer_streaming -- an EIMER cover: per-view delta updates plus a dedup'd
                       compose on queried batches (both TIMED); ingestion into the
                       staging tables is untimed by convention. The arm body is the
                       engine-neutral demo/eimer_arm.py (shared with duckdb_exec).

Measurement discipline pinned per session in ,,connect'': join reordering OFF (the
cost-model-selected join order runs verbatim), intra-query parallelism OFF, and a
server-side per-query wall. No index structures exist on any table. Every arm runs
an untimed warm-up pass (identical statements, result discarded) before the timed
pass, so no arm pays the engine's cold start.

Both arms return full-tuple key sets on the final batch for the multiset identity
gate (EIMER result == native MATCH_RECOGNIZE).
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

from trino_client import TrinoClient, TrinoClientError, TrinoConnectionConfig  # noqa: E402
from eimer.query.query_spec import query_spec_to_match_recognize_sql  # noqa: E402
from eimer.sql.sql_dialect import SqlDialect  # noqa: E402
import eimer_arm  # noqa: E402
from eimer_arm import b_unified_workload, build_cover_plan  # noqa: E402,F401  (re-exported)

EVENT_COLS = ("id", "time", "ts", "primary_type", "etype", "lon", "lat")
# Loadable column types; ekey (BIGINT equi key, DatagenConfig.equi) is optional and
# frame-driven: present in the frame -> present in the table.
_COL_TYPES = {"id": "BIGINT", "time": "TIMESTAMP(6)", "ts": "TIMESTAMP(6)",
              "primary_type": "VARCHAR", "etype": "VARCHAR", "lon": "DOUBLE",
              "lat": "DOUBLE", "ekey": "BIGINT"}
# Trino error markers that mean "the query hit the wall / blew its budget" -> a
# feasibility DNF (did-not-finish), not a bug.
_DNF_TOKENS = ("EXCEEDED_TIME_LIMIT", "exceeded the maximum execution time",
               "query_max_run_time", "EXCEEDED_GLOBAL_MEMORY_LIMIT",
               "EXCEEDED_LOCAL_MEMORY_LIMIT", "Query exceeded",
               "CLUSTER_OUT_OF_MEMORY", "EXCEEDED_SPILL_LIMIT", "EXCEEDED_TIME")


def connect(server: str = "localhost:8080", catalog: str = "memory",
            schema: str = "default", user: str = "trino", timeout_s: int = 1800):
    """Open a Trino client and pin the measurement discipline: per-query wall (T),
    join reordering OFF, intra-query parallelism OFF. The HTTP read timeout is set
    ABOVE T so the server-side kill (a DNF) is observed rather than the HTTP socket
    timing out first."""
    cfg = TrinoConnectionConfig(server=server, catalog=catalog, schema=schema,
                                user=user, timeout_seconds=float(timeout_s) + 300.0)
    client = TrinoClient(cfg)
    client.execute(f"SET SESSION query_max_run_time = '{int(timeout_s)}s'")
    client.execute("SET SESSION join_reordering_strategy = 'NONE'")
    client.execute("SET SESSION task_concurrency = 1")
    return client


def is_dnf(err) -> bool:
    s = str(err)
    return any(tok in s for tok in _DNF_TOKENS)


def _render_row(t) -> str:
    # t = (id, time, ts, primary_type, etype, lon, lat[, ekey]); bit-exact doubles
    # via !r, microsecond timestamps to preserve the tie-free grid.
    tm = t[1].strftime("%Y-%m-%d %H:%M:%S.%f")
    ts = t[2].strftime("%Y-%m-%d %H:%M:%S.%f")
    pe = str(t[3]).replace("'", "''")
    ee = str(t[4]).replace("'", "''")
    row = (f"({int(t[0])}, TIMESTAMP '{tm}', TIMESTAMP '{ts}', "
           f"'{pe}', '{ee}', DOUBLE '{float(t[5])!r}', DOUBLE '{float(t[6])!r}'")
    if len(t) > 7:
        row += f", {int(t[7])}"
    return row + ")"


def drop_table(client, table: str) -> None:
    try:
        client.execute(f"DROP TABLE IF EXISTS {table}")
    except TrinoClientError:
        pass


def load_frame(client, frame, table: str = "events", chunk_chars: int = 700_000) -> int:
    """CREATE + chunked INSERT the synthetic frame into ,,table''; the column set is
    frame-driven (the 7 canonical columns, plus ekey when the equi predicate is on).
    Returns the loaded row count (callers assert it equals len(frame))."""
    cols = [c for c in frame.columns if c in _COL_TYPES]
    drop_table(client, table)
    client.execute(f"CREATE TABLE {table} ("
                   + ", ".join(f"{c} {_COL_TYPES[c]}" for c in cols) + ")")
    prefix = f"INSERT INTO {table} VALUES "
    pend, nchar = [], len(prefix)
    for r in frame[cols].itertuples(index=False, name=None):
        lit = _render_row(r)
        if pend and nchar + len(lit) + 2 > chunk_chars:
            client.execute(prefix + ", ".join(pend))
            pend, nchar = [], len(prefix)
        pend.append(lit)
        nchar += len(lit) + 2
    if pend:
        client.execute(prefix + ", ".join(pend))
    _, rows = client.execute(f"SELECT count(*) FROM {table}")
    return int(rows[0][0])


def mr_streaming(client, spec, table, N, B, qb, key_cols, T, warmup=True):
    """Native MATCH_RECOGNIZE arm: re-scan the cumulative window id <= (b*N)//B on
    each queried batch (count(*) forces full evaluation); the final batch pulls the
    full-tuple keys for the identity gate. The wall T applies PER RE-SCAN. With
    warmup (the default), the whole workload first runs once untimed. Returns
    (total_ms, final_count, final_tuple_set, meta), or (None, None, None, meta) on a
    wall/memory DNF."""
    if warmup:
        warm = mr_streaming(client, spec, table, N, B, qb, key_cols, T, warmup=False)
        if warm[0] is None:  # a warm-up DNF: the timed pass would DNF too
            return warm
        return mr_streaming(client, spec, table, N, B, qb, key_cols, T, warmup=False)
    tot = 0.0
    final = None
    tuples = None
    per_scan = []  # (window_rows, ms) per re-scan
    qbs = sorted(qb)
    keysel = ", ".join(c.lower() for c in key_cols)
    client.execute(f"SET SESSION query_max_run_time = '{int(T)}s'")
    for b in qbs:
        win_hi = (b * N) // B
        win = f"(SELECT * FROM {table} WHERE id <= {win_hi})"
        mr = query_spec_to_match_recognize_sql(spec, events_table=win).rstrip().rstrip(";")
        last = (b == qbs[-1])
        t0 = time.perf_counter()
        try:
            _, rows = client.execute(f"SELECT {keysel if last else 'count(*)'} FROM ({mr})")
        except TrinoClientError as e:
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
    return round(tot, 1), final, tuples, dict(spent_ms=round(tot, 1), per_scan_ms=per_scan)


class TrinoEngine(eimer_arm.Engine):
    dialect = SqlDialect.TRINO
    error_type = TrinoClientError

    def set_wall(self, seconds: float) -> None:
        self.client.execute(f"SET SESSION query_max_run_time = '{int(seconds)}s'")

    def is_dnf(self, err) -> bool:
        return is_dnf(err)


def eimer_streaming(client, dep, toks, all_vars, wl, spec, table, N, B, qb, key_cols, T,
                    measure_state: bool = False, warmup: bool = True):
    """the shared EIMER arm (demo/eimer_arm.py) on this Trino client: full per-batch
    protocol, ingestion untimed, delta updates + dedup'd compose timed, full-tuple keys
    on the final batch; measure_state snapshots the resident state untimed."""
    return eimer_arm.eimer_streaming(TrinoEngine(client), dep, toks, all_vars, wl, spec, table,
                                     N, B, qb, key_cols, T, measure_state=measure_state, warmup=warmup)
