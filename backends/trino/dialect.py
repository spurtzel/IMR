"""TrinoBackend: the Trino dialect implementation.

The base class ,,eimer.sql.backend.SqlBackend'' already emits Trino SQL; this
subclass is the named registry target for ,,backend_for("trino")''.
"""

from __future__ import annotations

from eimer.sql.backend import SqlBackend


class TrinoBackend(SqlBackend):
    """Alias of the base literals: named for registry symmetry."""
