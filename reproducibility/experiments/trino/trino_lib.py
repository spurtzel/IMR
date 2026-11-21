#!/usr/bin/env python3
"""Shared Trino layer of the Trino experiments.

Session: Trino 479, Iceberg tables, single-threaded (task_concurrency = 1), join reordering off
(join_reordering_strategy = 'NONE'), one server-side time limit per statement (query_max_run_time).

  connect()          the session with the three settings above
  ensure_events()    the generated table in an Iceberg table (row count and id range asserted)
  bounded_mr_sql()   native MATCH_RECOGNIZE with the query's window also stated on the gap variable
  mr_streaming()     the native baseline: one MATCH_RECOGNIZE over the accumulated table per queried batch
  eimer_streaming()  an EIMER strategy: per-batch view updates + the composed query on queried batches
  eimer_delta_streaming()  the same in the delta form (the view with the last variable is built per batch only; new
                     matches are appended to the result)
"""
from __future__ import annotations

import heapq
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPRO = HERE.parents[1]
EIMER_REPO = Path(os.environ.get("EIMER_REPO", HERE.parents[2]))
for _p in (str(EIMER_REPO), str(EIMER_REPO / "execution"), str(EIMER_REPO / "demo")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import eimer_arm  # noqa: E402
import trino_exec  # noqa: E402
from trino_client import TrinoClientError  # noqa: E402
from eimer.query.query_spec import query_spec_to_match_recognize_sql  # noqa: E402

SESSION = dict(join_reordering_strategy="NONE", task_concurrency=1)
CATALOG = os.environ.get("TRINO_CATALOG", "iceberg")
IN_MEMORY = (CATALOG == "memory")


def clear_sql(sql: str) -> str:
    """a statement that empties a table, for the catalog in use: `DELETE FROM t` (without a condition) becomes
    `TRUNCATE TABLE t` on the in-memory connector; every other statement is returned as is."""
    s = sql.strip()
    if IN_MEMORY and s.upper().startswith("DELETE FROM ") and " WHERE " not in s.upper():
        return "TRUNCATE TABLE " + s[len("DELETE FROM "):]
    return sql
MR_CELL_BOUND_S = float(os.environ.get("EIMER_REPRO_MR_CELL_BOUND", 7200))


def connect(timeout_s: int = 1800):
    """the Trino session."""
    client = trino_exec.connect(server=os.environ.get("TRINO_SERVER", "localhost:8080"),
                                catalog=os.environ.get("TRINO_CATALOG", "iceberg"),
                                schema=os.environ.get("TRINO_SCHEMA", "default"),
                                user=os.environ.get("TRINO_USER", "eimer"), timeout_s=timeout_s)
    for setting in filter(None, os.environ.get("EIMER_REPRO_TRINO_SESSION", "").split(";")):
        client.execute(f"SET SESSION {setting}")
    inner = client.execute
    client.peak_memory_bytes = 0
    client.cpu_ms = client.elapsed_ms = client.n_statements = 0

    def execute(sql):
        out = inner(sql)
        st = getattr(client, "last_stats", None) or {}
        client.peak_memory_bytes = max(client.peak_memory_bytes, int(st.get("peakMemoryBytes") or 0))
        client.cpu_ms += int(st.get("cpuTimeMillis") or 0); client.elapsed_ms += int(st.get("elapsedTimeMillis") or 0); client.n_statements += 1
        return out

    client.execute = execute
    return client


def track_peak(fn):
    """decorator for an approach (client or an object with .client first; returns (record, tuples)): the record gets
    peak_query_memory_bytes = the largest peak memory reservation Trino reported for one of the approach's statements."""
    def wrapper(first, *args, **kwargs):
        client = getattr(first, "client", first)
        client.peak_memory_bytes = 0
        client.cpu_ms = client.elapsed_ms = client.n_statements = 0
        rec, tuples = fn(first, *args, **kwargs)
        if isinstance(rec, dict):
            rec["peak_query_memory_bytes"] = getattr(client, "peak_memory_bytes", None)
            rec["trino_cpu_ms"], rec["trino_elapsed_ms"], rec["trino_statements"] = client.cpu_ms, client.elapsed_ms, client.n_statements
        return rec, tuples
    wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
    return wrapper


def engine_info(client) -> dict:
    extra = [x.strip() for x in os.environ.get("EIMER_REPRO_TRINO_SESSION", "").split(";") if x.strip()]
    return dict(engine=f"Trino {client.execute('SELECT version()')[1][0][0]}",
                catalog=CATALOG, schema=os.environ.get("TRINO_SCHEMA", "default"),
                session=SESSION, **({"session_extra": extra} if extra else {}),
                worker_threads=os.environ.get("TRINO_WORKER_THREADS") or "engine default",
                **({"tables": "in-memory (no Iceberg commits; tables emptied with TRUNCATE)"} if IN_MEMORY else {}))


def ensure_events(client, table: str, n: int, make_frame) -> str:
    """the generated events in `table`."""
    try:
        r = client.execute(f"SELECT count(*), min(id), max(id) FROM {table}")[1][0]
        ok = (int(r[0]), int(r[1]), int(r[2])) == (n, 1, n)
    except (TrinoClientError, TypeError):
        ok = False
    if not ok:
        t0 = time.time()
        frame = make_frame()
        loaded = trino_exec.load_frame(client, frame, table)
        assert loaded == len(frame) == n, f"row mismatch: generated {len(frame)}, loaded {loaded}, expected {n}"
        print(f"   [data {table}: {loaded} rows loaded in {time.time() - t0:.0f} s]", flush=True)
    r = client.execute(f"SELECT count(*), min(id), max(id) FROM {table}")[1][0]
    assert (int(r[0]), int(r[1]), int(r[2])) == (n, 1, n), r
    return table


_UNIT_S = {"SECOND": 1.0, "MINUTE": 60.0, "HOUR": 3600.0, "DAY": 86400.0}
_EDGE = re.compile(r"\b(\w+)\.(\w+)\s+BETWEEN\s+(\w+)\.\2\s+AND\s+\3\.\2\s*\+\s*(INTERVAL\s+'([0-9.]+)'\s+(SECOND|MINUTE|HOUR|DAY))\b")


def gap_bound(spec):
    """(time column, first variable, [interval literals along the tightest chain first -> last]) or None if the query's
    conditions do not bound the last variable's time against the first one's."""
    vs = list(spec.variable_names)
    first, last = vs[0], vs[-1]
    edges, cols = {}, set()
    for p in spec.dependent_predicates:
        for m in _EDGE.finditer(p.sql_template):
            y, col, x, lit, val, unit = m.groups()
            cols.add(col)
            w = float(val) * _UNIT_S[unit]
            if (x, y) not in edges or w < edges[(x, y)][0]:
                edges[(x, y)] = (w, lit)
    if not edges:
        return None
    assert len(cols) == 1, f"time conditions on more than one column: {sorted(cols)}"
    dist, path, heap = {first: 0.0}, {first: []}, [(0.0, first)]
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist.get(u, float("inf")):
            continue
        for (x, y), (w, lit) in edges.items():
            if x == u and d + w < dist.get(y, float("inf")):
                dist[y] = d + w
                path[y] = path[u] + [lit]
                heapq.heappush(heap, (d + w, y))
    if last not in path or not path[last]:
        return None
    return cols.pop(), first, path[last]


def bounded_mr_sql(spec, events_table: str) -> str:
    """the query's MATCH_RECOGNIZE statement with the gap variable bounded by the query's own window
    (`Z AS Z.ts <= <first>.ts + W`). Raises if the query has no such bound."""
    mr = query_spec_to_match_recognize_sql(spec, events_table=events_table)
    b = gap_bound(spec)
    if b is None:
        raise ValueError(f"no time bound of the last variable against the first in {getattr(spec, 'name', '?')!r}")
    col, first, lits = b
    z = spec.effective_pattern.wildcard
    line = f"        {z} AS TRUE"
    assert mr.count(line) == 1, "wildcard DEFINE line not found exactly once"
    return mr.replace(line, f"        {z} AS {z}.{col} <= {first}.{col} + " + " + ".join(lits))


def gap_define_line(spec) -> str:
    z = spec.effective_pattern.wildcard
    mr = bounded_mr_sql(spec, "events")
    return [ln.strip() for ln in mr.splitlines() if ln.strip().startswith(f"{z} AS")][0]


@track_peak
def mr_streaming(client, spec, table, n, batches, qb, key_cols, wall_s, total_cap_ms=None, bounded=True):
    """native MATCH_RECOGNIZE (window on the gap variable; bounded=False: the query as stated) over the accumulated
    table id <= (b * n) // batches, once per queried batch: count(*) per batch, the full tuples on the last batch.
    `wall_s` applies per statement; the approach is censored at the first statement that exceeds it, or once its
    total passes MR_CELL_BOUND_S (reported as a lower bound). Returns a record dict and the final tuple set (None
    unless it finished)."""
    tot = 0.0
    final = None
    tuples = None
    per_scan = []
    qbs = sorted(qb)
    keysel = ", ".join(c.lower() for c in key_cols)
    wall_s = float(os.environ.get("EIMER_REPRO_MR_WALL", wall_s))
    client.execute(f"SET SESSION query_max_run_time = '{int(wall_s)}s'")
    bound_ms = MR_CELL_BOUND_S * 1000.0 if total_cap_ms is None else total_cap_ms
    for b in qbs:
        if tot >= bound_ms:
            return dict(status="dnf", censored="cell_bound", bound_s=bound_ms / 1000.0, spent_ms=round(tot, 1),
                        batches_done=len(per_scan), per_scan_ms=per_scan), None
        hi = (b * n) // batches
        src = f"(SELECT * FROM {table} WHERE id <= {hi})"
        mr = (bounded_mr_sql(spec, src) if bounded else query_spec_to_match_recognize_sql(spec, events_table=src)).rstrip().rstrip(";")
        last = (b == qbs[-1])
        t0 = time.perf_counter()
        try:
            _, rows = client.execute(f"SELECT {keysel if last else 'count(*)'} FROM ({mr})")
        except TrinoClientError as e:
            if trino_exec.is_dnf(e):
                return dict(status="dnf", censored="statement_limit", spent_ms=round(tot + (time.perf_counter() - t0) * 1000.0, 1),
                            batches_done=len(per_scan), per_scan_ms=per_scan, notes=str(e)[:200]), None
            raise
        ms = (time.perf_counter() - t0) * 1000.0
        tot += ms
        per_scan.append([hi, round(ms, 1)])
        if last:
            tuples = frozenset(tuple(int(x) for x in r) for r in rows)
            final = len(rows)
        else:
            final = int(rows[0][0]) if rows else 0
    return dict(status="ok", total_ms=round(tot, 1), final_count=final, per_scan_ms=per_scan), tuples


def load_rows(client, table: str, ddl: str, rows, chunk_chars: int = 700_000) -> int:
    """CREATE `table` and insert `rows`."""
    trino_exec.drop_table(client, table)
    client.execute(f"CREATE TABLE {table} ({ddl})")
    prefix = f"INSERT INTO {table} VALUES "
    pend, nchar, total = [], len(prefix), 0
    for r in rows:
        lit = "(" + ", ".join(("'" + v.replace("'", "''") + "'") if isinstance(v, str) else str(int(v)) for v in r) + ")"
        if pend and nchar + len(lit) + 2 > chunk_chars:
            client.execute(prefix + ", ".join(pend))
            pend, nchar = [], len(prefix)
        pend.append(lit)
        nchar += len(lit) + 2
        total += 1
    if pend:
        client.execute(prefix + ", ".join(pend))
    loaded = int(client.execute(f"SELECT count(*) FROM {table}")[1][0][0])
    assert loaded == total, (loaded, total)
    return loaded


_TRINO_TYPE = (("datetime64", "TIMESTAMP(6)"), ("bool", "BOOLEAN"), ("int", "BIGINT"), ("float", "DOUBLE"))


def frame_ddl(frame) -> list:
    """[(column, Trino type)] of a pandas frame: integers BIGINT, floats DOUBLE, booleans BOOLEAN, timestamps
    TIMESTAMP(6), everything else VARCHAR."""
    out = []
    for c in frame.columns:
        kind = str(frame[c].dtype).lower()
        out.append((c, next((t for k, t in _TRINO_TYPE if k in kind), "VARCHAR")))
    return out


def _typed_literal(v, ty: str) -> str:
    if v is None or v != v:
        return "NULL"
    if ty == "BIGINT":
        return str(int(v))
    if ty == "DOUBLE":
        return f"DOUBLE '{float(v)!r}'"
    if ty == "BOOLEAN":
        return "true" if v else "false"
    if ty == "TIMESTAMP(6)":
        return f"TIMESTAMP '{v.strftime('%Y-%m-%d %H:%M:%S.%f')}'"
    return "'" + str(v).replace("'", "''") + "'"


def load_typed_frame(client, table: str, frame, chunk_chars: int = 700_000) -> int:
    """a pandas frame with columns of several types into `table`: chunked INSERT into a staging table, then one CREATE
    TABLE AS. Floats as DOUBLE literals of their exact value, timestamps with microseconds, missing values as NULL.
    Returns the number of rows loaded (asserted)."""
    ddl = frame_ddl(frame)
    stg = f"{table}_stg"
    for t in (table, stg):
        trino_exec.drop_table(client, t)
    client.execute(f"CREATE TABLE {stg} (" + ", ".join(f"{c} {ty}" for c, ty in ddl) + ")")
    prefix = f"INSERT INTO {stg} VALUES "
    pend, nchar = [], len(prefix)
    types = [ty for _, ty in ddl]
    for row in frame.astype(object).itertuples(index=False, name=None):
        vals = []
        for v, ty in zip(row, types):
            try:
                vals.append(_typed_literal(v, ty))
            except TypeError:
                vals.append("NULL")
        lit = "(" + ", ".join(vals) + ")"
        if pend and nchar + len(lit) + 2 > chunk_chars:
            client.execute(prefix + ", ".join(pend))
            pend, nchar = [], len(prefix)
        pend.append(lit)
        nchar += len(lit) + 2
    if pend:
        client.execute(prefix + ", ".join(pend))
    client.execute(f"CREATE TABLE {table} AS SELECT * FROM {stg}")
    trino_exec.drop_table(client, stg)
    loaded = int(client.execute(f"SELECT count(*) FROM {table}")[1][0][0])
    assert loaded == len(frame), (loaded, len(frame))
    return loaded


def storage_state(client, tables: list) -> dict:
    """the size of the given tables: rows and uncompressed bytes (8 bytes per fixed-width value, the characters of text
    columns), and, for Iceberg tables, bytes and number of their data files (None for in-memory tables)."""
    rows = unc = 0
    for t in tables:
        cols = [(r[0], str(r[1]).lower()) for r in client.execute(f"DESCRIBE {t}")[1]]
        text = [c for c, ty in cols if ty.startswith(("varchar", "char"))]
        r = client.execute(f"SELECT count(*){''.join(f', coalesce(sum(length({c})), 0)' for c in text)} FROM {t}")[1][0]
        rows += int(r[0])
        unc += int(r[0]) * 8 * (len(cols) - len(text)) + sum(int(x) for x in r[1:])
    if IN_MEMORY:
        return dict(bytes=None, files=None, rows=rows, uncompressed_bytes=unc)
    out = dict(bytes=0, files=0, rows=rows, uncompressed_bytes=unc)
    for t in tables:
        r = client.execute(f'SELECT coalesce(sum(file_size_in_bytes), 0), count(*), coalesce(sum(record_count), 0) FROM "{t.lower()}$files"')[1][0]
        out["bytes"] += int(r[0]); out["files"] += int(r[1])
    return out


@track_peak
def eimer_streaming(client, dep, toks, all_vars, wl, spec, table, n, batches, qb, key_cols, wall_s, time_ingestion=False, measure_storage=False,
                    total_wall_s=None):
    """an EIMER strategy (the views `toks`) through the repository's streaming runner (demo/eimer_arm.py): per batch
    the views' delta updates, on queried batches the composed query; statements from the SQL emitter (Trino dialect).
    `total_wall_s` (optional) bounds the timed total: past it the approach ends as a DNF (lower bound).
    Returns a record dict and the final tuple set (None unless it finished)."""
    prof: dict = {}
    t0 = time.time()
    try:
        ms, cnt, tuples, _ = eimer_arm.eimer_streaming(trino_exec.TrinoEngine(client), dep, list(toks), all_vars, wl, spec, table,
                                                       n, batches, qb, key_cols, wall_s, measure_state=False, warmup=False,
                                                       time_ingestion=time_ingestion, profile=prof, total_wall_s=total_wall_s,
                                                       finish_hook=((lambda eng, tabs: storage_state(eng.client, tabs)) if measure_storage else None))
    except TrinoClientError as e:
        return dict(status="error", notes=str(e)[:300], wall_clock_s=round(time.time() - t0, 1)), None
    rec = dict(status="ok" if ms is not None else "dnf", total_ms=ms, final_count=cnt, cover=list(toks),
               update_ms=round(prof.get("update_ms", 0.0), 1), compose_ms=round(prof.get("compose_ms", 0.0), 1),
               ingest_ms=round(prof.get("ingest_ms", 0.0), 1), ingestion_charged=bool(time_ingestion),
               batches_done=prof.get("batches_done"), wall_clock_s=round(time.time() - t0, 1),
               **({"state": prof.get("finish")} if measure_storage else {}))
    if ms is None:
        rec["spent_ms"] = round(prof.get("update_ms", 0.0) + prof.get("compose_ms", 0.0) + prof.get("ingest_ms", 0.0), 1)
    return rec, tuples


def explicit_predicates_graph(dep):
    """the dependency graph with the window bounds derived by propagation removed from the query's own (explicit)
    edges; edges that exist only through propagation stay. The estimator's input."""
    from dataclasses import replace
    from eimer.query.temporal_propagation import DERIVED_WINDOW_OPERATOR, is_derived_window_edge
    changed, kept = False, []
    for e in dep.edges:
        if not is_derived_window_edge(e):
            own = [pr for pr in e.dependent_predicates if pr.operator != DERIVED_WINDOW_OPERATOR]
            if len(own) != len(e.dependent_predicates):
                changed = True
                e = replace(e, dependent_predicates=own)
        kept.append(e)
    return replace(dep, edges=kept) if changed else dep


def estimated_workload(client, dep, table: str, n: int, batches: int, window_rule: str):
    """(workload, payload): the b_unified selectivity estimates measured on the loaded table."""
    from compute_selectivities import compute_selectivities_payload
    from selectivity_payload import parse_selectivities_payload
    from eimer.workload import make_workload
    payload = compute_selectivities_payload(explicit_predicates_graph(dep), mode="b_unified", client=client, table=table)
    wl = make_workload(dep, total_events=n, batches=batches, selectivities=parse_selectivities_payload(payload, dep), window_rule=window_rule)
    return wl, payload


@track_peak
def eimer_stream_bounds(client, dep, toks, all_vars, wl, table, bounds: list, queried: set, key_cols, wall_s, measure_storage=False):
    """an EIMER strategy over batches with given boundaries: `bounds` = the last id of each batch (ascending; a first
    batch may hold a whole prefix), `queried` = the 1-based batches after which the query runs. Same protocol as
    eimer_streaming, with the per-batch times kept.
    Returns a record dict (per_batch = [batch, last id, update ms, query ms or None]) and the final tuple set."""
    import re
    from eimer.sql.sql_dialect import SqlDialect
    engine = trino_exec.TrinoEngine(client)
    engine.set_wall(wall_s)
    hist, bat = "hist_bp", "bat_bp"
    st = eimer_arm.cover_statements(dep, list(toks), all_vars, wl, dialect=SqlDialect.TRINO, hist=hist, bat=bat)
    result_q = (getattr(st, "post_filter_sql", "") or st.composition_sql).rstrip().rstrip(";")
    keysel = ", ".join(c.lower() for c in key_cols)
    cache_sel = {}
    for u in st.update_statements:
        cache = re.search(r"INSERT\s+INTO\s+(cache_\w+)", u, re.I).group(1)
        cache_sel[cache] = re.sub(rf"INSERT\s+INTO\s+{cache}\s*", "", u, flags=re.I).rstrip().rstrip(";")
    per_batch, tuples, final, tot = [], None, None, 0.0
    t_start = time.time()
    try:
        for t in (hist, bat):
            engine.drop_table(t)
            engine.execute(f"CREATE TABLE {t} AS SELECT * FROM {table} WITH NO DATA")
        for cache, sel in cache_sel.items():
            engine.drop_table(cache)
            engine.execute(f"CREATE TABLE {cache} AS {sel}")
        lo = 0
        for b, hi in enumerate(bounds, 1):
            engine.drop_table(bat)
            engine.execute(f"CREATE TABLE {bat} AS SELECT * FROM {table} WHERE id > {lo} AND id <= {hi}")
            engine.execute(f"INSERT INTO {hist} SELECT * FROM {bat}")
            t0 = time.perf_counter()
            for cache, sel in cache_sel.items():
                engine.execute(f"INSERT INTO {cache} {sel}")
            upd = (time.perf_counter() - t0) * 1000.0
            comp = None
            if b in queried:
                last = (b == len(bounds))
                t0 = time.perf_counter()
                _, rows = engine.execute(f"SELECT {keysel if last else 'count(*)'} FROM ({result_q})")
                comp = (time.perf_counter() - t0) * 1000.0
                if last:
                    tuples = frozenset(tuple(int(x) for x in r) for r in rows)
                    final = len(rows)
                else:
                    final = int(rows[0][0]) if rows else 0
            tot += upd + (comp or 0.0)
            per_batch.append([b, hi, round(upd, 1), (round(comp, 1) if comp is not None else None)])
            lo = hi
        state = storage_state(client, list(cache_sel)) if measure_storage else None
        return dict(status="ok", total_ms=round(tot, 1), final_count=final, cover=list(toks), per_batch=per_batch, state=state,
                    wall_clock_s=round(time.time() - t_start, 1)), tuples
    except TrinoClientError as e:
        return dict(status=("dnf" if trino_exec.is_dnf(e) else "error"), notes=str(e)[:300], spent_ms=round(tot, 1), cover=list(toks),
                    per_batch=per_batch, wall_clock_s=round(time.time() - t_start, 1)), None
    finally:
        for t in [hist, bat] + list(cache_sel):
            engine.drop_table(t)


@track_peak
def eimer_delta_streaming(client, dep, toks, all_vars, wl, table, n, batches, key_cols, wall_s, total_wall_s=None, kleene="stitch", measure_storage=False):
    """an EIMER strategy (the views `toks`) in the delta form through the repository's runner (demo/eimer_arm.py,
    `eimer_delta_streaming`), queried after every batch: the views without the pattern's last variable are kept and
    updated per batch; of the view with the last variable only the batch's delta is built; the composed query joins it
    with the other views and appends the matches that complete in the batch; a final dedup leaves one row per match.
    `kleene`: a view of a single Kleene variable is maintained by the incremental stitch ("stitch") or by a
    MATCH_RECOGNIZE over the whole table per batch ("rescan"). `total_wall_s` (optional) bounds the timed total: past
    it the approach ends as a DNF (its time is a lower bound).
    Returns a record dict and the final tuple set (None unless it finished)."""
    prof: dict = {}
    t_start = time.time()

    def done(status, **kw):
        upd, kl, dl = prof.get("other_views_ms", 0.0), prof.get("kleene_view_ms", 0.0), prof.get("delta_ms", 0.0)
        return dict(status=status, cover=list(toks), delta_view=prof.get("delta_view"), result_from_delta_alone=prof.get("result_from_delta_alone"),
                    kleene_maintenance=prof.get("kleene_maintenance"), update_ms=round(upd + kl + dl, 1),
                    kleene_view_ms=round(kl, 1), other_views_ms=round(upd, 1), delta_ms=round(dl, 1), compose_ms=round(prof.get("compose_ms", 0.0), 1),
                    batches_done=prof.get("batches_done", 0), wall_clock_s=round(time.time() - t_start, 1), **kw)

    def sizes(engine, tabs):
        return dict(views=storage_state(engine.client, tabs["views"]), delta=storage_state(engine.client, [tabs["delta"]]),
                    result_rows=storage_state(engine.client, [tabs["result"]]))

    try:
        ms, cnt, tuples, _ = eimer_arm.eimer_delta_streaming(trino_exec.TrinoEngine(client), dep, list(toks), all_vars, wl, table, n, batches, key_cols, wall_s,
                                                             total_wall_s=total_wall_s, kleene=kleene, clear_table=lambda t: clear_sql(f"DELETE FROM {t}"),
                                                             profile=prof, finish_hook=(sizes if measure_storage else None))
    except TrinoClientError as e:
        return done("error", notes=str(e)[:300], spent_ms=round(prof.get("spent_ms", 0.0), 1)), None
    if ms is None:
        if prof.get("censored") == "total_limit":
            return done("dnf", censored="total_limit", spent_ms=round(prof["spent_ms"], 1)), None
        return done("dnf", notes=prof.get("notes"), spent_ms=round(prof.get("spent_ms", 0.0), 1)), None
    return done("ok", total_ms=ms, final_count=cnt, final_dedup_ms=round(prof["final_dedup_ms"], 1), per_batch_ms=prof["per_batch_ms"], state=prof.get("finish")), tuples


def cover_sql(dep, toks, all_vars, wl) -> dict:
    """the statements executed for `toks`."""
    from eimer.sql.sql_dialect import SqlDialect
    st = eimer_arm.cover_statements(dep, list(toks), all_vars, wl, dialect=SqlDialect.TRINO)
    return dict(updates=list(st.update_statements), compose=(getattr(st, "post_filter_sql", "") or st.composition_sql))


def write_record(out_root: Path, name: str, rec: dict) -> bool:
    """write a workload's record. If an approach ended with an engine error (not a time limit), the record goes to
    failed/ instead of records/."""
    def errored(x) -> bool:
        if isinstance(x, dict):
            return x.get("status") == "error" or any(errored(v) for v in x.values())
        if isinstance(x, list):
            return any(errored(v) for v in x)
        return False
    ok = not errored(rec)
    path = out_root / ("records" if ok else "failed") / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rec, indent=1, default=str))
    os.replace(tmp, path)
    if not ok:
        print(f"   an arm ended with an engine error: record written to {path} (the workload is not counted as done)", flush=True)
    return ok


