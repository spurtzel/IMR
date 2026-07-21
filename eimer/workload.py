"""workload model: batch schedule, initial base size, arrival rate, and the
independent/dependent selectivities the cost model reads."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Tuple

from eimer.models import DependencyGraph, canonical_variable_pair


@dataclass(frozen=True)
class ConstantSelectivity:
    value: float


@dataclass(frozen=True)
class WindowSelectivity:
    w: float


DependentSelectivity = ConstantSelectivity | WindowSelectivity
EdgeKey = Tuple[str, str]


@dataclass(frozen=True)
class Selectivities:
    independent: Mapping[str, float]
    dependent: Mapping[EdgeKey, Tuple[DependentSelectivity, ...]]


@dataclass(frozen=True)
class Workload:
    batch_sizes: Tuple[int, ...]
    initial_base_size: int
    rho: float
    dependency_graph: DependencyGraph
    selectivities: Selectivities

    def __post_init__(self) -> None:
        if any(not isinstance(size, int) or size <= 0 for size in self.batch_sizes):
            raise ValueError("batch_sizes must contain only positive integers")
        if self.initial_base_size < 0:
            raise ValueError("initial_base_size must be >= 0")
        if self.rho <= 0:
            raise ValueError("rho must be > 0")

        for variable in self.dependency_graph.variables:
            if variable not in self.selectivities.independent:
                raise ValueError(f"Missing independent selectivity for variable {variable!r}")
            value = self.selectivities.independent[variable]
            if value <= 0.0 or value > 1.0:
                raise ValueError(f"Independent selectivity for {variable!r} must be within (0, 1]")

        for edge in self.dependency_graph.edges:
            key = canonical_variable_pair(edge.var_left, edge.var_right, self.dependency_graph.positions)
            if key not in self.selectivities.dependent:
                raise ValueError(f"Missing dependent selectivity for edge {key!r}")
            components = self.selectivities.dependent[key]
            for component in components:
                if isinstance(component, ConstantSelectivity):
                    if component.value <= 0.0 or component.value > 1.0:
                        raise ValueError(
                            f"Constant selectivity for edge {key!r} must be within (0, 1]"
                        )
                    continue
                if isinstance(component, WindowSelectivity):
                    if component.w <= 0.0:
                        raise ValueError(f"Window selectivity for edge {key!r} must have w > 0")
                    continue
                raise ValueError(f"Unknown dependent selectivity type for edge {key!r}: {type(component)!r}")

    @property
    def k(self) -> int:
        return len(self.batch_sizes)

    def n_at(self, t: int) -> int:
        if t < 0:
            raise ValueError(f"batch index must be non-negative, got {t}")
        if t>self.k:
            raise ValueError(f"batch index {t} exceeds workload k={self.k}")
        return self.initial_base_size + sum(self.batch_sizes[:t])

    def t_span(self, t: int) -> float:
        return self.n_at(t) / self.rho


def make_workload(
    dep_graph: DependencyGraph,
    *,
    total_events: int | None = None,
    batches: int | None = None,
    batch_size: int | None = None,
    batch_sizes: "tuple[int, ...] | list[int] | None" = None,
    initial_base_size: int = 0,
    selectivities: Selectivities,
    rho: float = 1000.0,
) -> Workload:
    """a workload is its sequence of batch sizes s=[s1,...,sB]; table size N is derived:
    N = initial_base_size + sum(s); window after batch t = initial_base_size + sum(s[:t]) (== Workload.n_at(t)).
    precedence:
      1. batch_sizes=[...]           -> arbitrary non-uniform sequence.
      2. (batch_size=, batches=)     -> uniform sequence [batch_size]*batches.
      3. (total_events=N, batches=B) -> uniform split, remainder over the first batches (sum == N exactly).
    forms 1/2 fix the per-batch size; form 3 couples batch size to N (= N/B)."""
    if batch_sizes is not None:
        seq = tuple(int(x) for x in batch_sizes)
        if not seq:
            raise ValueError("batch_sizes must be a non-empty sequence")
    elif batch_size is not None:
        if batches is None:
            raise ValueError("batch_size requires batches")
        seq = (int(batch_size),) * int(batches)
    elif total_events is not None and batches is not None:
        if total_events < batches:
            raise ValueError("total_events must be >= batches")
        base = total_events // batches
        remainder = total_events % batches
        seq = tuple(base + (1 if idx < remainder else 0) for idx in range(batches))
    else:
        raise ValueError("provide batch_sizes=, or (batch_size=, batches=), or (total_events=, batches=)")
    return Workload(
        batch_sizes=seq,
        initial_base_size=int(initial_base_size),
        rho=rho,
        dependency_graph=dep_graph,
        selectivities=selectivities,
    )
