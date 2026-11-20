#!/usr/bin/env python3
"""GDYN baseline on Trino: dynamic-Yannakakis-style evaluation of the chain query (with or without a window) in SQL.

Instead of materialized views of partial matches, GDYN keeps one reduced set per pattern variable (LIVE_<v>: the ids of
the events of <v>'s type that have a continuation up to the last variable along the chain) and enumerates the result
from these sets at query time.

Per batch (the paper's protocol; ingestion into the history table is untimed, as for EIMER):
  update   bottom-up along the chain, one statement group per variable: the new members of LIVE_<v> are the history
           events of <v>'s type with a chain successor among the new members of the next variable (the batch itself
           for the last one) that are not yet in LIVE_<v>;
  query    top-down enumeration: SEL_1 = the live first-variable events, SEL_j+1 = SEL_j extended by one variable
           (per first event the latest continuation, the result of MATCH_RECOGNIZE with greedy gaps). A window makes
           the query cyclic: each extension then checks the remaining variables with one EXISTS over their join, and
           every pair condition carries the window (a match's events all lie within W of its first event), as EIMER's
           views do. Without a window the query is acyclic and the reduced sets alone guarantee a continuation.
The reduced sets are maintained after every batch; the query phase runs on the queried batches only.
The join order is the chain order as written (the session disables join reordering).
"""
from __future__ import annotations

import time

import trino_exec
import trino_lib as tl
from trino_client import TrinoClientError

DDL = "id BIGINT, time TIMESTAMP(6), ts TIMESTAMP(6), primary_type VARCHAR, etype VARCHAR, lon DOUBLE, lat DOUBLE"


class ChainQuery:
    """chain A Z* B Z* ... over the generated events: variable i has type T<i>, consecutive variables are joined by
    the lon/lat band; with ,,w_ms'' > 0 the last variable lies within the window of the first (cyclic query)."""

    def __init__(self, k: int, w_ms: int, half_width=(0.02, 0.02)):
        assert w_ms >= 0 and w_ms % 1000 == 0, "the window is given in whole seconds (0 = no window)"
        self.k, self.w = k, w_ms
        self.window = self.cyclic = w_ms > 0
        self.hw_lon, self.hw_lat = half_width
        self.vs = list("ABCDEFGH"[:k])
        self.pos = {v: i for i, v in enumerate(self.vs)}
        self.types = {v: f"T{i}" for i, v in enumerate(self.vs)}
        self.consec = [(self.vs[i], self.vs[i + 1]) for i in range(k - 1)]

    def band_sql(self, x, y, col):
        return (f"{col(y, 'lon')} BETWEEN {col(x, 'lon')} - {self.hw_lon} AND {col(x, 'lon')} + {self.hw_lon} "
                f"AND {col(y, 'lat')} BETWEEN {col(x, 'lat')} - {self.hw_lat} AND {col(x, 'lat')} + {self.hw_lat}")

    def win_sql(self, x, y, col):
        return f"{col(y, 'ts')} BETWEEN {col(x, 'ts')} AND {col(x, 'ts')}+INTERVAL '{self.w // 1000}' SECOND"

    def conds_between(self, x, y, col) -> list:
        assert self.pos[x] < self.pos[y]
        out = [f"{col(y, 'id')} > {col(x, 'id')}"]
        if (x, y) in self.consec:
            out.append(self.band_sql(x, y, col))
        if self.window:
            out.append(self.win_sql(x, y, col))   # the window on (first, last); the bound it implies on every other pair
        return out


