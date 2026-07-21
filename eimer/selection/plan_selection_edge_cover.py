"""structural edge-cover plan selector.

picks the materialized-view cover from the dependency-graph value edges, with no cost model: each
value edge X-Y becomes a 2-view make_view([X,Y]). the pick is the full edge-cover, capped to max_views
by priority (sigma ascending, span descending); uncovered variables auto-fill as singleton base scans
in build_plan. the returned .score is a structural proxy (-n_equi_join_steps).
"""
from eimer.models import make_view, view_sort_key
from eimer.selection.join_order import _edge_sigma, analytic_node_size, within_storage_budget
from eimer.selection.plan_build import PlanResult, build_plan, valid_views, label, with_kleene_caches


def _n_equi(cover):
    """number of cover view-pairs that share a variable (equi-join steps in the compose)."""
    cov = list(cover)
    return sum(1 for i in range(len(cov)) for j in range(i + 1, len(cov)) if cov[i] & cov[j])


def select_edge_cover_plan(dep, workload, *, profile=None, max_views=4, query_batches=None,
                           max_storage_rows=None):
    """structural edge-cover pick. returns a PlanResult whose .compose is the chosen cover: every
    value-edge view the dependency graph defines, capped to max_views (uncovered variables auto-fill as
    singleton base scans)."""
    if workload is None:
        raise ValueError("select_edge_cover_plan requires a workload (build_plan needs a sigma source)")
    pos = dep.positions
    all_vars = set(dep.variables)
    bidx = len(workload.batch_sizes)            # qlast snapshot for sigma/card (stable for ConstantSelectivity)
    vvset = set(valid_views(dep))

    # STEP 1: enumerate value-edge 2-views, de-dup by frozenset, tag with sigma + positional span
    seen, cand = set(), []
    for e in dep.edges:
        v = make_view([e.var_left, e.var_right])
        if v in seen or v not in vvset:
            continue
        seen.add(v)
        cand.append({"view": v,
                     "sigma": _edge_sigma(workload, bidx, e.var_left, e.var_right),
                     "card": analytic_node_size(v, workload, bidx),
                     "span": abs(pos[e.var_left] - pos[e.var_right])})

    if not cand:
        cover = frozenset()                     # edgeless pattern: only case the empty cover is allowed
        capped = False
    else:
        # STEP 2: priority is smallest/most-selective first, then longest span, then canonical tiebreak
        cand.sort(key=lambda c: (c["sigma"], -c["span"], view_sort_key(c["view"], pos)))

        # STEP 3: keep every value-edge view, so the cover is all views the dependency graph defines
        survivors = list(cand)

        # STEP 4: cap to max_views by merging least-priority overlapping pairs that genuinely collapse, else drop
        capped = False
        while len(survivors) > max_views:
            capped = True
            merged = False
            for i in range(len(survivors) - 1, 0, -1):
                a, b = survivors[i - 1]["view"], survivors[i]["view"]
                if not (a & b):
                    continue
                u = make_view(set(a) | set(b))
                if u not in vvset or len(u) > len(all_vars) - 1:   # never form the full-pattern view
                    continue
                if analytic_node_size(u, workload, bidx) > (analytic_node_size(a, workload, bidx)
                                                            + analytic_node_size(b, workload, bidx)):
                    continue                                       # union is not a collapse, skip
                survivors[i - 1] = {"view": u,
                                    "sigma": min(survivors[i - 1]["sigma"], survivors[i]["sigma"]),
                                    "span": max(survivors[i - 1]["span"], survivors[i]["span"])}
                del survivors[i]
                merged = True
                break
            if not merged:
                survivors.pop()                                    # drop the least-priority tail edge

        # STEP 4b: storage gate (rows). tighten until maintained resident rows fit M: prefer a
        # resident-reducing merge (same guards as STEP 4), else drop the largest-resident survivor.
        # never drop below one view; if even that plus forced Kleene caches exceeds M, flag infeasible.
        if max_storage_rows is not None:
            # maintained set = the surviving cover views plus the mandatory Kleene singleton caches
            # build_plan force-materializes (no-op for Kleene-free queries); gate on the true resident.
            def _maint_survivors():
                cv = [c["view"] for c in survivors]
                return with_kleene_caches(dep, cv, cv, all_vars)[0]
            while len(survivors) > 1 and not within_storage_budget(
                    _maint_survivors(), workload, max_storage_rows):
                did_merge = False
                for i in range(len(survivors) - 1, 0, -1):
                    a, b = survivors[i - 1]["view"], survivors[i]["view"]
                    if not (a & b):
                        continue
                    u = make_view(set(a) | set(b))
                    if u not in vvset or len(u) > len(all_vars) - 1:
                        continue
                    if analytic_node_size(u, workload, bidx) <= (analytic_node_size(a, workload, bidx)
                                                                 + analytic_node_size(b, workload, bidx)):
                        survivors[i - 1] = {"view": u,
                                            "sigma": min(survivors[i - 1]["sigma"], survivors[i]["sigma"]),
                                            "span": max(survivors[i - 1]["span"], survivors[i]["span"])}
                        del survivors[i]
                        did_merge = True
                        break
                if did_merge:
                    continue
                drop_i = max(range(len(survivors)),
                             key=lambda i: (analytic_node_size(survivors[i]["view"], workload, bidx),
                                            view_sort_key(survivors[i]["view"], pos)))
                del survivors[drop_i]
        cover = frozenset(c["view"] for c in survivors)

    # STEP 5: build the plan; uncovered variables auto-fill as singletons in compose
    strategy, evaluation_plan, _ = build_plan(dep, cover, cover, all_vars, workload=workload)
    cert = {"selector": "edge_cover",
            "n_edges": len(seen), "n_kept": len(cover), "n_equi_join_steps": _n_equi(cover),
            "capped": capped, "cover_label": label(cover, pos)}
    if max_storage_rows is not None:
        cert["storage_infeasible"] = not within_storage_budget(
            with_kleene_caches(dep, cover, cover, all_vars)[0], workload, max_storage_rows)
    return PlanResult(family=cover, compose=cover, score=float(-cert["n_equi_join_steps"]),
                      evals=0, certificate=cert, escalated=False,
                      strategy=strategy, evaluation_plan=evaluation_plan, positions=pos)


if __name__ == "__main__":
    import argparse
    from pathlib import Path
    import sys
    sys.path.insert(0, "execution")
    import bootstrap  # noqa: F401
    from eimer.workload import make_workload
    from selectivity_payload import load_selectivities
    from eimer.query.query_spec import load_query_spec_file, query_spec_to_match_recognize_sql
    from eimer.query.graph_adapter import build_dependency_graph_from_sql_text
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--sel", required=True)
    ap.add_argument("--events", type=int, default=25000)
    ap.add_argument("--batches", type=int, default=5)
    ap.add_argument("--max-views", type=int, default=4)
    a = ap.parse_args()
    spec = load_query_spec_file(a.spec)
    dep = build_dependency_graph_from_sql_text(query_spec_to_match_recognize_sql(spec))
    sels, _ = load_selectivities(Path(a.sel), dep)
    wl = make_workload(dep, total_events=a.events, batches=a.batches, selectivities=sels)
    r = select_edge_cover_plan(dep, wl, max_views=a.max_views)
    print(r.certificate)
