"""restricted-pattern query specification.

QuerySpec is a typed description of the restricted model V1 Z* V2 Z* ... Z* Vn; it
renders to MATCH_RECOGNIZE sql and compiles to a DependencyGraph.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from typing import Any, Literal

from eimer.query.dependency_graph import compile_sql_to_dependency_graph
from eimer.models import DependencyGraph


PredicateKind = Literal["independent", "dependent"]


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    sql_type: str


@dataclass(frozen=True)
class VariableSpec:
    name: str
    output_prefix: str
    independent_predicate_id: str
    quantifier: str = "ONE"  # ONE | PLUS | RELUCTANT_PLUS (single-variable independent Kleene)


@dataclass(frozen=True)
class QueryPredicateSpec:
    predicate_id: str
    kind: PredicateKind
    variables: tuple[str, ...]
    sql_template: str
    referenced_columns: tuple[str, ...] = ()
    selectivity_key: str | None = None


@dataclass(frozen=True)
class MeasureSpec:
    expression: str
    alias: str
    sql_type: str | None = None


@dataclass(frozen=True)
class PatternSpec:
    variables: tuple[str, ...]
    wildcard: str = "Z"
    wildcard_quantifier: str = "*"
    # optional per-gap quantifiers: entry i is the wildcard quantifier for the gap
    # between variables[i] and variables[i+1] (len == len(variables) - 1), each greedy
    # "*" or reluctant "*?". None means every gap uses wildcard_quantifier.
    gap_quantifiers: tuple[str, ...] | None = None


@dataclass(frozen=True)
class QuerySpec:
    name: str
    variables: tuple[VariableSpec, ...]
    independent_predicates: tuple[QueryPredicateSpec, ...]
    dependent_predicates: tuple[QueryPredicateSpec, ...]
    measures: tuple[MeasureSpec, ...]
    result_key_columns: tuple[str, ...]
    event_schema: tuple[ColumnSpec, ...]
    pattern: PatternSpec | None = None
    id_column: str = "id"
    time_column: str = "time"
    ts_column: str = "ts"
    order_by: tuple[str, ...] = ("ts", "id")
    row_output: str = "ONE ROW PER MATCH"
    after_match: str = "AFTER MATCH SKIP TO NEXT ROW"
    partition_by: tuple[str, ...] = ()
    window: Any | None = None
    kleene_annotations: tuple[Any, ...] = ()

    @property
    def variable_names(self) -> tuple[str, ...]:
        return tuple(variable.name for variable in self.variables)

    @property
    def effective_pattern(self) -> PatternSpec:
        return self.pattern or PatternSpec(self.variable_names)


def _require_mapping(payload: Any, *, context: str) -> Mapping[str, Any]:
    """assert the payload is a mapping, else raise with ,,context'' in the message."""
    if not isinstance(payload, Mapping):
        raise ValueError(f"{context} must be an object")
    return payload


def _require_sequence(payload: Mapping[str, Any], key: str) -> tuple[Any, ...]:
    """fetch a required array-valued field as a tuple, else raise."""
    if key not in payload:
        raise ValueError(f"QuerySpec JSON missing required field {key!r}")
    value = payload[key]
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"QuerySpec JSON field {key!r} must be an array")
    return tuple(value)


def query_spec_from_dict(payload: Mapping[str, Any]) -> QuerySpec:
    """Deserialize a restricted-model QuerySpec from a JSON-compatible dict."""

    payload = _require_mapping(payload, context="QuerySpec JSON")
    for key in (
        "name",
        "variables",
        "independent_predicates",
        "dependent_predicates",
        "measures",
        "result_key_columns",
        "event_schema",
        "id_column",
        "time_column",
        "ts_column",
        "order_by",
        "row_output",
        "after_match",
    ):
        if key not in payload:
            raise ValueError(f"QuerySpec JSON missing required field {key!r}")

    pattern_payload = payload.get("pattern")
    pattern = None
    if pattern_payload is not None:
        pattern_mapping = _require_mapping(pattern_payload, context="QuerySpec.pattern")
        gap_payload = pattern_mapping.get("gap_quantifiers")
        pattern = PatternSpec(
            variables=tuple(str(item) for item in _require_sequence(pattern_mapping, "variables")),
            wildcard=str(pattern_mapping.get("wildcard", "Z")),
            wildcard_quantifier=str(pattern_mapping.get("wildcard_quantifier", "*")),
            gap_quantifiers=None if gap_payload is None
            else tuple(str(item) for item in gap_payload),
        )

    spec = QuerySpec(
        name=str(payload["name"]),
        variables=tuple(
            VariableSpec(
                name=str(_require_mapping(item, context="VariableSpec")["name"]),
                output_prefix=str(_require_mapping(item, context="VariableSpec")["output_prefix"]),
                independent_predicate_id=str(
                    _require_mapping(item, context="VariableSpec")["independent_predicate_id"]
                ),
                quantifier=str(_require_mapping(item, context="VariableSpec").get("quantifier", "ONE")),
            )
            for item in _require_sequence(payload, "variables")
        ),
        independent_predicates=tuple(
            QueryPredicateSpec(
                predicate_id=str(_require_mapping(item, context="QueryPredicateSpec")["predicate_id"]),
                kind=str(_require_mapping(item, context="QueryPredicateSpec")["kind"]),  # type: ignore[arg-type]
                variables=tuple(str(value) for value in _require_sequence(_require_mapping(item, context="QueryPredicateSpec"), "variables")),
                sql_template=str(_require_mapping(item, context="QueryPredicateSpec")["sql_template"]),
                referenced_columns=tuple(
                    str(value)
                    for value in _require_mapping(item, context="QueryPredicateSpec").get("referenced_columns", ())
                ),
                selectivity_key=(
                    None
                    if _require_mapping(item, context="QueryPredicateSpec").get("selectivity_key") in (None, "")
                    else str(_require_mapping(item, context="QueryPredicateSpec").get("selectivity_key"))
                ),
            )
            for item in _require_sequence(payload, "independent_predicates")
        ),
        dependent_predicates=tuple(
            QueryPredicateSpec(
                predicate_id=str(_require_mapping(item, context="QueryPredicateSpec")["predicate_id"]),
                kind=str(_require_mapping(item, context="QueryPredicateSpec")["kind"]),  # type: ignore[arg-type]
                variables=tuple(str(value) for value in _require_sequence(_require_mapping(item, context="QueryPredicateSpec"), "variables")),
                sql_template=str(_require_mapping(item, context="QueryPredicateSpec")["sql_template"]),
                referenced_columns=tuple(
                    str(value)
                    for value in _require_mapping(item, context="QueryPredicateSpec").get("referenced_columns", ())
                ),
                selectivity_key=(
                    None
                    if _require_mapping(item, context="QueryPredicateSpec").get("selectivity_key") in (None, "")
                    else str(_require_mapping(item, context="QueryPredicateSpec").get("selectivity_key"))
                ),
            )
            for item in _require_sequence(payload, "dependent_predicates")
        ),
        measures=tuple(
            MeasureSpec(
                expression=str(_require_mapping(item, context="MeasureSpec")["expression"]),
                alias=str(_require_mapping(item, context="MeasureSpec")["alias"]),
                sql_type=(
                    None
                    if _require_mapping(item, context="MeasureSpec").get("sql_type") in (None, "")
                    else str(_require_mapping(item, context="MeasureSpec").get("sql_type"))
                ),
            )
            for item in _require_sequence(payload, "measures")
        ),
        result_key_columns=tuple(str(item) for item in _require_sequence(payload, "result_key_columns")),
        event_schema=tuple(
            ColumnSpec(
                name=str(_require_mapping(item, context="ColumnSpec")["name"]),
                sql_type=str(_require_mapping(item, context="ColumnSpec")["sql_type"]),
            )
            for item in _require_sequence(payload, "event_schema")
        ),
        pattern=pattern,
        id_column=str(payload["id_column"]),
        time_column=str(payload["time_column"]),
        ts_column=str(payload["ts_column"]),
        order_by=tuple(str(item) for item in _require_sequence(payload, "order_by")),
        row_output=str(payload["row_output"]),
        after_match=str(payload["after_match"]),
        partition_by=tuple(str(item) for item in payload.get("partition_by", ())),
        window=payload.get("window"),
        kleene_annotations=tuple(payload.get("kleene_annotations", ())),
    )
    validate_query_spec(spec)
    return spec


def query_spec_to_dict(spec: QuerySpec) -> dict[str, Any]:
    """Serialize a QuerySpec to normalized JSON-compatible data."""

    validate_query_spec(spec)
    pattern = spec.effective_pattern
    return {
        "name": spec.name,
        "variables": [
            {
                "name": variable.name,
                "output_prefix": variable.output_prefix,
                "independent_predicate_id": variable.independent_predicate_id,
                # Emit quantifier only when non-default, keeping default specs compact.
                **({"quantifier": variable.quantifier} if variable.quantifier != "ONE" else {}),
            }
            for variable in spec.variables
        ],
        "independent_predicates": [
            {
                "predicate_id": predicate.predicate_id,
                "kind": predicate.kind,
                "variables": list(predicate.variables),
                "sql_template": predicate.sql_template,
                "referenced_columns": list(predicate.referenced_columns),
                "selectivity_key": predicate.selectivity_key,
            }
            for predicate in spec.independent_predicates
        ],
        "dependent_predicates": [
            {
                "predicate_id": predicate.predicate_id,
                "kind": predicate.kind,
                "variables": list(predicate.variables),
                "sql_template": predicate.sql_template,
                "referenced_columns": list(predicate.referenced_columns),
                "selectivity_key": predicate.selectivity_key,
            }
            for predicate in spec.dependent_predicates
        ],
        "measures": [
            {
                "expression": measure.expression,
                "alias": measure.alias,
                "sql_type": measure.sql_type,
            }
            for measure in spec.measures
        ],
        "result_key_columns": list(spec.result_key_columns),
        "event_schema": [
            {
                "name": column.name,
                "sql_type": column.sql_type,
            }
            for column in spec.event_schema
        ],
        "pattern": {
            "variables": list(pattern.variables),
            "wildcard": pattern.wildcard,
            "wildcard_quantifier": pattern.wildcard_quantifier,
            # emitted only when set
            **({"gap_quantifiers": list(pattern.gap_quantifiers)}
               if pattern.gap_quantifiers is not None else {}),
        },
        "id_column": spec.id_column,
        "time_column": spec.time_column,
        "ts_column": spec.ts_column,
        "order_by": list(spec.order_by),
        "row_output": spec.row_output,
        "after_match": spec.after_match,
        "partition_by": list(spec.partition_by),
        "window": spec.window,
        "kleene_annotations": list(spec.kleene_annotations),
    }


def normalized_query_spec_json(spec: QuerySpec) -> str:
    """canonical (sorted-key, compact) json for hashing and comparison."""
    return json.dumps(query_spec_to_dict(spec), sort_keys=True, separators=(",", ":"))


def query_spec_fingerprint(spec: QuerySpec) -> str:
    """stable sha256 fingerprint of a spec's normalized json."""
    return hashlib.sha256(normalized_query_spec_json(spec).encode("utf-8")).hexdigest()


