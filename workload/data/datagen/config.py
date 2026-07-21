"""Configuration for the external dataset generator.

All knobs are validated eagerly at construction; a config that constructs is a
config the generator can realize (calibration feasibility is checked separately
in spatial.calibrate, which raises with the achievable range on miss).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The benchmark event-schema contract (canonical_query.py required_event_schema):
# generated tables must provide exactly these columns, in this order.
EVENT_COLUMNS = ("id", "time", "ts", "primary_type", "etype", "lon", "lat")

# Mirrors sql_generator.py's etype CASE: the three canonical query types map to
# R/B/M, everything else to Z.
CANONICAL_ETYPE_MAPPING = {
    "ROBBERY": "R",
    "BATTERY": "B",
    "MOTOR VEHICLE THEFT": "M",
}
DEFAULT_ETYPE_TAG = "Z"

# TIMESTAMP(6) resolution: timestamps are strictly increasing on a microsecond
# grid, so mean rates above 1e6 events/second cannot be represented tie-free.
MAX_RHO_EVENTS_PER_SECOND = 1_000_000.0

DEFAULT_EPOCH = "2020-01-01 00:00:00"


@dataclass(frozen=True)
class SpatialConfig:
    """Coordinate-mixture model family: uniform background over the (possibly
    scaled) domain plus up to cluster_count equal-weight axis-aligned boxes.

    The realized layout (cluster fraction c, domain scale s, cluster count and
    positions) is produced by spatial.calibrate() from a BandTarget; this
    config only fixes the family. Scaling is origin-anchored: lon/lat origins
    never move, so absolute coordinate literals in specs (lon stripes) keep
    their meaning.
    """

    lon_origin: float = -87.90
    lat_origin: float = 41.60
    lon_extent: float = 0.20
    lat_extent: float = 0.20
    cluster_count: int = 4
    cluster_extent_lon: float = 0.010
    cluster_extent_lat: float = 0.010

    def __post_init__(self) -> None:
        if self.lon_extent <= 0 or self.lat_extent <= 0:
            raise ValueError("spatial extents must be > 0")
        if self.cluster_count < 1:
            raise ValueError("cluster_count must be >= 1")
        if self.cluster_extent_lon <= 0 or self.cluster_extent_lat <= 0:
            raise ValueError("cluster extents must be > 0")
        if self.cluster_extent_lon >= self.lon_extent or self.cluster_extent_lat >= self.lat_extent:
            raise ValueError("cluster extents must be smaller than the domain extents")


@dataclass(frozen=True)
class BandTarget:
    """A dependent spatial-band selectivity target.

    half_width_lon/lat are the query-side band half-widths (the w in
    "B.lon BETWEEN A.lon - w AND A.lon + w"); sigma is the target mode_c
    selectivity P(|dlon| <= w_lon AND |dlat| <= w_lat) for two i.i.d. rows.
    """

    sigma: float
    half_width_lon: float
    half_width_lat: float

    def __post_init__(self) -> None:
        if not 0.0 < self.sigma <= 1.0:
            raise ValueError("band sigma target must be in (0, 1]")
        if self.half_width_lon <= 0 or self.half_width_lat <= 0:
            raise ValueError("band half-widths must be > 0")


@dataclass(frozen=True)
class BurstConfig:
    """Periodic burst structure: within each period of period_rows rows, the
    first burst_rows rows are spaced rate_multiplier times tighter than the
    base grid; the remaining rows are spaced wider so the mean rate stays at
    TemporalConfig.rho."""

    burst_rows: int
    period_rows: int
    rate_multiplier: float

    def __post_init__(self) -> None:
        if self.period_rows < 2:
            raise ValueError("period_rows must be >= 2")
        if not 0 < self.burst_rows < self.period_rows:
            raise ValueError("burst_rows must be in [1, period_rows - 1]")
        if self.rate_multiplier <= 1.0:
            raise ValueError("rate_multiplier must be > 1")


@dataclass(frozen=True)
class EquiTarget:
    """Calibration-free equi-predicate selectivity: a categorical key balanced
    over K = round(1/sigma) values, shuffled on a dedicated rng stream so it is
    independent of the spatial clusters (joint band-and-equi selectivity is then
    the product of the marginals). sigma is realized exactly iff 1/sigma is an
    integer, else as 1/round(1/sigma), recorded in
    GenerationResult.equi_predicted_sigma."""

    sigma: float
    column: str = "ekey"

    def __post_init__(self) -> None:
        if not 0.0 < self.sigma <= 1.0:
            raise ValueError("equi sigma must be in (0, 1]")
        if not self.column.isidentifier():
            raise ValueError(f"equi column name {self.column!r} must be an identifier")


@dataclass(frozen=True)
class TemporalConfig:
    """Event-time structure: strictly increasing timestamps at mean rate rho
    (events/second) from the epoch. rho=1000 reproduces the 1-ms grid."""

    rho: float = 1000.0
    burst: BurstConfig | None = None
    epoch: str = DEFAULT_EPOCH

    def __post_init__(self) -> None:
        if self.rho <= 0:
            raise ValueError("rho must be > 0")
        effective_peak = self.rho * (self.burst.rate_multiplier if self.burst else 1.0)
        if effective_peak > MAX_RHO_EVENTS_PER_SECOND:
            raise ValueError(
                "peak event rate exceeds 1e6/s: TIMESTAMP(6) cannot represent "
                "tie-free sub-microsecond spacing (timestamp-tie hazard)"
            )
        if self.burst is not None:
            # Off-phase spacing must stay representable (>= 1 microsecond).
            burst = self.burst
            off_rows = burst.period_rows - burst.burst_rows
            off_spacing = (burst.period_rows - burst.burst_rows / burst.rate_multiplier) / (
                self.rho * off_rows
            )
            if off_spacing < 1e-6:
                raise ValueError("burst configuration drives off-phase spacing below 1 microsecond")


@dataclass(frozen=True)
class DatagenConfig:
    """Top-level generator configuration.

    types: ordered (type_name, rate) pairs; rates are exact global row
    fractions (largest-remainder rounding), the remaining mass is other_type.
    """

    types: tuple[tuple[str, float], ...]
    batch_sizes: tuple[int, ...]
    seed: int
    spatial: SpatialConfig = field(default_factory=SpatialConfig)
    temporal: TemporalConfig = field(default_factory=TemporalConfig)
    spatial_band: BandTarget | None = None
    equi: EquiTarget | None = None
    etype_mapping: tuple[tuple[str, str], ...] = tuple(CANONICAL_ETYPE_MAPPING.items())
    other_type: str = "OTHER"
    # Per edge, n_u * n_v * sigma expected matching pairs below this floor make
    # the realized sigma statistically unresolvable (it can be exactly 0, which
    # the workload contract rejects). Checked against the two smallest
    # configured rates at generation time; 0 disables.
    min_expected_pair_hits: int = 100

    def __post_init__(self) -> None:
        if not self.types:
            raise ValueError("at least one event type is required")
        names = [name for name, _ in self.types]
        if len(set(names)) != len(names):
            raise ValueError("duplicate type names")
        if self.other_type in names:
            raise ValueError(f"other_type {self.other_type!r} collides with a configured type")
        for name, rate in self.types:
            if not name:
                raise ValueError("type names must be non-empty")
            if not 0.0 < rate <= 1.0:
                raise ValueError(f"rate for type {name!r} must be in (0, 1]")
        if sum(rate for _, rate in self.types) > 1.0 + 1e-12:
            raise ValueError("type rates must sum to <= 1")
        if not self.batch_sizes:
            raise ValueError("at least one batch is required")
        if any(size <= 0 for size in self.batch_sizes):
            raise ValueError("batch sizes must be > 0")

    @property
    def total_rows(self) -> int:
        return sum(self.batch_sizes)

    def etype_for(self, type_name: str) -> str:
        for name, tag in self.etype_mapping:
            if name == type_name:
                return tag
        return DEFAULT_ETYPE_TAG
