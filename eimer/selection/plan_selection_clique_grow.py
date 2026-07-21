"""clique-grow plan selector: picks a materialized-view cover by amortized cost.

ranks covers by TotalCost = Q * compose_cost + kappa * B * maintenance, where
maintenance sums each view's prefix cardinality. the search descends from the flat
edge cover by local merge/drop moves, compares an empty and a full-pattern anchor,
and takes the global argmin (deterministic tie-break). an optional storage budget M
repairs the pick to fit. compose cost comes from best_bushy_tree.
"""
from __future__ import annotations

from eimer.models import make_view, view_sort_key
from eimer.selection.join_order import (_edge_sigma, analytic_node_size, best_bushy_tree,
                          max_resident_rows, within_storage_budget)
from eimer.plans.composition import build_composition_join_graph_for_nodes
from eimer.selection.plan_build import PlanResult, build_plan, label, valid_views, with_kleene_caches
from eimer.sql.sql_render import is_kleene_variable
from eimer.selection.state_size_trino import peak_state_bytes_trino

# c_M/c_C maintenance-to-compose unit ratio
KAPPA_DEFAULT = 150.0


def _cyclic_bloat(varset, dep, wl, bidx):
    """build-cost correction for cyclic views (join-order invariant). a view's induced
    subgraph has c = |E| - (|V|-1) closing edges that no join order can apply as join keys;
    each is a residual that bloats the transient by 1/sigma. returns the bloat over the
    loosest c edges (a lower bound), or 1.0 when the view is acyclic and the model is exact."""
    vs = set(varset)
    edges = [(e.var_left, e.var_right) for e in dep.edges if {e.var_left, e.var_right} <= vs]
    c = len(edges) - (len(vs) - 1)
    if c <= 0:
        return 1.0
    loosest = sorted((_edge_sigma(wl, bidx, a, b) for a, b in edges), reverse=True)[:c]
    bloat = 1.0
    for s in loosest:
        bloat /= max(s, 1e-12)
    return bloat


def _prefix_card(varset, wl, bidx, pos, dep):
    """maintenance-cost driver: |prefix(V)| (V minus its newest variable, the delta driver),
    inflated by V's cyclic build-bloat. singletons have no prefix."""
    if len(varset) < 2:
        return 0.0
    last = max(varset, key=lambda v: pos[v])
    base = analytic_node_size(make_view([v for v in varset if v != last]), wl, bidx)
    return base * _cyclic_bloat(varset, dep, wl, bidx)


def _compose_cost(dep, cover_sets, wl, bidx):
    """bushy compose-tree split-cost over the cover views plus live singletons for uncovered
    variables; a single-node compose yields 0. ij_over_fp=1.0 makes the equi-step constant
    selection-inert for cover ranking (join_order keeps its own default for absolute scores)."""
    all_vars = set(dep.variables)
    covered = set().union(*cover_sets) if cover_sets else set()
    nodes = {make_view(s) for s in cover_sets} | {make_view([v]) for v in sorted(all_vars - covered)}
    return best_bushy_tree(build_composition_join_graph_for_nodes(dep, nodes), wl, bidx,
                           ij_over_fp=1.0)[1]


def _canon(cover_sets):
    """canonical view-set of a cover: dedup, then drop any view that is a proper subset of
    another (keeping it would double-count maintenance and grant a phantom compose discount).
    cost depends only on this canonical set."""
    cs = list({frozenset(s) for s in cover_sets})
    return [s for s in cs if not any(s != t and s < t for t in cs)]


def _total_cost(dep, cover_sets, wl, bidx, B, Q, kappa):
    cs = _canon(cover_sets)
    compose = _compose_cost(dep, cs, wl, bidx)
    maint = sum(_prefix_card(s, wl, bidx, dep.positions, dep) for s in cs)
    return Q * compose + kappa * B * maint


def _n_equi(cover_sets):
    """number of compose view-pairs sharing >=1 variable (equi-join steps)."""
    cs = [set(s) for s in cover_sets]
    return sum(1 for i in range(len(cs)) for j in range(i + 1, len(cs)) if cs[i] & cs[j])


def _drop(cover, i):
    return [c for k, c in enumerate(cover) if k != i]


def _merge(cover, i, j, u):
    return [c for k, c in enumerate(cover) if k not in (i, j)] + [u]


def _sorted(cover, pos):
    return sorted(cover, key=lambda s: view_sort_key(make_view(s), pos))


