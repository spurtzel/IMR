"""serialization and stable-key helpers for :class:,,PlanDescriptor''."""

from __future__ import annotations

from typing import Any, Collection, Mapping

from eimer.plans.plan_descriptor import PlanDescriptor
from eimer.query.query_schedule import format_query_batches, normalize_query_batches


_DESCRIPTOR_FIELDS = {
    "kind",
    "descriptor_kind",
    "strategy_id",
    "update_plan_idx",
    "composition_plan_idx",
    "composition_variant_idx",
    "composition_variant_mode",
    "max_join_orders_per_tree",
    "query_batches",
    "query_spec",
    "query_spec_source",
    "query_spec_fingerprint",
    "allowed_query_schedules",
}

_STABLE_METADATA_FIELDS = (
    "generation_method",
    "cover_idx",
    "plan_idx_within_cover",
    "base_tree_idx",
    "join_order_idx",
)


def _row_value(row: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in row:
            return row[name]
    return default


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _parse_query_batches_value(value: Any, *, batch_count: int | None = None) -> frozenset[int] | None:
    """parse a query_batches payload (string, collection, or None) into a normalized frozenset."""
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "all", "*"}:
            return None
        if normalized in {"none", "update-only", "updates-only"}:
            return frozenset()
        if batch_count is None and "last" in {item.strip().lower() for item in value.split(",")}:
            raise ValueError("query_batches containing 'last' requires batch_count")
        if batch_count is not None:
            return normalize_query_batches(value, batch_count)
        return frozenset(int(item.strip()) for item in value.split(",") if item.strip())
    if isinstance(value, Collection):
        normalized = frozenset(int(batch) for batch in value)
        if batch_count is not None:
            return normalize_query_batches(normalized, batch_count)
        return normalized
    raise ValueError(f"unsupported query_batches payload: {value!r}")


