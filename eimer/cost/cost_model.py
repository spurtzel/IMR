"""analytic cost model: per-operator cardinality and cost for the update, compose, and
post-filter steps of an evaluation plan."""

from __future__ import annotations

import math
from dataclasses import dataclass
from math import factorial
from typing import Collection, Iterable, Mapping, Tuple

from eimer.cost.sigma_partition_guard import validate_sigma_partition

from eimer.plans.evaluation_plan import EvaluationPlan, SourceBlock, UpdateOp
from eimer.models import JoinType, UpdateMode, View, canonical_variable_pair, ordered_variables, view_sort_key
from eimer.query.query_schedule import normalize_query_batches
from eimer.workload import ConstantSelectivity, DependentSelectivity, WindowSelectivity, Workload


INEQ_ORDERING_FACTOR = 0.5

# plan-invariant price for the singleton MR_GROUP_DETECT (Kleene group-detect) op: every cover
# forces K into the same singleton view, so a constant cannot mis-rank within a query; deliberately 0.
KLEENE_MR_GROUP_DETECT_CONST = 0


@dataclass(frozen=True)
class OpCost:
    op_id: str
    input_cardinality: int
    output_cardinality: int
    intermediate_work: int = 0

    @property
    def total(self) -> int:
        return self.input_cardinality + self.intermediate_work + self.output_cardinality


@dataclass(frozen=True)
class BatchCostBreakdown:
    batch_index: int
    update: Tuple[OpCost, ...]
    compose: Tuple[OpCost, ...]
    post_filter: OpCost

    @property
    def total(self) -> int:
        return (
            sum(op.total for op in self.update)
            + sum(op.total for op in self.compose)
            + self.post_filter.total
        )


class CostModelError(Exception):
    pass


@dataclass(frozen=True)
class _ComposeStepCardinality:
    batch_index: int
    step_index: int
    join_op_id: str
    visited_views: Tuple[View, ...]
    new_view: View
    join_type: JoinType
    left_rows: float
    right_rows: float
    raw_pre_rule_output_rows: float
    output_rows_before_residual: float
    output_rows_after_residual: float
    intermediate_work: float
    sigma_applied_here: float
    structural_predicates: Tuple[str, ...]
    cardinality_rule: str = "default"
    containment_side: str | None = None


class StateLevelTape:
    """mutable per-view state used during costing."""

    def __init__(self, views: Iterable[View]):
        self._state: dict[View, int] = {view: 0 for view in views}

    def get(self, view: View) -> int:
        if view not in self._state:
            raise CostModelError(f"view {tuple(sorted(view))} not registered in state tape")
        return self._state[view]

    def set(self, view: View, value: int) -> None:
        if value < 0:
            raise CostModelError(f"negative state {value} for view {tuple(sorted(view))}")
        if view not in self._state:
            raise CostModelError(f"view {tuple(sorted(view))} not registered in state tape")
        self._state[view] = value

    def snapshot(self) -> Mapping[View, int]:
        return dict(self._state)

    def as_fires_at_tape(self) -> Mapping[object, int]:
        tape: dict[object, int] = {}
        for view, size in self._state.items():
            tape[view] = size
            tape[tuple(sorted(view))] = size
            tape["_".join(sorted(view))] = size
        return tape


def _with_context(plan: EvaluationPlan | None, batch_index: int | None, op_id: str | None, message: str) -> CostModelError:
    parts = [message]
    if plan is not None:
        parts.append(f"plan_id={plan.plan_id}")
    if batch_index is not None:
        parts.append(f"batch_index={batch_index}")
    if op_id is not None:
        parts.append(f"op_id={op_id}")
    return CostModelError(" | ".join(parts))


def _ensure_kleene_cost_supported(plan: EvaluationPlan) -> None:
    # kleene singleton (MR_GROUP_DETECT) plans are costable (priced as a logical-size-n_K
    # singleton in update_op_cost); unsupported kleene regimes are blocked at build time, not here.
    return None


def _checked_floor(value: float, *, label: str, plan: EvaluationPlan | None = None,
                   batch_index: int | None = None, op_id: str | None = None) -> int:
    if not math.isfinite(value):
        raise _with_context(plan, batch_index, op_id, f"non-finite {label}: {value!r}")
    if value < 0.0:
        raise _with_context(plan, batch_index, op_id, f"negative {label}: {value!r}")
    return int(math.floor(value))


def _analytic_singleton_size(workload: Workload, batch_index: int, variable: str) -> int:
    return _checked_floor(
        workload.n_at(batch_index) * workload.selectivities.independent[variable],
        label=f"analytic singleton size for {variable}",
    )


def _analytic_batch_singleton_size(workload: Workload, batch_index: int, variable: str) -> int:
    return _checked_floor(
        workload.batch_sizes[batch_index - 1] * workload.selectivities.independent[variable],
        label=f"batch singleton size for {variable}",
    )


def _edge_components(workload: Workload, left: str, right: str) -> Tuple[DependentSelectivity, ...]:
    key = canonical_variable_pair(left, right, workload.dependency_graph.positions)
    try:
        return workload.selectivities.dependent[key]
    except KeyError as exc:
        raise CostModelError(f"missing dependent selectivity for edge {key!r}") from exc


def _sigma_at(components: Tuple[DependentSelectivity, ...], workload: Workload, t: int) -> float:
    result = 1.0
    for comp in components:
        if isinstance(comp, ConstantSelectivity):
            result *= comp.value
            continue
        if isinstance(comp, WindowSelectivity):
            base_size = workload.n_at(t)
            if base_size <= 0:
                sigma_w = 1.0
            else:
                sigma_w = max(0.0, min(1.0, (2.0 * comp.w * workload.rho) / base_size))
            result *= sigma_w
            continue
        raise CostModelError(f"unknown DependentSelectivity subtype: {type(comp)!r}")
    return result




def _view_source_map(source_blocks: Tuple[SourceBlock, ...]) -> dict[str, View]:
    variable_to_source: dict[str, View] = {}
    for block in source_blocks:
        for variable in block.variables:
            variable_to_source[variable] = block.variables
    return variable_to_source


def _cross_block_ordering(num_slots: int) -> float:
    return 1.0 / factorial(num_slots)


def _update_block_contributions(op: UpdateOp, workload: Workload, batch_index: int,
                                source_sizes: Mapping[View, int]) -> list[tuple[float, float, float]]:
    block_contributions: list[tuple[float, float, float]] = []

    for block in op.source_blocks:
        source = block.variables
        source_size = float(source_sizes[source])

        if block.kind == "cache":
            batch_contribution = _batch_filtered_cache_size(
                source_size,
                workload,
                batch_index,
            )
        else:
            if len(source) != 1:
                raise CostModelError(f"base scan block must be singleton for {op.op_id}")
            variable = next(iter(source))
            batch_contribution = float(_analytic_batch_singleton_size(workload, batch_index, variable))

        if source == op.last_variable_source:
            total_size = batch_contribution
            history_contribution = 0.0
        else:
            total_size = source_size
            history_contribution = max(0.0, source_size - batch_contribution)

        block_contributions.append((history_contribution, batch_contribution, total_size))

    return block_contributions


