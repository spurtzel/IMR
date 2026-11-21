"""`TrinoBackend`: the Trino dialect implementation, the registry target for `backend_for("trino")`."""

from __future__ import annotations

from eimer.sql.backend import SqlBackend


class TrinoBackend(SqlBackend):
    """Trino dialect."""
