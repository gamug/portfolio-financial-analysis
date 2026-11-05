"""Mean-variance optimization via cvxpy (Clarabel, with OSQP/SCS fallback).

Every objective shares the same hard constraints: fully invested (``sum w = 1``),
long only (``w >= 0``), a per-name box cap, and per-GICS-sector caps -- the same
*intent* as ``cycle.construction.build_book``'s caps (and, since T-137, the same effective values,
resolved by ``quant.caps``) but enforced inside the optimizer as hard linear constraints rather
than by an exact projection of a target. An optional turnover cap bounds ``sum |w - w_prev|`` against the previous book
(T-077: ``Constraints.w_prev`` is populated from the previous book of the same chain; a name that book held and today's
panel does not still counts, as the constant ``w_prev_outside``).
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

import cvxpy as cp
import numpy as np
import numpy.typing as npt

from quant.risk import nearest_psd

Mat = npt.NDArray[np.float64]
Vec = npt.NDArray[np.float64]

_SOLVERS = ("CLARABEL", "OSQP", "SCS")
_OK = ("optimal", "optimal_inaccurate")
_CAP_TOL = 1e-9  # a cap breach smaller than this is solver noise, not a book to project
_MATRIX_NDIM = 2  # a scenario matrix is (q, n)


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
    # weight the previous book held in names outside today's panel: it is sold in full, so it adds
    # to the turnover as a constant -- norm1(w - w_prev) + w_prev_outside <= turnover_cap
    w_prev_outside: float = 0.0
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
    #: Final round: whether the caps moved a raw (heuristic) book onto the feasible set.
    projected: bool = False


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


def _breaches_caps(w: Vec, c: Constraints, n: int) -> bool:
    if c.max_name_weight is not None and w.max() > c.max_name_weight + _CAP_TOL:
        return True
    return any(w[g].sum() > (c.max_sector_weight or 0.0) + _CAP_TOL for g in _sector_groups(c, n))


def _project_onto_caps(w: Vec, c: Constraints, solver: str, *, tight: bool = False) -> Vec:
    """The Euclidean projection of *w* onto the constraint set every other objective uses.

    A risk-parity book is a point, not an optimum of the box/sector-constrained problem, so a cap
    that it breaches is enforced by moving it to the nearest feasible book (T-137: the name cap now
    follows N, so on a small panel an ERC book can sit above it; before, a water-fill handled the
    name cap alone and the sector cap not at all)."""
    n = len(w)
    v = cp.Variable(n)
    free = replace(c, turnover_cap=None, w_prev=None)  # ERC has no previous book to be near
    prob = cp.Problem(cp.Minimize(cp.sum_squares(v - w)), _w_constraints(v, free, n))
    _solve(prob, solver, tolerances=_TIGHT if tight else None)
    return np.asarray(v.value, dtype=np.float64)


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
        cons.append(cp.norm1(w - c.w_prev) <= c.turnover_cap - c.w_prev_outside)
    return cons


def turnover_active(c: Constraints) -> bool:
    """Whether *c* binds a turnover cap: one needs both the cap and a previous book."""
    return c.turnover_cap is not None and c.w_prev is not None


def min_turnover(c: Constraints, solver: str = "CLARABEL") -> float:
    """The smallest ``sum |w - w_prev|`` over the panel any book satisfying the other constraints
    (fully invested, long only, name and sector caps) can reach -- an LP. The turnover cap is
    infeasible below ``min_turnover + w_prev_outside``; the caller relaxes it to that value."""
    if c.w_prev is None:
        return 0.0
    n = len(c.w_prev)
    w = cp.Variable(n)
    free = replace(c, turnover_cap=None, w_prev=None, w_prev_outside=0.0)
    prob = cp.Problem(cp.Minimize(cp.norm1(w - c.w_prev)), _w_constraints(w, free, n))
    _solve(prob, solver)
    return float(np.abs(np.asarray(w.value) - c.w_prev).sum())


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


#: Final round §11: the explicit tolerances of the new projection / QP / LP solves. They are
#: only passed to CLARABEL (the other fallback solvers take different option names).
_TIGHT = {"tol_gap_abs": 1e-12, "tol_gap_rel": 1e-12, "tol_feas": 1e-12}
_STANDARD = {"tol_gap_abs": 1e-10, "tol_gap_rel": 1e-10, "tol_feas": 1e-10}


def _solve(
    problem: cp.Problem,
    primary: str,
    *,
    tolerances: Mapping[str, float] | None = None,
) -> str:
    order = (primary, *[s for s in _SOLVERS if s != primary])
    last = "no_solver"
    for s in order:
        options = dict(tolerances) if tolerances is not None and s == "CLARABEL" else {}
        try:
            problem.solve(solver=s, verbose=False, **options)  # type: ignore[no-untyped-call]
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
    *,
    projected: bool = False,
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
        projected=projected,
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


def risk_parity(
    sigma: Mat,
    *,
    constraints: Constraints,
    mu: Vec | None = None,
    rf: float = 0.0,
    solver: str = "CLARABEL",
) -> OptResult:
    """Equal-risk-contribution portfolio (Spinu 2013 convex form): minimize
    ``0.5 w'Sigma w - sum(log(w_i))`` over ``w > 0``, then normalize. mu-free. The caps rarely
    bind on a ~500-name ERC book (weights are inverse-vol scale, well under 5%); when they do -- a
    small panel, where the name cap is 1.5 / n_held -- the book is projected onto both."""
    sig = _sym_psd(sigma)
    n = sig.shape[0]
    t0 = time.perf_counter()
    w = cp.Variable(n, pos=True)
    prob = cp.Problem(cp.Minimize(0.5 * cp.quad_form(w, cp.psd_wrap(sig)) - cp.sum(cp.log(w))))
    used = _solve(prob, solver)
    wv = np.maximum(np.asarray(w.value), 1e-12)
    wv = wv / wv.sum()
    if _breaches_caps(wv, constraints, n):
        wv = _project_onto_caps(wv, constraints, solver)
    ids = constraints.asset_ids or list(range(n))
    return _result("risk_parity", wv, ids, sig, mu, rf, used, prob.status, time.perf_counter() - t0)


def risk_parity_slsqp(  # noqa: C901 - the plan's constrained objective, one linear solve
    sigma: Mat, *, constraints: Constraints
) -> OptResult:
    """Equal-risk-contribution weights by the round-4 plan's SLSQP objective.

    Minimize ``sum_i sum_j (w_i (Sigma w)_i - w_j (Sigma w)_j)^2`` subject to
    ``w >= 0``, ``sum w = 1``, the name cap and the sector caps, with ``Sigma`` scaled by
    ``1 / mean(diag Sigma)`` (the argmin is scale-free), from equal weights, SLSQP with
    ``ftol=1e-12`` and ``maxiter=1000``. A solve that does not converge raises
    :class:`ValueError`; so does a final point that violates a constraint beyond 1e-8.
    """
    from scipy.optimize import minimize  # noqa: PLC0415 - quant's numeric stack

    sig = _sym_psd(np.asarray(sigma, dtype=np.float64))
    n = sig.shape[0]
    if n == 0:
        raise ValueError("risk_parity_slsqp needs at least one asset")
    t0 = time.perf_counter()
    mean_var = float(np.mean(np.diag(sig)))
    if not np.isfinite(mean_var) or mean_var <= 0.0:
        raise ValueError(f"risk_parity_slsqp: degenerate covariance (mean variance {mean_var!r})")
    scaled = sig / mean_var

    def objective(w: npt.NDArray[np.float64]) -> float:
        risk = w * (scaled @ w)
        difference = risk[:, None] - risk[None, :]
        return float(np.sum(difference * difference))

    def objective_jac(w: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        # f = sum_i sum_j (rc_i - rc_j)^2 = 2n sum_i (rc_i - mean(rc))^2, rc_i = w_i (S w)_i
        risk = w * (scaled @ w)
        gradient = 4.0 * n * (risk - risk.mean())
        return np.asarray(gradient * (scaled @ w) + scaled @ (gradient * w), dtype=np.float64)

    cap = constraints.max_name_weight if constraints.max_name_weight is not None else 1.0
    bounds = [(0.0, cap)] * n
    cons: list[dict[str, object]] = [
        {
            "type": "eq",
            "fun": lambda w: np.array([float(np.sum(w) - 1.0)]),
            "jac": lambda w: np.ones((1, n)),
        }
    ]
    sector_groups: dict[int | None, list[int]] = {}
    if constraints.max_sector_weight is not None and constraints.sector_of is not None:
        ids = constraints.asset_ids or list(range(n))
        for index, asset_id in enumerate(ids):
            sector_groups.setdefault(constraints.sector_of.get(int(asset_id)), []).append(index)
        sector_cap = float(constraints.max_sector_weight)
        matrix = np.zeros((len(sector_groups), n), dtype=np.float64)
        for row, indices in enumerate(sector_groups.values()):
            matrix[row, indices] = 1.0
        cons.append(
            {
                "type": "ineq",
                "fun": lambda w: sector_cap - matrix @ w,
                "jac": lambda w: -matrix,
            }
        )
    x0 = np.full(n, 1.0 / n, dtype=np.float64)
    result = minimize(
        objective,
        x0,
        jac=objective_jac,
        method="SLSQP",
        bounds=bounds,
        constraints=cons,
        options={"ftol": 1e-12, "maxiter": 1000},
    )
    if not result.success:
        raise ValueError(f"risk_parity_slsqp did not converge: {result.message}")
    weights = np.clip(np.asarray(result.x, dtype=np.float64), 0.0, None)
    total = float(weights.sum())
    if total <= 0.0:
        raise ValueError("risk_parity_slsqp returned an all-zero book")
    weights = weights / total
    tolerance = 1e-8
    if abs(float(weights.sum()) - 1.0) > tolerance:
        raise ValueError("risk_parity_slsqp: weights do not sum to one")
    if cap < 1.0 and float(weights.max()) > cap + tolerance:
        raise ValueError("risk_parity_slsqp: the name cap is violated")
    sector_limit = constraints.max_sector_weight
    if sector_limit is not None:
        for indices in sector_groups.values():
            if float(weights[indices].sum()) > float(sector_limit) + tolerance:
                raise ValueError("risk_parity_slsqp: a sector cap is violated")
    ids = constraints.asset_ids or list(range(n))
    return _result(
        "risk_parity_slsqp",
        weights,
        ids,
        sig,
        None,
        0.0,
        "SLSQP",
        str(result.status),
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


def _tangency_from_frontier(
    mu: Vec, sig: Mat, *, rf: float, constraints: Constraints, solver: str
) -> OptResult:
    """The best-Sharpe point of a 25-point frontier scan: the tangency book when no long-only
    tangency exists, when a turnover cap binds (it cannot be linearised in y-space), or when the
    y-space solve fails (audit Q5)."""
    n = sig.shape[0]
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


def max_sharpe(
    mu: Vec, sigma: Mat, *, rf: float, constraints: Constraints, solver: str = "CLARABEL"
) -> OptResult:
    sig = _sym_psd(sigma)
    n = sig.shape[0]
    excess = np.asarray(mu, dtype=np.float64) - rf
    if not np.any(excess > 0) or turnover_active(constraints):
        # no long-only tangency, or turnover can't be linearised in y-space:
        # take the best Sharpe point off the frontier instead.
        return _tangency_from_frontier(mu, sig, rf=rf, constraints=constraints, solver=solver)

    t0 = time.perf_counter()
    y = cp.Variable(n)
    cons = [*_y_constraints(y, constraints, n), excess @ y == 1]
    prob = cp.Problem(cp.Minimize(cp.quad_form(y, cp.psd_wrap(sig))), cons)
    try:
        used = _solve(prob, solver)
    except OptimizeError:
        # audit Q5: the y-space program can be infeasible under the caps (excess return reachable
        # only above them); the frontier scan still returns the best feasible book
        return _tangency_from_frontier(mu, sig, rf=rf, constraints=constraints, solver=solver)
    yv = np.asarray(y.value)
    w = yv / yv.sum()
    ids = constraints.asset_ids or list(range(n))
    return _result("tangency", w, ids, sig, mu, rf, used, prob.status, time.perf_counter() - t0)


def _max_feasible_return(
    mu: Vec, sigma: Mat, constraints: Constraints, solver: str
) -> float | None:
    """The highest ``muᵀw`` attainable under the constraints (a box cap makes
    ``mu.max()`` unreachable)."""
    n = sigma.shape[0]
    w = cp.Variable(n)
    prob = cp.Problem(cp.Maximize(mu @ w), _w_constraints(w, constraints, n))
    try:
        _solve(prob, solver)
    except OptimizeError:
        return None
    return float(mu @ np.asarray(w.value))


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
    r_max = _max_feasible_return(mu, sig, constraints, solver)
    if r_max is None or r_max <= r_min + 1e-9:
        # mu carries no cross-sectional signal under the constraints (T-112): there is no
        # feasible return range to sweep, so one point -- the min-variance portfolio, marked
        # 'degenerate' -- represents the whole "frontier", not k identical copies of it
        # mislabelled 'optimal' as if a real k-point sweep had actually run.
        return [
            FrontierPoint(0, r_min, r_min, lo.expected_vol, lo.sharpe, "degenerate", lo.weights)
        ]
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


# -- final round: the satellite weighting methods (plan §3) -------------------------------------


def equal_weight(n: int) -> Vec:
    """The 1/N book: ``w_i = 1/n`` (DeMiguel, Garlappi & Uppal 2009)."""
    if n < 1:
        raise ValueError(f"equal_weight needs at least one asset, got {n}")
    return np.full(n, 1.0 / n, dtype=np.float64)


def volatility_timing(sigma: Mat, *, eta: float = 1.0) -> Vec:
    """VT(eta): ``w_i ∝ (1 / sigma_i**2)**eta`` (Kirby & Ostdiek 2012, eq. 12/14).

    ``eta = 1`` is the plan's natural choice. The raw vector is not capped; the caller
    projects it onto the feasible set.
    """
    raw = np.asarray(sigma, dtype=np.float64)
    var = np.diag(raw)
    if np.any(~np.isfinite(var)) or np.any(var <= 0.0):
        raise ValueError("volatility_timing: every asset needs a positive finite variance")
    if not np.isfinite(eta) or eta <= 0.0:
        raise ValueError(f"volatility_timing: eta must be positive, got {eta!r}")
    inv = (1.0 / var) ** eta
    return np.asarray(inv / inv.sum(), dtype=np.float64)


def project_onto_caps(w: Vec, constraints: Constraints, solver: str = "CLARABEL") -> Vec:
    """The Euclidean projection of *w* onto the cap set, or *w* itself when it is feasible."""
    vector = np.asarray(w, dtype=np.float64)
    if vector.ndim != 1 or vector.size == 0:
        raise ValueError(f"project_onto_caps needs a non-empty vector, got shape {vector.shape}")
    if not _breaches_caps(vector, constraints, vector.size):
        return vector.copy()
    projected = _project_onto_caps(vector, constraints, solver, tight=True)
    projected = np.maximum(projected, 0.0)
    total = float(projected.sum())
    if not np.isfinite(total) or total <= 0.0:
        raise OptimizeError("the cap projection returned an all-zero book")
    projected = projected / total
    n = projected.size
    if constraints.max_name_weight is not None and (
        projected.max() > constraints.max_name_weight + 1e-7
    ):
        raise OptimizeError("the cap projection violates the name cap")
    for group in _sector_groups(constraints, n):
        if projected[group].sum() > (constraints.max_sector_weight or 0.0) + 1e-7:
            raise OptimizeError("the cap projection violates a sector cap")
    return projected


def _feasible_result(  # noqa: PLR0913 - one constructed result
    name: str,
    weights: Vec,
    sigma: Mat,
    constraints: Constraints,
    *,
    projected: bool,
    elapsed: float,
    solver: str = "projected",
) -> OptResult:
    ids = constraints.asset_ids or list(range(weights.size))
    return _result(
        name,
        np.asarray(weights, dtype=np.float64),
        ids,
        sigma,
        None,
        0.0,
        solver,
        "optimal",
        elapsed,
        projected=projected,
    )


def max_diversification(
    sigma: Mat, *, constraints: Constraints, solver: str = "CLARABEL"
) -> OptResult:
    """Maximum diversification (Choueifaty & Coignard 2008) under the caps.

    The ratio ``D(w) = w'sigma / sqrt(w'Sigma w)`` is scale-free. In the y-space
    ``w = y / sum(y)`` the homogeneous caps become linear and the equivalent convex QP is
    ``min y'Sigma y`` subject to ``sigma'y >= 1``, ``y >= 0`` and the caps.
    """
    sig = _sym_psd(np.asarray(sigma, dtype=np.float64))
    n = sig.shape[0]
    if n == 0:
        raise ValueError("max_diversification needs at least one asset")
    vols = np.sqrt(np.diag(sig))
    if np.any(~np.isfinite(vols)) or np.any(vols <= 0.0):
        raise ValueError("max_diversification: every asset needs a positive finite variance")
    t0 = time.perf_counter()
    y = cp.Variable(n, nonneg=True)
    total = cp.sum(y)
    cons: list[cp.Constraint] = [vols @ y >= 1.0]
    if constraints.max_name_weight is not None:
        cons.append(y <= constraints.max_name_weight * total)
    for group in _sector_groups(constraints, n):
        cons.append(cp.sum(y[group]) <= constraints.max_sector_weight * total)
    prob = cp.Problem(cp.Minimize(cp.quad_form(y, cp.psd_wrap(sig))), cons)
    used = _solve(prob, solver, tolerances=_STANDARD)
    yv = np.maximum(np.asarray(y.value, dtype=np.float64), 0.0)
    total_value = float(yv.sum())
    if not np.isfinite(total_value) or total_value <= 0.0:
        raise OptimizeError("max_diversification returned an all-zero book")
    weights = yv / total_value
    projected = False
    if _breaches_caps(weights, constraints, n):
        weights = project_onto_caps(weights, constraints, solver)
        projected = True
    return _feasible_result(
        "max_div",
        weights,
        sig,
        constraints,
        projected=projected,
        elapsed=time.perf_counter() - t0,
        solver=used,
    )


def _quasi_diagonalise(link: Mat) -> list[int]:
    """López de Prado (2016, Code snippet 1): the clustered order of the original items."""
    link_int = np.asarray(link, dtype=np.int64)
    sort_ix = [int(link_int[-1, 0]), int(link_int[-1, 1])]
    num_items = int(link_int[-1, 3])
    while max(sort_ix) >= num_items:
        expanded: list[int] = []
        for value in sort_ix:
            if value >= num_items:
                row = link_int[value - num_items]
                expanded.extend([int(row[0]), int(row[1])])
            else:
                expanded.append(value)
        sort_ix = expanded
    return sort_ix


def _cluster_variance(cov: Mat, members: Sequence[int]) -> float:
    sub = cov[np.ix_(members, members)]
    variance = np.diag(sub)
    if np.any(variance <= 0.0):
        raise ValueError("hierarchical_risk_parity: a cluster has a non-positive variance")
    inverse = 1.0 / variance
    inverse = inverse / inverse.sum()
    return float(inverse @ sub @ inverse)


def hierarchical_risk_parity(
    sigma: Mat, *, constraints: Constraints, solver: str = "CLARABEL"
) -> OptResult:
    """HRP of López de Prado (2016): correlation distances, single linkage, recursive bisection."""
    from scipy.cluster.hierarchy import linkage  # noqa: PLC0415 - quant's numeric stack
    from scipy.spatial.distance import pdist  # noqa: PLC0415 - quant's numeric stack

    sig = _sym_psd(np.asarray(sigma, dtype=np.float64))
    n = sig.shape[0]
    if n <= 1:
        raise ValueError("hierarchical_risk_parity needs at least two assets")
    t0 = time.perf_counter()
    std = np.sqrt(np.diag(sig))
    if np.any(~np.isfinite(std)) or np.any(std <= 0.0):
        raise ValueError("hierarchical_risk_parity: every asset needs a positive variance")
    corr = sig / np.outer(std, std)
    corr = np.clip(corr, -1.0, 1.0)
    np.fill_diagonal(corr, 1.0)
    distance = np.sqrt(0.5 * (1.0 - corr))
    # The paper's stage 1: Euclidean distance between the columns of D, then single linkage.
    condensed = pdist(distance.T, metric="euclidean")
    link = linkage(condensed, method="single")
    order = _quasi_diagonalise(link)
    weights = np.ones(n, dtype=np.float64)
    clusters = [order]
    while clusters:
        split: list[list[int]] = []
        for cluster in clusters:
            if len(cluster) > 1:
                mid = len(cluster) // 2
                split.extend([cluster[:mid], cluster[mid:]])
        clusters = split
        for index in range(0, len(clusters), 2):
            left, right = clusters[index], clusters[index + 1]
            left_var = _cluster_variance(sig, left)
            right_var = _cluster_variance(sig, right)
            total = left_var + right_var
            alpha = 1.0 - left_var / total if total > 0.0 else 0.5
            weights[left] *= alpha
            weights[right] *= 1.0 - alpha
    total_weight = float(weights.sum())
    if not np.isfinite(total_weight) or total_weight <= 0.0:
        raise OptimizeError("hierarchical_risk_parity returned an all-zero book")
    weights = weights / total_weight
    projected = False
    if _breaches_caps(weights, constraints, n):
        weights = project_onto_caps(weights, constraints, solver)
        projected = True
    return _feasible_result(
        "hrp",
        weights,
        sig,
        constraints,
        projected=projected,
        elapsed=time.perf_counter() - t0,
        solver="scipy+projection",
    )


def min_cvar(
    scenarios: Mat,
    sigma: Mat,
    *,
    beta: float = 0.95,
    constraints: Constraints,
    solver: str = "CLARABEL",
) -> OptResult:
    """Minimum CVaR (Rockafellar & Uryasev 2000, eq. 17) under the caps.

    ``min_{w,a} a + 1/(q(1-beta)) sum_k u_k`` with ``u_k >= 0``, ``u_k >= -w'y_k - a`` and
    the usual fully-invested / long-only / cap constraints.
    """
    if not 0.0 < beta < 1.0:
        raise ValueError(f"min_cvar: beta must be in (0, 1), got {beta!r}")
    returns = np.asarray(scenarios, dtype=np.float64)
    if returns.ndim != _MATRIX_NDIM or returns.shape[0] <= 1:
        raise ValueError(f"min_cvar needs a (q>=2, n) scenario matrix, got {returns.shape}")
    q, n = returns.shape
    sig = _sym_psd(np.asarray(sigma, dtype=np.float64))
    if sig.shape != (n, n):
        raise ValueError("min_cvar: scenarios and covariance disagree on n")
    t0 = time.perf_counter()
    w = cp.Variable(n)
    a = cp.Variable()
    u = cp.Variable(q, nonneg=True)
    cons = [*_w_constraints(w, constraints, n), u >= -returns @ w - a]
    prob = cp.Problem(cp.Minimize(a + (1.0 / (q * (1.0 - beta))) * cp.sum(u)), cons)
    used = _solve(prob, solver, tolerances=_STANDARD)
    weights = np.maximum(np.asarray(w.value, dtype=np.float64), 0.0)
    total = float(weights.sum())
    if not np.isfinite(total) or total <= 0.0:
        raise OptimizeError("min_cvar returned an all-zero book")
    weights = weights / total
    projected = False
    if _breaches_caps(weights, constraints, n):
        weights = project_onto_caps(weights, constraints, solver)
        projected = True
    return _feasible_result(
        "min_cvar",
        weights,
        sig,
        constraints,
        projected=projected,
        elapsed=time.perf_counter() - t0,
        solver=used,
    )


def ensemble(vectors: Sequence[Vec]) -> Vec:
    """The simple arithmetic mean of feasible weight vectors (Ang & Kim 2026)."""
    if not vectors:
        raise ValueError("ensemble needs at least one weight vector")
    arrays = [np.asarray(vector, dtype=np.float64) for vector in vectors]
    size = arrays[0].size
    if any(array.size != size for array in arrays):
        raise ValueError("ensemble: every weight vector must have the same length")
    stacked = np.vstack(arrays)
    return np.asarray(stacked.mean(axis=0), dtype=np.float64)
