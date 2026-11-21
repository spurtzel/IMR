#!/usr/bin/env python3
"""Join-based prefilter for MATCH_RECOGNIZE on Trino (Zhu, Huang, Chaudhuri: "High-Performance Row Pattern
Recognition Using Joins"), for the windowed chain queries of Fig. 9.

For a subsequence of the pattern variables that contains the first and the last one, the rewrite
  ranges     joins the events of these variables (types, bands between pattern-adjacent variables, time order, the
             window) and keeps the time range [first.ts, last.ts] of every combination,
  prefilter  keeps the events that lie in at least one range,
and evaluates the unchanged MATCH_RECOGNIZE on the prefiltered events only. Every subsequence yields the same result;
they differ in cost. All subsequences are executed and the fastest is reported.

The join carries the time bound the window implies on every pair of its variables, and the MATCH_RECOGNIZE states the
window on the gap variable as well.
"""
from __future__ import annotations

import time
from itertools import combinations

import trino_exec
import trino_lib as tl
from trino_client import TrinoClientError


def enumerate_subsets(vs: list) -> list:
    """the subsequences that contain the first and the last variable, in pattern order: 2^(k-2)."""
    first, last, interior = vs[0], vs[-1], vs[1:-1]
    return [(first, *combo, last) for r in range(len(interior) + 1) for combo in combinations(interior, r)]


def prefilter_mr_sql(spec, vs, types, half_width, subset, events_table, w_ms, key_cols) -> str:
    """one statement: ranges + prefilter over `subset`, then the query's MATCH_RECOGNIZE over the prefiltered events.
    `events_table` is the accumulated table expression, e.g. (SELECT * FROM events WHERE id <= hi)."""
    sub = list(subset)
    first, last = sub[0], sub[-1]
    assert (first, last) == (vs[0], vs[-1])
    hw_lon, hw_lat = half_width
    w = f"INTERVAL '{w_ms / 1000.0:g}' SECOND"
    conds = [f"{a}.ts <= {b}.ts" for a, b in zip(sub, sub[1:])]
    conds += [f"{v}.primary_type = '{types[v]}'" for v in sub]
    for left, right in zip(vs, vs[1:]):
        if left in sub and right in sub:
            conds.append(f"{right}.lon BETWEEN {left}.lon - {hw_lon} AND {left}.lon + {hw_lon} "
                         f"AND {right}.lat BETWEEN {left}.lat - {hw_lat} AND {left}.lat + {hw_lat}")
    conds.append(f"{last}.ts <= {first}.ts + {w}")
    conds += [f"{b}.ts <= {a}.ts + {w}" for i, a in enumerate(sub) for b in sub[i + 1:] if (a, b) != (first, last)]
    froms = ", ".join(f"({events_table}) {v}" for v in sub)
    ranges = (f"ranges AS (\n    SELECT {first}.ts AS t_s, {last}.ts AS t_e\n    FROM {froms}\n    WHERE "
              + "\n      AND ".join(conds) + "\n  )")
    prefilter = (f"prefilter AS (\n    SELECT e.* FROM ({events_table}) e\n"
                 "    WHERE EXISTS (SELECT 1 FROM ranges r WHERE e.ts BETWEEN r.t_s AND r.t_e)\n  )")
    mr = tl.bounded_mr_sql(spec, "prefilter").rstrip().rstrip(";")
    return f"WITH {ranges},\n  {prefilter}\nSELECT {', '.join(c.lower() for c in key_cols)} FROM ({mr})"


@tl.track_peak
def prefilter_streaming(client, spec, vs, types, half_width, subset, table, n, batches, qb, w_ms, key_cols, wall_s, cap_ms=None, total_s=None):
    """the prefiltered query over the accumulated table, once per queried batch. `wall_s` applies per statement.
    `cap_ms`: the best total of a subsequence already measured on this workload; a subsequence is stopped once it
    exceeds it (status "dominated"). `total_s`: limit for the subsequence's total; past it the subsequence ends as a
    DNF (its time is a lower bound).
    Returns a record dict and the final tuple set (None unless it finished)."""
    tot = 0.0
    tuples = None
    qbs = sorted(qb)
    done = 0
    for b in qbs:
        if total_s is not None and tot >= total_s * 1000.0:
            return dict(status="dnf", censored="cell_bound", bound_s=total_s, spent_ms=round(tot, 1), batches_done=done), None
        limit = wall_s if cap_ms is None else max(1.0, min(wall_s, (cap_ms - tot) / 1000.0 + 1.0))
        client.execute(f"SET SESSION query_max_run_time = '{int(limit) + 1}s'")
        hi = (b * n) // batches
        sql = prefilter_mr_sql(spec, vs, types, half_width, subset, f"SELECT * FROM {table} WHERE id <= {hi}", w_ms, key_cols)
        last = (b == qbs[-1])
        t0 = time.perf_counter()
        try:
            _, rows = client.execute(sql if last else f"SELECT count(*) FROM ({sql})")
        except TrinoClientError as e:
            if not trino_exec.is_dnf(e):
                raise
            spent = tot + (time.perf_counter() - t0) * 1000.0
            if cap_ms is not None and limit < wall_s:
                return dict(status="dominated", spent_ms=round(spent, 1), cap_ms=round(cap_ms, 1), batches_done=done), None
            return dict(status="dnf", censored="statement_limit", spent_ms=round(spent, 1), batches_done=done, notes=str(e)[:200]), None
        tot += (time.perf_counter() - t0) * 1000.0
        done += 1
        if last:
            tuples = frozenset(tuple(int(x) for x in r) for r in rows)
        elif cap_ms is not None and tot > cap_ms:
            return dict(status="dominated", spent_ms=round(tot, 1), cap_ms=round(cap_ms, 1), batches_done=done), None
    return dict(status="ok", total_ms=round(tot, 1), final_count=len(tuples)), tuples
