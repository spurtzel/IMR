"""shared helpers for eimer query-batch schedules.

repository convention:

- ,,None'' means query after every batch.
- an empty collection means update only, with no query batches.
- explicit batch numbers are 1-based.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Collection


def _validate_batch_count(batch_count: int) -> None:
    """require a positive batch count."""
    if batch_count <= 0:
        raise ValueError("batch_count must be positive")


def _normalize_explicit_batches(query_batches: Collection[int], batch_count: int) -> frozenset[int]:
    """dedup explicit batch numbers to a frozenset, checking each is within [1, batch_count]."""
    _validate_batch_count(batch_count)
    normalized = frozenset(int(batch) for batch in query_batches)
    invalid = sorted(batch for batch in normalized if batch < 1 or batch > batch_count)
    if invalid:
        raise ValueError(
            f"query_batches are 1-based and must be within [1, {batch_count}], got {invalid!r}"
        )
    return normalized


def parse_query_batches(value: str | Collection[int] | None, batch_count: int) -> frozenset[int] | None:
    """Parse a query schedule into the core normalized representation."""

    _validate_batch_count(batch_count)
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"all", "*"}:
            return None
        if normalized in {"", "none", "update-only", "updates-only"}:
            return frozenset()
        result: set[int] = set()
        for item in value.split(","):
            token = item.strip().lower()
            if not token:
                continue
            if token == "last":
                result.add(batch_count)
            else:
                result.add(int(token))
        return _normalize_explicit_batches(result, batch_count)
    return _normalize_explicit_batches(value, batch_count)


def normalize_query_batches(query_batches: str | Collection[int] | None, batch_count: int) -> frozenset[int] | None:
    """Normalize any accepted query schedule representation."""

    return parse_query_batches(query_batches, batch_count)


def validate_query_batches(query_batches: str | Collection[int] | None, batch_count: int) -> None:
    """Raise if ,,query_batches'' is not a valid schedule for ,,batch_count''."""

    normalize_query_batches(query_batches, batch_count)


def format_query_batches(query_batches: Collection[int] | None) -> str:
    """render a schedule back to its string form (all / none / comma-separated batches)."""
    if query_batches is None:
        return "all"
    normalized = frozenset(int(batch) for batch in query_batches)
    if not normalized:
        return "none"
    return ",".join(str(batch) for batch in sorted(normalized))


_EVERY_NTH = {"every2nd": 2, "every4th": 4, "every8th": 8}


def frequency_batches(frequency: str, batch_count: int) -> frozenset[int] | None:
    """map the config query_frequency enum to a normalized schedule (None = every batch).
    the final batch is always included: the cumulative last result is schedule-invariant,
    so the schedule changes only WHEN compose fires, not what the last batch returns."""
    _validate_batch_count(batch_count)
    if frequency == "all":
        return None
    if frequency == "last":
        return frozenset({batch_count})
    step = _EVERY_NTH.get(frequency)
    if step is None:
        raise ValueError(f"unknown query_frequency {frequency!r}")
    return frozenset(b for b in range(1, batch_count + 1) if b % step == 0) | {batch_count}


def query_frequency(query_batches: Collection[int] | None, batch_count: int) -> float:
    """fraction of batches that trigger a query (1.0 when None = every batch)."""
    _validate_batch_count(batch_count)
    if query_batches is None:
        return 1.0
    return len(frozenset(query_batches)) / batch_count
