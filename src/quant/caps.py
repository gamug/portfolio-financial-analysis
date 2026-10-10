"""The benchmark's weight caps as a function of N (T-137; the T-134 decisions, FR-007).

This is the rule ``cycle.construction`` applies to the thesis book, **copied** (``quant`` is a leaf and
must not import ``cycle`` -- see ``tests/test_quant_import_isolation.py``; ``quant.state`` copies
``cycle.state`` for the same reason) and pinned against it by ``tests/test_quant_caps.py``:

* ``n_held = min(N, panel size)`` -- the same meaning as ``cycle``'s ``n_held``;
* name cap ``1.5 / n_held`` with **no 0.10 floor**, unless given explicitly; a cap below
  ``1 / n_held`` cannot sum to 1 and is relaxed to it, recorded;
* sector cap 0.30 unless given; when the panel's sectors cannot hold it under the name cap it is
  relaxed to the smallest feasible value, recorded.

There is **no limit on the number of names** (no integer programming): the optimizer decides how many
to hold, and the caps are linear constraints on its weights. Where ``cycle`` also has a score band
``[0.5/N, 1.5/N]`` the benchmark has a floor of 0, and an asset with no sector is uncapped (as
``quant.optimize`` always treated it) rather than one more capped group.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

DEFAULT_TOP_N = 30
DEFAULT_SECTOR_CAP = 0.30
_NAME_CEILING = 1.5  # name cap = 1.5 / n_held
_EPS = 1e-12  # "is this a real relaxation": far below the solver tolerance


@dataclass(frozen=True)
class Relaxation:
    """A cap the panel could not satisfy as requested, and what it was relaxed to."""

    cap: Literal["name", "sector", "turnover"]
    requested: float
    effective: float
    reason: str


@dataclass(frozen=True)
class EffectiveCaps:
    """The caps every objective runs under, and how they were derived."""

    top_n: int
    n_held: int
    max_name_weight: float
    max_sector_weight: float | None  # None: no sector cap was asked for
    relaxations: tuple[Relaxation, ...]

    def record(self) -> dict[str, Any]:
        """What ``quant_run.params_json`` and ``quant_portfolio.params_json`` keep."""
        return {
            "top_n": self.top_n,
            "n_held": self.n_held,
            "max_name_weight": self.max_name_weight,
            "max_sector_weight": self.max_sector_weight,
            "relaxations": [asdict(r) for r in self.relaxations],
        }


def feasible_sector_cap(
    counts: Sequence[int], hi: float, requested: float, *, unclassified: int = 0
) -> float:
    """The smallest sector cap >= *requested* under which a fully invested book exists.

    A sector holding k names can carry any weight up to ``k * hi`` (*hi* the name cap), so a cap *c*
    is feasible iff ``sum(min(c, k * hi)) + unclassified * hi >= 1``; the smallest such *c* is solved
    exactly on the sorted capacities. This is ``cycle.construction._feasible_sector_cap`` with a band
    floor of 0 and the free capacity of assets that have no sector (uncapped here); with
    ``unclassified = 0`` the two agree exactly.
    """
    capacities = sorted(k * hi for k in counts)
    need = 1.0 - unclassified * hi
    if need <= 0.0 or not capacities:
        return requested
    reach = capacities[-1]
    placed = 0.0
    for idx, cap in enumerate(capacities):
        c = (need - placed) / (len(capacities) - idx)
        if c <= cap:
            reach = c
            break
        placed += cap
    return max(requested, reach)


def resolve_caps(
    top_n: int,
    panel_size: int,
    sector_counts: Sequence[int] = (),
    *,
    max_name_weight: float | None = None,
    max_sector_weight: float | None = DEFAULT_SECTOR_CAP,
) -> EffectiveCaps:
    """The effective name and sector caps for a panel of *panel_size* assets.

    *sector_counts* is the number of panel assets in each sector; the rest of the panel
    (``panel_size - sum(sector_counts)``) has no sector and is uncapped. ``max_sector_weight=None``
    means no sector cap at all. Bad inputs raise ``ValueError``."""
    if top_n < 1:
        raise ValueError(f"top_n must be >= 1, got {top_n}")
    if panel_size < 1:
        raise ValueError("the panel has no assets")
    if sum(sector_counts) > panel_size:
        raise ValueError("more sector members than panel assets")
    for name, cap in (
        ("max_name_weight", max_name_weight),
        ("max_sector_weight", max_sector_weight),
    ):
        if cap is not None and not 0.0 < cap <= 1.0:
            raise ValueError(f"{name} must be in (0, 1], got {cap}")

    n_held = min(top_n, panel_size)
    relaxations: list[Relaxation] = []
    requested_name = max_name_weight if max_name_weight is not None else _NAME_CEILING / n_held
    name_cap = requested_name
    if requested_name < 1.0 / n_held - _EPS:
        name_cap = 1.0 / n_held
        why = f"{n_held} names at most {requested_name:.6g} each cannot sum to 1; relaxed to 1/{n_held}"
        relaxations.append(Relaxation("name", requested_name, name_cap, why))
    name_cap = min(name_cap, 1.0)

    sector_cap = max_sector_weight
    if max_sector_weight is not None:
        free = panel_size - sum(sector_counts)
        feasible = feasible_sector_cap(
            sector_counts, name_cap, max_sector_weight, unclassified=free
        )
        if feasible > max_sector_weight + _EPS:
            sector_cap = feasible
            why = (
                f"{len(sector_counts)} sector(s) over {panel_size} assets, name cap {name_cap:.6g}: no "
                f"book sums to 1 with every sector at most {max_sector_weight:.6g}; relaxed to the "
                "smallest feasible value"
            )
            relaxations.append(Relaxation("sector", max_sector_weight, feasible, why))
    return EffectiveCaps(top_n, n_held, name_cap, sector_cap, tuple(relaxations))
