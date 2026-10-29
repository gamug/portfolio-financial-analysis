"""GUARD: the deterministic, four-layer risk throttle of the backtest overlay.

The pure core lives in :mod:`cycle.guard.core` (no database, no I/O, inputs never
mutated); this package only re-exports its fixed interface (``BACKTEST_SPEC.md`` §8.1)
so callers write ``from cycle.guard import GuardParams, evaluate, initial_state``.
"""

from __future__ import annotations

__all__ = [
    "Alert",
    "GuardInput",
    "GuardOutput",
    "GuardParams",
    "GuardState",
    "NewsCounts",
    "NewsParams",
    "evaluate",
    "initial_state",
]

from cycle.guard.core import (
    Alert,
    GuardInput,
    GuardOutput,
    GuardParams,
    GuardState,
    NewsCounts,
    NewsParams,
    evaluate,
    initial_state,
)
