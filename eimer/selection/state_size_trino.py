"""calibrated byte-size model for a cover's persistent state on trino/iceberg.

pass ,,peak_state_bytes_trino'' as ,,peak_bytes_fn'' to ,,select_clique_grow_plan'' to
enforce the storage budget M. parquet is columnar and compressed, so bytes/row are
calibrated on materialized views rather than derived from the storage format:

    bytes(view) = n_files * (F0 + FC * n_columns) + rows * SAFETY * sum_col bytes_per_row(col)

views are INSERT-grown per batch and never truncated, so all views are cumulative at the
final batch with one parquet file per batch. Kleene caches are unpriced; constants are
pinned to a trino/iceberg/minio snappy-parquet stack and must be refit if it changes.
"""
from __future__ import annotations

from eimer.selection.join_order import _edge_sigma, analytic_node_size
from eimer.models import make_view

# calibrated constants
C_ID = 0.939            # bytes/row per BIGINT id column
C_TS = 1.029            # bytes/row per TIMESTAMP(6) column
C_DBL = 3.746           # bytes/row per DOUBLE column
FILE_OVERHEAD_BASE = 46.5      # bytes per parquet file
FILE_OVERHEAD_PER_COL = 456.0  # + per column (footer/statistics scale with width)
# safety factor inflating the total prediction; refit if it drifts.
TOTAL_SAFETY = 1.13

_TYPE_BYTES = {"NUMBER_ID": C_ID, "TIMESTAMP6": C_TS, "BINARY_DOUBLE": C_DBL}


def view_columns(varset, dep, *, measures_time: bool = True) -> list[tuple[str, str]]:
    """schema-min columns the emitter materializes for a conjunctive view: id + ts per
    variable, lat+lon if the variable is in a spatial-band predicate, time if measured."""
    band_vars = set()
    for e in dep.edges:
        band_vars.add(e.var_left)
        band_vars.add(e.var_right)
    cols = []
    for v in sorted(varset, key=lambda x: dep.positions[x]):
        cols.append((f"{v}_id", "NUMBER_ID"))
        cols.append((f"{v}_ts", "TIMESTAMP6"))
        if v in band_vars:
            cols.append((f"{v}_lat", "BINARY_DOUBLE"))
            cols.append((f"{v}_lon", "BINARY_DOUBLE"))
        if measures_time:
            cols.append((f"{v}_time", "TIMESTAMP6"))
    return cols


def _cyclic_card_correction(varset, dep, wl, batch_index: int) -> float:
    """conservative correction for cyclic views: the c = |E|-(|V|-1) closing edges
    cannot act as filters, so un-apply the loosest c sigmas (tree-bound cardinality).
    over-predicts rather than under-predicts, which a hard memory bound needs. acyclic
    views return 1."""
    vs = set(varset)
    edges = [(e.var_left, e.var_right) for e in dep.edges if {e.var_left, e.var_right} <= vs]
    c = len(edges) - (len(vs) - 1)
    if c<=0:
        return 1.0
    loosest = sorted((_edge_sigma(wl, batch_index, a, b) for a, b in edges), reverse=True)[:c]
    corr = 1.0
    for sig in loosest:
        corr /= max(sig, 1e-12)
    return corr


def view_rows(varset, wl, batch_index: int, dep=None) -> float:
    """resident rows of a materialized view, with the cyclic correction applied when a dep graph is given."""
    rows = analytic_node_size(make_view(varset), wl, batch_index)
    if dep is not None:
        rows *= _cyclic_card_correction(varset, dep, wl, batch_index)
    return rows


def cache_bytes_trino(varset, dep, wl, batch_index: int, n_batches: int) -> float:
    """predicted parquet bytes for one materialized view: per-file overhead over n_batches plus rows * bytes/row."""
    cols = view_columns(varset, dep)
    row_bytes = sum(_TYPE_BYTES[t] for _c, t in cols)
    rows = view_rows(varset, wl, batch_index, dep)
    return n_batches * (FILE_OVERHEAD_BASE + FILE_OVERHEAD_PER_COL * len(cols)) + rows * row_bytes


def peak_state_bytes_trino(cover_sets, dep, wl, *, query_batches=None) -> int:
    """predicted persistent iceberg state (sum of per-view parquet bytes) at the
    final batch for this cover. empty cover -> 0."""
    if not cover_sets:
        return 0
    K = len(wl.batch_sizes)
    pos = dep.positions
    all_last = max(dep.variables, key=lambda v: pos[v])
    caches = list(cover_sets)
    if not any(all_last in s for s in caches):
        caches.append(frozenset([all_last]))       # staged delta-driver singleton
    return int(round(TOTAL_SAFETY * sum(cache_bytes_trino(s, dep, wl, K, K) for s in caches)))
