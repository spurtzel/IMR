"""Vectorized event-stream generation with the exact EIMER benchmark schema.

Determinism contract: for a fixed (config, seed) and pinned library versions
the output is identical. The RNG draw order is part of that contract:
(1) type permutation, (2) cluster membership, (3) cluster index,
(4) lon uniforms, (5) lat uniforms. Do not reorder.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from workload.data.datagen.config import EVENT_COLUMNS, DatagenConfig
from workload.data.datagen.spatial import SpatialLayout, calibrate, uniform_layout

_MICROS_PER_SECOND = 1_000_000
_NANOS_PER_SECOND = 1_000_000_000


@dataclass(frozen=True)
class GenerationResult:
    frame: pd.DataFrame
    layout: SpatialLayout
    type_counts: dict[str, int]
    rho_mean: float  # realized mean rate, events/second
    rho_eff: float  # row-weighted mean local rate (determines temporal sigma)
    equi_predicted_sigma: float | None = None  # derived P(key_a==key_b), distinct rows


def exact_type_counts(config: DatagenConfig) -> dict[str, int]:
    """Largest-remainder rounding of rate*N per type; remainder is other_type."""
    n = config.total_rows
    raw = [(name, rate * n) for name, rate in config.types]
    floors = {name: int(np.floor(value)) for name, value in raw}
    remainder_order = sorted(raw, key=lambda item: (item[1] - np.floor(item[1]), item[0]), reverse=True)
    leftover = round(sum(value for _, value in raw)) - sum(floors.values())
    # ,,leftover'' extra rows go to the largest fractional parts (ties broken by
    # name for determinism).
    for name, _ in remainder_order[: max(0, leftover)]:
        floors[name] += 1
    assigned = sum(floors.values())
    if assigned > n:
        raise ValueError("type rates allocate more rows than the table holds")
    counts = dict(floors)
    counts[config.other_type] = n - assigned
    return counts


def _type_column(config: DatagenConfig, counts: dict[str, int], rng: np.random.Generator) -> np.ndarray:
    names = [name for name, _ in config.types] + [config.other_type]
    codes = np.repeat(np.arange(len(names), dtype=np.int32), [counts[name] for name in names])
    rng.shuffle(codes)
    return np.asarray(names, dtype=object)[codes]


def _timestamps_micros(config: DatagenConfig) -> tuple[np.ndarray, float, float]:
    """Strictly increasing integer-microsecond offsets plus (rho_mean, rho_eff).

    Spacings are integer nanoseconds (>= 1000 ns each, enforced by config),
    accumulated in int64 and floored to microseconds, so microsecond values
    are strictly increasing with no float accumulation or tie hazard.
    """
    n = config.total_rows
    temporal = config.temporal
    base_gap_ns = round(_NANOS_PER_SECOND / temporal.rho)
    if n * base_gap_ns >= 2**63:
        raise ValueError(
            f"event horizon overflows int64 nanoseconds (total_rows={n} at rho={temporal.rho}/s); "
            "raise rho or lower total_rows"
        )

    if temporal.burst is None:
        gaps_ns = np.full(n, base_gap_ns, dtype=np.int64)
    else:
        burst = temporal.burst
        burst_gap_ns = round(_NANOS_PER_SECOND / (temporal.rho * burst.rate_multiplier))
        off_rows = burst.period_rows - burst.burst_rows
        # Mean-rate constraint over one period: burst_rows*g_burst + off_rows*g_off
        # == period_rows * (1e9 / rho).
        off_gap_ns = round((burst.period_rows * _NANOS_PER_SECOND / temporal.rho - burst.burst_rows * burst_gap_ns) / off_rows)
        if burst_gap_ns < 1000 or off_gap_ns < 1000:
            raise ValueError("burst configuration drives spacing below 1 microsecond")
        in_burst = (np.arange(n, dtype=np.int64) % burst.period_rows) < burst.burst_rows
        gaps_ns = np.where(in_burst, np.int64(burst_gap_ns), np.int64(off_gap_ns))

    offsets_ns = np.cumsum(gaps_ns)  # offset of row i = sum of gaps 0..i (row 0 at one gap past epoch)
    micros = offsets_ns // 1000
    span_seconds = float(offsets_ns[-1] - offsets_ns[0]) / _NANOS_PER_SECOND if n > 1 else 0.0
    rho_mean = (n - 1) / span_seconds if span_seconds > 0 else temporal.rho
    rho_eff = float(np.mean(_NANOS_PER_SECOND / gaps_ns))
    return micros, rho_mean, rho_eff


def _draw_coordinates(layout: SpatialLayout, n: int, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    if layout.cluster_fraction > 0.0:
        member = rng.random(n) < layout.cluster_fraction
        cluster_idx = rng.integers(0, layout.cluster_count, size=n)
    else:
        member = np.zeros(n, dtype=bool)
        cluster_idx = np.zeros(n, dtype=np.int64)

    lon_u = rng.random(n)
    lat_u = rng.random(n)

    lon = layout.lon_origin + lon_u * layout.lon_extent
    lat = layout.lat_origin + lat_u * layout.lat_extent
    if layout.cluster_fraction > 0.0:
        box_lon = np.asarray(layout.cluster_lon_origins)[cluster_idx]
        box_lat = np.asarray(layout.cluster_lat_origins)[cluster_idx]
        lon = np.where(member, box_lon + lon_u * layout.cluster_extent_lon, lon)
        lat = np.where(member, box_lat + lat_u * layout.cluster_extent_lat, lat)
    return lon, lat


def _equi_column(config: DatagenConfig) -> tuple[np.ndarray, float]:
    """Balanced K-valued key (K = round(1/sigma)) on a DEDICATED rng stream: the
    spatial/temporal/type draws are byte-identical with equi on or off. Returns
    (codes 1..K, derived pairwise sigma from the exact balanced counts)."""
    equi = config.equi
    n = config.total_rows
    k = max(1, round(1.0 / equi.sigma))
    codes = np.tile(np.arange(1, k + 1, dtype=np.int64), n // k + 1)[:n]
    rng = np.random.default_rng(np.random.SeedSequence([config.seed, 0xE9B1]))
    rng.shuffle(codes)
    hi, lo = n - (n // k) * k, k - (n - (n // k) * k)          # hi values get ceil(n/k)
    c, f = n // k + 1, n // k
    predicted = (hi * c * (c - 1) + lo * f * (f - 1)) / (n * (n - 1)) if n > 1 else 1.0
    return codes, predicted


def _check_expected_pair_hits(config: DatagenConfig) -> None:
    if config.min_expected_pair_hits <= 0:
        return
    rarest = min(rate for _, rate in config.types)
    n = config.total_rows
    if config.equi is not None:
        expected_e = (rarest * n) ** 2 * config.equi.sigma
        if expected_e < config.min_expected_pair_hits:
            raise ValueError(
                f"expected equi-matching pairs for the rarest type pair is {expected_e:.1f} "
                f"(< {config.min_expected_pair_hits}): the realized equi sigma would be "
                "statistically unresolvable at this (N, rates, sigma)."
            )
    if config.spatial_band is None:
        return
    # Conservative over every ordered type pair INCLUDING self-pairs (type
    # reuse): the rarest self-edge (rate*N)^2 is the binding case.
    expected = (rarest * n) ** 2 * config.spatial_band.sigma
    if expected < config.min_expected_pair_hits:
        raise ValueError(
            f"expected matching pairs for the rarest type pair is {expected:.1f} "
            f"(< {config.min_expected_pair_hits}): the realized sigma would be statistically "
            "unresolvable at this (N, rates, sigma). Increase N/rates/sigma or set "
            "min_expected_pair_hits=0 to override."
        )


def generate_events(config: DatagenConfig) -> GenerationResult:
    """Generate the full event table (columns per EVENT_COLUMNS, ids 1..N)."""
    _check_expected_pair_hits(config)

    if config.spatial_band is not None:
        layout = calibrate(config.spatial, config.spatial_band)
    else:
        layout = uniform_layout(config.spatial)

    n = config.total_rows
    rng = np.random.default_rng(config.seed)

    counts = exact_type_counts(config)
    primary_type = _type_column(config, counts, rng)
    lon, lat = _draw_coordinates(layout, n, rng)

    micros, rho_mean, rho_eff = _timestamps_micros(config)
    epoch = np.datetime64(config.temporal.epoch.replace(" ", "T"), "us")
    time = epoch + micros.astype("timedelta64[us]")

    # Map per distinct type (a handful), not per row.
    etype_by_type = {name: config.etype_for(name) for name in counts}
    etype_column = pd.Series(primary_type).map(etype_by_type).to_numpy(dtype=object)

    frame = pd.DataFrame(
        {
            "id": np.arange(1, n + 1, dtype=np.int64),
            "time": pd.Series(time),
            "ts": pd.Series(time.copy()),
            "primary_type": primary_type,
            "etype": etype_column,
            "lon": lon,
            "lat": lat,
        },
        columns=list(EVENT_COLUMNS),
    )
    equi_predicted = None
    if config.equi is not None:
        codes, equi_predicted = _equi_column(config)
        frame[config.equi.column] = codes
    if not frame["time"].is_monotonic_increasing or frame["time"].duplicated().any():
        raise AssertionError("timestamps are not strictly increasing")
    return GenerationResult(
        frame=frame,
        equi_predicted_sigma=equi_predicted,
        layout=layout,
        type_counts=counts,
        rho_mean=rho_mean,
        rho_eff=rho_eff,
    )