def _ordering_factor_from_block_contributions(block_contributions: list[tuple[float, float, float]]) -> float:
    total_product = 1.0
    for _, _, total_size in block_contributions:
        total_product *= total_size

    if total_product <= 0.0:
        return 0.0

    weighted_sum = 0.0
    num_blocks = len(block_contributions)
    for history_prefix in range(num_blocks):
        contribution = 1.0
        for history_contribution, _, _ in block_contributions[:history_prefix]:
            contribution *= history_contribution
        for _, batch_contribution, _ in block_contributions[history_prefix:]:
            contribution *= batch_contribution
        if contribution <= 0.0:
            continue
        weighted_sum += contribution / float(factorial(history_prefix) * factorial(num_blocks - history_prefix))

    return weighted_sum / total_product


def _effective_ordering_factor(op: UpdateOp, workload: Workload, batch_index: int,
                               source_sizes: Mapping[View, int]) -> float:
    """estimate update ordering under a history-prefix / batch-suffix model. batch 1 reduces
    to 1 / factorial(len(source_blocks)); later batches split non-last sources into history
    and current-batch contributions, the last source staying batch-only."""

    return _ordering_factor_from_block_contributions(
        _update_block_contributions(op, workload, batch_index, source_sizes)
    )


def _update_join_ops_form_tree(op: UpdateOp) -> bool:
    """true iff ,,op.join_ops'' encode a bushy source tree rather than the canonical chain. a bushy
    tree merges two sub-results, so some operand is a multi-source variable-set that is no source
    block; the canonical chain joins single sources and never triggers the tree dispatch."""
    source_views = {block.variables for block in op.source_blocks}
    for join_op in op.join_ops:
        if join_op.anchor_view not in source_views or join_op.new_view not in source_views:
            return True
    return False


def _tree_update_attributed_sigmas(
    op: UpdateOp, workload: Workload, batch_index: int
) -> dict[frozenset, tuple[int, float]]:
    """map each cross-source dependent edge to the tree step where it spans the two operands,
    carrying that edge's sigma (an edge counts iff its endpoints are in ,,op.target_view'' and in
    different source blocks). the product of all attributed sigmas equals ,,sigma_cross'', so this
    only chooses where each sigma lands, not the output cardinality."""
    variable_to_source: dict[str, View] = {}
    for block in op.source_blocks:
        for variable in block.variables:
            variable_to_source[variable] = block.variables
    attributed: dict[frozenset, tuple[int, float]] = {}
    for edge in workload.dependency_graph.edges:
        left, right = edge.var_left, edge.var_right
        if left not in op.target_view or right not in op.target_view:
            continue
        if variable_to_source.get(left) == variable_to_source.get(right):
            continue
        step_index = _tree_edge_span_step(op.join_ops, left, right)
        if step_index is None:
            continue
        sigma = _sigma_at(_edge_components(workload, left, right), workload, batch_index)
        attributed[frozenset({left, right})] = (step_index, sigma)
    return attributed


def _tree_update_per_step_sigma_and_residual(
    op: UpdateOp, workload: Workload, batch_index: int
) -> tuple[tuple[float, ...], float]:
    """partition the bushy edge sigmas: edges co-occurring before the final step multiply
    ,,per_step[step]'' (index 0 is the unused seed); edges at the final step go to the residual,
    applied only to the final output, never to ,,intermediate_work''."""
    final_step = len(op.join_ops)
    per_step = [1.0] * (final_step + 1)
    residual = 1.0
    for step_index, sigma in _tree_update_attributed_sigmas(op, workload, batch_index).values():
        if step_index < final_step:
            per_step[step_index] *= sigma
        else:
            residual *= sigma
    return tuple(per_step), residual


def _tree_update_intermediate_work(op: UpdateOp, workload: Workload, batch_index: int,
                                   source_sizes: Mapping[View, int]) -> int:
    """tree-aware update intermediate work: walk ,,op.join_ops'' over a results map keyed by source
    variable-set, summing each node's intermediate cardinality with the spanning band sigma. per step
    uses the shared-key collapse divisor for shared-variable operands, else the interleave ordering
    factor ,,s! w! / (s + w)!''. the final-step residual sigma lands only on the output, not here."""
    block_contributions = _update_block_contributions(op, workload, batch_index, source_sizes)
    block_total: dict[View, float] = {
        block.variables: total_size
        for block, (_history, _batch, total_size) in zip(op.source_blocks, block_contributions)
    }
    per_step_sigma, _residual = _tree_update_per_step_sigma_and_residual(op, workload, batch_index)

    results: dict[frozenset, float] = {frozenset(view): float(size) for view, size in block_total.items()}
    total_intermediate = 0.0
    for step_index, join_op in enumerate(op.join_ops, start=1):
        left_view = frozenset(join_op.anchor_view)
        right_view = frozenset(join_op.new_view)
        if left_view not in results or right_view not in results:
            raise CostModelError(f"missing update sub-result for bushy tree step in {op.op_id}")
        left_rows, right_rows = results[left_view], results[right_view]
        shared = left_view & right_view
        if shared:
            divisor = _compose_shared_key_divisor(frozenset(shared), workload, batch_index, block_total)
            raw_next_size = 0.0 if divisor <= 0.0 else (left_rows * right_rows) / divisor
        else:
            interleave = float(factorial(len(left_view)) * factorial(len(right_view))) / float(
                factorial(len(left_view) + len(right_view))
            )
            raw_next_size = left_rows * right_rows * interleave
        next_size = raw_next_size * per_step_sigma[step_index]
        results[left_view | right_view] = next_size
        total_intermediate += next_size
    return _checked_floor(total_intermediate, label="update tree intermediate work", op_id=op.op_id)


def _update_intermediate_work(op: UpdateOp, workload: Workload, batch_index: int, source_sizes: Mapping[View, int]) -> int:
    if len(op.source_blocks) <= 1:
        return 0

    if _update_join_ops_form_tree(op):
        return _tree_update_intermediate_work(op, workload, batch_index, source_sizes)

    block_contributions = _update_block_contributions(op, workload, batch_index, source_sizes)
    total_intermediate = 0.0

    for prefix_len in range(2, len(block_contributions) + 1):
        prefix = block_contributions[:prefix_len]
        prefix_product = 1.0
        for _, _, total_size in prefix:
            prefix_product *= total_size
        total_intermediate += prefix_product * _ordering_factor_from_block_contributions(prefix)

    return _checked_floor(total_intermediate, label="update intermediate work", op_id=op.op_id)