def load_query_spec_file(path: str | Path) -> QuerySpec:
    """load and validate a QuerySpec from a json file."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return query_spec_from_dict(payload)


def dump_query_spec_file(spec: QuerySpec, path: str | Path) -> None:
    """write a QuerySpec to a pretty-printed json file, creating parent dirs."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(query_spec_to_dict(spec), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def validate_query_spec(spec: QuerySpec, *, allow_kleene: bool = False) -> None:
    """check a spec's internal consistency: unique names, matching pattern, predicate wiring,
    the kleene boundary, and required result/schema columns. raises ValueError on the first breach."""
    variable_names = spec.variable_names
    variable_set = set(variable_names)
    if len(variable_names)!=len(variable_set):
        raise ValueError(f"QuerySpec {spec.name!r} has duplicate variable names")

    pattern = spec.effective_pattern
    if pattern.variables != variable_names:
        raise ValueError(
            f"QuerySpec {spec.name!r} pattern variables must match variable order: "
            f"pattern={pattern.variables!r} variables={variable_names!r}"
        )
    if pattern.gap_quantifiers is not None:
        expected_gaps = len(pattern.variables) - 1
        if len(pattern.gap_quantifiers) != expected_gaps:
            raise ValueError(
                f"QuerySpec {spec.name!r} pattern gap_quantifiers must have one entry per "
                f"gap ({expected_gaps}), got {len(pattern.gap_quantifiers)}"
            )
        invalid_gaps = sorted(set(pattern.gap_quantifiers) - {"*", "*?"})
        if invalid_gaps:
            raise ValueError(
                f"QuerySpec {spec.name!r} pattern gap_quantifiers contain invalid "
                f"quantifier(s) {invalid_gaps} (expected '*' or '*?')"
            )

    independent_by_id = {predicate.predicate_id: predicate for predicate in spec.independent_predicates}
    if len(independent_by_id) != len(spec.independent_predicates):
        raise ValueError(f"QuerySpec {spec.name!r} has duplicate independent predicate ids")

    for variable in spec.variables:
        predicate = independent_by_id.get(variable.independent_predicate_id)
        if predicate is None:
            raise ValueError(
                f"Variable {variable.name!r} references unknown independent predicate "
                f"{variable.independent_predicate_id!r}"
            )
        if predicate.kind != "independent" or predicate.variables != (variable.name,):
            raise ValueError(
                f"Independent predicate {predicate.predicate_id!r} must reference exactly "
                f"variable {variable.name!r}"
            )

    for predicate in spec.independent_predicates:
        if predicate.kind != "independent":
            raise ValueError(f"Predicate {predicate.predicate_id!r} must be kind='independent'")
        if len(predicate.variables) != 1:
            raise ValueError(f"Independent predicate {predicate.predicate_id!r} must reference one variable")
        if predicate.variables[0] not in variable_set:
            raise ValueError(f"Independent predicate {predicate.predicate_id!r} references unknown variable")

    dependent_ids: set[str] = set()
    for predicate in spec.dependent_predicates:
        if predicate.predicate_id in dependent_ids:
            raise ValueError(f"QuerySpec {spec.name!r} has duplicate dependent predicate id {predicate.predicate_id!r}")
        dependent_ids.add(predicate.predicate_id)
        if predicate.kind != "dependent":
            raise ValueError(f"Predicate {predicate.predicate_id!r} must be kind='dependent'")
        if len(predicate.variables) != 2:
            raise ValueError(f"Dependent predicate {predicate.predicate_id!r} must reference two variables")
        unknown = [variable for variable in predicate.variables if variable not in variable_set]
        if unknown:
            raise ValueError(f"Dependent predicate {predicate.predicate_id!r} references unknown variables: {unknown!r}")

    # Per-variable Kleene quantifiers (single-variable independent Kleene).
    valid_quantifiers = {"ONE", "PLUS", "RELUCTANT_PLUS"}
    kleene_variables = set()
    for variable in spec.variables:
        quantifier = getattr(variable, "quantifier", "ONE")
        if quantifier not in valid_quantifiers:
            raise ValueError(
                f"Variable {variable.name!r} has invalid quantifier {quantifier!r} "
                f"(expected one of {sorted(valid_quantifiers)})"
            )
        if quantifier != "ONE":
            kleene_variables.add(variable.name)
    # kleene boundary: a kleene variable carries an INDEPENDENT condition only; no dependent
    # predicate may reference it (dependent-condition kleene is out of scope).
    for predicate in spec.dependent_predicates:
        touched = [variable for variable in predicate.variables if variable in kleene_variables]
        if touched:
            raise ValueError(
                f"Dependent predicate {predicate.predicate_id!r} references Kleene variable(s) {touched!r}; "
                f"dependent-condition Kleene is out of scope"
            )

    if not spec.result_key_columns:
        raise ValueError(f"QuerySpec {spec.name!r} must define result_key_columns")
    measure_aliases = {measure.alias for measure in spec.measures}
    missing_key_columns = [column for column in spec.result_key_columns if column not in measure_aliases]
    if missing_key_columns:
        raise ValueError(
            f"QuerySpec {spec.name!r} result_key_columns must be measure aliases; "
            f"missing={missing_key_columns!r}"
        )

    schema_columns = {column.name for column in spec.event_schema}
    for required in (spec.id_column, spec.time_column, spec.ts_column):
        if required not in schema_columns:
            raise ValueError(f"QuerySpec {spec.name!r} event_schema is missing required column {required!r}")

    if spec.kleene_annotations and not allow_kleene:
        raise ValueError("QuerySpec v1 cost-supported path does not allow Kleene annotations")


