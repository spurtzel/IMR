"""Spatial mixture model and band-selectivity calibration.

Model: coordinates are i.i.d. across rows,
drawn with probability (1-c) uniform on the origin-anchored domain
[lon0, lon0 + s*Dx] x [lat0, lat0 + s*Dy], and with probability c uniform on
one of K equal-weight boxes of side (dx, dy) laid out on a grid whose per-axis
pitch exceeds (w_axis + d_axis), interior to [w, sD - w - d] per axis.

For the band |dlon| <= wx AND |dlat| <= wy between two i.i.d. draws:

    sigma(c) = A(1-c)^2 + 2Bc(1-c) + Cc^2
    A = U(wx; sDx) * U(wy; sDy)
    B = (2wx/(sDx)) * (2wy/(sDy))
    C = (1/K) * U(wx; dx) * U(wy; dy)

with the CAPPED axis-overlap probability U(w; L) = 1 - max(0, 1 - w/L)^2
(saturates at 1 for w >= L; the uncapped parabola is wrong for tight clusters).
The cross-cluster term is zero by the grid layout's per-axis edge-gap
separation. sigma(c) is a quadratic and NOT monotone in general; calibration
solves it analytically and takes the smallest root in [0, 1].
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass

from workload.data.datagen.config import BandTarget, SpatialConfig

# Multiplicative slack applied to the minimal grid pitch / feasibility scale so
# that separation and interiority hold strictly, not at a measure-zero boundary.
_GEOMETRY_SLACK = 1.0 + 1e-9
# Absolute tolerance on sigma targets: |sigma(c) - sigma*| below this counts as hit.
_SIGMA_TOLERANCE = 1e-12


def axis_overlap_probability(w: float, length: float) -> float:
    """P(|X - Y| <= w) for X, Y i.i.d. uniform on an interval of the given
    length: 1 - (1 - w/L)^2, capped at 1 for w >= L."""
    if w <= 0:
        raise ValueError("band half-width must be > 0")
    if length <= 0:
        raise ValueError("interval length must be > 0")
    return 1.0 - max(0.0, 1.0 - w / length) ** 2


@dataclass(frozen=True)
class SpatialLayout:
    """A realized spatial mixture: everything the generator needs to draw
    coordinates and the manifest needs to record."""

    lon_origin: float
    lat_origin: float
    lon_extent: float  # scaled extent s * Dx
    lat_extent: float
    domain_scale: float
    cluster_fraction: float  # c
    cluster_count: int  # K actually used; 0 when cluster_fraction == 0
    cluster_extent_lon: float
    cluster_extent_lat: float
    cluster_lon_origins: tuple[float, ...]
    cluster_lat_origins: tuple[float, ...]
    target_sigma: float | None
    predicted_sigma: float | None


def uniform_layout(config: SpatialConfig, *, domain_scale: float = 1.0) -> SpatialLayout:
    return SpatialLayout(
        lon_origin=config.lon_origin,
        lat_origin=config.lat_origin,
        lon_extent=config.lon_extent * domain_scale,
        lat_extent=config.lat_extent * domain_scale,
        domain_scale=domain_scale,
        cluster_fraction=0.0,
        cluster_count=0,
        cluster_extent_lon=config.cluster_extent_lon,
        cluster_extent_lat=config.cluster_extent_lat,
        cluster_lon_origins=(),
        cluster_lat_origins=(),
        target_sigma=None,
        predicted_sigma=None,
    )


def sigma_components(
    config: SpatialConfig,
    band: BandTarget,
    *,
    domain_scale: float,
    cluster_count: int,
) -> tuple[float, float, float]:
    """(A, B, C) of sigma(c) at the given scale and cluster count."""
    wx, wy = band.half_width_lon, band.half_width_lat
    sdx = config.lon_extent * domain_scale
    sdy = config.lat_extent * domain_scale
    a = axis_overlap_probability(wx, sdx) * axis_overlap_probability(wy, sdy)
    b = (2.0 * wx / sdx) * (2.0 * wy / sdy)
    c_term = (
        axis_overlap_probability(wx, config.cluster_extent_lon)
        * axis_overlap_probability(wy, config.cluster_extent_lat)
        / cluster_count
    )
    return a, b, c_term


def predict_sigma(layout: SpatialLayout, band: BandTarget) -> float:
    """Closed-form sigma of the band under this layout."""
    wx, wy = band.half_width_lon, band.half_width_lat
    a = axis_overlap_probability(wx, layout.lon_extent) * axis_overlap_probability(wy, layout.lat_extent)
    c = layout.cluster_fraction
    if c == 0.0:
        return a
    b = (2.0 * wx / layout.lon_extent) * (2.0 * wy / layout.lat_extent)
    c_term = (
        axis_overlap_probability(wx, layout.cluster_extent_lon)
        * axis_overlap_probability(wy, layout.cluster_extent_lat)
        / layout.cluster_count
    )
    return a * (1.0 - c) ** 2 + 2.0 * b * c * (1.0 - c) + c_term * c**2


def _sigma_max(a: float, b: float, c_term: float) -> float:
    """Maximum of sigma(c) over c in [0, 1]: the endpoint sigma(1) = C, or the
    interior peak A + (B-A)^2/(2B - A - C) when sigma is concave with the
    vertex inside (0, 1)."""
    best = max(a, c_term)
    denom = 2.0 * b - a - c_term
    if denom > 0.0:
        c_star = (b - a) / denom
        if 0.0 < c_star < 1.0:
            best = max(best, a + (b - a) ** 2 / denom)
    return best


def _solve_cluster_fraction(a: float, b: float, c_term: float, sigma_target: float) -> float | None:
    """Smallest c in [0, 1] with sigma(c) = sigma_target, or None.

    sigma(c) - sigma* = (A - 2B + C) c^2 + 2(B - A) c + (A - sigma*).
    Uses the citardauq pairing (q = -(lin + sign(lin)*sqrt(disc))/2; roots q/quad
    and const/q): lin = 2(B-A) > 0 always, so the naive (-lin + sqrt)/2quad form
    cancels catastrophically when |quad| is small.
    """
    quad = a - 2.0 * b + c_term
    lin = 2.0 * (b - a)
    const = a - sigma_target

    roots: list[float] = []
    if abs(quad) < 1e-12 * max(abs(lin), 1.0):
        if lin != 0.0:
            roots.append(-const / lin)
    else:
        disc = lin * lin - 4.0 * quad * const
        if disc < 0.0:
            # Targets at the admitted ceiling can round disc tiny-negative;
            # the caller has already checked sigma_target <= sigma_max.
            if disc < -1e-9 * lin * lin:
                return None
            disc = 0.0
        q = -(lin + math.copysign(math.sqrt(disc), lin)) / 2.0
        roots.append(q / quad)
        if q != 0.0:
            roots.append(const / q)

    candidates = sorted(r for r in roots if -1e-12 <= r <= 1.0 + 1e-12)
    if not candidates:
        return None
    return min(max(candidates[0], 0.0), 1.0)


def _grid_layout(
    config: SpatialConfig,
    band: BandTarget,
    *,
    cluster_count: int,
) -> tuple[float, list[float], list[float]]:
    """Minimal feasible domain scale and cluster box origins for a grid of
    cluster_count boxes, with per-axis pitch > (w + d) and boxes interior to
    [w, sD - w - d] per axis.

    Any two distinct grid cells differ in at least one grid coordinate, so they
    are edge-gap separated by > w in that axis: the per-pair OR-over-axes
    separation the zero cross-cluster term requires.

    Returns (scale, lon_origins, lat_origins).
    """
    wx, wy = band.half_width_lon, band.half_width_lat
    dx, dy = config.cluster_extent_lon, config.cluster_extent_lat

    best: tuple[float, int, int] | None = None
    for gx in range(1, cluster_count + 1):
        gy = -(-cluster_count // gx)  # ceil
        pitch_x = (wx + dx) * _GEOMETRY_SLACK
        pitch_y = (wy + dy) * _GEOMETRY_SLACK
        span_x = (gx - 1) * pitch_x + dx
        span_y = (gy - 1) * pitch_y + dy
        scale_x = (span_x + 2.0 * wx) / config.lon_extent
        scale_y = (span_y + 2.0 * wy) / config.lat_extent
        scale = max(1.0, scale_x, scale_y) * _GEOMETRY_SLACK
        if best is None or scale < best[0]:
            best = (scale, gx, gy)
    assert best is not None
    scale, gx, gy = best

    sdx = config.lon_extent * scale
    sdy = config.lat_extent * scale
    # Spread the grid evenly over the interior [w, sD - w - d]; the resulting
    # pitch is >= the minimal one by construction of ,,scale''.
    lon_starts = _spread(config.lon_origin + wx, config.lon_origin + sdx - wx - dx, gx)
    lat_starts = _spread(config.lat_origin + wy, config.lat_origin + sdy - wy - dy, gy)

    lon_origins: list[float] = []
    lat_origins: list[float] = []
    for k in range(cluster_count):
        lon_origins.append(lon_starts[k % gx])
        lat_origins.append(lat_starts[k // gx])
    return scale, lon_origins, lat_origins


def _spread(lo: float, hi: float, count: int) -> list[float]:
    if count == 1:
        return [(lo + hi) / 2.0]
    step = (hi - lo) / (count - 1)
    return [lo + i * step for i in range(count)]


def _validate_separation(
    config: SpatialConfig,
    band: BandTarget,
    lon_origins: list[float],
    lat_origins: list[float],
) -> None:
    """Defense-in-depth: every box pair must have an edge-to-edge gap > w in at
    least one axis (otherwise the cross-cluster term is not zero)."""
    wx, wy = band.half_width_lon, band.half_width_lat
    dx, dy = config.cluster_extent_lon, config.cluster_extent_lat
    n = len(lon_origins)
    for i in range(n):
        for j in range(i + 1, n):
            gap_x = abs(lon_origins[i] - lon_origins[j]) - dx
            gap_y = abs(lat_origins[i] - lat_origins[j]) - dy
            if gap_x <= wx and gap_y <= wy:
                raise AssertionError(
                    f"cluster boxes {i} and {j} are not band-separated in any axis "
                    f"(gaps {gap_x:.6g}/{gap_y:.6g} vs half-widths {wx}/{wy})"
                )


def _solve_domain_scale(config: SpatialConfig, band: BandTarget) -> float:
    """Scale s >= 1 with A(s) = sigma target; A(s) is strictly decreasing."""
    target = band.sigma

    def a_of(scale: float) -> float:
        return axis_overlap_probability(band.half_width_lon, config.lon_extent * scale) * axis_overlap_probability(
            band.half_width_lat, config.lat_extent * scale
        )

    lo, hi = 1.0, 2.0
    while a_of(hi) > target:
        hi *= 2.0
        if hi > 1e12:
            raise ValueError(f"sigma target {target} is below the numerically reachable range")
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if a_of(mid) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def calibrate(config: SpatialConfig, band: BandTarget) -> SpatialLayout:
    """Layout realizing the band's sigma target:

    1. sigma* == A(1): pure uniform.
    2. sigma* <  A(1): uniform with the domain scaled up (origin-anchored).
    3. sigma* >  A(1): clusters, for K from the configured count down to 1,
       find the minimal feasible scale and solve the quadratic for c; smaller K
       reaches higher sigma (K=1 with d <= w approaches 1).

    Raises ValueError with the achievable ranges if no K works.
    """
    sigma_uniform = axis_overlap_probability(band.half_width_lon, config.lon_extent) * axis_overlap_probability(
        band.half_width_lat, config.lat_extent
    )

    if abs(band.sigma - sigma_uniform) <= _SIGMA_TOLERANCE:
        layout = uniform_layout(config)
        return _with_prediction(layout, band)

    if band.sigma < sigma_uniform:
        scale = _solve_domain_scale(config, band)
        layout = uniform_layout(config, domain_scale=scale)
        return _with_prediction(layout, band)

    attempts: list[str] = []
    for cluster_count in range(config.cluster_count, 0, -1):
        scale, lon_origins, lat_origins = _grid_layout(config, band, cluster_count=cluster_count)
        a, b, c_term = sigma_components(config, band, domain_scale=scale, cluster_count=cluster_count)
        sigma_ceiling = _sigma_max(a, b, c_term)
        if band.sigma > sigma_ceiling + _SIGMA_TOLERANCE:
            attempts.append(f"K={cluster_count}: max sigma {sigma_ceiling:.6g} at scale {scale:.6g}")
            continue
        fraction = _solve_cluster_fraction(a, b, c_term, band.sigma)
        if fraction is None:
            attempts.append(f"K={cluster_count}: no root in [0,1] (A={a:.6g}, B={b:.6g}, C={c_term:.6g})")
            continue
        _validate_separation(config, band, lon_origins, lat_origins)
        layout = SpatialLayout(
            lon_origin=config.lon_origin,
            lat_origin=config.lat_origin,
            lon_extent=config.lon_extent * scale,
            lat_extent=config.lat_extent * scale,
            domain_scale=scale,
            cluster_fraction=fraction,
            cluster_count=cluster_count,
            cluster_extent_lon=config.cluster_extent_lon,
            cluster_extent_lat=config.cluster_extent_lat,
            cluster_lon_origins=tuple(lon_origins),
            cluster_lat_origins=tuple(lat_origins),
            target_sigma=band.sigma,
            predicted_sigma=None,
        )
        return _with_prediction(layout, band)

    raise ValueError(
        f"band sigma target {band.sigma} is unreachable for half-widths "
        f"({band.half_width_lon}, {band.half_width_lat}): uniform sigma {sigma_uniform:.6g}; "
        + "; ".join(attempts)
    )


def _with_prediction(layout: SpatialLayout, band: BandTarget) -> SpatialLayout:
    predicted = predict_sigma(layout, band)
    if abs(predicted - band.sigma) > 1e-6 * max(band.sigma, 1e-12):
        raise AssertionError(
            f"calibration self-check failed: predicted sigma {predicted!r} vs target {band.sigma!r}"
        )
    return dataclasses.replace(layout, target_sigma=band.sigma, predicted_sigma=predicted)