def _anchor_view_size(state: StateLevelTape, workload: Workload, batch_index: int) -> int:
    anchor_var = workload.dependency_graph.variables[0]
    anchor_view = frozenset({anchor_var})
    if anchor_view in state.snapshot():
        return state.get(anchor_view)
    return _analytic_singleton_size(workload, batch_index, anchor_var)


def _batch_filtered_cache_size(source_size: int, workload: Workload, batch_index: int) -> float:
    total_base = workload.n_at(batch_index)
    if total_base <= 0:
        return float(source_size)
    return float(source_size) * (workload.batch_sizes[batch_index - 1] / total_base)




def update_op_cost(op: UpdateOp, workload: Workload, batch_index: int, state: StateLevelTape) -> OpCost:
    if op.update_mode == UpdateMode.MR_GROUP_DETECT:
        # a kleene group-detect op is a singleton {K} view with EMPTY source_blocks; the generic
        # path below would KeyError on output_factors[source]. price it as a logical-size-n_K singleton.
        # state.set is ABSOLUTE (not +=) because _analytic_singleton_size is already cumulative (n_at).
        if len(op.target_view) != 1:
            raise CostModelError(f"MR_GROUP_DETECT op must target a singleton view for {op.op_id}")
        variable = next(iter(op.target_view))
        n_K = _analytic_singleton_size(workload, batch_index, variable)
        state.set(op.target_view, n_K)
        return OpCost(
            op_id=op.op_id,
            input_cardinality=KLEENE_MR_GROUP_DETECT_CONST,
            output_cardinality=n_K,
            intermediate_work=KLEENE_MR_GROUP_DETECT_CONST,
        )

    source_sizes: dict[View, int] = {}
    output_factors: dict[View, float] = {}

    for block in op.source_blocks:
        if block.kind == "cache":
            if block.source_view is None:
                raise CostModelError(f"cache source block missing source_view for {op.op_id}")
            size = state.get(block.source_view)
            source_sizes[block.variables] = size
            output_factors[block.variables] = float(size)
            continue

        if len(block.variables) != 1:
            raise CostModelError(f"base scan block must be singleton for {op.op_id}")
        variable = next(iter(block.variables))
        if block.variables == op.last_variable_source:
            size = _analytic_batch_singleton_size(workload, batch_index, variable)
        else:
            size = _analytic_singleton_size(workload, batch_index, variable)
        source_sizes[block.variables] = size
        output_factors[block.variables] = float(size)

    if op.last_variable_source in source_sizes and op.last_variable_source in output_factors:
        last_source_size = source_sizes[op.last_variable_source]
        if op.last_variable_source in op.selected_sources:
            output_factors[op.last_variable_source] = _batch_filtered_cache_size(
                last_source_size,
                workload,
                batch_index,
            )

    variable_to_source = _view_source_map(op.source_blocks)
    sigma_cross = 1.0
    for edge in workload.dependency_graph.edges:
        if edge.var_left not in op.target_view or edge.var_right not in op.target_view:
            continue
        left_source = variable_to_source[edge.var_left]
        right_source = variable_to_source[edge.var_right]
        if left_source==right_source:
            continue
        sigma_cross *= _sigma_at(_edge_components(workload, edge.var_left, edge.var_right), workload, batch_index)

    input_cardinality = sum(source_sizes.values())
    intermediate_work = _update_intermediate_work(op, workload, batch_index, source_sizes)
    output_cardinality = 1.0
    for source in op.effective_sources:
        output_cardinality *= output_factors[source]
    output_cardinality *= sigma_cross
    output_cardinality *= _effective_ordering_factor(op, workload, batch_index, source_sizes)
    output_size = _checked_floor(output_cardinality, label="update output", op_id=op.op_id)

    state.set(op.target_view, state.get(op.target_view) + output_size)
    return OpCost(
        op_id=op.op_id,
        input_cardinality=input_cardinality,
        output_cardinality=output_size,
        intermediate_work=intermediate_work,
    )




def _compose_nodes_ordering_factor(node_views: Tuple[View, ...], workload: Workload) -> float:
    positions = workload.dependency_graph.positions
    variables = sorted({var for node_view in node_views for var in node_view}, key=positions.__getitem__)
    if not variables:
        raise CostModelError("compose_cost requires at least one composition variable")

    variable_index = {variable: idx for idx, variable in enumerate(variables)}
    prerequisites = [0] * len(variables)
    for node_view in node_views:
        ordered = ordered_variables(node_view, positions)
        for earlier, later in zip(ordered, ordered[1:]):
            prerequisites[variable_index[later]] |= 1 << variable_index[earlier]

    full_mask = (1 << len(variables)) - 1
    memo: dict[int, int] = {}

    def count_linear_extensions(mask: int) -> int:
        if mask==full_mask:
            return 1
        if mask in memo:
            return memo[mask]

        total = 0
        for idx in range(len(variables)):
            bit = 1 << idx
            if mask & bit:
                continue
            if prerequisites[idx] & ~mask:
                continue
            total += count_linear_extensions(mask | bit)

        memo[mask] = total
        return total

    valid_linearizations = count_linear_extensions(0)
    if valid_linearizations <= 0:
        raise CostModelError("compose ordering has no valid linearization")
    return 1.0 / float(valid_linearizations)


def _compose_ordering_factor(plan: EvaluationPlan, workload: Workload) -> float:
    return _compose_nodes_ordering_factor(tuple(node.view for node in plan.composition.nodes), workload)


def _compose_shared_variable_divisor(plan: EvaluationPlan, workload: Workload, batch_index: int,
                                     node_sizes: Mapping[View, int]) -> float:
    occurrences: dict[str, int] = {}
    for node in plan.composition.nodes:
        for variable in node.view:
            occurrences[variable] = occurrences.get(variable, 0) + 1

    divisor = 1.0
    for variable, count in occurrences.items():
        if count <= 1:
            continue
        singleton_view = frozenset({variable})
        if singleton_view in node_sizes:
            shared_size = node_sizes[singleton_view]
        else:
            shared_size = _analytic_singleton_size(workload, batch_index, variable)
        if shared_size <= 0:
            return 0.0
        divisor *= float(shared_size) ** float(count - 1)
    return divisor


def _edge_internalized_in_compose_nodes(plan: EvaluationPlan, left: str, right: str) -> bool:
    return any(left in node.view and right in node.view for node in plan.composition.nodes)