def _skey(cover, pos):
    return tuple(sorted(view_sort_key(make_view(s), pos) for s in cover))


def _hill_climb(start, cost, vvset, pos, *, allow_drop):
    """descend from ,,start'' by the single best TotalCost-lowering move each step: merge two
    overlapping views (clique growth), or drop a view. deterministic, ranking moves by
    (cost, canonical key). allow_drop=False gives a merge-only descent into the consolidation
    basin; running it alongside the merge+drop descent keeps the saddle from trapping the result."""
    cover = _sorted(start, pos)
    while True:
        base = cost(cover)
        moves = []  # (newcost, canonical-key, newcover)
        for i in range(len(cover)):
            for j in range(i + 1, len(cover)):
                if cover[i] & cover[j]:
                    u = frozenset(cover[i] | cover[j])
                    if u in vvset:
                        nc = _sorted(_merge(cover, i, j, u), pos)
                        moves.append((cost(nc), _skey(nc, pos), nc))
        if allow_drop:
            for i in range(len(cover)):
                nc = _sorted(_drop(cover, i), pos)
                moves.append((cost(nc), _skey(nc, pos), nc))
        moves = [m for m in moves if m[0] < base - 1e-6]
        if not moves:
            return cover
        moves.sort(key=lambda m: (m[0], m[1]))
        cover = moves[0][2]


def _enforce_budget(cover, cost, vvset, max_views, pos):
    """reduce a cover to <= max_views, preferring the least-cost merge, then the least-cost drop."""
    cover = _sorted(_canon(cover), pos)
    while len(cover) > max_views:
        cands = []
        for i in range(len(cover)):
            for j in range(i + 1, len(cover)):
                if cover[i] & cover[j]:
                    u = frozenset(cover[i] | cover[j])
                    if u in vvset:
                        nc = _sorted(_merge(cover, i, j, u), pos)
                        cands.append((cost(nc), _skey(nc, pos), nc))
        if not cands:
            cands = [(cost(_sorted(_drop(cover, i), pos)), _skey(_drop(cover, i), pos),
                      _sorted(_drop(cover, i), pos)) for i in range(len(cover))]
        cands.sort(key=lambda t: (t[0], t[1]))
        cover = cands[0][2]
    return cover


