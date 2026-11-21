#!/usr/bin/env python3
"""The Kleene experiment on Trino (the two Kleene panels of the paper's Fig. 11).

Part "payoff" (left panel): maintaining one Kleene view, the maximal runs of PATTERN (C+), over a growing table, by
  rescan   per batch, MATCH_RECOGNIZE over the whole table, the runs that end in the batch are appended;
  stitch   incremental maintenance: per batch, MATCH_RECOGNIZE over the window from the start of the run that was
           open at the previous batch boundary to the end of the batch; the runs are appended, and a run that
           continued supersedes its shorter version on read.
  Table: blocks of L matching rows at density rho = 0.5, batches of 1,000 rows; per-batch maintenance time of both
  forms on the same table, per seed.    paper: N = 100,000, L = 30, seeds 1, 2, 3

Part "sweep" (right panel): the recurring query A Z* B Z*? C+ Z* D (C a Kleene variable; equality on (A, B), range
conditions on (A, D) and (B, D), selectivity 0.01) over the run length L of C, queried after every batch:
  mr              native MATCH_RECOGNIZE over the accumulated table
  eimer_nostitch  EIMER (clique-grow's pick plus the Kleene view), the Kleene view maintained by rescan
  eimer_stitch    the same strategy, the Kleene view maintained by the stitch
  Table: one (A, B, run of L C's, D) group per 200 rows, generated with a fixed-seed hash.
  paper: N = 100,000 in 100 batches, L = 5, 10, 25, 50, 100

Checks: payoff: both forms' views equal the runs of a single MATCH_RECOGNIZE over the final table. sweep: on the first
1,000 events (20 batches), mr == eimer_nostitch == eimer_stitch on full tuples; on the full run, the EIMER approaches
are identical to each other and to mr where it finishes; the number of matches is the number of complete groups.

Axis values and limits (environment):
  EIMER_REPRO_KLEENE_PARTS=payoff,sweep
  EIMER_REPRO_KLEENE_PAYOFF_N=100000   EIMER_REPRO_KLEENE_PAYOFF_L=30   EIMER_REPRO_KLEENE_SEEDS=1,2,3
  EIMER_REPRO_KLEENE_N=100000   EIMER_REPRO_KLEENE_B=100   EIMER_REPRO_KLEENE_LS=5,10,25,50,100
  EIMER_REPRO_WALL=<s>               limit per statement (default 1,800 s)
  EIMER_REPRO_MR_CELL_BOUND=<s>      limit for the total of the native-MR approach (default 7,200 s)

  python3 kleene.py --list
  python3 kleene.py --only sweep_n100000_b100_L25 --results-dir <dir>
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import trino_lib as tl  # noqa: E402
import trino_exec  # noqa: E402
from trino_client import TrinoClientError  # noqa: E402
from eimer.query.query_spec import load_query_spec_file, query_spec_to_dependency_graph  # noqa: E402
from eimer.query.graph_adapter import collect_referenced_columns  # noqa: E402
from eimer.selection.plan_selection_clique_grow import select_clique_grow_plan  # noqa: E402
from eimer.sql.backend import SqlDialect, backend_for  # noqa: E402
from eimer.sql.sql_emitter import _kleene_staging_table_name, _mr_group_detect_update_sql  # noqa: E402
from eimer.workload import make_workload  # noqa: E402
from eimer.models import make_view  # noqa: E402
from selectivity_payload import parse_selectivities_payload  # noqa: E402

SPECS = Path(os.environ.get("EIMER_REPRO_SPECS_DIR", HERE / "specs"))
BACKEND = backend_for(SqlDialect.TRINO)
VRANGE = 1_000_000
SIGMA = 0.01
P_GROUP = 200
RHO, PAYOFF_BATCH = 0.5, 1000
N_GATE, B_GATE = 1000, 20
REFERENCE = json.loads((tl.REPRO / "reference" / "paper_trino.json").read_text())["kleene"]


PARTS = [p for p in os.environ.get("EIMER_REPRO_KLEENE_PARTS", "payoff,sweep").split(",") if p]
PAYOFF_N = tl.env_list("EIMER_REPRO_KLEENE_PAYOFF_N", (100_000,))[0]
PAYOFF_L = tl.env_list("EIMER_REPRO_KLEENE_PAYOFF_L", (30,))[0]
SEEDS = tl.env_list("EIMER_REPRO_KLEENE_SEEDS", (1, 2, 3))
SWEEP_N = tl.env_list("EIMER_REPRO_KLEENE_N", (100_000,))[0]
SWEEP_B = tl.env_list("EIMER_REPRO_KLEENE_B", (100,))[0]
LS = tl.env_list("EIMER_REPRO_KLEENE_LS", (5, 10, 25, 50, 100))
assert PAYOFF_N % PAYOFF_BATCH == 0, f"payoff: the table size is a multiple of {PAYOFF_BATCH}"
assert SWEEP_N % SWEEP_B == 0 and SWEEP_N >= 2 * N_GATE, "sweep: N is a multiple of B and at least 2,000"
assert all(1 <= l <= P_GROUP - 8 for l in LS), f"sweep: run lengths 1 .. {P_GROUP - 8}"


def cells() -> list:
    out = []
    if "payoff" in PARTS:
        out += [dict(part="payoff", n=PAYOFF_N, batch=PAYOFF_BATCH, rho=RHO, l_run=PAYOFF_L, seed=s) for s in SEEDS]
    if "sweep" in PARTS:
        out += [dict(part="sweep", n=SWEEP_N, batches=SWEEP_B, l_run=l, sigma=SIGMA, p_group=P_GROUP) for l in sorted(LS)]
    return out


def cell_name(c: dict) -> str:
    if c["part"] == "payoff":
        return f"payoff_n{c['n']}_L{c['l_run']}_s{c['seed']}"
    return f"sweep_n{c['n']}_b{c['batches']}_L{c['l_run']}"


def wall_s() -> int:
    return int(os.environ.get("EIMER_REPRO_WALL", 1800))


def _drop(client, *tables):
    for t in tables:
        trino_exec.drop_table(client, t)


def _timed(client, sql: str) -> float:
    t0 = time.perf_counter()
    client.execute(sql)
    return (time.perf_counter() - t0) * 1000.0


COLS = "first_id, first_ts, first_val, last_id, last_ts, last_val, cnt"
MEAS = ("FIRST(C.id) AS first_id, FIRST(C.ts) AS first_ts, FIRST(C.val) AS first_val, "
        "LAST(C.id) AS last_id, LAST(C.ts) AS last_ts, LAST(C.val) AS last_val, COUNT(C.id) AS cnt")
EV, EB, SRC, CR, CS, ST = "ksp_events", "ksp_events_batch", "ksp_src", "ksp_cache_rescan", "ksp_cache_stitch", "ksp_stg_stitch"


def _runs_mr(source_sql: str) -> str:
    return (f"SELECT {COLS} FROM ({source_sql}) MATCH_RECOGNIZE (\n    ORDER BY ts\n    MEASURES {MEAS}\n    ONE ROW PER MATCH\n"
            f"    AFTER MATCH SKIP TO NEXT ROW\n    PATTERN (C+)\n    DEFINE C AS C.m = 1\n)")


def _dedup_select(table: str) -> str:
    """the maximal runs of a view."""
    return (f"SELECT first_id, last_id, cnt FROM (SELECT first_id, last_id, cnt, "
            f"ROW_NUMBER() OVER (PARTITION BY first_id ORDER BY last_ts DESC) rn FROM {table}) WHERE rn = 1")


def payoff_rows(n: int, rho: float, l_run: int, seed: int):
    """the table of the payoff part."""
    period = max(l_run + 1, round(l_run / rho))
    phase = (seed * 7919) % period
    for i in range(1, n + 1):
        yield (i, i, 1 if (i - 1 + phase) % period < l_run else 0, (i * 7) % 97 + 1)


def run_payoff(c: dict, client) -> dict:
    n, b, seed = c["n"], c["batch"], c["seed"]
    client.execute(f"SET SESSION query_max_run_time = '{wall_s()}s'")
    tl.load_rows(client, SRC, "id BIGINT, ts BIGINT, m BIGINT, val BIGINT", payoff_rows(n, c["rho"], c["l_run"], seed))
    _drop(client, EV, EB, CR, CS, ST)
    for t in (EV, EB):
        client.execute(f"CREATE TABLE {t} (id BIGINT, ts BIGINT, m BIGINT, val BIGINT)")
    for t in (CR, CS, ST):
        client.execute(f"CREATE TABLE {t} (first_id BIGINT, first_ts BIGINT, first_val BIGINT, last_id BIGINT, last_ts BIGINT, last_val BIGINT, cnt BIGINT)")
    rescan_sql = (f"INSERT INTO {CR} ({COLS})\nSELECT mr.first_id, mr.first_ts, mr.first_val, mr.last_id, mr.last_ts, mr.last_val, mr.cnt\n"
                  f"FROM ({_runs_mr(f'SELECT id, ts, m, val FROM {EV}')}) mr\nWHERE EXISTS (SELECT 1 FROM {EB} eb WHERE eb.id = mr.last_id)")
    k = n // b
    per_batch, open_a = [], None
    try:
        for bi in range(1, k + 1):
            lo, hi = (bi - 1) * b + 1, bi * b
            client.execute(f"INSERT INTO {EV} SELECT id, ts, m, val FROM {SRC} WHERE id BETWEEN {lo} AND {hi}")
            client.execute(tl.clear_sql(f"DELETE FROM {EB}"))
            client.execute(f"INSERT INTO {EB} SELECT id, ts, m, val FROM {SRC} WHERE id BETWEEN {lo} AND {hi}")
            r_ms = _timed(client, rescan_sql)
            window_lo = open_a if open_a is not None else lo
            s_ms = _timed(client, tl.clear_sql(f"DELETE FROM {ST}"))
            s_ms += _timed(client, f"INSERT INTO {ST} ({COLS}) {_runs_mr(f'SELECT id, ts, m, val FROM {EV} WHERE id BETWEEN {window_lo} AND {hi}')}")
            s_ms += _timed(client, f"INSERT INTO {CS} ({COLS}) SELECT {COLS} FROM {ST}")
            t0 = time.perf_counter()
            nxt = client.execute(f"SELECT MIN(first_id) FROM {ST} WHERE last_id = {hi}")[1][0][0]
            s_ms += (time.perf_counter() - t0) * 1000.0
            open_a = int(nxt) if nxt is not None else None
            per_batch.append(dict(b=bi, lo=lo, hi=hi, rescan_maint_ms=round(r_ms, 2), stitch_maint_ms=round(s_ms, 2)))
        base = {tuple(int(x) for x in r) for r in client.execute(f"SELECT first_id, last_id, cnt FROM ({_runs_mr(f'SELECT id, ts, m, val FROM {EV}')})")[1]}
        rset = {tuple(int(x) for x in r) for r in client.execute(_dedup_select(CR))[1]}
        sset = {tuple(int(x) for x in r) for r in client.execute(_dedup_select(CS))[1]}
        rows = dict(rescan=int(client.execute(f"SELECT count(*) FROM {CR}")[1][0][0]), stitch=int(client.execute(f"SELECT count(*) FROM {CS}")[1][0][0]))
    finally:
        _drop(client, SRC, EV, EB, CR, CS, ST)
    r_tot, s_tot = sum(p["rescan_maint_ms"] for p in per_batch), sum(p["stitch_maint_ms"] for p in per_batch)
    rec = dict(per_batch=per_batch,
               totals=dict(rescan_maint_ms=round(r_tot, 1), stitch_maint_ms=round(s_tot, 1), rescan_over_stitch=(round(r_tot / s_tot, 2) if s_tot else None),
                           stored_rows=rows, runs=len(base)),
               gates=dict(rescan_correct=(rset == base), stitch_correct=(sset == base), results_identical=(rset == base and sset == base), n_runs=len(base)))
    print(f"   payoff: rescan {r_tot / 1000:.1f} s, stitch {s_tot / 1000:.1f} s over {k} batches ({rec['totals']['rescan_over_stitch']}x) | "
          f"runs {len(base)} | rescan correct {rset == base}, stitch correct {sset == base}", flush=True)
    return rec


def _h(salt: str, period: int) -> int:
    return int.from_bytes(hashlib.blake2b(f"{salt}:{period}".encode(), digest_size=8).digest(), "big") % VRANGE


def kleene_rows(n: int, l_run: int, p_group: int):
    """per group of p_group rows: A at offset 0, B at 2, the C-run at 4 .. 3 + l_run, D at 5 + l_run, gap rows (Z)
    elsewhere. A and B share veq (the group number), A and D share vr1, B and D share vr2 (two hashes of the group
    number), so every group holds one match and other groups qualify with the conditions' selectivity."""
    d_off = 5 + l_run
    assert p_group > d_off + 1
    for i in range(1, n + 1):
        off, period = (i - 1) % p_group, (i - 1) // p_group
        if off == 0:
            yield (i, i, "A", period, _h("vr1", period), -1)
        elif off == 2:
            yield (i, i, "B", period, -1, _h("vr2", period))
        elif 4 <= off <= 3 + l_run:
            yield (i, i, "C", -1, -1, -1)
        elif off == d_off:
            yield (i, i, "D", -1, _h("vr1", period), _h("vr2", period))
        else:
            yield (i, i, "Z", -1, -1, -1)