def _compose_predicate_strings(plan: EvaluationPlan) -> Tuple[str, ...]:
    predicate_strings: list[str] = list(plan.composition.where_predicates)
    for join_op in plan.composition.join_ops:
        predicate_strings.extend(join_op.deferred_predicates)
    return tuple(predicate_strings)


def _predicate_mentions_edge(predicate: str, left: str, right: str) -> bool:
    return f"{left}_" in predicate and f"{right}_" in predicate


def _compose_alias_availability(plan: EvaluationPlan, workload: Workload) -> Tuple[Tuple[str, ...], ...]:
    positions = workload.dependency_graph.positions
    ordered_nodes = tuple(sorted(plan.composition.nodes, key=lambda node: view_sort_key(node.view, positions)))
    if not ordered_nodes:
        return ((),)

    alias_by_view = {node.view: node.alias for node in ordered_nodes}
    root_view = plan.composition.join_ops[0].anchor_view if plan.composition.join_ops else ordered_nodes[0].view
    root_alias = alias_by_view.get(root_view)
    if root_alias is None:
        raise CostModelError(f"missing compose alias for root view {tuple(sorted(root_view))}")
    visited_views = {root_view}
    aliases = [root_alias]
    availability: list[Tuple[str, ...]] = [tuple(aliases)]

    for join_op in plan.composition.join_ops:
        new_view = join_op.new_view
        if new_view not in alias_by_view:
            raise CostModelError(f"missing compose alias for view {tuple(sorted(new_view))}")
        if new_view not in visited_views:
            aliases.append(alias_by_view[new_view])
            visited_views.add(new_view)
        availability.append(tuple(aliases))

    return tuple(availability)


def _predicate_aliases(predicate: str, aliases: Tuple[str, ...]) -> Tuple[str, ...]:
    return tuple(alias for alias in aliases if f"{alias}." in predicate)


def _compose_sigma_attribution(plan: EvaluationPlan, workload: Workload,
                               batch_index: int) -> Tuple[Tuple[str, str, int, float], ...]:
    return tuple(
        (detail["var_left"], detail["var_right"], detail["earliest_step"], detail["sigma"])
        for detail in _compose_sigma_attribution_details(plan, workload, batch_index)
    )


def _compose_sigma_attribution_details(plan: EvaluationPlan, workload: Workload,
                                       batch_index: int) -> Tuple[dict[str, object], ...]:
    availability = _compose_alias_availability(plan, workload)
    aliases = tuple(dict.fromkeys(alias for step in availability for alias in step))
    predicate_strings = tuple(dict.fromkeys(_compose_predicate_strings(plan)))
    final_step = len(plan.composition.join_ops)

    attributed: list[dict[str, object]] = []
    for edge in workload.dependency_graph.edges:
        if _edge_internalized_in_compose_nodes(plan, edge.var_left, edge.var_right):
            continue

        edge_predicates = tuple(
            predicate
            for predicate in predicate_strings
            if _predicate_mentions_edge(predicate, edge.var_left, edge.var_right)
        )
        if not edge_predicates:
            continue

        earliest_step = final_step
        for step_index, aliases_available in enumerate(availability):
            if any(
                referenced_aliases
                and all(alias in aliases_available for alias in referenced_aliases)
                for referenced_aliases in (
                    _predicate_aliases(predicate, aliases)
                    for predicate in edge_predicates
                )
            ):
                earliest_step = step_index
                break

        sigma = _sigma_at(
            _edge_components(workload, edge.var_left, edge.var_right),
            workload,
            batch_index,
        )
        attributed.append(
            {
                "var_left": edge.var_left,
                "var_right": edge.var_right,
                "earliest_step": earliest_step,
                "sigma": sigma,
                "predicates": edge_predicates,
            }
        )

    return tuple(attributed)


def _compose_per_step_sigma(plan: EvaluationPlan, workload: Workload, batch_index: int) -> Tuple[float, ...]:
    final_step = len(plan.composition.join_ops)
    per_step = [1.0] * (final_step + 1)

    for _, _, earliest_step, sigma in _compose_sigma_attribution(plan, workload, batch_index):
        if earliest_step < final_step:
            per_step[earliest_step] *= sigma

    return tuple(per_step)


def _compose_sigma_residual(plan: EvaluationPlan, workload: Workload, batch_index: int) -> float:
    final_step = len(plan.composition.join_ops)
    residual = 1.0

    for _, _, earliest_step, sigma in _compose_sigma_attribution(plan, workload, batch_index):
        if earliest_step >= final_step:
            residual *= sigma

    return residual


def _compose_sigma_applied(plan: EvaluationPlan, workload: Workload, batch_index: int) -> float:
    predicate_strings = _compose_predicate_strings(plan)
    sigma_applied = 1.0

    for edge in workload.dependency_graph.edges:
        if _edge_internalized_in_compose_nodes(plan, edge.var_left, edge.var_right):
            continue
        if not any(
            _predicate_mentions_edge(predicate, edge.var_left, edge.var_right) for predicate in predicate_strings
        ):
            continue
        sigma_applied *= _sigma_at(
            _edge_components(workload, edge.var_left, edge.var_right),
            workload,
            batch_index,
        )

    return sigma_applied


def _compose_shared_key_divisor(shared_view: View, workload: Workload, batch_index: int,
                                node_sizes: Mapping[View, int]) -> float:
    if not shared_view:
        return 1.0
    if shared_view in node_sizes:
        shared_size = node_sizes[shared_view]
        return 0.0 if shared_size <= 0 else float(shared_size)

    divisor = 1.0
    for variable in shared_view:
        singleton_view = frozenset({variable})
        if singleton_view in node_sizes:
            component_size = node_sizes[singleton_view]
        else:
            component_size = _analytic_singleton_size(workload, batch_index, variable)
        if component_size <= 0:
            return 0.0
        divisor *= float(component_size)
    return divisor


def _compose_ordering_increment(visited_views: Tuple[View, ...], new_view: View, workload: Workload) -> float:
    before = _compose_nodes_ordering_factor(visited_views, workload)
    if before <= 0.0:
        return 0.0
    after = _compose_nodes_ordering_factor(visited_views + (new_view,), workload)
    return after / before


def _compose_default_step_output(join_op, *, visited_views: Tuple[View, ...], current_vars: Collection[str],
                                 new_view: View, left_rows: float, right_rows: float, workload: Workload,
                                 batch_index: int, node_sizes: Mapping[View, int]) -> float:
    shared_view = frozenset(set(current_vars).intersection(new_view))

    if shared_view:
        divisor = _compose_shared_key_divisor(shared_view, workload, batch_index, node_sizes)
        return 0.0 if divisor <= 0.0 else (left_rows * right_rows) / divisor
    if join_op.join_type in (JoinType.INEQ, JoinType.BAND):
        ordering_increment = _compose_ordering_increment(visited_views, new_view, workload)
        return left_rows * right_rows * ordering_increment
    if join_op.join_type == JoinType.EQUI:
        raise CostModelError(
            f"compose equi join has no shared variables for {join_op.op_id}: "
            f"{tuple(sorted(current_vars))} vs {tuple(sorted(new_view))}"
        )
    raise CostModelError(f"unsupported compose join type: {join_op.join_type!r}")


