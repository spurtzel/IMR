from __future__ import annotations

"""Shared selectivity payload loading and workload guard helpers."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from eimer.models import canonical_variable_pair
from eimer.workload import ConstantSelectivity, Selectivities, WindowSelectivity


@dataclass(frozen=True)
class SelectivityPayloadValidation:
    requested_total_events: int
    source_rowcount: int | None
    status: str
    message: str
    config_status: str = "UNKNOWN"
    config_message: str = ""
    payload_config: Mapping[str, Any] | None = None
    query_spec_status: str = "UNKNOWN"
    query_spec_message: str = ""
    query_spec_fingerprint_status: str = "UNKNOWN"
    query_spec_fingerprint_message: str = ""


def load_selectivity_payload(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    return payload


def selectivity_payload_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    workload_config = payload.get("workload_config")
    sources = (payload, workload_config if isinstance(workload_config, Mapping) else {})
    for source in sources:
        for key in (
            "source_events_rowcount",
            "total_events",
            "row_count",
            "workload_profile",
            "workload_seed",
            "initial_history",
            "updates",
            "update_size",
            "batches",
            "sel_r",
            "sel_b",
            "sel_m",
            "rho",
            "events_table",
            "external_manifest_fingerprint",
            "query_spec",
            "query_spec_fingerprint",
        ):
            if key not in metadata and source.get(key) not in (None, ""):
                metadata[key] = source.get(key)
    return metadata


def selectivity_source_rowcount(payload: Mapping[str, Any]) -> int | None:
    metadata = selectivity_payload_metadata(payload)
    for key in ("source_events_rowcount", "total_events", "row_count"):
        value = metadata.get(key)
        if value not in (None, ""):
            return int(value)
    return None


def payload_rho(payload_or_path: Mapping[str, Any] | Path, *, default: float = 1000.0) -> float:
    """Event-time density from the payload; defaults to the generator's 1-event/ms grid."""
    payload = load_selectivity_payload(payload_or_path) if isinstance(payload_or_path, Path) else payload_or_path
    value = selectivity_payload_metadata(payload).get("rho")
    return float(value) if value not in (None, "") else default


def parse_selectivities_payload(payload: Mapping[str, Any], dep_graph) -> Selectivities:
    independent = {str(key): float(value) for key, value in payload.get("independent", {}).items()}
    dependent: dict[tuple[str, str], tuple[ConstantSelectivity | WindowSelectivity, ...]] = {}

    for key, raw_components in payload.get("dependent", {}).items():
        left, right = [item.strip() for item in str(key).split("|", 1)]
        ordered = canonical_variable_pair(left, right, dep_graph.positions)
        components = []
        for raw in raw_components:
            kind = str(raw.get("kind", "")).strip().lower()
            if kind == "constant":
                components.append(ConstantSelectivity(value=float(raw["value"])))
            elif kind == "window":
                components.append(WindowSelectivity(w=float(raw["w"])))
            else:
                raise ValueError(f"Unknown dependent selectivity kind {kind!r} for edge {key!r}")
        dependent[ordered] = tuple(components)

    # A dep edge missing from the payload but carrying an implied window gets the
    # analytic WindowSelectivity (sigma = 2*w*rho/N, w in seconds).
    from eimer.query.temporal_propagation import implied_windows

    implied = implied_windows(dep_graph)
    for edge in dep_graph.edges:
        ordered = canonical_variable_pair(edge.var_left, edge.var_right, dep_graph.positions)
        if ordered in dependent:
            continue
        w_ms = implied.get((ordered[0], ordered[1]))
        if w_ms is not None:
            dependent[ordered] = (WindowSelectivity(w=w_ms / 1000.0),)

    return Selectivities(independent=independent, dependent=dependent)


def load_selectivities(
    path: Path,
    dep_graph,
    *,
    selectivity_mode: str | None = None,
) -> tuple[Selectivities, str]:
    payload = load_selectivity_payload(path)
    mode = selectivity_mode or str(payload.get("mode", "") or "unknown")
    return parse_selectivities_payload(payload, dep_graph), mode