class GdynTrino:
    def __init__(self, client, q: ChainQuery, table: str, n: int, batches: int, wall_s: float, keys: list, query_batches=None,
                 state_fn=None):
        self.client, self.q, self.table, self.N, self.B, self.T, self.keys = client, q, table, n, batches, wall_s, keys
        self.live = {v: f"LIVE_{v}" for v in q.vs[:-1]}
        self.qb = None if query_batches is None else frozenset(query_batches)   # None = the query runs after every batch
        self.state_fn = state_fn   # optional: fn(client, tables) -> the persistent state's size, taken untimed at the end

    # ---- statement generators
    @staticmethod
    def hist_col(a):
        return lambda v, cn: f"{a}.{cn}"

    def consec_sql(self, x, y, ax, ay):
        return " AND ".join(self.q.conds_between(x, y, lambda v, cn: ax(v, cn) if v == x else ay(v, cn)))

    def chord_sql(self, j: int, acc: dict) -> list:
        vj = self.q.vs[j]
        return [self.q.win_sql(self.q.vs[0], vj, lambda v, cn: acc[v](v, cn))] if (self.q.window and j >= 2) else []

    def exists_chain(self, j: int, acc: dict) -> str:
        """one EXISTS over the join of the remaining variables, in chain order."""
        if j == self.q.k - 1:
            return ""
        src, conds, acc2 = [], [], dict(acc)
        for i in range(j + 1, self.q.k):
            vi, vp, z = self.q.vs[i], self.q.vs[i - 1], f"z{i}"
            acc2[vi] = self.hist_col(z)
            src.append(f"HIST_DY {z}" + (f", {self.live[vi]} l{i}" if i < self.q.k - 1 else ""))
            conds += [f"{z}.primary_type = '{self.q.types[vi]}'"] + ([f"l{i}.id = {z}.id"] if i < self.q.k - 1 else []) + \
                     [self.consec_sql(vp, vi, acc2[vp], acc2[vi])] + self.chord_sql(i, acc2)
        return f" AND EXISTS (SELECT 1 FROM {', '.join(src)} WHERE {' AND '.join(conds)})"

    def update_stmts(self) -> list:
        out, vs, ty = [], self.q.vs, self.q.types
        for j in range(self.q.k - 2, -1, -1):
            v, nxt = vs[j], vs[j + 1]
            if j == self.q.k - 2:
                src, ycond = "BAT_DY y", f"y.primary_type = '{ty[nxt]}'"
            else:
                src, ycond = f"NL_{nxt} n, HIST_DY y", "y.id = n.id"
            out += [f"DELETE FROM NL_{v}",
                    f"INSERT INTO NL_{v} SELECT DISTINCT x.id FROM {src}, HIST_DY x WHERE {ycond} AND x.primary_type = '{ty[v]}' "
                    f"AND {self.consec_sql(v, nxt, self.hist_col('x'), self.hist_col('y'))} AND NOT EXISTS (SELECT 1 FROM {self.live[v]} l WHERE l.id = x.id)",
                    f"INSERT INTO {self.live[v]} SELECT id FROM NL_{v}"]
        return out

    def query_stmts(self) -> list:
        vs, a = self.q.vs, self.q.vs[0]
        proj = lambda alias, v: f"{alias}.id AS {v}_id, {alias}.ts AS {v}_ts, {alias}.lon AS {v}_lon, {alias}.lat AS {v}_lat"
        out = ["DELETE FROM SEL_1", f"INSERT INTO SEL_1 SELECT a.id, a.ts, a.lon, a.lat FROM {self.live[a]} l, HIST_DY a WHERE a.id = l.id"]
        for j in range(1, self.q.k):
            v, prev = vs[j], vs[j - 1]
            src = f"SEL_{j} s, {self.live[v]} l, HIST_DY y" if j < self.q.k - 1 else f"SEL_{j} s, HIST_DY y"
            link = ["y.id = l.id"] if j < self.q.k - 1 else [f"y.primary_type = '{self.q.types[v]}'"]
            acc = {u: (lambda u_: (lambda vv, cn: f"s.{u_}_{cn}"))(u) for u in vs[:j]}
            acc[v] = self.hist_col("y")
            conds = link + [self.consec_sql(prev, v, acc[prev], acc[v])] + self.chord_sql(j, acc)
            if self.q.cyclic:
                conds.append("1 = 1" + self.exists_chain(j, acc))
            prev_cols = ", ".join(f"s.{u}_{cn}" for u in vs[:j] for cn in ("id", "ts", "lon", "lat"))
            inner = f"SELECT {prev_cols}, {proj('y', v)}, ROW_NUMBER() OVER (PARTITION BY s.{a}_id ORDER BY y.ts DESC) AS rn FROM {src} WHERE {' AND '.join(conds)}"
            outcols = ", ".join(f"{u}_{cn}" for u in vs[: j + 1] for cn in ("id", "ts", "lon", "lat"))
            out += [f"DELETE FROM SEL_{j + 1}", f"INSERT INTO SEL_{j + 1} SELECT {outcols} FROM ({inner}) WHERE rn = 1"]
        out += ["DELETE FROM RES_DY", f"INSERT INTO RES_DY SELECT {', '.join(self.keys)} FROM SEL_{self.q.k}"]
        return out

    # ---- execution
    def _drop(self, t):
        try:
            self.client.execute(f"DROP TABLE IF EXISTS {t}")
        except TrinoClientError:
            pass

    def tables(self):
        return ["HIST_DY", "BAT_DY", "RES_DY", *self.live.values(), *[f"NL_{v}" for v in self.q.vs[:-1]], *[f"SEL_{j}" for j in range(1, self.q.k + 1)]]

    def setup(self):
        """all scratch tables are created once (untimed); per batch only DELETE + INSERT ... SELECT run."""
        self.teardown()
        cols = lambda vs: ", ".join(f"{v}_id BIGINT, {v}_ts TIMESTAMP(6), {v}_lon DOUBLE, {v}_lat DOUBLE" for v in vs)
        self.client.execute(f"CREATE TABLE HIST_DY ({DDL})")
        for v, t in self.live.items():
            self.client.execute(f"CREATE TABLE {t} (id BIGINT)")
            self.client.execute(f"CREATE TABLE NL_{v} (id BIGINT)")
        for j in range(1, self.q.k + 1):
            self.client.execute(f"CREATE TABLE SEL_{j} ({cols(self.q.vs[:j])})")
        self.client.execute(f"CREATE TABLE RES_DY ({', '.join(f'{c} BIGINT' for c in self.keys)})")

    def teardown(self):
        for t in self.tables():
            self._drop(t)

    def _count(self, t):
        return int(self.client.execute(f"SELECT count(*) FROM {t}")[1][0][0])

    @tl.track_peak
    def run(self):
        """returns a record dict and the final key set (None unless it finished). ,,wall_s'' bounds every statement and,
        cumulatively, the whole arm."""
        pb, tot = [], 0.0
        t_start = time.time()
        self.client.execute(f"SET SESSION query_max_run_time = '{int(self.T)}s'")
        self.setup()
        upd_stmts = [tl.clear_sql(s) for s in self.update_stmts()]   # in-memory tables: emptied with TRUNCATE
        qry_stmts = [tl.clear_sql(s) for s in self.query_stmts()]
        try:
            for b in range(1, self.B + 1):
                lo, hi = ((b - 1) * self.N) // self.B, (b * self.N) // self.B
                self._drop("BAT_DY")   # ingestion, untimed
                self.client.execute(f"CREATE TABLE BAT_DY AS SELECT * FROM {self.table} WHERE id > {lo} AND id <= {hi}")
                self.client.execute("INSERT INTO HIST_DY SELECT * FROM BAT_DY")
                upd = qry = 0.0
                for s in upd_stmts:
                    t0 = time.perf_counter(); self.client.execute(s); upd += (time.perf_counter() - t0) * 1000
                for s in (qry_stmts if (self.qb is None or b in self.qb) else ()):
                    t0 = time.perf_counter(); self.client.execute(s); qry += (time.perf_counter() - t0) * 1000
                tot += upd + qry
                pb.append(dict(b=b, update_ms=round(upd, 2), query_ms=round(qry, 2)))
                if tot > self.T * 1000:
                    return dict(status="dnf", notes=f"total limit {self.T} s exceeded after batch {b}", spent_ms=round(tot, 1),
                                batches_done=b, wall_clock_s=round(time.time() - t_start, 1)), None
            _, rows = self.client.execute(f"SELECT {', '.join(self.keys)} FROM RES_DY")
            keys = frozenset(tuple(int(x) for x in r) for r in rows)
            live_rows = {v: self._count(t) for v, t in self.live.items()}
            state = self.state_fn(self.client, list(self.live.values())) if self.state_fn else None
            return dict(status="ok", total_ms=round(tot, 1), final_count=len(rows), final_distinct=len(keys), state=state,
                        n_queries=(self.B if self.qb is None else len(self.qb)),
                        update_ms=round(sum(x["update_ms"] for x in pb), 1), query_ms=round(sum(x["query_ms"] for x in pb), 1),
                        live_rows=live_rows, wall_clock_s=round(time.time() - t_start, 1)), keys
        except TrinoClientError as e:
            return dict(status=("dnf" if trino_exec.is_dnf(e) else "error"), notes=str(e)[:300], spent_ms=round(tot, 1),
                        batches_done=len(pb), wall_clock_s=round(time.time() - t_start, 1)), None
        finally:
            self.teardown()