def _is_repeated_variable_equality_predicate(predicate: str) -> bool:
    normalized = predicate.upper()
    if " BETWEEN " in normalized or "<" in predicate or ">" in predicate:
        return False
    if "=" not in predicate or "!=" in predicate or "<>" in predicate:
        return False
    return True


def _composition_containment_side(plan: EvaluationPlan, workload: Workload, *, visited_views: Tuple[View, ...],
                                  current_vars: Collection[str], new_view: View, join_op) -> str | None:
    """conservatively detect subset/superset consistency joins: two materialized view leaves where one
    view is a strict subset of the other and the ON predicates are only repeated-variable equality
    checks. an intermediate assembled from multiple leaves falls back to the default model."""

    current_view = frozenset(current_vars)
    if len(visited_views) != 1:
        return None
    left_view = visited_views[0]
    if current_view != left_view:
        return None

    node_by_view = {node.view: node for node in plan.composition.nodes}
    left_node = node_by_view.get(left_view)
    right_node = node_by_view.get(new_view)
    if left_node is None or right_node is None:
        return None
    if left_node.kind != "cache" or right_node.kind != "cache":
        return None
    if join_op.join_type != JoinType.EQUI:
        return None
    if not join_op.structural_predicates:
        return None
    if not all(_is_repeated_variable_equality_predicate(predicate) for predicate in join_op.structural_predicates):
        return None

    if left_view < new_view:
        containment_side = "RIGHT_SUPERSET"
        superset_view = new_view
    elif new_view < left_view:
        containment_side = "LEFT_SUPERSET"
        superset_view = left_view
    else:
        return None

    for predicate in join_op.deferred_predicates:
        if not any(
            edge.var_left in superset_view
            and edge.var_right in superset_view
            and _predicate_mentions_edge(predicate, edge.var_left, edge.var_right)
            for edge in workload.dependency_graph.edges
        ):
            return None

    return containment_side


def _compose_join_ops_form_tree(plan: EvaluationPlan) -> bool:
    """return true iff the composition's ,,join_ops'' encode a bushy tree rather than a chain. a bushy
    tree joins two sub-results, so some operand is a merged variable-set that is no composition node;
    the chain path joins one running intermediate against a single node per step. a chain-shaped tree
    registers as a tree and is handled identically by the tree interpreter."""
    node_views = {node.view for node in plan.composition.nodes}
    for join_op in plan.composition.join_ops:
        if join_op.anchor_view not in node_views or join_op.new_view not in node_views:
            return True
    return False


def _tree_edge_span_step(join_ops, left: str, right: str) -> int | None:
    """the 1-based step whose join first brings ,,left'' and ,,right'' into one sub-result: the node
    where the edge spans the two operands (one endpoint in ,,anchor_view'', the other in
    ,,new_view''). cumulative variable presence is not enough: in a two-island tree a cross edge's
    endpoints can both appear in the running set one step before the islands join. returns ,,None''
    if the edge never spans (its endpoints are co-located in a single operand subtree)."""
    for step_index, join_op in enumerate(join_ops, start=1):
        anchor = join_op.anchor_view
        new = join_op.new_view
        if (left in anchor and right in new) or (right in anchor and left in new):
            return step_index
    return None


def _tree_attributed_sigmas(plan: EvaluationPlan, workload: Workload,
                            batch_index: int) -> dict[frozenset, Tuple[int, float]]:
    """map each non-internalized dependent edge to the tree step where it spans the two operands,
    carrying that edge's sigma. mirrors ,,_compose_sigma_applied'''s edge selection exactly, so the
    product of all attributed sigmas equals the order-invariant ,,_compose_sigma_applied'' and
    ,,compose_cost'''s aggregate output is unchanged."""
    predicate_strings = _compose_predicate_strings(plan)
    join_ops = plan.composition.join_ops

    attributed: dict[frozenset, Tuple[int, float]] = {}
    for edge in workload.dependency_graph.edges:
        if _edge_internalized_in_compose_nodes(plan, edge.var_left, edge.var_right):
            continue
        if not any(
            _predicate_mentions_edge(predicate, edge.var_left, edge.var_right) for predicate in predicate_strings
        ):
            continue
        step_index = _tree_edge_span_step(join_ops, edge.var_left, edge.var_right)
        if step_index is None:
            continue
        sigma = _sigma_at(
            _edge_components(workload, edge.var_left, edge.var_right),
            workload,
            batch_index,
        )
        attributed[frozenset({edge.var_left, edge.var_right})] = (step_index, sigma)
    return attributed


def _tree_per_step_sigma_and_residual(plan: EvaluationPlan, workload: Workload,
                                      batch_index: int) -> Tuple[Tuple[float, ...], float]:
    """partition the bushy edge sigmas as the linear path does: edges co-occurring before the final
    step multiply ,,per_step[step]'' (index 0 is the unused seed); edges at the final step go to the
    residual, applied only to the final output. tree analog of ,,_compose_per_step_sigma'' plus
    ,,_compose_sigma_residual'', so ,,math.prod(per_step) * residual == _compose_sigma_applied''."""
    final_step = len(plan.composition.join_ops)
    per_step = [1.0] * (final_step + 1)
    residual = 1.0
    for step_index, sigma in _tree_attributed_sigmas(plan, workload, batch_index).values():
        if step_index < final_step:
            per_step[step_index] *= sigma
        else:
            residual *= sigma
    return tuple(per_step), residual


def _merged_view_alias(view: View, node_by_view: Mapping[View, object]) -> str:
    """render a tree operand's display alias: a leaf's own compose alias, or the constituent leaf
    aliases of a merged sub-result joined by ,,+'' (diagnostic only; downstream ranking reads the
    numeric fields, not this string)."""
    node = node_by_view.get(view)
    if node is not None:
        return node.alias
    members = [
        node_by_view[leaf].alias
        for leaf in sorted(node_by_view, key=lambda candidate: tuple(sorted(candidate)))
        if leaf <= view
    ]
    return "+".join(members)


