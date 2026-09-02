"""Mean-variance optimization via cvxpy (Clarabel, with OSQP/SCS fallback).

Every objective shares the same hard constraints: fully invested (``sum w = 1``),
long only (``w >= 0``), a per-name box cap, and per-GICS-sector caps -- the same
*intent* as ``cycle.construction._cap_sectors`` but enforced as hard linear
constraints rather than water-fill redistribution. An optional turnover cap
bounds ``sum |w - w_prev|`` against the previous book.
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

import cvxpy as cp
import numpy as np
import numpy.typing as npt

from quant.risk import nearest_psd

Mat = npt.NDArray[np.float64]
Vec = npt.NDArray[np.float64]

_SOLVERS = ("CLARABEL", "OSQP", "SCS")
_OK = ("optimal", "optimal_inaccurate")


class OptimizeError(RuntimeError):
    """The solver could not return an optimal (or near-optimal) portfolio."""


@dataclass(frozen=True)
class Constraints:
    long_only: bool = True
    fully_invested: bool = True
    max_name_weight: float | None = 0.05
    min_name_weight: float = 0.0
    max_sector_weight: float | None = 0.30
    sector_of: dict[int, int | None] | None = None
    turnover_cap: float | None = None
    w_prev: Vec | None = None
    asset_ids: Sequence[int] | None = None


@dataclass(frozen=True)
class OptResult:
    weights: Vec
    asset_ids: list[int]
    expected_return: float | None
    expected_vol: float
    sharpe: float | None
    objective: str
    solver: str
    status: str
    solve_time_s: float


@dataclass(frozen=True)
class FrontierPoint:
    k: int
    target_return: float
    expected_return: float
    expected_vol: float
    sharpe: float | None
    status: str
    weights: Vec = field(repr=False)


def _sym_psd(sigma: Mat) -> Mat:
    return nearest_psd(np.asarray(sigma, dtype=np.float64))


def _sector_groups(c: Constraints, n: int) -> list[list[int]]:
    if c.max_sector_weight is None or c.sector_of is None or c.asset_ids is None:
        return []
    buckets: dict[int, list[int]] = {}
    for idx, aid in enumerate(c.asset_ids):
        sec = c.sector_of.get(int(aid))
        if sec is not None:
            buckets.setdefault(int(sec), []).append(idx)
    return [g for g in buckets.values() if g]


def _w_constraints(w: cp.Variable, c: Constraints, n: int) -> list[cp.Constraint]:
    cons: list[cp.Constraint] = []
    if c.fully_invested:
        cons.append(cp.sum(w) == 1)
    if c.long_only:
        cons.append(w >= c.min_name_weight)
    if c.max_name_weight is not None:
        cons.append(w <= c.max_name_weight)
    for g in _sector_groups(c, n):
        cons.append(cp.sum(w[g]) <= c.max_sector_weight)
    if c.turnover_cap is not None and c.w_prev is not None:
        cons.append(cp.norm1(w - c.w_prev) <= c.turnover_cap)
    return cons


def _y_constraints(y: cp.Variable, c: Constraints, n: int) -> list[cp.Constraint]:
    """Box / sector caps in the max-Sharpe y-space (``w = y / sum(y)``)."""
    cons: list[cp.Constraint] = [y >= 0]
    sy = cp.sum(y)
    if c.max_name_weight is not None:
        cons.append(y <= c.max_name_weight * sy)
    if c.min_name_weight > 0:
        cons.append(y >= c.min_name_weight * sy)
    for g in _sector_groups(c, n):
        cons.append(cp.sum(y[g]) <= c.max_sector_weight * sy)
    return cons


def _solve(problem: cp.Problem, primary: str) -> str:
    order = (primary, *[s for s in _SOLVERS if s != primary])
    last = "no_solver"
    for s in order:
        try:
            problem.solve(solver=s, verbose=False)  # type: ignore[no-untyped-call]
        except Exception:  # any solver blowup -> record it and try the next solver
            last = f"{s}:error"
            continue
        if problem.status in _OK:
            return s
        last = f"{s}:{problem.status}"
    raise OptimizeError(f"no solver converged ({last})")


def _stats(
    w: Vec, sigma: Mat, mu: Vec | None, rf: float
) -> tuple[float | None, float, float | None]:
    variance = float(w @ sigma @ w)
    vol = math.sqrt(variance) if variance > 0 else 0.0
    if mu is None:
        return None, vol, None
    ret = float(mu @ w)
    sharpe = (ret - rf) / vol if vol > 0 else None
    return ret, vol, sharpe


def _result(  # noqa: PLR0913, PLR0917 - a plain field-forwarding constructor
    name: str,
    w: Vec,
    ids: Sequence[int],
    sigma: Mat,
    mu: Vec | None,
    rf: float,
    solver: str,
    status: str,
    elapsed: float,
) -> OptResult:
    ret, vol, sharpe = _stats(w, sigma, mu, rf)
    return OptResult(
        weights=w,
        asset_ids=list(ids),
        expected_return=ret,
        expected_vol=vol,
        sharpe=sharpe,
        objective=name,
        solver=solver,
        status=status,
        solve_time_s=elapsed,
    )


def min_variance(
    sigma: Mat,
    *,
    constraints: Constraints,
    mu: Vec | None = None,
    rf: float = 0.0,
    solver: str = "CLARABEL",
) -> OptResult:
    sig = _sym_psd(sigma)
    n = sig.shape[0]
    t0 = time.perf_counter()
    w = cp.Variable(n)
    prob = cp.Problem(
        cp.Minimize(cp.quad_form(w, cp.psd_wrap(sig))), _w_constraints(w, constraints, n)
    )
    used = _solve(prob, solver)
    ids = constraints.asset_ids or list(range(n))
    return _result(
        "min_var",
        np.asarray(w.value),
        ids,
        sig,
        mu,
        rf,
        used,
        prob.status,
        time.perf_counter() - t0,
    )


def target_return_portfolio(  # noqa: PLR0913 - keyword-only optimizer knobs
    mu: Vec,
    sigma: Mat,
    target: float,
    *,
    constraints: Constraints,
    rf: float = 0.0,
    solver: str = "CLARABEL",
) -> OptResult:
    sig = _sym_psd(sigma)
    n = sig.shape[0]
    t0 = time.perf_counter()
    w = cp.Variable(n)
    cons = [*_w_constraints(w, constraints, n), mu @ w >= target]
    prob = cp.Problem(cp.Minimize(cp.quad_form(w, cp.psd_wrap(sig))), cons)
    used = _solve(prob, solver)
    ids = constraints.asset_ids or list(range(n))
    return _result(
        "target_return",
        np.asarray(w.value),
        ids,
        sig,
        mu,
        rf,
        used,
        prob.status,
        time.perf_counter() - t0,
    )


def target_volatility_portfolio(  # noqa: PLR0913 - keyword-only optimizer knobs
    mu: Vec,
    sigma: Mat,
    target_vol: float,
    *,
    constraints: Constraints,
    rf: float = 0.0,
    solver: str = "CLARABEL",
) -> OptResult:
    sig = _sym_psd(sigma)
    n = sig.shape[0]
    t0 = time.perf_counter()
    w = cp.Variable(n)
    cons = [*_w_constraints(w, constraints, n), cp.quad_form(w, cp.psd_wrap(sig)) <= target_vol**2]
    prob = cp.Problem(cp.Maximize(mu @ w), cons)
    try:
        used = _solve(prob, solver)
        status = prob.status
        weights = np.asarray(w.value)
    except OptimizeError:
        fallback = min_variance(sig, constraints=constraints, mu=mu, rf=rf, solver=solver)
        return _result(
            "target_vol",
            fallback.weights,
            fallback.asset_ids,
            sig,
            mu,
            rf,
            fallback.solver,
            "vol_infeasible",
            time.perf_counter() - t0,
        )
    ids = constraints.asset_ids or list(range(n))
    return _result("target_vol", weights, ids, sig, mu, rf, used, status, time.perf_counter() - t0)


def max_sharpe(
    mu: Vec, sigma: Mat, *, rf: float, constraints: Constraints, solver: str = "CLARABEL"
) -> OptResult:
    sig = _sym_psd(sigma)
    n = sig.shape[0]
    excess = np.asarray(mu, dtype=np.float64) - rf
    if not np.any(excess > 0) or constraints.turnover_cap is not None:
        # no long-only tangency, or turnover can't be linearised in y-space:
        # take the best Sharpe point off the frontier instead.
        pts = efficient_frontier(mu, sig, k=25, constraints=constraints, rf=rf, solver=solver)
        feasible = [p for p in pts if p.status in _OK and p.sharpe is not None]
        if not feasible:
            fb = min_variance(sig, constraints=constraints, mu=mu, rf=rf, solver=solver)
            return _result(
                "tangency", fb.weights, fb.asset_ids, sig, mu, rf, fb.solver, "no_tangency", 0.0
            )
        best = max(feasible, key=lambda p: p.sharpe or -1e9)
        return _result(
            "tangency",
            best.weights,
            constraints.asset_ids or list(range(n)),
            sig,
            mu,
            rf,
            "frontier",
            "from_frontier",
            0.0,
        )

    t0 = time.perf_counter()
    y = cp.Variable(n)
    cons = [*_y_constraints(y, constraints, n), excess @ y == 1]
    prob = cp.Problem(cp.Minimize(cp.quad_form(y, cp.psd_wrap(sig))), cons)
    used = _solve(prob, solver)
    yv = np.asarray(y.value)
    w = yv / yv.sum()
    ids = constraints.asset_ids or list(range(n))
    return _result("tangency", w, ids, sig, mu, rf, used, prob.status, time.perf_counter() - t0)


def efficient_frontier(  # noqa: PLR0913 - keyword-only optimizer knobs
    mu: Vec,
    sigma: Mat,
    *,
    k: int,
    constraints: Constraints,
    rf: float = 0.0,
    solver: str = "CLARABEL",
) -> list[FrontierPoint]:
    sig = _sym_psd(sigma)
    mu = np.asarray(mu, dtype=np.float64)
    lo = min_variance(sig, constraints=constraints, mu=mu, rf=rf, solver=solver)
    r_min = lo.expected_return if lo.expected_return is not None else float(mu.min())
    r_max = float(mu.max())
    if r_max <= r_min:
        r_max = r_min + abs(r_min) * 0.5 + 1e-4
    targets = np.linspace(r_min, r_max, k)

    points: list[FrontierPoint] = []
    for i, tgt in enumerate(targets):
        try:
            res = target_return_portfolio(
                mu, sig, float(tgt), constraints=constraints, rf=rf, solver=solver
            )
            points.append(
                FrontierPoint(
                    k=i,
                    target_return=float(tgt),
                    expected_return=res.expected_return or 0.0,
                    expected_vol=res.expected_vol,
                    sharpe=res.sharpe,
                    status=res.status,
                    weights=res.weights,
                )
            )
        except OptimizeError:
            points.append(
                FrontierPoint(i, float(tgt), 0.0, 0.0, None, "infeasible", np.zeros(sig.shape[0]))
            )
    return points
