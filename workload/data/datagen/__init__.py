"""External dataset generator for the EIMER benchmark.

Generates the event stream as per-batch parquet files plus a manifest, with
exact independent type rates and calibrated dependent (band) selectivities
under the mode_c pair-count semantics.

Requires numpy/pandas/pyarrow: kept outside eimer, whose runtime stays
dependency-free.
"""

from workload.data.datagen.config import (
    BandTarget,
    EquiTarget,
    BurstConfig,
    DatagenConfig,
    SpatialConfig,
    TemporalConfig,
)
from workload.data.datagen.generator import generate_events
from workload.data.datagen.verification import verify_dataframe
from workload.data.datagen.writer import load_manifest, write_dataset

__all__ = [
    "BandTarget",
    "EquiTarget",
    "BurstConfig",
    "DatagenConfig",
    "SpatialConfig",
    "TemporalConfig",
    "generate_events",
    "verify_dataframe",
    "write_dataset",
    "load_manifest",
]