def _tree_predicates_by_step(plan: EvaluationPlan, workload: Workload,
                             batch_index: int) -> dict[int, list[dict[str, object]]]:
    """group rendered dependent predicates by the bushy step where their edge spans the two
    operands, matching ,,_tree_attributed_sigmas'''s attribution so ,,predicates_applied_here'' lines
    up with each step's sigma."""
    predicate_strings = _compose_predicate_strings(plan)
    join_ops = plan.composition.join_ops

    by_step: dict[int, list[dict[str, object]]] = {}
    for edge in workload.dependency_graph.edges:
        if _edge_internalized_in_compose_nodes(plan, edge.var_left, edge.var_right):
            continue
        edge_predicates = tuple(
            predicate
            for predicate in predicate_strings
            if _predicate_mentions_edge(predicate, edge.var_left, edge.var_right)
        )
        if not edge_predicates:
            continue
        step_index = _tree_edge_span_step(join_ops, edge.var_left, edge.var_right)
        if step_index is None:
            continue
        by_step.setdefault(step_index, []).append({"predicates": edge_predicates})
    return by_step


def _compose_per_step_sigma_and_residual(plan: EvaluationPlan, workload: Workload,
                                         batch_index: int) -> Tuple[Tuple[float, ...], float]:
    """dispatch the per-step sigma partition: the linear alias-availability walk for chain-shaped
    ,,join_ops'', the variable-set co-occurrence walk for bushy ones. both branches partition the same
    order-invariant ,,_compose_sigma_applied'', so ,,compose_cost'''s consistency assertion holds
    either way."""
    if _compose_join_ops_form_tree(plan):
        return _tree_per_step_sigma_and_residual(plan, workload, batch_index)
    return (
        _compose_per_step_sigma(plan, workload, batch_index),
        _compose_sigma_residual(plan, workload, batch_index),
    )


def _tree_compose_step_cardinalities(plan: EvaluationPlan, workload: Workload, batch_index: int,
                                     node_sizes: Mapping[View, int],
                                     per_step_sigma: Tuple[float, ...]) -> Tuple[_ComposeStepCardinality, ...]:
    """tree-shaped analogue of ,,_compose_step_cardinalities'': walk the bushy ,,join_ops'' over a
    results map keyed by variable-set, resolving each operand as a leaf node size or a prior
    sub-result. per step the output is ,,left * right'' with the per-step edge sigma and either the
    shared-key collapse divisor (shared variable) or the interleave ordering factor ,,s! w! / (s+w)!''.
    the final-step residual sigma is applied only to ,,output_rows_after_residual''."""
    join_ops = plan.composition.join_ops
    final_step = len(join_ops)
    _, residual_sigma = _tree_per_step_sigma_and_residual(plan, workload, batch_index)

    results: dict[frozenset, float] = {}
    leaves_of: dict[frozenset, Tuple[View, ...]] = {}
    for node in plan.composition.nodes:
        if node.view not in node_sizes:
            raise CostModelError(f"missing compose node size for view {tuple(sorted(node.view))}")
        results[frozenset(node.view)] = float(node_sizes[node.view])
        leaves_of[frozenset(node.view)] = (node.view,)

    def resolve(operand: View) -> Tuple[float, Tuple[View, ...]]:
        key = frozenset(operand)
        if key not in results:
            raise CostModelError(f"missing compose sub-result for view {tuple(sorted(operand))}")
        return results[key], leaves_of[key]

    rows: list[_ComposeStepCardinality] = []
    for step_index, join_op in enumerate(join_ops, start=1):
        left_rows, left_leaves = resolve(join_op.anchor_view)
        right_rows, right_leaves = resolve(join_op.new_view)
        left_vars = set(join_op.anchor_view)
        right_vars = set(join_op.new_view)
        shared_view = frozenset(left_vars & right_vars)

        if shared_view:
            divisor = _compose_shared_key_divisor(shared_view, workload, batch_index, node_sizes)
            raw_next_size = 0.0 if divisor <= 0.0 else (left_rows * right_rows) / divisor
        else:
            interleave = (
                float(factorial(len(left_vars)) * factorial(len(right_vars)))
                / float(factorial(len(left_vars) + len(right_vars)))
            )
            raw_next_size = left_rows * right_rows * interleave

        next_size = raw_next_size * per_step_sigma[step_index]
        output_after_residual = next_size * residual_sigma if step_index == final_step else next_size
        merged_view = frozenset(left_vars | right_vars)
        results[merged_view] = next_size
        leaves_of[merged_view] = tuple(left_leaves) + tuple(right_leaves)

        rows.append(
            _ComposeStepCardinality(
                batch_index=batch_index,
                step_index=step_index,
                join_op_id=join_op.operator_id or join_op.op_id,
                visited_views=tuple(left_leaves),
                new_view=join_op.new_view,
                join_type=join_op.join_type,
                left_rows=left_rows,
                right_rows=right_rows,
                raw_pre_rule_output_rows=raw_next_size,
                output_rows_before_residual=next_size,
                output_rows_after_residual=output_after_residual,
                intermediate_work=next_size,
                sigma_applied_here=(
                    per_step_sigma[step_index] * residual_sigma
                    if step_index == final_step
                    else per_step_sigma[step_index]
                ),
                structural_predicates=tuple(join_op.structural_predicates),
                cardinality_rule="default",
                containment_side=None,
            )
        )

    return tuple(rows)


