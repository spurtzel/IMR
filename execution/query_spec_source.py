from __future__ import annotations

"""Shared QuerySpec loading for benchmark CLIs.

Registry presets and external JSON files both return the same QuerySpec object.
External files are not registered globally; their strategy ids are scoped by
the QuerySpec name and fingerprint recorded in artifacts.
"""

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import bootstrap  # noqa: F401

from query_registry import CANONICAL_QUERY_SPEC_NAME, get_query_spec
from eimer.query.query_spec import QuerySpec, load_query_spec_file, query_spec_fingerprint


@dataclass(frozen=True)
class LoadedQuerySpec:
    spec: QuerySpec
    source: str
    fingerprint: str
    path: Path | None = None


def add_query_spec_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--query-spec", default=CANONICAL_QUERY_SPEC_NAME)
    parser.add_argument(
        "--query-spec-file",
        type=Path,
        default=None,
        help="External restricted QuerySpec JSON file. Overrides the registry default.",
    )


def load_query_spec_from_args(args: Any) -> LoadedQuerySpec:
    query_spec_file = getattr(args, "query_spec_file", None)
    query_spec_name = getattr(args, "query_spec", CANONICAL_QUERY_SPEC_NAME)
    if query_spec_file is not None:
        path = Path(query_spec_file)
        spec = load_query_spec_file(path)
        if query_spec_name not in (None, "", CANONICAL_QUERY_SPEC_NAME, spec.name):
            raise ValueError("--query-spec and --query-spec-file cannot name different queries")
        return LoadedQuerySpec(
            spec=spec,
            source="file",
            fingerprint=query_spec_fingerprint(spec),
            path=path,
        )
    spec = get_query_spec(query_spec_name)
    return LoadedQuerySpec(
        spec=spec,
        source="registry",
        fingerprint=query_spec_fingerprint(spec),
        path=None,
    )
