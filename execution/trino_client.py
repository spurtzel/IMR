from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

import bootstrap  # noqa: F401


class TrinoClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class TrinoConnectionConfig:
    server: str
    catalog: str
    schema: str
    user: str
    source: str = "benchmark_tools"
    timeout_seconds: float = 3600.0

    @classmethod
    def from_env(
        cls,
        *,
        server: str | None = None,
        catalog: str | None = None,
        schema: str | None = None,
        user: str | None = None,
        source: str | None = None,
        timeout_seconds: float | None = None,
    ) -> "TrinoConnectionConfig":
        import os

        return cls(
            server=(server or os.environ.get("TRINO_SERVER", "localhost:8080")).strip(),
            catalog=(catalog or os.environ.get("TRINO_CATALOG", "memory")).strip(),
            schema=(schema or os.environ.get("TRINO_SCHEMA", "default")).strip(),
            user=(user or os.environ.get("TRINO_USER", "trino")).strip(),
            source=(source or os.environ.get("TRINO_HTTP_SOURCE", "benchmark_tools")).strip(),
            timeout_seconds=float(timeout_seconds or os.environ.get("TRINO_HTTP_TIMEOUT_SECONDS", "3600")),
        )


class TrinoClient:
    def __init__(self, config: TrinoConnectionConfig) -> None:
        self.config = config
        if config.server.startswith("http://") or config.server.startswith("https://"):
            self.base_url = config.server.rstrip("/")
        else:
            self.base_url = f"http://{config.server}".rstrip("/")
        self._session: dict[str, str] = {}

    def _headers(self) -> dict[str, str]:
        headers = {
            "X-Trino-User": self.config.user,
            "X-Trino-Catalog": self.config.catalog,
            "X-Trino-Schema": self.config.schema,
            "X-Trino-Source": self.config.source,
        }
        if self._session:
            headers["X-Trino-Session"] = ",".join(f"{key}={value}" for key, value in sorted(self._session.items()))
        return headers

    def _apply_response_headers(self, resp_headers) -> None:
        for raw in resp_headers.get_all("X-Trino-Set-Session", []):
            for item in raw.split(","):
                item = item.strip()
                if not item or "=" not in item:
                    continue
                key, value = item.split("=", 1)
                self._session[key.strip()] = value.strip()
        for raw in resp_headers.get_all("X-Trino-Clear-Session", []):
            for key in raw.split(","):
                key = key.strip()
                if key:
                    self._session.pop(key, None)

    def _request_json(self, *, url: str, method: str, body: str | None = None) -> dict:
        payload = body.encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url=url,
            method=method,
            data=payload,
            headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw_body = response.read().decode("utf-8")
                self._apply_response_headers(response.headers)
                return json.loads(raw_body) if raw_body else {}
        except urllib.error.HTTPError as exc:
            raw_body = exc.read().decode("utf-8", errors="replace")
            self._apply_response_headers(exc.headers)
            if raw_body:
                try:
                    payload_obj = json.loads(raw_body)
                except json.JSONDecodeError as decode_error:
                    raise TrinoClientError(f"Trino HTTP error {exc.code}: {raw_body.strip()}") from decode_error
                raise TrinoClientError(_error_message(payload_obj, fallback=f"Trino HTTP error {exc.code}"))
            raise TrinoClientError(f"Trino HTTP error {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise TrinoClientError(f"Trino transport error: {exc.reason}") from exc
        except TimeoutError as exc:
            raise TrinoClientError("Trino transport error: timeout") from exc

    @staticmethod
    def _extract_columns(payload: dict) -> list[str]:
        raw_columns = payload.get("columns")
        if not isinstance(raw_columns, list):
            return []
        names: list[str] = []
        for index, column in enumerate(raw_columns, start=1):
            if isinstance(column, dict) and column.get("name"):
                names.append(str(column["name"]))
            else:
                names.append(f"col_{index}")
        return names

    @staticmethod
    def _extract_rows(payload: dict) -> list[list]:
        data = payload.get("data")
        if not isinstance(data, list):
            return []
        rows: list[list] = []
        for row in data:
            if isinstance(row, list):
                rows.append(row)
            else:
                rows.append([row])
        return rows

    def execute(self, sql: str) -> tuple[list[str], list[list]]:
        payload = self._request_json(
            url=f"{self.base_url}/v1/statement",
            method="POST",
            body=sql,
        )
        if payload.get("error"):
            raise TrinoClientError(_error_message(payload))

        columns = self._extract_columns(payload)
        rows = self._extract_rows(payload)

        while payload.get("nextUri"):
            payload = self._request_json(url=str(payload["nextUri"]), method="GET")
            if payload.get("error"):
                raise TrinoClientError(_error_message(payload))

            if not columns:
                columns = self._extract_columns(payload)
            rows.extend(self._extract_rows(payload))

        return columns, rows


def _error_message(payload: dict, *, fallback: str = "Trino query failed") -> str:
    error = payload.get("error")
    if not isinstance(error, dict):
        return fallback
    pieces: list[str] = []
    message = error.get("message")
    if message:
        pieces.append(str(message))
    error_name = error.get("errorName")
    if error_name:
        pieces.append(f"errorName={error_name}")
    error_type = error.get("errorType")
    if error_type:
        pieces.append(f"errorType={error_type}")
    return " | ".join(pieces) if pieces else fallback