def _compose_step_cardinalities(plan: EvaluationPlan, workload: Workload, batch_index: int,
                                node_sizes: Mapping[View, int],
                                per_step_sigma: Tuple[float, ...] | None = None) -> Tuple[_ComposeStepCardinality, ...]:
    if not plan.composition.join_ops:
        return ()

    if _compose_join_ops_form_tree(plan):
        if per_step_sigma is None:
            per_step_sigma, _ = _tree_per_step_sigma_and_residual(plan, workload, batch_index)
        return _tree_compose_step_cardinalities(
            plan, workload, batch_index, node_sizes, per_step_sigma
        )

    node_by_view = {node.view: node for node in plan.composition.nodes}
    root_view = plan.composition.join_ops[0].anchor_view
    root = node_by_view.get(root_view)
    if root is None:
        raise CostModelError(f"missing compose root node for view {tuple(sorted(root_view))}")
    if root.view not in node_sizes:
        raise CostModelError(f"missing compose node size for view {tuple(sorted(root.view))}")

    visited_views = [root.view]
    visited_view_set = {root.view}
    current_vars = set(root.view)
    current_size = float(node_sizes[root.view])
    if per_step_sigma is None:
        per_step_sigma = _compose_per_step_sigma(plan, workload, batch_index)
    expected_sigma_len = len(plan.composition.join_ops) + 1
    if len(per_step_sigma) != expected_sigma_len:
        raise CostModelError(
            f"compose per-step sigma length mismatch: expected {expected_sigma_len}, got {len(per_step_sigma)}"
        )
    current_size *= per_step_sigma[0]
    residual_sigma = _compose_sigma_residual(plan, workload, batch_index)
    final_step = len(plan.composition.join_ops)
    rows: list[_ComposeStepCardinality] = []

    for step_index, join_op in enumerate(plan.composition.join_ops, start=1):
        new_view = join_op.new_view
        if new_view not in node_sizes:
            raise CostModelError(f"missing compose node size for view {tuple(sorted(new_view))}")
        if new_view in visited_view_set:
            continue

        left_rows = current_size
        right_rows = float(node_sizes[new_view])
        raw_next_size = _compose_default_step_output(
            join_op,
            visited_views=tuple(visited_views),
            current_vars=current_vars,
            new_view=new_view,
            left_rows=left_rows,
            right_rows=right_rows,
            workload=workload,
            batch_index=batch_index,
            node_sizes=node_sizes,
        )
        containment_side = _composition_containment_side(
            plan,
            workload,
            visited_views=tuple(visited_views),
            current_vars=current_vars,
            new_view=new_view,
            join_op=join_op,
        )
        if containment_side == "LEFT_SUPERSET":
            next_size = left_rows
            cardinality_rule = "containment"
        elif containment_side == "RIGHT_SUPERSET":
            next_size = right_rows
            cardinality_rule = "containment"
        else:
            next_size = raw_next_size
            cardinality_rule = "default"

        next_size *= per_step_sigma[step_index]
        output_after_residual = next_size * residual_sigma if step_index == final_step else next_size
        rows.append(
            _ComposeStepCardinality(
                batch_index=batch_index,
                step_index=step_index,
                join_op_id=join_op.operator_id or join_op.op_id,
                visited_views=tuple(visited_views),
                new_view=new_view,
                join_type=join_op.join_type,
                left_rows=left_rows,
                right_rows=right_rows,
                raw_pre_rule_output_rows=raw_next_size,
                output_rows_before_residual=next_size,
                output_rows_after_residual=output_after_residual,
                intermediate_work=next_size,
                sigma_applied_here=(
                    per_step_sigma[step_index] * residual_sigma
                    if step_index == final_step
                    else per_step_sigma[step_index]
                ),
                structural_predicates=tuple(join_op.structural_predicates),
                cardinality_rule=cardinality_rule,
                containment_side=containment_side,
            )
        )
        current_size = next_size
        current_vars.update(new_view)
        visited_views.append(new_view)
        visited_view_set.add(new_view)

    return tuple(rows)


def _compose_intermediate_work(plan: EvaluationPlan, workload: Workload, batch_index: int,
                               node_sizes: Mapping[View, int],
                               per_step_sigma: Tuple[float, ...] | None = None) -> int:
    if not plan.composition.join_ops:
        return 0
    total_intermediate = sum(
        row.intermediate_work
        for row in _compose_step_cardinalities(
            plan,
            workload,
            batch_index,
            node_sizes,
            per_step_sigma=per_step_sigma,
        )
    )
    return _checked_floor(total_intermediate, label="compose intermediate work", op_id="compose")


def compose_step_trace(plan: EvaluationPlan, workload: Workload, batch_index: int,
                       node_sizes: Mapping[View, int]) -> Tuple[dict[str, object], ...]:
    """return diagnostic per-step cardinality estimates for composition, mirroring ,,compose_cost''
    without changing any formula. ,,predicted_intermediate_work'' is the per-prefix contribution
    summed by ,,_compose_intermediate_work''; ,,predicted_output_rows'' includes any final residual
    sigma so the last trace row matches the aggregate compose output."""

    if not plan.composition.join_ops:
        return ()

    node_by_view = {node.view: node for node in plan.composition.nodes}
    root_view = plan.composition.join_ops[0].anchor_view
    root = node_by_view.get(root_view)
    if root is None:
        raise CostModelError(f"missing compose root node for view {tuple(sorted(root_view))}")
    if root.view not in node_sizes:
        raise CostModelError(f"missing compose node size for view {tuple(sorted(root.view))}")

    is_tree = _compose_join_ops_form_tree(plan)
    per_step_sigma, _ = _compose_per_step_sigma_and_residual(plan, workload, batch_index)
    expected_sigma_len = len(plan.composition.join_ops) + 1
    if len(per_step_sigma) != expected_sigma_len:
        raise CostModelError(
            f"compose per-step sigma length mismatch: expected {expected_sigma_len}, got {len(per_step_sigma)}"
        )

    details_by_step: dict[int, list[dict[str, object]]] = {}
    final_step = len(plan.composition.join_ops)
    if is_tree:
        details_by_step = _tree_predicates_by_step(plan, workload, batch_index)
    else:
        for detail in _compose_sigma_attribution_details(plan, workload, batch_index):
            earliest_step = int(detail["earliest_step"])
            details_by_step.setdefault(earliest_step if earliest_step < final_step else final_step, []).append(detail)

    compose_op, aggregate_output = compose_cost(plan, workload, batch_index, node_sizes)

    step_cardinalities = _compose_step_cardinalities(
        plan,
        workload,
        batch_index,
        node_sizes,
        per_step_sigma=per_step_sigma,
    )
    trace: list[dict[str, object]] = []

    for row in step_cardinalities:
        step_details = details_by_step.get(row.step_index, [])
        predicates: list[str] = []
        for detail in step_details:
            predicates.extend(str(predicate) for predicate in detail["predicates"])

        # shared-key collapse divisor for this compose step: |shared key| for a shared-variable
        # equi-join (which collapses the running intermediate), else 1.0. exposed for the ranking
        # layer as an early-collapse-aware feature; diagnostic only, no cardinality change.
        step_current_vars: set[str] = set()
        for step_view in row.visited_views:
            step_current_vars |= set(step_view)
        step_shared_view = frozenset(step_current_vars & set(row.new_view))
        step_shared_key_divisor = (
            _compose_shared_key_divisor(step_shared_view, workload, batch_index, node_sizes)
            if step_shared_view
            else 1.0
        )

        trace.append(
            {
                "batch_index": batch_index,
                "step_index": row.step_index,
                "join_op_id": row.join_op_id,
                "left_nodes_or_aliases": tuple(node_by_view[view].alias for view in row.visited_views),
                "right_node_or_alias": (
                    _merged_view_alias(row.new_view, node_by_view)
                    if is_tree
                    else node_by_view[row.new_view].alias
                ),
                "join_type": row.join_type.value,
                "predicted_left_rows": row.left_rows,
                "predicted_right_rows": row.right_rows,
                "raw_pre_rule_output_rows": row.raw_pre_rule_output_rows,
                "predicted_output_rows": (
                    float(aggregate_output)
                    if row.step_index == final_step
                    else row.output_rows_after_residual
                ),
                "predicted_intermediate_work": row.intermediate_work,
                "sigma_applied_here": row.sigma_applied_here,
                "predicates_applied_here": tuple(dict.fromkeys(predicates)),
                "structural_predicates": row.structural_predicates,
                "cardinality_rule": row.cardinality_rule,
                "containment_side": row.containment_side,
                "shared_key_divisor": float(step_shared_key_divisor),
            }
        )

    if trace and trace[-1]["predicted_output_rows"] != float(aggregate_output):
        last = dict(trace[-1])
        last["predicted_output_rows"] = float(aggregate_output)
        trace[-1] = last

    # the summed per-prefix work must equal the quantity compose_cost uses.
    traced_work = _checked_floor(
        sum(float(row["predicted_intermediate_work"]) for row in trace),
        label="compose intermediate work",
        op_id="compose",
    )
    if traced_work != compose_op.intermediate_work:
        raise CostModelError(
            "compose step trace intermediate work mismatch: "
            f"trace={traced_work}, compose_cost={compose_op.intermediate_work}"
        )

    return tuple(trace)


