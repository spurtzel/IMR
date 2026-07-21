"""core data model: parser ast, normalized ir, dependency graph, and the plan and
strategy dataclasses, plus the view helper functions."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple


View = FrozenSet[str]
Strategy = FrozenSet[View]


class StrategyMode(str, Enum):
    DEPENDENCY_RESTRICTED = "dependency_restricted"
    FULL_SUBSETS = "full_subsets"


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    clause: Optional[str] = None
    detail: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.clause is not None:
            payload["clause"] = self.clause
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class PatternVariableAst:
    name: str
    quantifier: str
    raw_text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "quantifier": self.quantifier,
            "raw_text": self.raw_text,
        }


@dataclass(frozen=True)
class PatternGapAst:
    wildcard: str
    mode: str
    raw_text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "wildcard": self.wildcard,
            "mode": self.mode,
            "raw_text": self.raw_text,
        }


@dataclass(frozen=True)
class PatternAst:
    raw_text: str
    variables: List[PatternVariableAst]
    gaps: List[PatternGapAst]

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_text": self.raw_text,
            "variables": [item.to_dict() for item in self.variables],
            "gaps": [item.to_dict() for item in self.gaps],
        }


@dataclass(frozen=True)
class DefineConjunctAst:
    text: str
    kind: str
    operator: str
    referenced_variables: List[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "kind": self.kind,
            "operator": self.operator,
            "referenced_variables": list(self.referenced_variables),
        }


@dataclass(frozen=True)
class DefineEntryAst:
    variable: str
    raw_expression: str
    conjuncts: List[DefineConjunctAst]

    def to_dict(self) -> dict[str, Any]:
        return {
            "variable": self.variable,
            "raw_expression": self.raw_expression,
            "conjuncts": [item.to_dict() for item in self.conjuncts],
        }


@dataclass(frozen=True)
class MeasureAst:
    kind: str
    variable: str
    attribute: str
    alias: str
    raw_expression: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "variable": self.variable,
            "attribute": self.attribute,
            "alias": self.alias,
            "raw_expression": self.raw_expression,
        }


@dataclass(frozen=True)
class OrderByAst:
    raw_text: str

    def to_dict(self) -> dict[str, Any]:
        return {"raw_text": self.raw_text}


@dataclass(frozen=True)
class RowOutputAst:
    mode: str

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode}


@dataclass(frozen=True)
class AfterMatchAst:
    mode: str

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode}


@dataclass(frozen=True)
class MatchRecognizeAst:
    raw_body: str
    order_by: OrderByAst
    measures: List[MeasureAst]
    row_output: RowOutputAst
    after_match: AfterMatchAst
    pattern: PatternAst
    define_entries: List[DefineEntryAst]

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_body": self.raw_body,
            "order_by": self.order_by.to_dict(),
            "measures": [item.to_dict() for item in self.measures],
            "row_output": self.row_output.to_dict(),
            "after_match": self.after_match.to_dict(),
            "pattern": self.pattern.to_dict(),
            "define_entries": [item.to_dict() for item in self.define_entries],
        }


@dataclass(frozen=True)
class QueryAst:
    raw_sql: str
    prefix_sql: str
    suffix_sql: str
    match_recognize: MatchRecognizeAst

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_sql": self.raw_sql,
            "prefix_sql": self.prefix_sql,
            "suffix_sql": self.suffix_sql,
            "match_recognize": self.match_recognize.to_dict(),
        }


@dataclass(frozen=True)
class NormalizedMeasure:
    kind: str
    variable: str
    attribute: str
    alias: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "variable": self.variable,
            "attribute": self.attribute,
            "alias": self.alias,
        }


@dataclass(frozen=True)
class NormalizedDefineEntry:
    variable: str
    raw_expression: str
    conjuncts: List[DefineConjunctAst]

    def to_dict(self) -> dict[str, Any]:
        return {
            "variable": self.variable,
            "raw_expression": self.raw_expression,
            "conjuncts": [item.to_dict() for item in self.conjuncts],
        }


@dataclass(frozen=True)
class NormalizedIR:
    pattern_variables: List[str]
    variable_quantifiers: List[dict[str, str]]
    wildcard_gaps: List[dict[str, str]]
    order_by: str
    define_entries: List[NormalizedDefineEntry]
    measures: List[NormalizedMeasure]
    row_output_policy: str
    after_match_policy: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern_variables": list(self.pattern_variables),
            "variable_quantifiers": list(self.variable_quantifiers),
            "wildcard_gaps": list(self.wildcard_gaps),
            "order_by": self.order_by,
            "define_entries": [item.to_dict() for item in self.define_entries],
            "measures": [item.to_dict() for item in self.measures],
            "row_output_policy": self.row_output_policy,
            "after_match_policy": self.after_match_policy,
        }


ViewEdge = Tuple[View, View]


@dataclass(frozen=True)
class DependencyPredicate:
    text: str
    operator: str
    referenced_variables: List[str]
    source_variable: str
    kind: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "operator": self.operator,
            "referenced_variables": list(self.referenced_variables),
            "source_variable": self.source_variable,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class WildcardGap:
    left_variable: str
    right_variable: str
    wildcard: str
    mode: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "left_variable": self.left_variable,
            "right_variable": self.right_variable,
            "wildcard": self.wildcard,
            "mode": self.mode,
        }


@dataclass(frozen=True)
class DependencyVertex:
    variable: str
    position: int
    quantifier: str
    independent_predicates: List[DependencyPredicate]

    @property
    def independent_conditions(self) -> List[str]:
        return [predicate.text for predicate in self.independent_predicates]

    def to_dict(self) -> dict[str, Any]:
        return {
            "variable": self.variable,
            "position": self.position,
            "quantifier": self.quantifier,
            "independent_predicates": [item.to_dict() for item in self.independent_predicates],
        }


@dataclass(frozen=True)
class DependencyEdge:
    var_left: str
    var_right: str
    span: Tuple[int, int]
    dependent_predicates: List[DependencyPredicate]
    source_variables: List[str]

    @property
    def dependent_conditions(self) -> Tuple[str, ...]:
        return tuple(predicate.text for predicate in self.dependent_predicates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "var_left": self.var_left,
            "var_right": self.var_right,
            "span": [self.span[0], self.span[1]],
            "dependent_predicates": [item.to_dict() for item in self.dependent_predicates],
            "source_variables": list(self.source_variables),
        }


@dataclass(frozen=True)
class DependencyGraph:
    variables: List[str]
    positions: Dict[str, int]
    quantifiers: Dict[str, str]
    wildcard_gaps: List[WildcardGap]
    vertices: Dict[str, DependencyVertex]
    edges: List[DependencyEdge]
    warnings: List[str] = field(default_factory=list)
    # measures carried from the normalized ir so the emitter can project them;
    # populated by build_dependency_graph.
    measures: Tuple[NormalizedMeasure, ...] = ()

    @property
    def independent_conditions(self) -> Dict[str, List[str]]:
        return {
            var: list(self.vertices[var].independent_conditions)
            for var in self.variables
        }

    def edge_pairs(self) -> Set[Tuple[str, str]]:
        return {(edge.var_left, edge.var_right) for edge in self.edges}

    def to_dict(self) -> dict[str, Any]:
        ordered_variables_list = list(self.variables)
        return {
            "variables": ordered_variables_list,
            "positions": {var: self.positions[var] for var in ordered_variables_list},
            "quantifiers": {var: self.quantifiers[var] for var in ordered_variables_list},
            "wildcard_gaps": [item.to_dict() for item in self.wildcard_gaps],
            "vertices": {var: self.vertices[var].to_dict() for var in ordered_variables_list},
            "edges": [item.to_dict() for item in self.edges],
            "warnings": list(self.warnings),
            "measures": [item.to_dict() for item in self.measures],
        }


class JoinType(Enum):
    EQUI = "EQUI"
    BAND = "BAND"
    INEQ = "INEQ"


class ConditionType(Enum):
    EQUALITY = "EQUALITY"
    TEMPORAL_WINDOW = "TEMPORAL_WINDOW"
    RANGE_SPATIAL = "RANGE_SPATIAL"
    OTHER = "OTHER"


class UpdateMode(str, Enum):
    JOIN = "JOIN"
    MR_GROUP_DETECT = "MR_GROUP_DETECT"


@dataclass(frozen=True)
class DeferredCondition:
    var_left: str
    var_right: str
    expression: str
    condition_type: ConditionType


@dataclass(frozen=True)
class CompositionJoinEdge:
    left: View
    right: View
    join_type: JoinType
    deferred_conditions: Tuple[DeferredCondition, ...]


@dataclass(frozen=True)
class CompositionJoinGraph:
    nodes: Set[View]
    edges: Dict[ViewEdge, CompositionJoinEdge]


@dataclass(frozen=True)
class JoinTreeNode:
    """bushy join tree over composition leaves. a leaf carries ,,node'' (a ,,View''); an inner
    node carries ,,left'' and ,,right'' sub-trees. exactly one holds (leaf xor inner join),
    enforced by ,,__post_init__''. optional on ,,CompositionPlan''/,,UpdatePlan''; ,,None''
    everywhere means the default left-deep plans, and it never participates in plan_id hashing."""

    node: Optional[View] = None
    left: Optional["JoinTreeNode"] = None
    right: Optional["JoinTreeNode"] = None

    def __post_init__(self) -> None:
        is_leaf = self.node is not None
        is_inner = self.left is not None or self.right is not None
        if is_leaf==is_inner:
            raise ValueError("JoinTreeNode must be a leaf (node set) XOR an inner join (left and right set)")
        if is_inner and (self.left is None or self.right is None):
            raise ValueError("JoinTreeNode inner join requires both left and right sub-trees")

    @property
    def views(self) -> FrozenSet[View]:
        """the leaf ,,View''s of the tree (the composition leaves it joins)."""
        if self.node is not None:
            return frozenset({self.node})
        return self.left.views | self.right.views

    @property
    def variables(self) -> FrozenSet[str]:
        """the union of all pattern variables across the tree's leaves."""
        return frozenset().union(*self.views)

    def to_dict(self) -> dict[str, Any]:
        if self.node is not None:
            return {"node": sorted(self.node)}
        return {"left": self.left.to_dict(), "right": self.right.to_dict()}


