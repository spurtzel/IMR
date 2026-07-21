"""render helpers for value predicates: rewrite ,,var.col'' references into the
qualified view/scan column names the emitter expects."""
from __future__ import annotations

import re
from typing import Any, Callable, Dict


# matches a qualified reference: identifier dot identifier (var.col)
QUALIFIED_REF_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\b")


def replace_qualified_refs(expression: str, resolver: Callable[[str, str], str | None]) -> str:
    """rewrite each ,,var.col'' reference through resolver; leave it untouched when resolver returns None."""
    def _replace(match: re.Match[str]) -> str:
        var = match.group(1)
        col = match.group(2)
        replacement = resolver(var, col)
        if replacement is None:
            return match.group(0)
        return replacement

    return QUALIFIED_REF_RE.sub(_replace, expression)


def is_kleene_variable(variable: str, dep_graph: Any | None) -> bool:
    """true when the variable carries a Kleene-plus quantifier (PLUS / RELUCTANT_PLUS)."""
    if dep_graph is None:
        return False
    return getattr(dep_graph, "quantifiers", {}).get(variable) in {"PLUS", "RELUCTANT_PLUS"}


def prefixed_condition_column(variable: str, column: str, dep_graph: Any | None = None) -> str:
    """view column name for (variable, column); Kleene variables read from the run's last-event columns."""
    if not is_kleene_variable(variable, dep_graph):
        return f"{variable}_{column}"

    if column == "id":
        return f"{variable}_last_id"
    if column == "ts":
        return f"{variable}_last_ts"
    return f"{variable}_last_{column}"


def render_expression_for_prefixed_columns(expression: str, var_to_alias: Dict[str, str], dep_graph: Any | None = None) -> str:
    """render an expression against prefixed view columns, qualifying each variable by its node alias."""
    return replace_qualified_refs(
        expression,
        lambda var, col: (
            f"{var_to_alias[var]}.{prefixed_condition_column(var, col, dep_graph)}"
            if var in var_to_alias
            else None
        ),
    )


def render_expression_for_base_scan(expression: str, variable: str, base_alias: str) -> str:
    """render an independent predicate against the raw base scan, reading the event column directly."""
    return replace_qualified_refs(
        expression,
        lambda var, col: f"{base_alias}.{col}" if var == variable else None,
    )
