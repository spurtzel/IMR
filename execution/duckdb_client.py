"""DuckDB execution client with the same surface as ,,trino_client.TrinoClient'':
,,execute(sql) -> (column_names, rows)'', a connection config, a client error type.

Measurement discipline (the demo arms pin it via ,,duckdb_exec.connect''): DuckDB has
session settings for both fairness controls the paper applies, ,,SET threads = 1''
(no intra-query parallelism) and ,,SET disabled_optimizers = 'join_order''' (the
emitted join order runs verbatim, the equivalent of Trino's
join_reordering_strategy = NONE). DuckDB has no server-side query wall, so the
per-statement wall is enforced client-side: a timer thread calls
,,connection.interrupt()'' and the interrupted statement raises a
,,DuckDBClientError'' that ,,is_dnf'' recognizes. An optional ,,memory_limit'' (SET
memory_limit) makes a statement whose state outgrows the engine fail with DuckDB's
out-of-memory error instead of the process being killed; ,,is_dnf'' recognizes that
error too (a resource DNF, censored like the wall).

The Python package is a pinned requirement (duckdb==1.5.5).
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass

import bootstrap  # noqa: F401

from eimer.sql.backend import SqlDialect


class DuckDBClientError(RuntimeError):
    pass


_DNF_MARKER = "EIMER_WALL_EXCEEDED"
_PLANNER_DEFECT = "Failed to bind column reference"


@dataclass(frozen=True)
class DuckDBConnectionConfig:
    database: str = ":memory:"   # a file path, or :memory:
    threads: int = 1
    pin_join_order: bool = True  # SET disabled_optimizers = 'join_order'
    memory_limit: str | None = None  # e.g. '12GB' (SET memory_limit); None = DuckDB's default

    @classmethod
    def from_env(cls, *, database: str | None = None, threads: int | None = None) -> "DuckDBConnectionConfig":
        return cls(
            database=(database or os.environ.get("DUCKDB_DATABASE", ":memory:")).strip(),
            threads=int(threads or os.environ.get("DUCKDB_THREADS", "1")),
        )


class DuckDBClient:
    dialect = SqlDialect.DUCKDB   # the SQL the client executes (read by the statistics gatherers)

    def __init__(self, config: DuckDBConnectionConfig) -> None:
        import duckdb  # the pinned runtime dependency; imported here so trino-only runs never need it

        self.config = config
        self._conn = duckdb.connect(config.database)
        self._conn.execute(f"SET threads = {int(config.threads)}")
        if config.pin_join_order:
            self._conn.execute("SET disabled_optimizers = 'join_order'")
        if config.memory_limit:
            self._conn.execute(f"SET memory_limit = '{config.memory_limit}'")
        self._wall_seconds: float | None = None
        self.planner_fallbacks = 0

    # --- session discipline -------------------------------------------------------
    def set_wall(self, seconds: float | None) -> None:
        """per-statement wall (seconds); None disables it."""
        self._wall_seconds = float(seconds) if seconds else None

    def settings(self) -> dict[str, str]:
        """the effective session settings, for records (threads, disabled optimizers, memory limit)."""
        out = {}
        for key in ("threads", "disabled_optimizers", "memory_limit"):
            out[key] = str(self._conn.execute(f"SELECT current_setting('{key}')").fetchone()[0])
        return out

    # --- execution ----------------------------------------------------------------
    def execute(self, sql: str) -> tuple[list[str], list[list]]:
        timer = None
        fired = [False]
        if self._wall_seconds:
            def _kill():
                fired[0] = True
                self._conn.interrupt()
            timer = threading.Timer(self._wall_seconds, _kill)
            timer.daemon = True
            timer.start()
        try:
            try:
                columns, rows = self._fetch(self._conn.execute(sql))
            except Exception as exc:  # noqa: BLE001
                if fired[0] or not self.config.pin_join_order or _PLANNER_DEFECT not in str(exc):
                    raise
                columns, rows = self._execute_unpinned(sql)
        except Exception as exc:  # noqa: BLE001  (duckdb raises many concrete error classes)
            if fired[0]:
                raise DuckDBClientError(f"{_DNF_MARKER}: statement exceeded the {self._wall_seconds}s wall") from exc
            raise DuckDBClientError(str(exc).splitlines()[0]) from exc
        finally:
            if timer is not None:
                timer.cancel()
        return columns, rows

    @staticmethod
    def _fetch(cur):
        columns = [d[0] for d in (cur.description or [])]
        rows = [list(r) for r in cur.fetchall()] if cur.description else []
        return columns, rows

    def _execute_unpinned(self, sql: str):
        self.planner_fallbacks += 1
        self._conn.execute("SET disabled_optimizers = ''")
        try:
            return self._fetch(self._conn.execute(sql))
        finally:
            self._conn.execute("SET disabled_optimizers = 'join_order'")

    def register_frame(self, name: str, frame) -> None:
        """expose a pandas DataFrame as a relation ,,name'' (zero-copy; bit-exact types)."""
        self._conn.register(name, frame)

    def unregister_frame(self, name: str) -> None:
        self._conn.unregister(name)

    def close(self) -> None:
        self._conn.close()


def is_dnf(err) -> bool:
    """True when the error is the client-side wall or the engine's out-of-memory error
    (a feasibility DNF, not a bug)."""
    text = str(err)
    return _DNF_MARKER in text or "Out of Memory Error" in text