@dataclass(frozen=True)
class CompositionPlan:
    edges: List[ViewEdge]
    join_types: Dict[ViewEdge, JoinType]
    join_order: List[ViewEdge]
    deferred_conditions: Dict[ViewEdge, List[DeferredCondition]]
    join_tree: Optional[JoinTreeNode] = None


@dataclass(frozen=True)
class CompositionCover:
    """composition leaves for answering the query: the subset of the strategy's
    persistent views, plus virtual singleton base scans, joined during composition."""

    materialized_views: Tuple[View, ...]
    singleton_views: Tuple[View, ...]
    effective_view_set: FrozenSet[View]


@dataclass(frozen=True)
class CompositionVariant:
    """one concrete join plan for a composition cover."""

    cover: CompositionCover
    join_graph: CompositionJoinGraph
    plan: CompositionPlan
    cover_idx: int
    plan_idx_within_cover: int
    generation_method: str = "canonical"
    variant_idx: int = 0
    base_tree_idx: Optional[int] = None
    join_order_idx: Optional[int] = None


@dataclass(frozen=True)
class UpdatePlan:
    source_selections: Dict[View, Set[View]]
    effective_sources: Dict[View, Set[View]]
    update_dag: Dict[View, Set[View]]
    topological_order: List[View]
    last_variable_source: Dict[View, View]
    update_mode_by_view: Dict[View, UpdateMode] = field(default_factory=dict)
    source_join_trees: Optional[Dict[View, JoinTreeNode]] = None


