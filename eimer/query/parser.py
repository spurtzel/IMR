"""match_recognize sql front-end: parse the restricted pattern-matching fragment
into a normalized ir, collecting diagnostics instead of raising on the first error.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from eimer.models import (
    AfterMatchAst,
    CompileResult,
    DefineConjunctAst,
    DefineEntryAst,
    Diagnostic,
    MatchRecognizeAst,
    MeasureAst,
    NormalizedDefineEntry,
    NormalizedIR,
    NormalizedMeasure,
    OrderByAst,
    PatternAst,
    PatternGapAst,
    PatternVariableAst,
    QueryAst,
    RowOutputAst,
)


# a bare sql identifier: letter/underscore start, then word chars
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# a pattern variable with an optional +/+? kleene quantifier
_PATTERN_VAR_RE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<quant>\+\??)?$")
# a measures item of the form ,,<expr> AS <alias>''
_MEASURE_ALIAS_RE = re.compile(
    r"(?is)^(?P<expr>.+?)\s+AS\s+(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\s*$"
)
# a qualified column reference ,,var.attr''
_QUALIFIED_REF_RE = re.compile(
    r"\b(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*(?P<attr>[A-Za-z_][A-Za-z0-9_]*)\b"
)
# a measure aggregate over a qualified column: FIRST|LAST|COUNT(var.attr)
_MEASURE_FUNC_RE = re.compile(
    r"(?is)^(?P<func>FIRST|LAST|COUNT)\s*\(\s*(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*(?P<attr>[A-Za-z_][A-Za-z0-9_]*)\s*\)$"
)
_ALLOWED_COMPARATORS = (">=", "<=", "<>", "=", ">", "<")
_REQUIRED_CLAUSES = (
    "ORDER BY",
    "MEASURES",
    "ONE ROW PER MATCH",
    "AFTER MATCH",
    "PATTERN",
    "DEFINE",
)
_SUPPORTED_TOP_LEVEL_CLAUSES: Sequence[Tuple[str, Tuple[str, ...]]] = (
    ("ORDER BY", ("ORDER", "BY")),
    ("MEASURES", ("MEASURES",)),
    ("ONE ROW PER MATCH", ("ONE", "ROW", "PER", "MATCH")),
    ("AFTER MATCH", ("AFTER", "MATCH")),
    ("PATTERN", ("PATTERN",)),
    ("DEFINE", ("DEFINE",)),
)
_UNSUPPORTED_TOP_LEVEL_CLAUSES: Sequence[Tuple[str, Tuple[str, ...]]] = (
    ("SUBSET", ("SUBSET",)),
    ("WITHIN", ("WITHIN",)),
    ("ALL ROWS PER MATCH", ("ALL", "ROWS", "PER", "MATCH")),
)


class ParseError(ValueError):
    pass


class DiagnosticCollector:
    """accumulates parse diagnostics so compilation can report every problem at once."""

    def __init__(self) -> None:
        self.items: List[Diagnostic] = []

    def add(self, code: str, message: str, clause: Optional[str] = None, detail: Optional[str] = None) -> None:
        self.items.append(Diagnostic(code=code, message=message, clause=clause, detail=detail))

    def extend(self, diagnostics: Sequence[Diagnostic]) -> None:
        self.items.extend(diagnostics)

    def has_errors(self) -> bool:
        return bool(self.items)


def _find_keyword_positions(text: str, keyword: str) -> List[int]:
    """byte offsets of every occurrence of ,,keyword'', skipping string and comment spans."""
    positions: List[int] = []
    keyword_upper = keyword.upper()
    i = 0
    in_single = False
    in_double = False

    while i < len(text):
        ch = text[i]

        if in_single:
            if ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                in_single = False
            i += 1
            continue

        if in_double:
            if ch == '"':
                if i + 1 < len(text) and text[i + 1] == '"':
                    i += 2
                    continue
                in_double = False
            i += 1
            continue

        if ch == "'":
            in_single = True
            i += 1
            continue

        if ch == '"':
            in_double = True
            i += 1
            continue

        if ch == "-" and i + 1 < len(text) and text[i + 1] == "-":
            i += 2
            while i < len(text) and text[i] != "\n":
                i += 1
            continue

        if ch == "/" and i + 1 < len(text) and text[i + 1] == "*":
            i += 2
            while i + 1 < len(text) and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i = min(i + 2, len(text))
            continue

        if ch.isalpha() or ch == "_":
            start = i
            i += 1
            while i < len(text) and (text[i].isalnum() or text[i] == "_"):
                i += 1
            token = text[start:i]
            if token.upper()==keyword_upper:
                positions.append(start)
            continue

        i += 1

    return positions


def _find_next_non_space(text: str, start: int) -> int:
    """index of the first non-whitespace char at or after ,,start''."""
    i = start
    while i < len(text) and text[i].isspace():
        i += 1
    return i


def _extract_balanced_parenthesized(text: str, open_paren_index: int) -> Tuple[str, int]:
    """text inside the parenthesized block opening at ,,open_paren_index'', plus the closing-paren index."""
    if open_paren_index >= len(text) or text[open_paren_index] != "(":
        raise ParseError("Expected '(' while parsing parenthesized block")

    i = open_paren_index
    depth = 0
    in_single = False
    in_double = False

    while i < len(text):
        ch = text[i]

        if in_single:
            if ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                in_single = False
            i += 1
            continue

        if in_double:
            if ch == '"':
                if i + 1 < len(text) and text[i + 1] == '"':
                    i += 2
                    continue
                in_double = False
            i += 1
            continue

        if ch == "'":
            in_single = True
            i += 1
            continue

        if ch == '"':
            in_double = True
            i += 1
            continue

        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren_index + 1 : i], i

        i += 1

    raise ParseError("Unbalanced parentheses while parsing MATCH_RECOGNIZE block")


def _extract_single_match_recognize(sql_text: str) -> Tuple[str, int, int]:
    """locate the one MATCH_RECOGNIZE block; return (body, keyword start, close index)."""
    positions = _find_keyword_positions(sql_text, "MATCH_RECOGNIZE")
    if not positions:
        raise ParseError("Expected exactly one MATCH_RECOGNIZE clause but found 0")
    if len(positions) > 1:
        raise ParseError(f"Expected exactly one MATCH_RECOGNIZE clause but found {len(positions)}")

    keyword_start = positions[0]
    after_keyword = keyword_start + len("MATCH_RECOGNIZE")
    open_index = _find_next_non_space(sql_text, after_keyword)
    if open_index >= len(sql_text) or sql_text[open_index] != "(":
        raise ParseError("Expected '(' after MATCH_RECOGNIZE")

    body, close_index = _extract_balanced_parenthesized(sql_text, open_index)
    return body, keyword_start, close_index


def _top_level_tokens(text: str) -> List[Tuple[str, int]]:
    """(uppercased word, offset) for each identifier at paren depth 0, skipping quoted text."""
    tokens: List[Tuple[str, int]] = []
    depth = 0
    in_single = False
    in_double = False
    i = 0

    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        if in_single:
            if ch == "'":
                if nxt == "'":
                    i += 2
                    continue
                in_single = False
            i += 1
            continue

        if in_double:
            if ch == '"':
                if nxt == '"':
                    i += 2
                    continue
                in_double = False
            i += 1
            continue

        if ch == "'":
            in_single = True
            i += 1
            continue

        if ch == '"':
            in_double = True
            i += 1
            continue

        if ch == "(":
            depth += 1
            i += 1
            continue

        if ch == ")":
            depth = max(0, depth - 1)
            i += 1
            continue

        if depth == 0 and (ch.isalpha() or ch == "_"):
            start = i
            i += 1
            while i < len(text) and (text[i].isalnum() or text[i] == "_"):
                i += 1
            tokens.append((text[start:i].upper(), start))
            continue

        i += 1

    return tokens


def _phrase_matches(tokens: Sequence[Tuple[str, int]], token_idx: int, phrase: Sequence[str]) -> bool:
    """true when the token run at ,,token_idx'' spells out ,,phrase''."""
    if token_idx + len(phrase) > len(tokens):
        return False
    for offset, expected in enumerate(phrase):
        if tokens[token_idx + offset][0] != expected:
            return False
    return True


def _normalize_space(text: str) -> str:
    """collapse runs of whitespace to single spaces."""
    return " ".join(text.strip().split())


def _slice_after_phrase(segment: str, phrase_tokens: Sequence[str]) -> str:
    """text after a leading clause phrase, or empty if the segment does not start with it."""
    phrase = " ".join(phrase_tokens)
    normalized = segment.lstrip()
    if not normalized.upper().startswith(phrase):
        return ""
    return normalized[len(phrase) :].strip()


def _split_top_level_csv(text: str) -> List[str]:
    """split on commas at paren depth 0, ignoring commas inside quotes or parens."""
    parts: List[str] = []
    start = 0
    depth = 0
    in_single = False
    in_double = False
    i = 0

    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        if in_single:
            if ch == "'":
                if nxt == "'":
                    i += 2
                    continue
                in_single = False
            i += 1
            continue

        if in_double:
            if ch == '"':
                if nxt == '"':
                    i += 2
                    continue
                in_double = False
            i += 1
            continue

        if ch == "'":
            in_single = True
            i += 1
            continue

        if ch == '"':
            in_double = True
            i += 1
            continue

        if ch == "(":
            depth += 1
            i += 1
            continue

        if ch == ")":
            depth = max(0, depth - 1)
            i += 1
            continue

        if ch == "," and depth == 0:
            part = text[start:i].strip()
            if part:
                parts.append(part)
            start = i + 1
            i += 1
            continue

        i += 1

    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _split_top_level_conjuncts(expr: str) -> List[str]:
    """split a boolean expression on top-level AND, keeping BETWEEN ... AND ... intact."""
    parts: List[str] = []
    start = 0
    depth = 0
    i = 0
    in_single = False
    in_double = False
    between_pending = False

    while i < len(expr):
        ch = expr[i]
        nxt = expr[i + 1] if i + 1 < len(expr) else ""

        if in_single:
            if ch == "'":
                if nxt == "'":
                    i += 2
                    continue
                in_single = False
            i += 1
            continue

        if in_double:
            if ch == '"':
                if nxt == '"':
                    i += 2
                    continue
                in_double = False
            i += 1
            continue

        if ch == "'":
            in_single = True
            i += 1
            continue

        if ch == '"':
            in_double = True
            i += 1
            continue

        if ch == "(":
            depth += 1
            i += 1
            continue

        if ch == ")":
            depth = max(0, depth - 1)
            i += 1
            continue

        if depth == 0 and (ch.isalpha() or ch == "_"):
            token_start = i
            i += 1
            while i < len(expr) and (expr[i].isalnum() or expr[i] == "_"):
                i += 1
            token = expr[token_start:i].upper()

            if token == "BETWEEN":
                between_pending = True
                continue

            if token == "AND":
                if between_pending:
                    between_pending = False
                    continue
                part = expr[start:token_start].strip()
                if part:
                    parts.append(part)
                start = i
                continue

            continue

        i += 1

    tail = expr[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _contains_top_level_keyword(expr: str, keyword: str) -> bool:
    """true when ,,keyword'' appears as a word at paren depth 0."""
    depth = 0
    i = 0
    in_single = False
    in_double = False
    keyword_upper = keyword.upper()

    while i < len(expr):
        ch = expr[i]
        nxt = expr[i + 1] if i + 1 < len(expr) else ""

        if in_single:
            if ch == "'":
                if nxt == "'":
                    i += 2
                    continue
                in_single = False
            i += 1
            continue

        if in_double:
            if ch == '"':
                if nxt == '"':
                    i += 2
                    continue
                in_double = False
            i += 1
            continue

        if ch == "'":
            in_single = True
            i += 1
            continue

        if ch == '"':
            in_double = True
            i += 1
            continue

        if ch == "(":
            depth += 1
            i += 1
            continue

        if ch == ")":
            depth = max(0, depth - 1)
            i += 1
            continue

        if depth == 0 and (ch.isalpha() or ch == "_"):
            start = i
            i += 1
            while i < len(expr) and (expr[i].isalnum() or expr[i] == "_"):
                i += 1
            if expr[start:i].upper() == keyword_upper:
                return True
            continue

        i += 1

    return False


def _resolve_variable(name: str, known_variables: Sequence[str]) -> Optional[str]:
    """map a name to its canonical pattern-variable spelling, case-insensitively."""
    by_upper = {item.upper(): item for item in known_variables}
    return by_upper.get(name.upper())


def _all_qualified_refs(expr: str) -> List[Tuple[str, str]]:
    """every (var, attr) qualified reference in an expression."""
    return [(match.group("var"), match.group("attr")) for match in _QUALIFIED_REF_RE.finditer(expr)]


def _detect_operator(expr: str) -> Optional[str]:
    """the comparison driving a conjunct: BETWEEN, or the first top-level comparator."""
    if _contains_top_level_keyword(expr, "BETWEEN"):
        return "BETWEEN"

    depth = 0
    i = 0
    in_single = False
    in_double = False
    while i < len(expr):
        ch = expr[i]
        nxt = expr[i + 1] if i + 1 < len(expr) else ""

        if in_single:
            if ch == "'":
                if nxt == "'":
                    i += 2
                    continue
                in_single = False
            i += 1
            continue

        if in_double:
            if ch == '"':
                if nxt == '"':
                    i += 2
                    continue
                in_double = False
            i += 1
            continue

        if ch == "'":
            in_single = True
            i += 1
            continue

        if ch == '"':
            in_double = True
            i += 1
            continue

        if ch == "(":
            depth += 1
            i += 1
            continue

        if ch == ")":
            depth = max(0, depth - 1)
            i += 1
            continue

        if depth == 0:
            if expr.startswith("!=", i):
                return "!="
            for operator in _ALLOWED_COMPARATORS:
                if expr.startswith(operator, i):
                    return operator

        i += 1

    return None


def _validate_outer_sql(prefix_sql: str, collector: DiagnosticCollector) -> None:
    """check the outer statement is SELECT ... FROM ... before MATCH_RECOGNIZE."""
    select_positions = _find_keyword_positions(prefix_sql, "SELECT")
    from_positions = _find_keyword_positions(prefix_sql, "FROM")
    if not select_positions or not from_positions:
        collector.add(
            "outer_sql_shape",
            "Input must be a full SELECT ... FROM ... MATCH_RECOGNIZE(...) statement.",
            clause="QUERY",
        )
        return

    select_pos = select_positions[0]
    from_pos = next((pos for pos in from_positions if pos > select_pos), None)
    if from_pos is None:
        collector.add(
            "outer_sql_shape",
            "Input must contain FROM after SELECT before MATCH_RECOGNIZE.",
            clause="QUERY",
        )


def _extract_clause_segments(mr_body: str, collector: DiagnosticCollector) -> Dict[str, str]:
    """carve the MR body into its top-level clause segments, flagging duplicate and unsupported clauses."""
    tokens = _top_level_tokens(mr_body)
    supported_hits: List[Tuple[int, int, str, Tuple[str, ...]]] = []

    for idx, (_token, position) in enumerate(tokens):
        for clause_name, phrase in _UNSUPPORTED_TOP_LEVEL_CLAUSES:
            if _phrase_matches(tokens, idx, phrase):
                collector.add(
                    "unsupported_clause",
                    f"Unsupported MATCH_RECOGNIZE clause '{clause_name}'.",
                    clause=clause_name,
                )
        for clause_name, phrase in _SUPPORTED_TOP_LEVEL_CLAUSES:
            if _phrase_matches(tokens, idx, phrase):
                supported_hits.append((position, idx, clause_name, phrase))

    seen: Dict[str, Tuple[int, Tuple[str, ...]]] = {}
    for position, idx, clause_name, phrase in sorted(supported_hits, key=lambda item: item[0]):
        if clause_name in seen:
            collector.add(
                "duplicate_clause",
                f"Duplicate MATCH_RECOGNIZE clause '{clause_name}'.",
                clause=clause_name,
            )
            continue
        seen[clause_name] = (position, phrase)

    segments: Dict[str, str] = {}
    ordered = sorted(((position, name, phrase) for name, (position, phrase) in seen.items()), key=lambda item: item[0])
    for idx, (position, clause_name, _phrase) in enumerate(ordered):
        next_position = ordered[idx + 1][0] if idx + 1 < len(ordered) else len(mr_body)
        segments[clause_name] = mr_body[position:next_position].strip()

    for clause_name in _REQUIRED_CLAUSES:
        if clause_name not in segments:
            collector.add(
                "missing_clause",
                f"Required MATCH_RECOGNIZE clause '{clause_name}' is missing.",
                clause=clause_name,
            )

    return segments


def _parse_pattern(pattern_text: str, collector: DiagnosticCollector) -> Optional[PatternAst]:
    """parse the PATTERN clause into alternating named variables and wildcard gaps."""
    tokens = [token for token in pattern_text.replace("\n", " ").split() if token]
    if not tokens:
        collector.add("empty_pattern", "PATTERN clause must not be empty.", clause="PATTERN")
        return None

    if len(tokens) % 2 == 0:
        collector.add(
            "invalid_pattern_shape",
            "PATTERN must alternate named variables and wildcard gaps, ending in a named variable.",
            clause="PATTERN",
            detail=pattern_text,
        )
        return None

    variables: List[PatternVariableAst] = []
    gaps: List[PatternGapAst] = []
    seen_variables: set[str] = set()

    for index, token in enumerate(tokens):
        if index % 2 == 0:
            match = _PATTERN_VAR_RE.fullmatch(token)
            if not match:
                collector.add(
                    "unsupported_pattern_token",
                    f"Unsupported named-variable token '{token}' in PATTERN.",
                    clause="PATTERN",
                )
                return None
            name = match.group("name")
            quantifier = match.group("quant") or ""
            if name.upper() == "Z":
                collector.add(
                    "reserved_wildcard_name",
                    "Named pattern variables cannot use the reserved wildcard name 'Z'.",
                    clause="PATTERN",
                )
                return None
            if name.upper() in seen_variables:
                collector.add(
                    "duplicate_pattern_variable",
                    f"Duplicate pattern variable '{name}' is not allowed in this fragment.",
                    clause="PATTERN",
                )
                return None
            seen_variables.add(name.upper())
            normalized_quantifier = "ONE"
            if quantifier == "+":
                normalized_quantifier = "PLUS"
            elif quantifier == "+?":
                normalized_quantifier = "RELUCTANT_PLUS"
            variables.append(PatternVariableAst(name=name, quantifier=normalized_quantifier, raw_text=token))
            continue

        if token.upper() == "Z*":
            gaps.append(PatternGapAst(wildcard="Z", mode="GREEDY", raw_text=token))
            continue
        if token.upper() == "Z*?":
            gaps.append(PatternGapAst(wildcard="Z", mode="RELUCTANT", raw_text=token))
            continue
        collector.add(
            "unsupported_wildcard_gap",
            f"Unsupported wildcard gap token '{token}'. Only Z* and Z*? are allowed.",
            clause="PATTERN",
        )
        return None

    return PatternAst(raw_text=pattern_text.strip(), variables=variables, gaps=gaps)


def _parse_define_entries(
    define_text: str, pattern_ast: PatternAst, collector: DiagnosticCollector,
) -> Optional[List[DefineEntryAst]]:
    """parse DEFINE into per-variable independent and binary-dependent conjuncts."""
    parts = _split_top_level_csv(define_text)
    if not parts:
        collector.add("empty_define", "DEFINE clause must not be empty.", clause="DEFINE")
        return None

    known_variables = [item.name for item in pattern_ast.variables]
    known_upper = {item.upper() for item in known_variables}
    kleene_variables = {
        item.name.upper()
        for item in pattern_ast.variables
        if item.quantifier in {"PLUS", "RELUCTANT_PLUS"}
    }

    entries_by_upper: Dict[str, DefineEntryAst] = {}
    for part in parts:
        # a DEFINE entry: ,,<variable> AS <expression>''
        match = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s+AS\s+(.+?)\s*", part, re.IGNORECASE | re.DOTALL)
        if not match:
            collector.add(
                "invalid_define_entry",
                "DEFINE entries must use the form 'Variable AS expression'.",
                clause="DEFINE",
                detail=part,
            )
            continue

        variable_token = match.group(1)
        expr = match.group(2).strip()
        if not expr:
            collector.add(
                "empty_define_expression",
                f"DEFINE entry for '{variable_token}' has an empty expression.",
                clause="DEFINE",
            )
            continue

        variable_upper = variable_token.upper()
        if variable_upper != "Z" and variable_upper not in known_upper:
            collector.add(
                "unknown_define_variable",
                f"DEFINE entry refers to unknown variable '{variable_token}'.",
                clause="DEFINE",
            )
            continue

        if variable_upper in entries_by_upper:
            collector.add(
                "duplicate_define_entry",
                f"Duplicate DEFINE entry for variable '{variable_token}'.",
                clause="DEFINE",
            )
            continue

        if variable_upper == "Z":
            if _normalize_space(expr).upper() != "TRUE":
                collector.add(
                    "wildcard_define_not_true",
                    "Wildcard variable Z must be defined exactly as TRUE in this fragment.",
                    clause="DEFINE",
                )
                continue
            entries_by_upper[variable_upper] = DefineEntryAst(variable="Z", raw_expression="TRUE", conjuncts=[])
            continue

        resolved_variable = _resolve_variable(variable_token, known_variables)
        assert resolved_variable is not None
        if _contains_top_level_keyword(expr, "OR"):
            collector.add(
                "define_or_not_supported",
                "DEFINE expressions must be conjunctions only; OR is not supported.",
                clause="DEFINE",
                detail=part,
            )
            continue

        conjunct_texts = _split_top_level_conjuncts(expr)
        conjuncts: List[DefineConjunctAst] = []
        valid_entry = True
        for conjunct_text in conjunct_texts:
            operator = _detect_operator(conjunct_text)
            if operator is None:
                collector.add(
                    "unsupported_predicate_operator",
                    "DEFINE conjunct must use one of =, <>, >, >=, <, <=, or BETWEEN ... AND ... .",
                    clause="DEFINE",
                    detail=conjunct_text,
                )
                valid_entry = False
                continue
            if operator == "!=":
                collector.add(
                    "unsupported_predicate_operator",
                    "Operator '!=' is not supported in this fragment; use '<>' instead.",
                    clause="DEFINE",
                    detail=conjunct_text,
                )
                valid_entry = False
                continue

            all_refs = _all_qualified_refs(conjunct_text)
            unknown_qualifiers = sorted(
                {
                    var
                    for var, _attr in all_refs
                    if var.upper() not in known_upper and var.upper() != "Z"
                }
            )
            if unknown_qualifiers:
                collector.add(
                    "unknown_qualified_identifier",
                    "DEFINE conjunct contains an unknown qualified identifier.",
                    clause="DEFINE",
                    detail=conjunct_text,
                )
                valid_entry = False
                continue

            referenced_variables = []
            seen_ref_uppers: set[str] = set()
            for var, _attr in all_refs:
                if var.upper() == "Z":
                    collector.add(
                        "wildcard_ref_not_supported",
                        "Predicates over wildcard variable Z are not supported in this fragment.",
                        clause="DEFINE",
                        detail=conjunct_text,
                    )
                    valid_entry = False
                    break
                resolved_ref = _resolve_variable(var, known_variables)
                if resolved_ref is None:
                    continue
                if resolved_ref.upper() not in seen_ref_uppers:
                    seen_ref_uppers.add(resolved_ref.upper())
                    referenced_variables.append(resolved_ref)
            if not valid_entry:
                continue

            current_upper = resolved_variable.upper()
            ref_uppers = {item.upper() for item in referenced_variables}
            if not ref_uppers or ref_uppers == {current_upper}:
                conjuncts.append(
                    DefineConjunctAst(
                        text=conjunct_text,
                        kind="INDEPENDENT",
                        operator=operator,
                        referenced_variables=sorted(ref_uppers) if ref_uppers else [resolved_variable],
                    )
                )
                continue

            if len(ref_uppers) == 2 and current_upper in ref_uppers:
                if any(ref_upper in kleene_variables for ref_upper in ref_uppers):
                    collector.add(
                        "kleene_dependent_predicate_not_supported",
                        "Dependent predicates involving Kleene variables are not supported in this fragment.",
                        clause="DEFINE",
                        detail=conjunct_text,
                    )
                    valid_entry = False
                    continue
                conjuncts.append(
                    DefineConjunctAst(
                        text=conjunct_text,
                        kind="DEPENDENT",
                        operator=operator,
                        referenced_variables=sorted(ref_uppers),
                    )
                )
                continue

            collector.add(
                "unsupported_define_conjunct",
                "DEFINE conjunct must be independent or binary dependent on the current non-Kleene variable.",
                clause="DEFINE",
                detail=conjunct_text,
            )
            valid_entry = False

        if valid_entry:
            entries_by_upper[variable_upper] = DefineEntryAst(
                variable=resolved_variable,
                raw_expression=expr,
                conjuncts=conjuncts,
            )

    if "Z" not in entries_by_upper:
        collector.add(
            "missing_wildcard_define",
            "DEFINE clause must contain 'Z AS TRUE'.",
            clause="DEFINE",
        )

    ordered_entries: List[DefineEntryAst] = []
    for variable in known_variables:
        entry = entries_by_upper.get(variable.upper())
        if entry is None:
            ordered_entries.append(DefineEntryAst(variable=variable, raw_expression="", conjuncts=[]))
        else:
            ordered_entries.append(entry)
    if "Z" in entries_by_upper:
        ordered_entries.append(entries_by_upper["Z"])
    return ordered_entries


def _parse_measures(
    measures_text: str, pattern_ast: PatternAst, collector: DiagnosticCollector,
) -> Optional[List[MeasureAst]]:
    """parse MEASURES into typed measure asts (bare V.attr, or FIRST/LAST/COUNT aggregates)."""
    items = _split_top_level_csv(measures_text)
    if not items:
        collector.add("empty_measures", "MEASURES clause must not be empty.", clause="MEASURES")
        return None

    known_variables = [item.name for item in pattern_ast.variables]
    kleene_variables = {
        item.name.upper()
        for item in pattern_ast.variables
        if item.quantifier in {"PLUS", "RELUCTANT_PLUS"}
    }

    measures: List[MeasureAst] = []
    for item in items:
        alias_match = _MEASURE_ALIAS_RE.match(item)
        if not alias_match:
            collector.add(
                "measure_alias_required",
                "Each MEASURES item must use explicit 'AS alias'.",
                clause="MEASURES",
                detail=item,
            )
            continue

        expr = alias_match.group("expr").strip()
        alias = alias_match.group("alias")
        bare_match = _QUALIFIED_REF_RE.fullmatch(expr)
        if bare_match:
            variable = bare_match.group("var")
            attribute = bare_match.group("attr")
            resolved_variable = _resolve_variable(variable, known_variables)
            if resolved_variable is None:
                if variable.upper() == "Z":
                    collector.add(
                        "wildcard_measure_not_supported",
                        "Measures over wildcard variable Z are not supported in v1.",
                        clause="MEASURES",
                        detail=item,
                    )
                else:
                    collector.add(
                        "unknown_measure_variable",
                        f"Unknown MATCH_RECOGNIZE variable '{variable}' in MEASURES.",
                        clause="MEASURES",
                        detail=item,
                    )
                continue
            if resolved_variable.upper() in kleene_variables:
                collector.add(
                    "bare_kleene_measure_not_supported",
                    "Bare V.attr measures are allowed only for non-Kleene variables.",
                    clause="MEASURES",
                    detail=item,
                )
                continue
            measures.append(
                MeasureAst(
                    kind="VALUE",
                    variable=resolved_variable,
                    attribute=attribute,
                    alias=alias,
                    raw_expression=expr,
                )
            )
            continue

        func_match = _MEASURE_FUNC_RE.match(expr)
        if not func_match:
            collector.add(
                "unsupported_measure_expression",
                "Unsupported MEASURES expression in this fragment.",
                clause="MEASURES",
                detail=item,
            )
            continue

        function_name = func_match.group("func").upper()
        variable = func_match.group("var")
        attribute = func_match.group("attr")
        resolved_variable = _resolve_variable(variable, known_variables)
        if resolved_variable is None:
            if variable.upper() == "Z":
                collector.add(
                    "wildcard_measure_not_supported",
                    "Measures over wildcard variable Z are not supported in v1.",
                    clause="MEASURES",
                    detail=item,
                )
            else:
                collector.add(
                    "unknown_measure_variable",
                    f"Unknown MATCH_RECOGNIZE variable '{variable}' in MEASURES.",
                    clause="MEASURES",
                    detail=item,
                )
            continue

        variable_upper = resolved_variable.upper()
        if variable_upper not in kleene_variables:
            collector.add(
                "non_kleene_aggregate_measure_not_supported",
                "FIRST/LAST/COUNT measures are only allowed for Kleene variables in v1.",
                clause="MEASURES",
                detail=item,
            )
            continue

        if function_name == "COUNT":
            if attribute.lower() != "id":
                collector.add(
                    "invalid_count_measure",
                    "COUNT is only allowed as COUNT(V.id) for Kleene variables.",
                    clause="MEASURES",
                    detail=item,
                )
                continue
            measures.append(
                MeasureAst(
                    kind="COUNT",
                    variable=resolved_variable,
                    attribute="id",
                    alias=alias,
                    raw_expression=expr,
                )
            )
            continue

        measures.append(
            MeasureAst(
                kind=function_name,
                variable=resolved_variable,
                attribute=attribute,
                alias=alias,
                raw_expression=expr,
            )
        )

    return measures


def _build_ir(match_ast: MatchRecognizeAst) -> NormalizedIR:
    """flatten the validated ast into the normalized ir the compiler stages consume."""
    return NormalizedIR(
        pattern_variables=[item.name for item in match_ast.pattern.variables],
        variable_quantifiers=[
            {"name": item.name, "quantifier": item.quantifier}
            for item in match_ast.pattern.variables
        ],
        wildcard_gaps=[
            {"wildcard": item.wildcard, "mode": item.mode}
            for item in match_ast.pattern.gaps
        ],
        order_by=match_ast.order_by.raw_text,
        define_entries=[
            NormalizedDefineEntry(
                variable=item.variable,
                raw_expression=item.raw_expression,
                conjuncts=item.conjuncts,
            )
            for item in match_ast.define_entries
        ],
        measures=[
            NormalizedMeasure(
                kind=item.kind,
                variable=item.variable,
                attribute=item.attribute,
                alias=item.alias,
            )
            for item in match_ast.measures
        ],
        row_output_policy=match_ast.row_output.mode,
        after_match_policy=match_ast.after_match.mode,
    )


def compile_sql_text(sql_text: str) -> CompileResult:
    """compile a full statement into a CompileResult: ast plus ir, or the collected diagnostics."""
    collector = DiagnosticCollector()
    try:
        mr_body, mr_start, mr_close = _extract_single_match_recognize(sql_text)
    except ParseError as exc:
        collector.add("match_recognize_extraction", str(exc), clause="QUERY")
        return CompileResult(ok=False, diagnostics=collector.items)

    prefix_sql = sql_text[:mr_start]
    suffix_sql = sql_text[mr_close + 1 :]
    _validate_outer_sql(prefix_sql, collector)

    clause_segments = _extract_clause_segments(mr_body, collector)
    required_present = all(name in clause_segments for name in _REQUIRED_CLAUSES)
    if not required_present or collector.has_errors() and "PATTERN" not in clause_segments:
        return CompileResult(ok=False, diagnostics=collector.items)

    order_by_text = _slice_after_phrase(clause_segments["ORDER BY"], ("ORDER", "BY"))
    if not order_by_text:
        collector.add("empty_order_by", "ORDER BY clause must not be empty.", clause="ORDER BY")

    one_row_segment = _normalize_space(clause_segments["ONE ROW PER MATCH"]).upper()
    if one_row_segment!="ONE ROW PER MATCH":
        collector.add(
            "unsupported_row_output_policy",
            "Only ONE ROW PER MATCH is supported in v1.",
            clause="ONE ROW PER MATCH",
            detail=clause_segments["ONE ROW PER MATCH"],
        )

    after_match_segment = _normalize_space(clause_segments["AFTER MATCH"]).upper()
    if after_match_segment != "AFTER MATCH SKIP TO NEXT ROW":
        collector.add(
            "unsupported_after_match_policy",
            "Only AFTER MATCH SKIP TO NEXT ROW is supported in v1.",
            clause="AFTER MATCH",
            detail=clause_segments["AFTER MATCH"],
        )

    pattern_text = _slice_after_phrase(clause_segments["PATTERN"], ("PATTERN",))
    if not pattern_text:
        collector.add("empty_pattern", "PATTERN clause must not be empty.", clause="PATTERN")
        return CompileResult(ok=False, diagnostics=collector.items)
    if pattern_text.startswith("(") and pattern_text.endswith(")"):
        pattern_text = pattern_text[1:-1].strip()
    pattern_ast = _parse_pattern(pattern_text, collector)

    define_text = _slice_after_phrase(clause_segments["DEFINE"], ("DEFINE",))
    if not define_text:
        collector.add("empty_define", "DEFINE clause must not be empty.", clause="DEFINE")
    measures_text = _slice_after_phrase(clause_segments["MEASURES"], ("MEASURES",))
    if not measures_text:
        collector.add("empty_measures", "MEASURES clause must not be empty.", clause="MEASURES")

    define_entries: Optional[List[DefineEntryAst]] = None
    measures: Optional[List[MeasureAst]] = None
    if pattern_ast is not None and define_text:
        define_entries = _parse_define_entries(define_text, pattern_ast, collector)
    if pattern_ast is not None and measures_text:
        measures = _parse_measures(measures_text, pattern_ast, collector)

    if collector.has_errors() or pattern_ast is None or define_entries is None or measures is None or not order_by_text:
        return CompileResult(ok=False, diagnostics=collector.items)

    match_ast = MatchRecognizeAst(
        raw_body=mr_body,
        order_by=OrderByAst(raw_text=order_by_text),
        measures=measures,
        row_output=RowOutputAst(mode="ONE_ROW_PER_MATCH"),
        after_match=AfterMatchAst(mode="AFTER_MATCH_SKIP_TO_NEXT_ROW"),
        pattern=pattern_ast,
        define_entries=define_entries,
    )
    query_ast = QueryAst(
        raw_sql=sql_text,
        prefix_sql=prefix_sql,
        suffix_sql=suffix_sql,
        match_recognize=match_ast,
    )
    normalized_ir = _build_ir(match_ast)
    return CompileResult(ok=True, diagnostics=[], ast=query_ast, ir=normalized_ir)


def compile_sql_file(path: str | Path) -> CompileResult:
    """compile the sql read from a file."""
    sql_text = Path(path).read_text(encoding="utf-8")
    return compile_sql_text(sql_text)


