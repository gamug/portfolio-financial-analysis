"""The extensible optimization-metric family.

``OBJECTIVES`` maps a name to a builder that takes the risk model + constraints
and returns an :class:`~quant.optimize.OptResult`. Adding a metric
(max-return-for-risk-budget, risk parity, max diversification, ...) is one
function plus one dict entry -- ``run_optimize`` iterates whatever
``--objectives`` resolves to.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from quant.optimize import (
    Constraints,
    OptResult,
    max_sharpe,
    min_variance,
    risk_parity,
    target_volatility_portfolio,
)

Vec = npt.NDArray[np.float64]


@dataclass(frozen=True)
class ObjectiveContext:
    sigma: Vec
    mu: Vec
    rf: float
    constraints: Constraints
    target_volatility: float | None
    solver: str


ObjectiveBuilder = Callable[[ObjectiveContext], OptResult]


def _min_var(ctx: ObjectiveContext) -> OptResult:
    return min_variance(
        ctx.sigma, constraints=ctx.constraints, mu=ctx.mu, rf=ctx.rf, solver=ctx.solver
    )


def _tangency(ctx: ObjectiveContext) -> OptResult:
    return max_sharpe(ctx.mu, ctx.sigma, rf=ctx.rf, constraints=ctx.constraints, solver=ctx.solver)


def _target_vol(ctx: ObjectiveContext) -> OptResult:
    if ctx.target_volatility is None:
        raise ValueError("target_vol objective needs ObjectiveContext.target_volatility")
    return target_volatility_portfolio(
        ctx.mu,
        ctx.sigma,
        ctx.target_volatility,
        constraints=ctx.constraints,
        rf=ctx.rf,
        solver=ctx.solver,
    )


def _risk_parity(ctx: ObjectiveContext) -> OptResult:
    return risk_parity(
        ctx.sigma, constraints=ctx.constraints, mu=ctx.mu, rf=ctx.rf, solver=ctx.solver
    )


OBJECTIVES: dict[str, ObjectiveBuilder] = {
    "min_var": _min_var,
    "tangency": _tangency,
    "target_vol": _target_vol,
    "risk_parity": _risk_parity,
}


def resolve_objectives(names: Sequence[str]) -> list[tuple[str, ObjectiveBuilder]]:
    unknown = [n for n in names if n not in OBJECTIVES and n != "frontier"]
    if unknown:
        raise ValueError(
            f"unknown objective(s) {unknown}; valid: {sorted(OBJECTIVES)} (+ 'frontier')"
        )
    return [(n, OBJECTIVES[n]) for n in names if n in OBJECTIVES]


def objective_param(name: str, ctx: ObjectiveContext) -> float | None:
    """The scalar the objective was parameterised by, for ``quant_portfolio.target_param``."""
    if name == "target_vol":
        return ctx.target_volatility
    return None


__all__ = [
    "OBJECTIVES",
    "ObjectiveBuilder",
    "ObjectiveContext",
    "objective_param",
    "resolve_objectives",
]
