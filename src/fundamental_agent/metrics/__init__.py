"""Deterministic metric skills, one module per group.

Each module exposes ``GROUP`` and a ``compute(stmts, period_key, prior_key=None,
ttm=None)`` function returning a list of
:class:`~fundamental_agent.metrics.base.MetricResult`. ``ttm`` is only consulted by
``profitability``/``efficiency`` (F4, docs/model_fixes.md) -- every other group
accepts and ignores it, purely to keep one uniform ``ComputeFn`` signature for
:data:`COMPUTERS`'s generic dispatch.
"""

from __future__ import annotations

from collections.abc import Callable

from fundamental_agent.metrics import (
    cagr,
    cashflow,
    efficiency,
    growth,
    leverage,
    liquidity,
    profitability,
    roic,
)
from fundamental_agent.metrics.base import MetricResult, safe_div
from fundamental_agent.statements import Statements

ComputeFn = Callable[[Statements, str, str | None, "dict[str, float] | None"], list[MetricResult]]

# Groups the pipeline always computes regardless of the orchestrator's choices.
CORE_GROUPS = ("profitability", "liquidity", "leverage", "cashflow")
# Groups the orchestrator may add when the filing supports them.
OPTIONAL_GROUPS = ("efficiency", "growth", "roic", "cagr")

_MODULES = (
    profitability,
    liquidity,
    leverage,
    efficiency,
    growth,
    cashflow,
    roic,
    cagr,
)
COMPUTERS: dict[str, ComputeFn] = {module.GROUP: module.compute for module in _MODULES}


def compute_group(
    group: str,
    stmts: Statements,
    period_key: str,
    prior_key: str | None = None,
    ttm: dict[str, float] | None = None,
) -> list[MetricResult]:
    """Run one metric group by name."""
    return COMPUTERS[group](stmts, period_key, prior_key, ttm)


__all__ = [
    "COMPUTERS",
    "CORE_GROUPS",
    "OPTIONAL_GROUPS",
    "MetricResult",
    "compute_group",
    "safe_div",
]