def kleene_spec(sigma: float) -> dict:
    w = max(0, round(sigma * VRANGE / 2))
    typ = lambda v: {"predicate_id": f"{v}_type", "kind": "independent", "variables": [v], "sql_template": f"{v}.primary_type = '{v}'",
                     "referenced_columns": ["primary_type"], "selectivity_key": v}
    return {
        "name": f"kleene4_tri_sig{sigma}",
        "suite_metadata": {"pattern_length": 4, "shape": "kleene_mid_triangle",
                           "purpose": f"A Z* B Z*? C+ Z* D; (A,B) equality, (A,D) and (B,D) range conditions of half-width {w}; C a Kleene variable"},
        "event_schema": [{"name": "id", "sql_type": "BIGINT"}, {"name": "ts", "sql_type": "BIGINT"}, {"name": "primary_type", "sql_type": "VARCHAR"},
                         {"name": "veq", "sql_type": "BIGINT"}, {"name": "vr1", "sql_type": "BIGINT"}, {"name": "vr2", "sql_type": "BIGINT"}],
        "id_column": "id", "time_column": "ts", "ts_column": "ts", "order_by": ["ts"],
        "row_output": "ONE ROW PER MATCH", "after_match": "AFTER MATCH SKIP TO NEXT ROW", "partition_by": [], "window": None, "kleene_annotations": [],
        "variables": [{"name": "A", "output_prefix": "a", "independent_predicate_id": "A_type"},
                      {"name": "B", "output_prefix": "b", "independent_predicate_id": "B_type"},
                      {"name": "C", "output_prefix": "c", "independent_predicate_id": "C_type", "quantifier": "PLUS"},
                      {"name": "D", "output_prefix": "d", "independent_predicate_id": "D_type"}],
        "pattern": {"variables": ["A", "B", "C", "D"], "wildcard": "Z", "wildcard_quantifier": "*", "gap_quantifiers": ["*", "*?", "*"]},
        "independent_predicates": [typ(v) for v in "ABCD"],
        "dependent_predicates": [
            {"predicate_id": "A_B_eq", "kind": "dependent", "variables": ["A", "B"], "sql_template": "B.veq = A.veq",
             "referenced_columns": ["veq"], "selectivity_key": "A|B"},
            {"predicate_id": "A_D_rng", "kind": "dependent", "variables": ["A", "D"], "sql_template": f"D.vr1 BETWEEN A.vr1 - {w} AND A.vr1 + {w}",
             "referenced_columns": ["vr1"], "selectivity_key": "A|D"},
            {"predicate_id": "B_D_rng", "kind": "dependent", "variables": ["B", "D"], "sql_template": f"D.vr2 BETWEEN B.vr2 - {w} AND B.vr2 + {w}",
             "referenced_columns": ["vr2"], "selectivity_key": "B|D"}],
        "measures": [{"expression": "A.id", "alias": "a_id", "sql_type": "BIGINT"}, {"expression": "B.id", "alias": "b_id", "sql_type": "BIGINT"},
                     {"expression": "FIRST(C.id)", "alias": "c_first_id", "sql_type": "BIGINT"},
                     {"expression": "LAST(C.id)", "alias": "c_last_id", "sql_type": "BIGINT"},
                     {"expression": "D.id", "alias": "d_id", "sql_type": "BIGINT"}],
        "result_key_columns": ["a_id", "b_id", "c_first_id", "c_last_id", "d_id"]}


