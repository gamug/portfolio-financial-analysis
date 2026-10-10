"""Turnover (T-077): which previous book a book is measured against, the cap's constraint, and the
cost ``evaluate`` deducts.

A book belongs to a **chain**: the books of one configuration over time -- the same objective
(``kind``), ``frontier_k``, expected-return estimator, optimizer engine, input manifest and turnover
cap. The chain is encoded in ``quant_portfolio.engine_version`` (:func:`book_engine_version`), so the
previous book is simply the newest one of the same ``(kind, frontier_k, engine_version)`` with an
earlier ``as_of``. The default configuration (``equilibrium``, no cap) keeps its pre-T-077 key, so no
stored book changes key; another estimator or a cap appends a suffix, so books that differ only in
those no longer overwrite each other.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from quant.caps import Relaxation
from quant.config import QuantSettings
from quant.manifest import QuantManifest
from quant.optimize import Constraints, min_turnover

DEFAULT_ESTIMATOR = "equilibrium"
TURNOVER_CAP_MAX = 2.0  # sum |w - w_prev| of two fully invested long-only books is at most 2
_RELAX_SLACK = 1e-6  # headroom over the exact minimum, so the solver sees a non-empty set


def check_turnover_cap(cap: float | None) -> float | None:
    """*cap* if it is ``None`` or in ``(0, 2]``, else ``ValueError``."""
    if cap is not None and not 0.0 < cap <= TURNOVER_CAP_MAX:
        raise ValueError(f"turnover cap must be in (0, {TURNOVER_CAP_MAX:g}], got {cap}")
    return cap


def book_engine_version(settings: QuantSettings, manifest: QuantManifest) -> str:
    """``opt-v2+<tag>`` plus ``+mu-<estimator>`` for a non-default estimator and ``+to-<cap>`` when
    a turnover cap is set: the key of a book *and* the identity of its chain."""
    version = manifest.book_tagged(settings.optimizer_engine_version)
    if settings.ret_estimator != DEFAULT_ESTIMATOR:
        version += f"+mu-{settings.ret_estimator}"
    if settings.turnover_cap is not None:
        version += f"+to-{settings.turnover_cap:g}"
    return version


@dataclass(frozen=True)
class PreviousBook:
    portfolio_id: int
    as_of: str
    weights: dict[int, float]


def turnover_between(weights: Mapping[int, float], previous: Mapping[int, float] | None) -> float:
    """``sum |w - w_prev|`` over the union of names (a name in one book only counts in full). With
    no previous book the whole book is bought: ``sum |w|``."""
    prev = previous or {}
    return float(sum(abs(weights.get(a, 0.0) - prev.get(a, 0.0)) for a in set(weights) | set(prev)))


@dataclass(frozen=True)
class TurnoverPlan:
    """What ``optimize`` does about turnover for one book."""

    constraints: Constraints  # the cap bound (or not) on the panel
    record: dict[str, Any]  # what the book's params_json keeps
    relaxation: Relaxation | None


def plan_turnover(  # noqa: PLR0913 - the panel, the previous book and the cap, plus keyword knobs
    constraints: Constraints,
    asset_ids: Sequence[int],
    previous: PreviousBook | None,
    cap: float | None,
    *,
    applies: bool = True,
    solver: str = "CLARABEL",
) -> TurnoverPlan:
    """Bind the turnover cap to *previous* on the panel *asset_ids*.

    No cap, or no previous book: nothing is bound (``applied`` false, with the reason). A name the
    previous book held outside the panel is sold in full, and counts: the constraint is
    ``norm1(w - w_prev over the panel) + sum w_prev(outside) <= cap``. A cap below the smallest
    turnover the other constraints allow is relaxed to that value plus a hair and the relaxation
    is recorded. *applies* is false for an objective the cap does not reach (``risk_parity``)."""
    record: dict[str, Any] = {
        "cap_requested": cap,
        "cap_effective": None,
        "applied": False,
        "reason_not_applied": None,
        "previous_portfolio_id": previous.portfolio_id if previous else None,
        "previous_as_of": previous.as_of if previous else None,
        "outside_panel_weight": 0.0,
        "realized": None,
    }
    if cap is None:
        record["reason_not_applied"] = "no turnover cap requested"
        return TurnoverPlan(constraints, record, None)
    if previous is None:
        record["reason_not_applied"] = "no previous book in the chain"
        return TurnoverPlan(replace(constraints, turnover_cap=None, w_prev=None), record, None)
    index = {int(a): i for i, a in enumerate(asset_ids)}
    w_prev = np.zeros(len(asset_ids))
    outside = 0.0
    for a, w in previous.weights.items():
        if a in index:
            w_prev[index[a]] = w
        else:
            outside += w
    record["outside_panel_weight"] = outside
    if not applies:
        record["reason_not_applied"] = "this objective ignores the turnover cap"
        return TurnoverPlan(replace(constraints, turnover_cap=None, w_prev=None), record, None)
    bound = replace(constraints, turnover_cap=cap, w_prev=w_prev, w_prev_outside=outside)
    smallest = min_turnover(bound, solver) + outside
    relaxation: Relaxation | None = None
    effective = cap
    if smallest > cap:
        effective = smallest + _RELAX_SLACK
        why = (
            f"no book satisfying the name and sector caps trades less than {smallest:.6g} against "
            f"the previous book ({outside:.6g} of it held outside today's panel); relaxed to the "
            "smallest feasible value"
        )
        relaxation = Relaxation("turnover", cap, effective, why)
        bound = replace(bound, turnover_cap=effective)
    record.update(applied=True, cap_effective=effective)
    return TurnoverPlan(bound, record, relaxation)