def query_schedule(batches: int, percent: float = 100) -> list:
    """the batches after which the query runs: every batch, or `percent` % of them, evenly spaced, the last included."""
    q = round(batches * float(percent) / 100)
    if q < 1:
        raise ValueError(f"{percent} % of {batches} batches: no queried batch")
    return sorted({-(-i * batches // q) for i in range(1, q + 1)})


def env_list(env: str, default, cast=int) -> list:
    """a comma-separated option from the environment."""
    raw = os.environ.get(env, "").strip()
    return [cast(x) for x in raw.replace(" ", "").split(",") if x] if raw else list(default)


def toks_of(cover, pos) -> list:
    """a cover as sorted view names."""
    return sorted("_".join(sorted(v, key=lambda x: pos[x])) for v in cover)


def run_main(doc, cells, cell_name, run_cell, default_results_dir) -> None:
    """the command line of an experiment driver: `--list` prints the workloads, `--only` runs one of them; exits with 1
    when a workload's record could not be written as done."""
    import argparse
    ap = argparse.ArgumentParser(description=doc, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--results-dir", default=str(default_results_dir))
    a = ap.parse_args()
    pool = cells()
    if a.list:
        for c in pool:
            print(cell_name(c))
        return
    if a.only:
        pool = [c for c in pool if cell_name(c) == a.only]
        assert pool, a.only
    for c in pool:
        if (Path(a.results_dir) / "records" / f"{cell_name(c)}.json").exists():
            print(f"skip {cell_name(c)} (record exists)")
            continue
        if not run_cell(c, Path(a.results_dir)).get("_recorded", True):
            raise SystemExit(1)