def build_spec(sigma: float) -> Path:
    out = SPECS / f"kleene4_sig{sigma}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(kleene_spec(sigma), indent=2) + "\n")
    os.replace(tmp, out)
    return out


def with_single_views(dep, toks: list) -> list:
    """the pick's views plus single-variable views."""
    pos = dep.positions
    covered = {x for t in toks for x in make_view(t.split("_"))}
    return sorted(set(list(toks) + [v for v in sorted(dep.variables, key=lambda v: pos[v]) if v not in covered]))


def select_pick(dep, wl, batches: int) -> list:
    pos = dep.positions
    pick = select_clique_grow_plan(dep, wl, query_batches=frozenset(range(1, batches + 1)), kappa=BACKEND.kappa)
    return sorted("_".join(sorted(v, key=lambda x: pos[x])) for v in pick.compose)


@tl.track_peak
def eimer_kleene_arm(client, dep, toks, wl, table: str, n: int, batches: int, key_cols: list, wall: float, stitch: bool):
    """EIMER on a strategy with a Kleene view: per batch the views' updates (the Kleene view by rescan or by the
    stitch) and the composed query. Returns a record dict and the final tuple set."""
    engine = trino_exec.TrinoEngine(client)
    engine.set_wall(wall)
    hist, bat = "hist_bp", "bat_bp"
    st = tl.eimer_arm.cover_statements(dep, list(toks), set(dep.variables), wl, dialect=SqlDialect.TRINO, hist=hist, bat=bat)
    result_q = (getattr(st, "post_filter_sql", "") or st.composition_sql).rstrip().rstrip(";")
    kleene_vars = {v for v in dep.variables if dep.quantifiers.get(v) in ("PLUS", "RELUCTANT_PLUS")}
    cols_by_var = collect_referenced_columns(dep)
    views = []
    for u in st.update_statements:
        m = re.match(r"\s*INSERT\s+INTO\s+(cache_\w+)\s*(\(([^)]*)\))?\s*", u, re.I)
        cache, cols = m.group(1), m.group(3)
        var = cache[len("cache_"):]
        views.append((cache, cols, u[m.end():].rstrip().rstrip(";"), var if var in kleene_vars else None))
    stg = {v: _kleene_staging_table_name(make_view([v]), dep.positions) for *_, v in views if v}
    open_a = {v: None for v in stg}
    keysel = ", ".join(key_cols)
    tot = upd = kl = comp = 0.0
    per_batch, tuples, final = [], None, None
    t_start = time.time()
    tables = [hist, bat] + [c for c, *_ in views] + list(stg.values())
    try:
        for t in (hist, bat):
            engine.drop_table(t)
            engine.execute(f"CREATE TABLE {t} AS SELECT * FROM {table} WITH NO DATA")
        for cache, cols, sel, var in views:
            engine.drop_table(cache)
            engine.execute(f"CREATE TABLE {cache} AS {sel}")
            if var and stitch:
                engine.drop_table(stg[var])
                engine.execute(f"CREATE TABLE {stg[var]} AS SELECT * FROM {cache} WITH NO DATA")
        for b in range(1, batches + 1):
            lo, hi = ((b - 1) * n) // batches, (b * n) // batches
            engine.drop_table(bat)
            engine.execute(f"CREATE TABLE {bat} AS SELECT * FROM {table} WHERE id > {lo} AND id <= {hi}")
            engine.execute(f"INSERT INTO {hist} SELECT * FROM {bat}")
            b_upd = b_kl = 0.0
            for cache, cols, sel, var in views:
                if var is None:
                    b_upd += _timed(client, f"INSERT INTO {cache} {sel}")
                elif not stitch:
                    b_kl += _timed(client, f"INSERT INTO {cache} ({cols}) {sel}")
                else:
                    redetect_lo = open_a[var] if open_a[var] is not None else lo + 1
                    raw = _mr_group_detect_update_sql(var, dep, cols_by_var, hist, bat, redetect_lo=redetect_lo, target_table=stg[var]).rstrip().rstrip(";")
                    b_kl += _timed(client, tl.clear_sql(f"DELETE FROM {stg[var]}"))
                    b_kl += _timed(client, raw)
                    b_kl += _timed(client, f"INSERT INTO {cache} ({cols}) SELECT {cols} FROM {stg[var]}")
                    t0 = time.perf_counter()
                    nxt = client.execute(f"SELECT MIN({var}_first_id) FROM {stg[var]} WHERE {var}_last_id = {hi}")[1][0][0]
                    b_kl += (time.perf_counter() - t0) * 1000.0
                    open_a[var] = int(nxt) if nxt is not None else None
            last = (b == batches)
            t0 = time.perf_counter()
            _, rows = engine.execute(f"SELECT {keysel if last else 'count(*)'} FROM ({result_q})")
            b_comp = (time.perf_counter() - t0) * 1000.0
            if last:
                tuples = frozenset(tuple(int(x) for x in r) for r in rows)
                final = len(rows)
            upd += b_upd; kl += b_kl; comp += b_comp; tot += b_upd + b_kl + b_comp
            per_batch.append([b, round(b_upd, 1), round(b_kl, 1), round(b_comp, 1)])
        return dict(status="ok", total_ms=round(tot, 1), final_count=final, cover=list(toks), kleene_maintenance=("stitch" if stitch else "rescan"),
                    update_ms=round(upd + kl, 1), kleene_view_ms=round(kl, 1), other_views_ms=round(upd, 1), compose_ms=round(comp, 1),
                    per_batch_ms=per_batch, wall_clock_s=round(time.time() - t_start, 1)), tuples
    except TrinoClientError as e:
        return dict(status=("dnf" if trino_exec.is_dnf(e) else "error"), notes=str(e)[:300], spent_ms=round(tot, 1), batches_done=len(per_batch),
                    wall_clock_s=round(time.time() - t_start, 1)), None
    finally:
        for t in tables:
            engine.drop_table(t)