def select_clique_grow_plan(dep, wl, *, max_views=None, query_batches=None, kappa=KAPPA_DEFAULT, profile=None,
                            max_storage_rows=None, max_storage_bytes=None, peak_bytes_fn=None):
    """amortized-cost cover selection: descends from the flat edge cover, compares the empty
    and full-pattern anchors, and returns the global argmin as a PlanResult (deterministic
    tie-break). the cover is materialized via build_plan, uncovered variables filling in as
    singletons. ,,profile'' is accepted for call-site parity but ignored.

    max_views: optional structural cap on cover size (forced least-cost merges/drops); default
    None lets the cost model decide the size.

    max_storage_bytes: storage budget M. take the unconstrained pick, then drop the view with
    the least cost increase per byte freed until the predicted peak fits M; the anchors that fit
    M then compete with the repaired cover, and the empty anchor always fits, so selection never
    fails. None (default) = M=inf, byte-identical to the unconstrained selector. mutually
    exclusive with max_storage_rows (the byte model is validated on Kleene-free workloads only).

    peak_bytes_fn: optional byte-model override, fn(cover_sets, dep, wl, query_batches=...) ->
    int. default = the calibrated trino/iceberg parquet model (peak_state_bytes_trino)."""
    pos = dep.positions
    bidx = len(wl.batch_sizes)
    B = bidx
    Q = len(query_batches) if query_batches else B
    all_vars = set(dep.variables)
    # kleene vars live in their own singleton cache, never inside a multi-variable view.
    # vvset gates every multi-var view the search can introduce, so dropping kleene-containing
    # views here keeps candidates clean; the kleene singletons are re-added at build time.
    kleene_vars = {v for v in all_vars if is_kleene_variable(v, dep)}
    vvset = {v for v in valid_views(dep) if not (set(v) & kleene_vars)}

    def cost(cover):
        return _total_cost(dep, cover, wl, bidx, B, Q, kappa)

    # start from the flat value-edge cover: seeding here rather than at empty dodges the
    # supermodular trap where the spine only pays as a whole.
    edge_cover = _sorted({frozenset((e.var_left, e.var_right)) for e in dep.edges}, pos)

    # candidate covers: the merge+drop descent from the edge-cover seed, plus the two anchors
    # (,,full'' for edge-less patterns where the seed is empty; ,,empty'' for a drop-direction
    # local minimum). each candidate is canonicalized and forced within budget.
    full_view = [frozenset(all_vars)] if frozenset(all_vars) in vvset else None
    raw = [_hill_climb(edge_cover, cost, vvset, pos, allow_drop=True),
           [], full_view]
    cands = [_canon(c if max_views is None else _enforce_budget(c, cost, vvset, max_views, pos))
             for c in raw if c is not None]
    n_repair_drops = 0
    if max_storage_bytes is not None:
        if max_storage_rows is not None:
            raise ValueError("max_storage_rows and max_storage_bytes are mutually exclusive")

        _peak_fn = peak_bytes_fn or peak_state_bytes_trino

        def peak(c):
            return _peak_fn([frozenset(s) for s in c], dep, wl, query_batches=query_batches)

        # unconstrained pick, then budget repair: drop the view with the least cost increase per
        # predicted byte freed, re-evaluated after every drop, until the peak fits M.
        cover = min(cands, key=lambda c: (cost(c), _skey(c, pos)))
        while cover and peak(cover) > max_storage_bytes:
            base_cost, base_peak = cost(cover), peak(cover)
            best = None
            for i in range(len(cover)):
                nc = _sorted(_drop(cover, i), pos)
                freed = base_peak - peak(nc)
                if freed <= 0:
                    continue
                key = ((cost(nc) - base_cost) / freed, _skey(nc, pos))
                if best is None or key < best[0]:
                    best = (key, nc)
            if best is None:                       # no drop frees bytes -> drop all
                cover = []
                n_repair_drops += 1
                break
            cover = best[1]
            n_repair_drops += 1
        # the anchors that fit M compete with the repaired cover by (cost, canonical key);
        # ,,[]'' is the empty anchor at predicted peak 0, so a fitting candidate always exists.
        anchors = [[]]
        if full_view is not None and peak(full_view) <= max_storage_bytes:
            anchors.append(_sorted(list(full_view), pos))
        cover = min([cover] + anchors, key=lambda c: (cost(c), _skey(c, pos)))
    elif max_storage_rows is None:
        cover = min(cands, key=lambda c: (cost(c), _skey(c, pos)))  # global min; deterministic tie-break
    else:
        # storage gate: keep only covers whose maintained set fits the row budget. the maintained
        # set is the cover plus the mandatory Kleene singleton caches build_plan materializes
        # (a no-op for Kleene-free queries). if the Kleene floor alone exceeds M, ,,feasible'' is
        # empty and we fall back to the minimum-resident cover (flagged infeasible below).
        def _maint(c):
            return with_kleene_caches(dep, c, c, all_vars)[0]
        feasible = [c for c in cands if within_storage_budget(_maint(c), wl, max_storage_rows)]
        if feasible:
            cover = min(feasible, key=lambda c: (cost(c), _skey(c, pos)))
        else:
            cover = min(cands, key=lambda c: (max_resident_rows(_maint(c), wl), cost(c), _skey(c, pos)))

    cover_views = frozenset(make_view(s) for s in cover)
    strategy, evaluation_plan, _ = build_plan(dep, cover_views, cover_views, all_vars, workload=wl)
    cert = {"selector": "clique_grow", "cover_label": label(cover_views, pos),
            "n_views": len(cover_views), "n_equi_join_steps": _n_equi(cover),
            "kappa": kappa, "total_cost": cost(cover)}
    if max_storage_rows is not None:
        cert["storage_infeasible"] = not within_storage_budget(
            with_kleene_caches(dep, cover, cover, all_vars)[0], wl, max_storage_rows)
    if max_storage_bytes is not None:
        cert["max_storage_bytes"] = max_storage_bytes
        cert["predicted_peak_bytes"] = (peak_bytes_fn or peak_state_bytes_trino)(
            [frozenset(s) for s in cover], dep, wl, query_batches=query_batches)
        cert["budget_repair_drops"] = n_repair_drops
    return PlanResult(family=cover_views, compose=cover_views, score=-float(cost(cover)),
                            evals=0, certificate=cert, escalated=False,
                            strategy=strategy, evaluation_plan=evaluation_plan, positions=pos)
