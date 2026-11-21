"""The EIMER streaming approach, engine-neutral: one protocol executed on every engine.

An `Engine` wraps a client with what the approach needs beyond
`execute(sql) -> (columns, rows)`: the dialect the SQL emitter renders for, the
per-statement wall, the error type to catch, and the DNF predicate. `trino_exec` and
`duckdb_exec` each provide one. The protocol: ingestion untimed, per-view delta updates timed,
dedup'd compose timed on queried batches, full-tuple keys on the final batch.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for _p in (str(REPO), str(REPO / "execution")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from compute_selectivities import compute_selectivities_payload  # noqa: E402
from selectivity_payload import parse_selectivities_payload  # noqa: E402
from eimer.workload import make_workload  # noqa: E402
from eimer.sql.sql_emitter import generate_sql_statements_for_cover  # noqa: E402
from eimer.sql.backend import SqlDialect, backend_for  # noqa: E402
from eimer.models import make_view  # noqa: E402
from eimer.selection.plan_build import build_plan  # noqa: E402


class Engine:
    """What the approach needs from an engine: a client plus its dialect, wall, error class
    and DNF predicate.
    """

    dialect: SqlDialect
    error_type: type

    def __init__(self, client) -> None:
        self.client = client

    def execute(self, sql: str):
        return self.client.execute(sql)

    def set_wall(self, seconds: float) -> None:
        raise NotImplementedError

    def is_dnf(self, err) -> bool:
        raise NotImplementedError

    def drop_table(self, table: str) -> None:
        try:
            self.client.execute(f"DROP TABLE IF EXISTS {table}")
        except self.error_type:
            pass


def b_unified_workload(client, dep, *, table, total_events, batches):
    """Workload scored from the b_unified estimator, measured on the loaded table.
        Returns (workload, payload).
    """
    payload = compute_selectivities_payload(dep, mode="b_unified", client=client,
                                            table=table)
    selectivities = parse_selectivities_payload(payload, dep)
    wl = make_workload(dep, total_events=total_events, batches=batches,
                       selectivities=selectivities,
                       window_rule=backend_for(client.dialect).window_rule)
    return wl, payload


def build_cover_plan(dep, family_toks, all_vars, wl):
    """Materializes the (family == compose) cover into an executable strategy via the
        plan builder (bushy inner join order, bushy update order).
    """
    fam = frozenset(make_view(t.split("_")) for t in family_toks)
    return build_plan(dep, fam, fam, all_vars, workload=wl)


def cover_statements(dep, toks, all_vars, wl, *, dialect: SqlDialect, hist: str = "hist_bp",
                     bat: str = "bat_bp"):
    """The emitter's statements for a cover over the (hist, bat) staging tables; the
        driver-staging singleton for the last variable is appended when the cover lacks it.
    """
    positions = dep.positions
    last_var = max(all_vars, key=lambda v: positions[v])
    toks = list(toks)
    if not any(last_var in make_view(t.split("_")) for t in toks):
        toks.append(last_var)
    o, plan, nodes = build_cover_plan(dep, toks, all_vars, wl)
    st = generate_sql_statements_for_cover(o, dep, nodes, o.composition_plans[0],
                                           o.update_plans[0], base_table=hist,
                                           batch_table=bat, dialect=dialect)
    return st


def eimer_streaming(engine: Engine, dep, toks, all_vars, wl, spec, table, N, B, qb, key_cols, T,
                    measure_state: bool = False, warmup: bool = True, total_wall_s: float | None = None,
                    time_ingestion: bool = False, profile: dict | None = None, finish_hook=None):
    """EIMER cover approach, full per-batch protocol: maintains hist/bat staging tables
    (ingestion untimed), then per batch runs the per-view delta updates (timed)
    and, on queried batches, the dedup'd compose (timed; count(*) on intermediate batches,
    full-tuple keys on the final one). Statements come from the SQL emitter of `eimer.sql`.

    measure_state=True additionally snapshots, untimed after each batch, the resident
    state (sum over cache tables of count(*)) and reports its max over batches,
    plus the per-batch (update ms, compose ms) profile.

    With warmup (the default), the whole protocol first runs once untimed. `total_wall_s`
    (optional) bounds the approach's timed total: once the batches completed so far
    exceed it, the approach stops as a DNF.

    time_ingestion=True also times the writing of each batch into the staging tables.
    `profile` (optional dict) receives the sums of the timed parts: update_ms, compose_ms,
    ingest_ms (0 unless time_ingestion) and the number of batches completed. `finish_hook` (optional,
    fn(engine, view tables) -> value) runs untimed after the last batch, before the views are dropped;
    its value is stored as profile["finish"].

    Returns (total_ms, count, tuple_set, state) where state is a dict
    (max_resident_rows, per_cache_rows, per_batch_ms) or None; (None, None, None, None) on a DNF.
    """
    if warmup:
        warm = eimer_streaming(engine, dep, toks, all_vars, wl, spec, table, N, B, qb,
                               key_cols, T, measure_state=False, warmup=False, total_wall_s=total_wall_s,
                               time_ingestion=time_ingestion)
        if warm[0] is None:
            return warm
        return eimer_streaming(engine, dep, toks, all_vars, wl, spec, table, N, B, qb,
                               key_cols, T, measure_state=measure_state, warmup=False,
                               total_wall_s=total_wall_s, time_ingestion=time_ingestion, profile=profile,
                               finish_hook=finish_hook)
    engine.set_wall(T)
    HIST, BAT = "hist_bp", "bat_bp"
    st = cover_statements(dep, toks, all_vars, wl, dialect=engine.dialect, hist=HIST, bat=BAT)
    result_q = (getattr(st, "post_filter_sql", "") or st.composition_sql).rstrip().rstrip(";")
    keysel = ", ".join(c.lower() for c in key_cols)
    cache_sel = {}
    for u in st.update_statements:
        cache = re.search(r"INSERT\s+INTO\s+(cache_\w+)", u, re.I).group(1)
        cache_sel[cache] = re.sub(rf"INSERT\s+INTO\s+{cache}\s*", "", u,
                                  flags=re.I).rstrip().rstrip(";")

    qbs = sorted(qb)
    tot = 0.0
    final = None
    tuples = None
    max_resident = 0
    peak_per_cache: dict[str, int] = {}
    per_batch: list = []
    prof = dict(update_ms=0.0, compose_ms=0.0, ingest_ms=0.0, batches_done=0)
    if profile is not None:
        profile.update(prof)
    try:
        for t in (HIST, BAT):
            engine.drop_table(t)
            engine.execute(f"CREATE TABLE {t} AS SELECT * FROM {table} WITH NO DATA")
        for cache, sel in cache_sel.items():
            engine.drop_table(cache)
            engine.execute(f"CREATE TABLE {cache} AS {sel}")
        for b in range(1, B + 1):
            lo, hi = ((b - 1) * N) // B, (b * N) // B
            engine.drop_table(BAT)
            t0 = time.perf_counter()
            engine.execute(f"CREATE TABLE {BAT} AS SELECT * FROM {table} "
                           f"WHERE id > {lo} AND id <= {hi}")
            engine.execute(f"INSERT INTO {HIST} SELECT * FROM {BAT}")
            if time_ingestion:
                ing_ms = (time.perf_counter() - t0) * 1000.0
                tot += ing_ms
                prof["ingest_ms"] += ing_ms
            t0 = time.perf_counter()
            for cache, sel in cache_sel.items():
                engine.execute(f"INSERT INTO {cache} {sel}")
            upd_ms = (time.perf_counter() - t0) * 1000.0
            tot += upd_ms
            comp_ms = 0.0
            if b in qb:
                last = (b == qbs[-1])
                t0 = time.perf_counter()
                _, rows = engine.execute(
                    f"SELECT {keysel if last else 'count(*)'} FROM ({result_q})")
                comp_ms = (time.perf_counter() - t0) * 1000.0
                tot += comp_ms
                if last:
                    tuples = frozenset(tuple(int(x) for x in r) for r in rows)
                    final = len(rows)
                else:
                    final = int(rows[0][0]) if rows else 0
            prof["update_ms"] += upd_ms
            prof["compose_ms"] += comp_ms
            prof["batches_done"] = b
            if profile is not None:
                profile.update(prof)
            if measure_state:
                resident = 0
                for cache in cache_sel:
                    _, rows = engine.execute(f"SELECT count(*) FROM {cache}")
                    n = int(rows[0][0])
                    resident += n
                    peak_per_cache[cache] = max(peak_per_cache.get(cache, 0), n)
                max_resident = max(max_resident, resident)
                per_batch.append([b, round(upd_ms, 2), round(comp_ms, 2)])
            if total_wall_s is not None and tot > total_wall_s * 1000.0:
                return None, None, None, None
        if finish_hook is not None and profile is not None:
            profile["finish"] = finish_hook(engine, list(cache_sel))
        state = (dict(max_resident_rows=max_resident, per_cache_rows=peak_per_cache,
                      per_batch_ms=per_batch)
                 if measure_state else None)
        return round(tot, 1), final, tuples, state
    except engine.error_type as e:
        if engine.is_dnf(e):
            return None, None, None, None
        raise
    finally:
        for t in [HIST, BAT] + list(cache_sel):
            engine.drop_table(t)


def eimer_delta_streaming(engine: Engine, dep, toks, all_vars, wl, table, N, B, key_cols, T, *,
                          total_wall_s: float | None = None, kleene: str = "stitch", clear_table=None,
                          profile: dict | None = None, finish_hook=None):
    """EIMER cover approach in the delta form, queried after every batch. Every view without the
    pattern's last variable is kept in full and updated per batch. The view with the last
    variable is not kept: per batch only its delta is built (its rows that end in the batch).
    The composed query joins this delta with the other views, which yields the matches that
    complete in the batch, and appends them to a result table; if the delta's view covers the
    whole pattern, its rows are the new result rows. One final dedup over the result table (the
    composition's post-filter key and order) leaves one row per match.

    A view of a single Kleene variable (the maximal runs of that variable) is maintained by the
    incremental stitch (`kleene="stitch"`: MATCH_RECOGNIZE over the rows from the run left
    open at the previous batch boundary to the end of the batch; longer versions of a run
    supersede shorter ones on read) or by rescan (`kleene="rescan"`: MATCH_RECOGNIZE over
    the whole table per batch). Both need an engine with a native MATCH_RECOGNIZE.

    Ingestion is untimed; view updates, the delta, the composition and the final dedup are
    timed. `total_wall_s` (optional) bounds the approach's timed total: past it the approach stops as
    a DNF. `clear_table` (optional, fn(table) -> sql) gives the statement that empties a
    table (default `DELETE FROM table`). `profile` (optional dict) receives the sums of the
    timed parts while the approach runs (kleene_view_ms, other_views_ms, delta_ms, compose_ms,
    spent_ms, batches_done, per_batch_ms), the names delta_view / result_from_delta_alone /
    kleene_maintenance, and on a DNF its kind ("total_limit" or "statement_limit")
    and the engine's message (notes). `finish_hook` (optional,
    fn(engine, dict(views=[...], delta=..., result=...)) -> value) runs untimed after the
    final dedup, before the tables are dropped; its value is stored as profile["finish"].

    Returns (total_ms, count, tuple_set, profile); (None, None, None, profile) on a DNF.
    """
    from eimer.sql.sql_emitter import (_kleene_staging_table_name, _mr_group_detect_update_sql,
                                       one_per_anchor_dedup_key)
    from eimer.query.graph_adapter import collect_referenced_columns
    if kleene not in ("stitch", "rescan"):
        raise ValueError(f"kleene: 'stitch' or 'rescan', got {kleene!r}")
    kleene_vars = {v for v in dep.variables if dep.quantifiers.get(v) in ("PLUS", "RELUCTANT_PLUS")}
    if kleene_vars and not backend_for(engine.dialect).supports_native_match_recognize:
        raise ValueError(f"a Kleene view needs a native MATCH_RECOGNIZE, which {engine.dialect.value} lacks")
    if clear_table is None:
        clear_table = lambda t: f"DELETE FROM {t}"  # noqa: E731
    engine.set_wall(T)
    HIST, BAT, DELTA, RES = "hist_bp", "bat_bp", "dlt_bp", "res_bp"
    pos = dep.positions
    last_var = max(all_vars, key=lambda v: pos[v])
    st = cover_statements(dep, list(toks), all_vars, wl, dialect=engine.dialect, hist=HIST, bat=BAT)
    result_q = (getattr(st, "post_filter_sql", "") or st.composition_sql).rstrip().rstrip(";")
    views = []
    for u in st.update_statements:
        m = re.match(r"\s*INSERT\s+INTO\s+(cache_\w+)\s*(\(([^)]*)\))?\s*", u, re.I)
        cache = m.group(1)
        views.append((cache, m.group(3), u[m.end():].rstrip().rstrip(";"),
                      tuple(cache[len("cache_"):].split("_"))))
    last_cache, _, last_sel, last_view = next(v for v in views if last_var in v[3])
    if set(last_view) & kleene_vars:
        raise ValueError("a Kleene variable in the view of the last variable is not supported")
    others = [v for v in views if v[0] != last_cache]
    stitched = ({v[0]: v[3][0] for v in others if len(v[3]) == 1 and v[3][0] in kleene_vars}
                if kleene == "stitch" else {})
    stg = {cache: _kleene_staging_table_name(make_view([var]), pos) for cache, var in stitched.items()}
    cols_by_var = collect_referenced_columns(dep) if stitched else {}
    open_a = {cache: None for cache in stitched}
    part, terms = one_per_anchor_dedup_key(dep)
    order = ", ".join(f"{c} {d}" for c, d in terms)
    keys = [c.lower() for c in key_cols]
    proj = ", ".join(keys + [c for c, _ in terms if c.lower() not in keys])
    keep = ", ".join(keys)
    blind = set(last_view) == set(all_vars)
    delta_pattern = r"\b" + re.escape(last_cache) + r"\b"
    delta_q = re.sub(delta_pattern, DELTA, result_q)
    new_rows = f"SELECT {proj} FROM {DELTA}" if blind else f"SELECT {proj} FROM ({delta_q})"
    tables = [HIST, BAT, DELTA, RES] + [v[0] for v in others] + list(stg.values())
    prof = dict(delta_view=last_cache[len("cache_"):], result_from_delta_alone=blind,
                kleene_maintenance=(kleene if (kleene_vars and others) else None),
                kleene_view_ms=0.0, other_views_ms=0.0, delta_ms=0.0, compose_ms=0.0, spent_ms=0.0,
                batches_done=0, per_batch_ms=[], censored=None, notes=None)
    if profile is None:
        profile = {}
    profile.update(prof)
    res_made = False

    def timed(sql):
        t0 = time.perf_counter()
        out = engine.execute(sql)
        return (time.perf_counter() - t0) * 1000.0, out

    try:
        for t in tables:
            engine.drop_table(t)
        for t in (HIST, BAT):
            engine.execute(f"CREATE TABLE {t} AS SELECT * FROM {table} WITH NO DATA")
        for cache, cols, sel, _ in others:
            engine.execute(f"CREATE TABLE {cache} AS {sel}")
            if cache in stitched:
                engine.execute(f"CREATE TABLE {stg[cache]} AS SELECT * FROM {cache} WITH NO DATA")
        for b in range(1, B + 1):
            lo, hi = ((b - 1) * N) // B, (b * N) // B
            engine.drop_table(BAT)
            engine.execute(f"CREATE TABLE {BAT} AS SELECT * FROM {table} WHERE id > {lo} AND id <= {hi}")
            engine.execute(f"INSERT INTO {HIST} SELECT * FROM {BAT}")
            b_upd = b_kl = 0.0
            for cache, cols, sel, vs in others:
                if not (len(vs) == 1 and vs[0] in kleene_vars):
                    b_upd += timed(f"INSERT INTO {cache} {sel}")[0]
                elif cache not in stitched:
                    b_kl += timed(f"INSERT INTO {cache} ({cols}) {sel}")[0]
                else:
                    var = stitched[cache]
                    redetect_lo = open_a[cache] if open_a[cache] is not None else lo + 1
                    raw = _mr_group_detect_update_sql(var, dep, cols_by_var, HIST, BAT, redetect_lo=redetect_lo,
                                                      target_table=stg[cache]).rstrip().rstrip(";")
                    b_kl += timed(clear_table(stg[cache]))[0]
                    b_kl += timed(raw)[0]
                    b_kl += timed(f"INSERT INTO {cache} ({cols}) SELECT {cols} FROM {stg[cache]}")[0]
                    ms, out = timed(f"SELECT MIN({var}_first_id) FROM {stg[cache]} WHERE {var}_last_id = {hi}")
                    b_kl += ms
                    open_a[cache] = int(out[1][0][0]) if out[1][0][0] is not None else None
            engine.drop_table(DELTA)
            b_dl = timed(f"CREATE TABLE {DELTA} AS {last_sel}")[0]
            b_comp = timed(f"INSERT INTO {RES} {new_rows}" if res_made
                           else f"CREATE TABLE {RES} AS {new_rows}")[0]
            res_made = True
            profile["other_views_ms"] += b_upd
            profile["kleene_view_ms"] += b_kl
            profile["delta_ms"] += b_dl
            profile["compose_ms"] += b_comp
            profile["spent_ms"] += b_upd + b_kl + b_dl + b_comp
            profile["batches_done"] = b
            profile["per_batch_ms"].append([b, round(b_upd + b_kl + b_dl, 1), round(b_comp, 1)])
            if total_wall_s is not None and profile["spent_ms"] > total_wall_s * 1000.0 and b < B:
                profile["censored"] = "total_limit"
                return None, None, None, profile
        ms, out = timed(f"SELECT {keep} FROM (SELECT {keep}, ROW_NUMBER() OVER (PARTITION BY {part} "
                        f"ORDER BY {order}) AS rn FROM {RES}) WHERE rn = 1")
        profile["final_dedup_ms"] = ms
        profile["compose_ms"] += ms
        profile["spent_ms"] += ms
        tuples = frozenset(tuple(int(x) for x in r) for r in out[1])
        if finish_hook is not None:
            profile["finish"] = finish_hook(engine, dict(views=[v[0] for v in others], delta=DELTA, result=RES))
        return round(profile["spent_ms"], 1), len(tuples), tuples, profile
    except engine.error_type as e:
        if engine.is_dnf(e):
            profile["censored"] = "statement_limit"
            profile["notes"] = str(e)[:300]
            return None, None, None, profile
        raise
    finally:
        for t in tables:
            engine.drop_table(t)
