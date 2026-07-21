"""shared plan-building helpers for the cover selectors.

build_plan turns a (family, compose cover) into a materialized StrategyWithPlans and its
EvaluationPlan: a bushy compose join order grafted onto the canonical composition plan, and
bushy update source trees. valid_views enumerates the valid views a selector may pick, and
with_kleene_caches forces the mandatory Kleene singleton views. clique_grow and edge_cover
both build on these.
"""
import dataclasses
import itertools

from eimer.models import StrategyWithPlans, make_view, view_name
from eimer.sql.sql_render import is_kleene_variable
from eimer.plans.composition import (
    build_composition_join_graph_for_nodes,
    construct_canonical_composition_plan,
)
from eimer.plans.update_plans import construct_bushy_update_plan
from eimer.plans.evaluation_plan import build_evaluation_plan_for_cover
from eimer.selection.join_order import best_bushy_tree, to_join_tree_node


def valid_views(dep):
    """valid views: contiguous in pattern order, or every gap between consecutive selected
    variables is spanned by a value edge fully contained in the view."""
    pos = dep.positions
    variables = sorted(dep.variables, key=lambda v: pos[v])
    edges = {frozenset((e.var_left, e.var_right)) for e in dep.edges}
    out = []
    for r in range(1, len(variables) + 1):
        for combo in itertools.combinations(variables, r):
            sel = sorted(combo, key=lambda v: pos[v])
            ok = True
            for a, b in zip(sel, sel[1:]):
                if pos[b] - pos[a] == 1:
                    continue  # contiguous step
                # gap: need an edge inside the view spanning it (pos(x) <= pos(a) < pos(b) <= pos(y))
                if not any(min(pos[x] for x in e) <= pos[a] and max(pos[x] for x in e) >= pos[b]
                           for e in edges if e <= set(combo)):
                    ok = False; break
            if ok:
                out.append(frozenset(combo))
    return out


def label(family, positions):
    return "|".join(sorted(view_name(v, positions) for v in family)) or "(empty)"


def with_kleene_caches(dep, family, compose_views, all_vars):
    """force every Kleene variable as its own singleton materialized view. Kleene vars carry no
    value edge, so selectors never cover them; unforced they auto-fill as base-scan compose
    nodes, which the build rejects. inject {K} into both the family and the compose cover."""
    ks = {make_view([v]) for v in all_vars if is_kleene_variable(v, dep)}
    return set(family) | ks, set(compose_views) | ks


def build_plan(dep, family, compose_views, all_vars, *, workload):
    """(family, compose cover) -> (StrategyWithPlans, EvaluationPlan, nodes). uncovered variables
    become base-scan compose nodes; compose join order is the bushy tree grafted onto the canonical
    compose plan, update sources are bushy source trees. both are cost-DP over the workload
    selectivities, so a workload is required."""
    if workload is None:
        raise ValueError("plan_build requires a workload: bushy compose/update need a sigma source")
    family, compose_views = with_kleene_caches(dep, family, compose_views, all_vars)
    covered = set().union(*compose_views) if compose_views else set()
    nodes = set(compose_views) | {make_view([v]) for v in sorted(all_vars - covered)}
    jg = build_composition_join_graph_for_nodes(dep, nodes)
    tree, _ = best_bushy_tree(jg, workload, len(workload.batch_sizes))
    comp = dataclasses.replace(construct_canonical_composition_plan(jg, dep.positions),
                               join_tree=to_join_tree_node(tree))
    upd = construct_bushy_update_plan(frozenset(family), dep, workload)
    o = StrategyWithPlans(strategy=set(family), effective_view_set=set(jg.nodes),
                          composition_join_graph=jg, composition_plans=[comp], update_plans=[upd])
    plan = build_evaluation_plan_for_cover(dep, o, nodes, comp, upd)
    return o, plan, nodes


@dataclasses.dataclass(frozen=True)
class PlanResult:
    """a selector's pick plus its materialized plan. ,,strategy'' and ,,evaluation_plan'' are built once on
    the winning (family, compose) so callers do not have to re-run build_plan to render the plan."""
    family: frozenset
    compose: frozenset
    score: float
    evals: int
    certificate: dict
    escalated: bool
    strategy: StrategyWithPlans
    evaluation_plan: object
    positions: dict

    def describe(self):
        return (f"family={label(self.family, self.positions)}  "
                f"compose={label(self.compose, self.positions)}  "
                f"score={self.score:.1f}  escalated={self.escalated}  evals={self.evals}")

    def __str__(self):
        return self.describe()