def run_sweep(c: dict, client) -> dict:
    n, B, l_run = c["n"], c["batches"], c["l_run"]
    T = wall_s()
    spec = load_query_spec_file(str(build_spec(c["sigma"])))
    dep = query_spec_to_dependency_graph(spec)
    key_cols = list(spec.result_key_columns)
    table = f"events_kleene_n{n}_l{l_run}"
    try:
        r = client.execute(f"SELECT count(*), min(id), max(id) FROM {table}")[1][0]
        loaded = (int(r[0]), int(r[1]), int(r[2])) == (n, 1, n)
    except (TrinoClientError, TypeError):
        loaded = False
    if not loaded:
        tl.load_rows(client, table, "id BIGINT, ts BIGINT, primary_type VARCHAR, veq BIGINT, vr1 BIGINT, vr2 BIGINT", kleene_rows(n, l_run, c["p_group"]))
    t0 = time.time()
    wl_live, payload_live = tl.estimated_workload(client, dep, table, n, B, BACKEND.window_rule)
    pick_live = select_pick(dep, wl_live, B)
    sel_s = round(time.time() - t0, 1)
    ref = REFERENCE.get(cell_name(c))
    if ref is not None:
        sels = parse_selectivities_payload(ref["selectivities"], dep)
        wl = make_workload(dep, total_events=n, batches=B, selectivities=sels, window_rule=BACKEND.window_rule)
        pick_paper_estimates = select_pick(dep, wl, B)
        toks = with_single_views(dep, ref["pick"])
        selection = dict(executed="the paper's pick with the paper's estimates", pick=list(ref["pick"]), pick_this_run=pick_live,
                         selector_on_paper_estimates=pick_paper_estimates, selector_reproduces_paper_pick=(pick_paper_estimates == list(ref["pick"])))
    else:
        sels = parse_selectivities_payload(payload_live, dep)
        wl, toks = wl_live, with_single_views(dep, pick_live)
        selection = dict(executed="this run's selection", pick=pick_live, pick_this_run=pick_live)
    selection.update(algorithm="clique-grow", kappa=BACKEND.kappa, estimator="b_unified on the loaded table, each stated predicate on its own", selection_s=sel_s,
                     estimates_this_run=payload_live)
    rec = dict(table=table, key_cols=key_cols, limits=dict(statement_s=T, mr_total_s=tl.MR_CELL_BOUND_S), pick=toks, selection=selection,
               expected_matches=n // c["p_group"] if (n % c["p_group"] == 0 or (n % c["p_group"]) > 5 + l_run) else None)
    print(f"   cover {toks} | table {table}", flush=True)

    wl_g = make_workload(dep, total_events=N_GATE, batches=B_GATE, selectivities=sels, window_rule=BACKEND.window_rule)
    g_mr, g_mr_t = tl.mr_streaming(client, spec, table, N_GATE, B_GATE, [B_GATE], key_cols, T, bounded=False)
    g_ns, g_ns_t = eimer_kleene_arm(client, dep, toks, wl_g, table, N_GATE, B_GATE, key_cols, T, stitch=False)
    g_st, g_st_t = eimer_kleene_arm(client, dep, toks, wl_g, table, N_GATE, B_GATE, key_cols, T, stitch=True)
    ok = lambda t: (t == g_mr_t) if (t is not None and g_mr_t is not None) else None
    rec["gate_first_events"] = dict(n=N_GATE, batches=B_GATE, n_mr=g_mr.get("final_count"), n_nostitch=g_ns.get("final_count"), n_stitch=g_st.get("final_count"),
                                    nostitch_eq_mr=ok(g_ns_t), stitch_eq_mr=ok(g_st_t))
    print(f"   gate (first {N_GATE} events, {B_GATE} batches): {rec['gate_first_events']}", flush=True)

    qb = tl.query_schedule(B)
    t0 = time.time()
    rec["mr"], m_t = tl.mr_streaming(client, spec, table, n, B, qb, key_cols, T, bounded=False)
    rec["mr"]["wall_clock_s"] = round(time.time() - t0, 1)
    print(f"   mr             : {rec['mr']['status']} total={rec['mr'].get('total_ms')} ms matches={rec['mr'].get('final_count')} {rec['mr'].get('censored') or ''}", flush=True)
    rec["eimer_nostitch"], ns_t = eimer_kleene_arm(client, dep, toks, wl, table, n, B, key_cols, T, stitch=False)
    rec["eimer_stitch"], st_t = eimer_kleene_arm(client, dep, toks, wl, table, n, B, key_cols, T, stitch=True)
    for a in ("eimer_nostitch", "eimer_stitch"):
        x = rec[a]
        print(f"   {a:15s}: {x['status']} total={x.get('total_ms')} ms (Kleene view {x.get('kleene_view_ms')} / other views {x.get('other_views_ms')} / queries {x.get('compose_ms')}) "
              f"matches={x.get('final_count')}", flush=True)
    cmp = [(st_t == ns_t) if (st_t is not None and ns_t is not None) else None,
           (ns_t == m_t) if (ns_t is not None and m_t is not None) else None,
           (st_t == m_t) if (st_t is not None and m_t is not None) else None]
    g = rec["gate_first_events"]
    cnt = rec["eimer_stitch"].get("final_count")
    rec["gates"] = dict(stitch_eq_nostitch=cmp[0], nostitch_eq_mr=cmp[1], stitch_eq_mr=cmp[2],
                        results_identical=(all(x for x in cmp if x is not None) and g["nostitch_eq_mr"] is not False and g["stitch_eq_mr"] is not False
                                           if any(x is not None for x in cmp) else None),
                        n_compared=sum(x is not None for x in cmp),
                        matches_as_expected=((cnt == rec["expected_matches"]) if (cnt is not None and rec["expected_matches"] is not None) else None))
    print(f"   gates          : {rec['gates']}", flush=True)
    return rec


def run_cell(c: dict, out_root: Path) -> dict:
    name = cell_name(c)
    rec_path = out_root / "records" / f"{name}.json"
    rec_path.parent.mkdir(parents=True, exist_ok=True)
    client = tl.connect(timeout_s=wall_s())
    print(f"[{name}] {c}", flush=True)
    body = run_payoff(c, client) if c["part"] == "payoff" else run_sweep(c, client)
    rec = dict(cell=name, figure="11, Kleene (Trino)", params=dict(c), **tl.engine_info(client),
               protocol=dict(warmup=False, ingestion="untimed", query_schedule="after every batch",
                             server_warmed_by_untimed_first_workload=(os.environ.get("EIMER_REPRO_SERVER_WARMUP") == "1")),
               **body)
    rec["_recorded"] = tl.write_record(out_root, name, rec)
    return rec


def main():
    tl.run_main(__doc__, cells, cell_name, run_cell, HERE / "results_kleene")


if __name__ == "__main__":
    main()