def validate_selectivity_payload_against_workload(
    payload_or_path: Mapping[str, Any] | Path,
    *,
    requested_total_events: int,
    dry_run: bool = False,
    allow_rowcount_mismatch: bool = False,
    expected_config: Mapping[str, Any] | None = None,
    allow_config_mismatch: bool = False,
    expected_query_spec: str | None = None,
    expected_query_spec_fingerprint: str | None = None,
    allow_query_spec_mismatch: bool = False,
) -> SelectivityPayloadValidation:
    payload = load_selectivity_payload(payload_or_path) if isinstance(payload_or_path, Path) else payload_or_path
    metadata = selectivity_payload_metadata(payload)
    source_rowcount = selectivity_source_rowcount(payload)

    if source_rowcount is None:
        status = "UNKNOWN"
        message = "selectivity payload has no source row-count metadata"
    elif source_rowcount == requested_total_events:
        status = "MATCH"
        message = "selectivity row-count metadata matches requested workload"
    else:
        message = (
            f"selectivity payload rowcount {source_rowcount} does not match "
            f"requested total events {requested_total_events}"
        )
        if dry_run:
            status = "MISMATCH_DRY_RUN_WARNING"
        elif allow_rowcount_mismatch:
            status = "MISMATCH_ALLOWED"
        else:
            raise ValueError(message + "; pass --allow-selectivity-rowcount-mismatch to override")

    config_status = "UNKNOWN"
    config_message = "selectivity payload has no workload config metadata"
    payload_config: dict[str, Any] = {}
    if expected_config:
        expected = {key: value for key, value in expected_config.items() if value not in (None, "")}
        payload_config = {key: metadata.get(key) for key in expected}
        known = {key: value for key, value in payload_config.items() if value not in (None, "")}
        if known:
            mismatches = [
                f"{key}: payload={known[key]!r} requested={expected[key]!r}"
                for key in sorted(known)
                if str(known[key]) != str(expected[key])
            ]
            if mismatches:
                config_message = "selectivity payload workload config mismatch: " + "; ".join(mismatches)
                if dry_run:
                    config_status = "MISMATCH_DRY_RUN_WARNING"
                elif allow_config_mismatch:
                    config_status = "MISMATCH_ALLOWED"
                else:
                    raise ValueError(config_message + "; pass --allow-selectivity-config-mismatch to override")
            else:
                missing = sorted(set(expected) - set(known))
                if missing:
                    config_status = "PARTIAL_MATCH"
                    config_message = "selectivity payload workload metadata matches known keys; missing keys: " + ",".join(missing)
                else:
                    config_status = "MATCH"
                    config_message = "selectivity payload workload metadata matches requested workload"

    query_spec_status = "UNKNOWN"
    query_spec_message = "selectivity payload has no query_spec metadata"
    payload_query_spec = metadata.get("query_spec")
    if expected_query_spec and payload_query_spec:
        if str(payload_query_spec) == str(expected_query_spec):
            query_spec_status = "MATCH"
            query_spec_message = "selectivity payload query_spec matches requested query"
        else:
            query_spec_message = (
                f"selectivity payload query_spec mismatch: payload={payload_query_spec!r} "
                f"requested={expected_query_spec!r}"
            )
            if dry_run:
                query_spec_status = "MISMATCH_DRY_RUN_WARNING"
            elif allow_query_spec_mismatch:
                query_spec_status = "MISMATCH_ALLOWED"
            else:
                raise ValueError(query_spec_message + "; pass --allow-selectivity-config-mismatch to override")

    query_spec_fingerprint_status = "UNKNOWN"
    query_spec_fingerprint_message = "selectivity payload has no query_spec_fingerprint metadata"
    payload_query_spec_fingerprint = metadata.get("query_spec_fingerprint")
    if expected_query_spec_fingerprint and payload_query_spec_fingerprint:
        if str(payload_query_spec_fingerprint) == str(expected_query_spec_fingerprint):
            query_spec_fingerprint_status = "MATCH"
            query_spec_fingerprint_message = "selectivity payload query_spec_fingerprint matches requested query"
        else:
            query_spec_fingerprint_message = (
                "selectivity payload query_spec_fingerprint mismatch: "
                f"payload={payload_query_spec_fingerprint!r} "
                f"requested={expected_query_spec_fingerprint!r}"
            )
            if dry_run:
                query_spec_fingerprint_status = "MISMATCH_DRY_RUN_WARNING"
            elif allow_query_spec_mismatch:
                query_spec_fingerprint_status = "MISMATCH_ALLOWED"
            else:
                raise ValueError(
                    query_spec_fingerprint_message + "; pass --allow-selectivity-config-mismatch to override"
                )

    return SelectivityPayloadValidation(
        requested_total_events=requested_total_events,
        source_rowcount=source_rowcount,
        status=status,
        message=message,
        config_status=config_status,
        config_message=config_message,
        payload_config=payload_config,
        query_spec_status=query_spec_status,
        query_spec_message=query_spec_message,
        query_spec_fingerprint_status=query_spec_fingerprint_status,
        query_spec_fingerprint_message=query_spec_fingerprint_message,
    )