_QUANTIFIER_SUFFIX = {"ONE": "", "PLUS": "+", "RELUCTANT_PLUS": "+?"}


def _format_pattern(pattern: PatternSpec, quantifiers: Mapping[str, str] | None = None) -> str:
    """render the PATTERN body: variables (with kleene suffixes) interleaved with wildcard gaps."""
    quantifiers = quantifiers or {}
    pieces: list[str] = []
    for idx, variable in enumerate(pattern.variables):
        if idx:
            gap = (pattern.gap_quantifiers[idx - 1] if pattern.gap_quantifiers is not None
                   else pattern.wildcard_quantifier)
            pieces.append(f"{pattern.wildcard}{gap}")
        suffix = _QUANTIFIER_SUFFIX.get(quantifiers.get(variable, "ONE"), "")
        pieces.append(f"{variable}{suffix}")
    return " ".join(pieces)


def _predicate_source_variable(spec: QuerySpec, predicate: QueryPredicateSpec) -> str:
    """the later of a dependent predicate's two variables owns it in the DEFINE clause."""
    positions = {variable: idx for idx, variable in enumerate(spec.variable_names)}
    return max(predicate.variables, key=lambda variable: positions[variable])


def query_spec_to_match_recognize_sql(spec: QuerySpec, *, events_table: str = "events") -> str:
    """render the spec to a MATCH_RECOGNIZE statement over ,,events_table''."""
    validate_query_spec(spec)
    predicates_by_variable: dict[str, list[str]] = {variable: [] for variable in spec.variable_names}
    independent_by_id = {predicate.predicate_id: predicate for predicate in spec.independent_predicates}

    for variable in spec.variables:
        predicates_by_variable[variable.name].append(independent_by_id[variable.independent_predicate_id].sql_template)

    for predicate in spec.dependent_predicates:
        predicates_by_variable[_predicate_source_variable(spec, predicate)].append(predicate.sql_template)

    wildcard = spec.effective_pattern.wildcard
    order_by = ", ".join(spec.order_by)

    lines = [
        "SELECT *",
        f"FROM {events_table} MATCH_RECOGNIZE (",
    ]
    if spec.partition_by:
        lines.append("    PARTITION BY " + ", ".join(spec.partition_by))
    lines.extend(
        [
            f"    ORDER BY {order_by}",
            "    MEASURES",
        ]
    )
    for idx, measure in enumerate(spec.measures):
        suffix = "," if idx + 1 < len(spec.measures) else ""
        lines.append(f"        {measure.expression} AS {measure.alias}{suffix}")
    lines.extend(
        [
            f"    {spec.row_output}",
            f"    {spec.after_match}",
            f"    PATTERN ({_format_pattern(spec.effective_pattern, {v.name: v.quantifier for v in spec.variables})})",
            "    DEFINE",
        ]
    )

    define_variables = list(spec.variable_names) + [wildcard]
    for idx, variable in enumerate(define_variables):
        suffix = "," if idx + 1 < len(define_variables) else ""
        if variable == wildcard:
            expression = "TRUE"
        else:
            expression = " AND ".join(predicates_by_variable[variable])
        lines.append(f"        {variable} AS {expression}{suffix}")
    lines.append(")")
    return "\n".join(lines) + "\n"


def query_spec_to_dependency_graph(spec: QuerySpec) -> DependencyGraph:
    """render the spec to sql and compile it to a DependencyGraph."""
    result = compile_sql_to_dependency_graph(query_spec_to_match_recognize_sql(spec))
    if not result.ok or result.dependency_graph is None:
        diagnostics = result.diagnostics_to_dict()
        raise ValueError(f"Could not compile QuerySpec {spec.name!r} to dependency graph: {diagnostics}")
    return result.dependency_graph


def query_spec_result_key_columns(spec: QuerySpec) -> tuple[str, ...]:
    """the result key columns (validated)."""
    validate_query_spec(spec)
    return tuple(spec.result_key_columns)


def query_spec_result_column_types(spec: QuerySpec) -> dict[str, str]:
    """map each measure alias to its sql type, defaulting to VARCHAR."""
    validate_query_spec(spec)
    return {measure.alias: (measure.sql_type or "VARCHAR") for measure in spec.measures}
