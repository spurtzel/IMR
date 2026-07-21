from __future__ import annotations

OP_ID_MARKER = "-- op_id:"
PRE_BATCH_MARKER = "-- validation: pre_batch_probes"


def prepend_op_id_marker(sql: str, op_id: str) -> str:
    return f"{OP_ID_MARKER} {op_id}\n{sql}"


def extract_op_id(comment_line: str) -> str | None:
    stripped = comment_line.strip()
    if not stripped.startswith(OP_ID_MARKER):
        return None
    value = stripped[len(OP_ID_MARKER) :].strip()
    return value or None


def extract_validation_event(comment_line: str) -> str | None:
    stripped = comment_line.strip()
    if stripped == PRE_BATCH_MARKER:
        return "pre_batch_probes"
    return None
