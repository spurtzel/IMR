"""Dataset persistence: per-batch parquet files + manifest.json.

The content fingerprint covers the logical column values, not file bytes,
since parquet encodings are not stable across library versions.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from workload.data.datagen.config import EVENT_COLUMNS, DatagenConfig
from workload.data.datagen.generator import GenerationResult
from workload.data.datagen.verification import VerificationReport

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_FILENAME = "manifest.json"

_ARROW_SCHEMA = pa.schema(
    [
        ("id", pa.int64()),
        ("time", pa.timestamp("us")),
        ("ts", pa.timestamp("us")),
        ("primary_type", pa.string()),
        ("etype", pa.string()),
        ("lon", pa.float64()),
        ("lat", pa.float64()),
    ]
)


def content_fingerprint(frame: pd.DataFrame) -> str:
    """SHA-256 over canonicalized logical column contents."""
    digest = hashlib.sha256()
    digest.update(frame["id"].to_numpy(dtype=np.int64).tobytes())
    for column in ("time", "ts"):
        digest.update(frame[column].astype("int64").to_numpy().tobytes())
    for column in ("primary_type", "etype"):
        digest.update("\x1f".join(frame[column].astype(str)).encode("utf-8"))
        digest.update(b"\x1e")
    for column in ("lon", "lat"):
        digest.update(frame[column].to_numpy(dtype=np.float64).tobytes())
    return digest.hexdigest()


def _library_versions() -> dict[str, str]:
    return {"numpy": np.__version__, "pandas": pd.__version__, "pyarrow": pa.__version__}


def _as_jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _as_jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, (tuple, list)):
        return [_as_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _as_jsonable(v) for k, v in value.items()}
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, float) and value != value:  # NaN -> null
        return None
    return value


def write_dataset(
    out_dir: Path | str,
    config: DatagenConfig,
    result: GenerationResult,
    report: VerificationReport | None = None,
) -> Path:
    """Write batch_NNNN.parquet files + manifest.json; returns the manifest path."""
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    frame = result.frame
    if list(frame.columns) != list(EVENT_COLUMNS):
        raise ValueError(f"frame columns {list(frame.columns)} != contract {list(EVENT_COLUMNS)}")
    if len(frame) != config.total_rows:
        raise ValueError("frame length does not match config.total_rows")

    batch_files = []
    start = 0
    for index, size in enumerate(config.batch_sizes, start=1):
        batch = frame.iloc[start : start + size]
        start += size
        filename = f"batch_{index:04d}.parquet"
        table = pa.Table.from_pandas(batch, schema=_ARROW_SCHEMA, preserve_index=False)
        pq.write_table(table, out_path / filename)
        batch_files.append({"file": filename, "rows": int(size)})

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "fingerprint": content_fingerprint(frame),
        "total_rows": config.total_rows,
        "batch_sizes": list(config.batch_sizes),
        "batches": batch_files,
        "seed": config.seed,
        "config": _as_jsonable(config),
        "layout": _as_jsonable(result.layout),
        "type_counts": _as_jsonable(result.type_counts),
        "rho_mean": result.rho_mean,
        "rho_eff": result.rho_eff,
        "library_versions": _library_versions(),
        "event_columns": list(EVENT_COLUMNS),
    }
    if report is not None:
        manifest["verification"] = _as_jsonable(report)

    manifest_path = out_path / MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest_path


def load_manifest(manifest_path: Path | str) -> dict[str, Any]:
    path = Path(manifest_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError(f"unsupported manifest schema_version {manifest.get('schema_version')!r}")
    for key in ("fingerprint", "total_rows", "batches"):
        if key not in manifest:
            raise ValueError(f"manifest is missing {key!r}")
    return manifest


def read_dataset_frames(manifest_path: Path | str):
    """Yield (filename, DataFrame) per batch, in manifest order."""
    path = Path(manifest_path)
    manifest = load_manifest(path)
    for entry in manifest["batches"]:
        frame = pq.read_table(path.parent / entry["file"]).to_pandas()
        yield entry["file"], frame