@dataclass(frozen=True)
class SQLStatements:
    update_statements: List[str]
    composition_sql: str
    post_filter_sql: str


@dataclass(frozen=True)
class StrategyWithPlans:
    """materialization strategy plus its generated plan families. ,,effective_view_set''
    and ,,composition_plans'' hold the single canonical cover; ,,composition_covers'' and
    ,,composition_variants'' hold the enumerated covers and concrete join variants."""

    strategy: Set[View]
    effective_view_set: Set[View]
    composition_join_graph: CompositionJoinGraph
    composition_plans: List[CompositionPlan]
    update_plans: List[UpdatePlan]
    composition_covers: Tuple[CompositionCover, ...] = field(default_factory=tuple)
    composition_variants: Tuple[CompositionVariant, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CompileResult:
    ok: bool
    diagnostics: List[Diagnostic] = field(default_factory=list)
    ast: Optional[QueryAst] = None
    ir: Optional[NormalizedIR] = None

    def diagnostics_to_dict(self) -> List[dict[str, Any]]:
        return [item.to_dict() for item in self.diagnostics]


@dataclass(frozen=True)
class CompileToDependencyGraphResult:
    ok: bool
    diagnostics: List[Diagnostic] = field(default_factory=list)
    ast: Optional[QueryAst] = None
    ir: Optional[NormalizedIR] = None
    dependency_graph: Optional[DependencyGraph] = None

    def diagnostics_to_dict(self) -> List[dict[str, Any]]:
        return [item.to_dict() for item in self.diagnostics]


def ordered_variables(view: View, positions: Dict[str, int]) -> Tuple[str, ...]:
    return tuple(sorted(view, key=lambda var: positions[var]))


def view_sort_key(view: View, positions: Dict[str, int]) -> Tuple[int, ...]:
    return tuple(positions[var] for var in ordered_variables(view, positions))


def view_name(view: View, positions: Dict[str, int]) -> str:
    return "_".join(ordered_variables(view, positions))


def strategy_sort_key(strategy: Iterable[View], positions: Dict[str, int]) -> Tuple[Tuple[int, ...], ...]:
    keys = [view_sort_key(view, positions) for view in strategy]
    return tuple(sorted(keys))


def canonical_edge_key(left: View, right: View, positions: Dict[str, int]) -> ViewEdge:
    if view_sort_key(left, positions) <= view_sort_key(right, positions):
        return (left, right)
    return (right, left)


def canonical_variable_pair(left: str, right: str, positions: Dict[str, int]) -> Tuple[str, str]:
    if positions[left] <= positions[right]:
        return (left, right)
    return (right, left)


def make_view(items: Iterable[str]) -> View:
    return frozenset(items)
