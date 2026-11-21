"""Dialect API: the dialect enum and its resolver, re-exported from `backend.py`. trino is the default
dialect; duckdb is the second dialect.
"""
from __future__ import annotations

from eimer.sql.backend import SqlDialect, resolve

__all__ = ["SqlDialect", "resolve"]
