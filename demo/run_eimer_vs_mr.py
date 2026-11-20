#!/usr/bin/env python3
"""Demo: plan-selection quality + EIMER vs a stateless baseline.

Runs one small streaming cell (defaults: chain, k=4, N=5000, sigma=0.05, B=5):
generate the synthetic frame, load it into the engine, estimate selectivities with
the b_unified estimator on the loaded table, then execute the baseline arm
(cumulative re-scan per batch) and a portfolio of EIMER covers selected from those
estimates (the clique_grow selector's pick, the edge cover when it differs from the
pick, all-singletons).

Engines (--engine): trino (default) runs native MATCH_RECOGNIZE as the baseline;
duckdb has no native MATCH_RECOGNIZE, so the baseline is the SQL given by
--baseline-sql (a SELECT over the relation ,,events_window'', e.g. the query as
translated by a MATCH_RECOGNIZE transpiler; the query text to translate is written
to <out-dir>/mr_to_transpile.sql), and without it only the EIMER arms run.

Each EIMER arm must pass a full-tuple identity gate against the baseline result
before its runtime counts. Writes results.json (runtimes, gates) and a
timing-free regression_fingerprint.json (spec + per-arm SQL + tuple-set hashes)
under --out-dir.

  python3 demo/run_eimer_vs_mr.py --n 5000 --k 4 --topology chain --sigma 0.05 \
      --batches 5 --catalog memory --out-dir out/demo_eimer_vs_mr
  python3 demo/run_eimer_vs_mr.py --engine duckdb --baseline-sql transpiled.sql \
      --n 5000 --k 4 --batches 5 --out-dir out/demo_eimer_vs_mr_duckdb
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402  (also wires repo-root + execution onto sys.path)
import trino_exec  # noqa: E402
import duckdb_exec  # noqa: E402

from eimer.selection.plan_selection_clique_grow import select_clique_grow_plan  # noqa: E402
from eimer.selection.plan_selection_edge_cover import select_edge_cover_plan  # noqa: E402
from eimer.models import make_view  # noqa: E402
from eimer.sql.sql_emitter import generate_sql_statements_for_cover  # noqa: E402
from eimer.sql.sql_dialect import SqlDialect  # noqa: E402
from eimer.sql.backend import backend_for  # noqa: E402
from eimer.query.query_spec import query_spec_to_match_recognize_sql  # noqa: E402

ENGINES = {"trino": (trino_exec, SqlDialect.TRINO), "duckdb": (duckdb_exec, SqlDialect.DUCKDB)}


def _cover_tokens(cover_views, positions) -> list[str]:
    """Canonical 'A_B'-style tokens for a frozenset-of-Views cover."""
    return sorted("_".join(sorted(v, key=lambda x: positions[x])) for v in cover_views)


def _key_cols(dep) -> list[str]:
    pos = dep.positions
    return [f"{v}_id" for v in sorted(dep.variables, key=lambda v: pos[v])]


def _arm_sql_hash(dep, toks, all_vars, wl, dialect=SqlDialect.TRINO) -> str:
    """Deterministic (timing-free) hash of the emitter's rendered SQL for the cover."""
    o, _plan, nodes = trino_exec.build_cover_plan(dep, list(toks), all_vars, wl)
    st = generate_sql_statements_for_cover(o, dep, nodes, o.composition_plans[0],
                                           o.update_plans[0], base_table="hist_bp",
                                           batch_table="bat_bp", dialect=dialect)
    blob = "\n---\n".join(list(st.update_statements)
                          + [st.composition_sql, getattr(st, "post_filter_sql", "") or ""])
    return common.sha256_text(blob)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=5000, help="total events N (keep <= 5000 for the demo)")
    ap.add_argument("--k", type=int, default=4)
    ap.add_argument("--topology", default="chain")
    ap.add_argument("--sigma", type=float, default=0.05)
    ap.add_argument("--predicates", choices=("band", "equi", "band_equi"), default="band",
                    help="dependent-predicate class: lon/lat band, ekey equality, or both")
    ap.add_argument("--batches", type=int, default=5)
    ap.add_argument("--seed", type=int, default=common.SEED)
    ap.add_argument("--timeout", type=int, default=900, help="per-query wall T (seconds)")
    ap.add_argument("--engine", choices=tuple(ENGINES), default="trino")
    ap.add_argument("--duckdb-database", default=os.environ.get("DUCKDB_DATABASE", ":memory:"),
                    help="duckdb: database file (default in-memory)")
    ap.add_argument("--baseline-sql", type=Path, default=None,
                    help="duckdb: file with the stateless baseline SQL over relation "
                         "events_window (e.g. the transpiled MATCH_RECOGNIZE); omitted -> "
                         "EIMER arms only, gates unverified")
    ap.add_argument("--server", default=os.environ.get("TRINO_SERVER", "localhost:8080"))
    ap.add_argument("--catalog", default=os.environ.get("TRINO_CATALOG", "memory"))
    ap.add_argument("--schema", default=os.environ.get("TRINO_SCHEMA", "default"))
    ap.add_argument("--user", default=os.environ.get("TRINO_USER", "eimer"))
    ap.add_argument("--out-dir", default="out/demo_eimer_vs_mr")
    a = ap.parse_args()

    spec, dep, spec_path = common.load_spec_and_dep(a.k, a.topology, predicates=a.predicates)
    all_vars = set(dep.variables)
    pos = dep.positions
    qb = frozenset(range(1, a.batches + 1))  # query every batch
    key_cols = _key_cols(dep)
    out = Path(a.out_dir)
    exec_mod, dialect = ENGINES[a.engine]
    print(f"cell: {a.topology} k={a.k} N={a.n} sigma={a.sigma} B={a.batches} "
          f"predicates={a.predicates} engine={a.engine}")

    # --- generate + load ---
    frame = common.generate_frame(a.n, a.sigma, seed=a.seed, predicates=a.predicates)
    table = "demo_events"
    if a.engine == "trino":
        client = trino_exec.connect(server=a.server, catalog=a.catalog, schema=a.schema,
                                    user=a.user, timeout_s=a.timeout)
        where = f"{a.catalog}.{a.schema}.{table}"
    else:
        client = duckdb_exec.connect(database=a.duckdb_database, timeout_s=a.timeout)
        where = f"duckdb:{a.duckdb_database}:{table}"
    loaded = exec_mod.load_frame(client, frame, table=table)
    assert loaded == len(frame) == a.n, (loaded, len(frame), a.n)
    print(f"loaded {loaded} rows into {where}")
    # the query text a MATCH_RECOGNIZE transpiler must translate for the duckdb baseline
    # (reads the relation the baseline arm re-points at each cumulative window)
    mr_text = query_spec_to_match_recognize_sql(spec, events_table=duckdb_exec.BASELINE_INPUT_VIEW)
    out.mkdir(parents=True, exist_ok=True)
    (out / "mr_to_transpile.sql").write_text(mr_text.rstrip() + ";\n")

    # --- b_unified selectivities, measured on the loaded table ---
    wl, sigma_payload = exec_mod.b_unified_workload(client, dep, table=table,
                                                    total_events=a.n, batches=a.batches)
    common.write_json(out / "selectivities_b_unified.json", sigma_payload)
    print(f"b_unified estimates (generation targets: ftype={common.FTYPE}, "
          f"sigma={a.sigma}) -> {out / 'selectivities_b_unified.json'}")

    # --- the cover portfolio (selected from the measured estimates) ---
    kappa = backend_for(dialect).kappa  # the engine's calibrated maintenance-to-compose ratio
    pick = select_clique_grow_plan(dep, wl, query_batches=qb, kappa=kappa)
    edge = select_edge_cover_plan(dep, wl, query_batches=qb)
    print(f"selector: clique_grow with kappa={kappa} ({dialect.value})")
    arms: dict[str, list[str]] = {}
    arms["clique_grow_pick"] = _cover_tokens(pick.compose, pos)
    edge_toks = _cover_tokens(edge.compose, pos)
    if edge_toks != arms["clique_grow_pick"]:
        arms["edge_cover"] = edge_toks
    else:
        print("edge_cover: same cover as the selector pick (not re-run)")
    arms["all_singletons"] = sorted(all_vars, key=lambda v: pos[v])
    print(f"selector pick (clique_grow): {arms['clique_grow_pick']} "
          f"(certificate: {pick.certificate})")

    # --- baseline arm: native MATCH_RECOGNIZE (trino) / the supplied SQL (duckdb) ---
    if a.engine == "trino":
        baseline_name = "MATCH_RECOGNIZE"
        mr_ms, mr_n, mr_tuples, _meta = trino_exec.mr_streaming(
            client, spec, table, a.n, a.batches, qb, key_cols, a.timeout)
    elif a.baseline_sql is not None:
        baseline_name = "baseline_sql"
        mr_ms, mr_n, mr_tuples, _meta = duckdb_exec.baseline_streaming(
            client, a.baseline_sql.read_text(), table, a.n, a.batches, qb, key_cols, a.timeout)
    else:
        baseline_name, mr_ms, mr_n, mr_tuples = None, None, None, None
        print("no --baseline-sql: EIMER arms only, identity gates unverified in-engine")
    if baseline_name is not None and mr_ms is None:
        print(f"{baseline_name} arm DNF'd (hit the wall): enlarge --timeout or shrink the cell",
              file=sys.stderr)
        return 1
    if baseline_name is not None:
        print(f"{baseline_name:<18}: {mr_ms:>10.1f} ms   final matches = {mr_n}")
    tuple_sets = {}
    if mr_tuples is not None:
        tuple_sets[baseline_name] = mr_tuples

    # --- EIMER arms ---
    results = ({baseline_name: dict(total_ms=mr_ms, final_count=mr_n, gate="baseline")}
               if baseline_name else {})
    fingerprints = {}
    for name, toks in arms.items():
        ms, n, tuples, _state = exec_mod.eimer_streaming(
            client, dep, toks, all_vars, wl, spec, table, a.n, a.batches, qb,
            key_cols, a.timeout)
        if ms is None:
            results[name] = dict(total_ms=None, gate="DNF")
            print(f"{name:<18}: DNF")
            continue
        tuple_sets[name] = tuples
        gate = "UNVERIFIED" if mr_tuples is None else ("PASSED" if tuples == mr_tuples else "FAILED")
        results[name] = dict(total_ms=ms, final_count=n, gate=gate,
                             speedup_vs_baseline=round(mr_ms / ms, 2) if (ms and mr_ms) else None,
                             cover=toks)
        fingerprints[name] = dict(cover=toks, sql_sha256=_arm_sql_hash(dep, toks, all_vars, wl, dialect))
        speed = f"   speedup vs baseline = {mr_ms / ms:.2f}x" if mr_ms else ""
        print(f"{name:<18}: {ms:>10.1f} ms   gate={gate}{speed}   cover={toks}")

    # --- verdicts ---
    gates = [r.get("gate") for nm, r in results.items() if nm != baseline_name]
    all_pass = all(g in ("PASSED", "UNVERIFIED") for g in gates if g != "DNF") and "FAILED" not in gates
    verdict = "ALL PASSED" if all_pass and mr_tuples is not None else ("UNVERIFIED" if all_pass else "FAILURE")
    print(f"\nfull-tuple identity gates: {verdict}")

    cell = dict(topology=a.topology, k=a.k, n=a.n, sigma=a.sigma, batches=a.batches,
                seed=a.seed, catalog=a.catalog, predicates=a.predicates,
                selectivity_source="b_unified", engine=a.engine)
    common.write_json(out / "results.json", dict(cell=cell, results=results))
    for name, tuples in tuple_sets.items():
        common.write_json(out / f"tuples_{name}.json",
                          dict(key_cols=[c.lower() for c in key_cols], rows=sorted(list(t) for t in tuples)))
    common.write_json(out / "regression_fingerprint.json", dict(
        cell=cell,
        spec_sha256=common.sha256_text(Path(spec_path).read_text()),
        arms=fingerprints,
        mr_final_count=mr_n,
        mr_tuple_set_sha256=(common.sha256_text(json.dumps(sorted(list(t) for t in mr_tuples)))
                             if mr_tuples is not None else None),
        selector_certificate={k: v for k, v in pick.certificate.items()
                              if k != "total_cost"} | {
                                  "total_cost": round(pick.certificate["total_cost"], 6)},
    ))
    print(f"results -> {out / 'results.json'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
