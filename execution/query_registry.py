from __future__ import annotations

"""Benchmark query registry backed by generic QuerySpec objects.

Registers one query, ``canonical_r_b_m``. Add further queries as QuerySpec
instances, not as separate hardcoded benchmark paths.
"""

import bootstrap  # noqa: F401

from eimer.query.query_spec import QuerySpec, validate_query_spec

from benchmark_types import CanonicalDependentConfig
from canonical_query import canonical_query_spec


CANONICAL_QUERY_SPEC_NAME = "canonical_r_b_m"


def get_query_spec(name: str = CANONICAL_QUERY_SPEC_NAME) -> QuerySpec:
    normalized = (name or CANONICAL_QUERY_SPEC_NAME).strip().lower()
    aliases = {
        CANONICAL_QUERY_SPEC_NAME,
        "canonical",
        "canonical_non_kleene",
        "go_to",
        "r_b_m",
    }
    if normalized in aliases:
        spec = canonical_query_spec(CanonicalDependentConfig())
        validate_query_spec(spec)
        return spec
    known = ", ".join(list_query_specs())
    raise ValueError(f"unknown query spec {name!r}; known query specs: {known}")


def list_query_specs() -> tuple[str, ...]:
    return (CANONICAL_QUERY_SPEC_NAME,)