def compose_cost(plan: EvaluationPlan, workload: Workload, batch_index: int,
                 node_sizes: Mapping[View, int]) -> tuple[OpCost, int]:
    if not plan.composition.nodes:
        raise CostModelError("compose_cost requires at least one composition node")

    input_cardinality = 0
    output_value = 1.0
    for node in plan.composition.nodes:
        try:
            node_size = node_sizes[node.view]
        except KeyError as exc:
            raise CostModelError(f"missing compose node size for view {tuple(sorted(node.view))}") from exc
        input_cardinality += node_size
        output_value *= float(node_size)

    # compose is emitted as one multi-way CTAS over the composition nodes. we model the remaining
    # global time-order freedom, then apply only the dependent-edge selectivities not already
    # internalized in a node. repeated variables across nodes contribute equality predicates in SQL,
    # so we divide by the corresponding singleton sizes to collapse those copies.
    ordering_factor = _compose_ordering_factor(plan, workload)
    per_step_sigma, sigma_residual = _compose_per_step_sigma_and_residual(plan, workload, batch_index)
    sigma_applied = _compose_sigma_applied(plan, workload, batch_index)
    shared_divisor = _compose_shared_variable_divisor(plan, workload, batch_index, node_sizes)
    if not math.isclose(math.prod(per_step_sigma) * sigma_residual, sigma_applied, rel_tol=1e-9, abs_tol=1e-12):
        raise CostModelError(
            "compose sigma attribution mismatch: "
            f"per_step={per_step_sigma!r}, residual={sigma_residual!r}, full={sigma_applied!r}"
        )
    step_cardinalities = _compose_step_cardinalities(
        plan,
        workload,
        batch_index,
        node_sizes,
        per_step_sigma=per_step_sigma,
    )
    intermediate_work = _checked_floor(
        sum(row.intermediate_work for row in step_cardinalities),
        label="compose intermediate work",
        op_id="compose",
    )
    if shared_divisor == 0.0:
        output_size = 0
    else:
        output_value *= ordering_factor
        output_value *= sigma_applied
        output_value /= shared_divisor
        output_size = _checked_floor(output_value, label="compose output", op_id="compose")
    if step_cardinalities and any(row.cardinality_rule == "containment" for row in step_cardinalities):
        output_size = _checked_floor(
            step_cardinalities[-1].output_rows_after_residual,
            label="compose containment output",
            op_id="compose",
        )

    return (
        OpCost(
            op_id="compose",
            input_cardinality=input_cardinality,
            output_cardinality=output_size,
            intermediate_work=intermediate_work,
        ),
        output_size,
    )


def postfilter_cost(candidate_size: int, anchor_size: int, op_id: str = "post_filter") -> OpCost:
    output = min(candidate_size, anchor_size)
    return OpCost(op_id=op_id, input_cardinality=candidate_size, output_cardinality=output)


def batch_cost(plan: EvaluationPlan, workload: Workload, batch_index: int, state: StateLevelTape,
               run_query: bool = True) -> BatchCostBreakdown:
    """cost one 1-based batch. updates always run; composition and post-filtering run only when
    ,,run_query'' is true, letting callers model batches without a query."""

    _ensure_kleene_cost_supported(plan)

    if batch_index < 1 or batch_index > workload.k:
        raise _with_context(plan, batch_index, None, "batch index out of range")

    validate_sigma_partition(plan, workload)

    update_costs: list[OpCost] = []
    compose_costs: list[OpCost] = []

    for update_op in plan.update_ops:
        try:
            update_costs.append(update_op_cost(update_op, workload, batch_index, state))
        except CostModelError as exc:
            raise _with_context(plan, batch_index, update_op.op_id, str(exc)) from exc

    if run_query:
        node_sizes: dict[View, int] = {}
        for node in plan.composition.nodes:
            try:
                if node.kind == "cache":
                    node_sizes[node.view] = state.get(node.view)
                else:
                    if len(node.view) != 1:
                        raise CostModelError(f"base_scan composition node must be singleton for {node.alias}")
                    variable = next(iter(node.view))
                    node_sizes[node.view] = _analytic_singleton_size(workload, batch_index, variable)
            except CostModelError as exc:
                raise _with_context(plan, batch_index, node.alias, str(exc)) from exc

        try:
            compose_op, candidate_size = compose_cost(plan, workload, batch_index, node_sizes)
            compose_costs.append(compose_op)
        except CostModelError as exc:
            raise _with_context(plan, batch_index, "compose", str(exc)) from exc

        anchor_size = _anchor_view_size(state, workload, batch_index)
        post_filter = postfilter_cost(candidate_size, anchor_size, plan.post_filter.op_id)
    else:
        post_filter = OpCost(
            op_id=plan.post_filter.op_id,
            input_cardinality=0,
            output_cardinality=0,
        )
    return BatchCostBreakdown(
        batch_index=batch_index,
        update=tuple(update_costs),
        compose=tuple(compose_costs),
        post_filter=post_filter,
    )


def plan_cost(plan: EvaluationPlan, workload: Workload, query_batches: Collection[int] | None = None) -> int:
    """return total cost across 1-based batches. ,,query_batches=None'' queries every batch; an empty
    collection models update-only maintenance."""

    _ensure_kleene_cost_supported(plan)

    try:
        normalized_query_batches = normalize_query_batches(query_batches, workload.k)
    except ValueError as exc:
        raise CostModelError(str(exc)) from exc

    maintained_views = plan.maintained_view_set or plan.strategy
    state = StateLevelTape(maintained_views)
    total = 0
    for batch_index in range(1, workload.k + 1):
        run_query = normalized_query_batches is None or batch_index in normalized_query_batches
        breakdown = batch_cost(plan, workload, batch_index, state, run_query=run_query)
        total += breakdown.total
    return total