def _parse_str_tuple(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        separator = "|" if "|" in value else ","
        return tuple(item.strip() for item in value.split(separator) if item.strip())
    if isinstance(value, Collection):
        return tuple(str(item) for item in value if str(item))
    return (str(value),)


def descriptor_to_json_dict(
    descriptor: PlanDescriptor,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """serialize a descriptor (plus any extra metadata) to a plain json-ready dict."""
    payload = {
        "kind": descriptor.kind,
        "strategy_id": descriptor.strategy_id,
        "update_plan_idx": descriptor.update_plan_idx,
        "composition_plan_idx": descriptor.composition_plan_idx,
        "composition_variant_idx": descriptor.composition_variant_idx,
        "composition_variant_mode": descriptor.composition_variant_mode,
        "max_join_orders_per_tree": descriptor.max_join_orders_per_tree,
        "query_batches": None if descriptor.query_batches is None else sorted(descriptor.query_batches),
        "query_spec": descriptor.query_spec,
        "query_spec_source": descriptor.query_spec_source,
        "query_spec_fingerprint": descriptor.query_spec_fingerprint,
        "allowed_query_schedules": list(descriptor.allowed_query_schedules),
    }
    payload.update(dict(descriptor.metadata))
    if metadata:
        payload.update(dict(metadata))
    return payload


def descriptor_from_json_dict(
    row: Mapping[str, Any],
    *,
    batch_count: int | None = None,
) -> PlanDescriptor:
    """rebuild a ,,PlanDescriptor'' from a json/dict row, keeping unknown keys as metadata."""
    try:
        kind = str(_row_value(row, "kind", "descriptor_kind"))
        strategy_id = str(row["strategy_id"])
        update_plan_idx = int(row["update_plan_idx"])
    except KeyError as exc:
        raise ValueError(f"descriptor row missing required field {exc.args[0]!r}: {row!r}") from exc

    metadata = {key: value for key, value in row.items() if key not in _DESCRIPTOR_FIELDS}
    return PlanDescriptor(
        kind=kind,
        strategy_id=strategy_id,
        update_plan_idx=update_plan_idx,
        composition_plan_idx=_optional_int(_row_value(row, "composition_plan_idx")),
        composition_variant_idx=_optional_int(_row_value(row, "composition_variant_idx")),
        composition_variant_mode=str(row.get("composition_variant_mode") or "canonical"),
        max_join_orders_per_tree=_optional_int(row.get("max_join_orders_per_tree")),
        query_batches=_parse_query_batches_value(row.get("query_batches"), batch_count=batch_count),
        query_spec=(None if row.get("query_spec") in (None, "") else str(row.get("query_spec"))),
        query_spec_source=(
            None if row.get("query_spec_source") in (None, "") else str(row.get("query_spec_source"))
        ),
        query_spec_fingerprint=(
            None if row.get("query_spec_fingerprint") in (None, "") else str(row.get("query_spec_fingerprint"))
        ),
        allowed_query_schedules=_parse_str_tuple(row.get("allowed_query_schedules")),
        metadata=metadata,
    )


def descriptor_to_csv_row(
    descriptor: PlanDescriptor,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """serialize a descriptor to a CSV row, flattening list-valued fields to strings."""
    row = descriptor_to_json_dict(descriptor, metadata=metadata)
    row["query_batches"] = format_query_batches(descriptor.query_batches)
    row["allowed_query_schedules"] = "|".join(descriptor.allowed_query_schedules)
    return row


def descriptor_from_csv_row(
    row: Mapping[str, Any],
    *,
    batch_count: int | None = None,
) -> PlanDescriptor:
    """rebuild a ,,PlanDescriptor'' from a CSV row."""
    return descriptor_from_json_dict(row, batch_count=batch_count)


def _mapping_for_key(descriptor_or_row: PlanDescriptor | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(descriptor_or_row, PlanDescriptor):
        return descriptor_to_json_dict(descriptor_or_row)
    return descriptor_or_row


def _format_key_query_batches(value: Any) -> str:
    if value is None:
        return "all"
    if isinstance(value, str):
        return value
    if isinstance(value, Collection):
        return format_query_batches(frozenset(int(batch) for batch in value))
    return str(value)


def stable_descriptor_key(
    descriptor_or_row: PlanDescriptor | Mapping[str, Any],
    *,
    include_ranking_profile: bool = False,
) -> tuple[str, ...]:
    """a deterministic identity tuple for a descriptor, used for de-duplication and lookups."""
    row = _mapping_for_key(descriptor_or_row)
    fields = [
        str(_row_value(row, "kind", "descriptor_kind", default="") or ""),
        str(row.get("strategy_id", "") or ""),
        str(row.get("update_plan_idx", "") or ""),
        str(row.get("composition_plan_idx", "") or ""),
        str(row.get("composition_variant_idx", "") or ""),
        str(row.get("composition_variant_mode", "") or ""),
        str(row.get("max_join_orders_per_tree", "") or ""),
        str(row.get("generation_method", "") or ""),
        str(row.get("cover_idx", "") or ""),
        str(row.get("plan_idx_within_cover", "") or ""),
        str(row.get("base_tree_idx", "") or ""),
        str(row.get("join_order_idx", "") or ""),
        _format_key_query_batches(row.get("query_batches")),
        str(row.get("query_spec", "") or ""),
        str(row.get("query_spec_fingerprint", "") or ""),
    ]
    if include_ranking_profile:
        fields.append(str(row.get("ranking_profile_name", "") or ""))
    return tuple(fields)


def descriptor_metadata_match(
    expected: PlanDescriptor | Mapping[str, Any],
    observed: PlanDescriptor | Mapping[str, Any],
    *,
    include_ranking_profile: bool = False,
) -> tuple[bool, tuple[str, ...]]:
    """compare two descriptors field by field; returns (match, names of mismatched fields)."""
    expected_row = _mapping_for_key(expected)
    observed_row = _mapping_for_key(observed)
    fields = (
        "kind",
        "descriptor_kind",
        "strategy_id",
        "update_plan_idx",
        "composition_plan_idx",
        "composition_variant_idx",
        "composition_variant_mode",
        "max_join_orders_per_tree",
        "query_batches",
        "query_spec",
        "query_spec_fingerprint",
        "allowed_query_schedules",
        *_STABLE_METADATA_FIELDS,
    )
    if include_ranking_profile:
        fields = (*fields, "ranking_profile_name")

    mismatches: list[str] = []
    for field in fields:
        if field not in expected_row and field not in observed_row:
            continue
        if field == "query_batches":
            left = _format_key_query_batches(expected_row.get(field))
            right = _format_key_query_batches(observed_row.get(field))
        elif field == "allowed_query_schedules":
            left = "|".join(_parse_str_tuple(expected_row.get(field)))
            right = "|".join(_parse_str_tuple(observed_row.get(field)))
        else:
            left = str(expected_row.get(field, "") or "")
            right = str(observed_row.get(field, "") or "")
        if left != right:
            mismatches.append(field)
    return not mismatches, tuple(mismatches)
