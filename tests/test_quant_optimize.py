"""cvxpy mean-variance optimizer: closed forms, frontier shape, constraints."""

from __future__ import annotations

from itertools import pairwise

import numpy as np
import pytest

from quant.optimize import (
    Constraints,
    OptimizeError,
    efficient_frontier,
    max_sharpe,
    min_variance,
    target_volatility_portfolio,
)

_TOL = 2e-4
_UNCAPPED = Constraints(max_name_weight=None, max_sector_weight=None)
_OK = ("optimal", "optimal_inaccurate")


def _spd(n: int = 6, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    a = rng.normal(size=(n, n))
    return a @ a.T / n + np.eye(n) * 0.02


def test_min_variance_diagonal_closed_form() -> None:
    sig = np.diag([0.04, 0.09, 0.16])
    res = min_variance(sig, constraints=_UNCAPPED)
    w_closed = np.array([1 / 0.04, 1 / 0.09, 1 / 0.16])
    w_closed /= w_closed.sum()
    assert np.max(np.abs(res.weights - w_closed)) < _TOL
    assert res.weights.sum() == pytest.approx(1.0, abs=1e-6)
    assert res.expected_vol > 0


def test_min_variance_two_asset_correlated_closed_form() -> None:
    s1, s2, rho = 0.2, 0.3, 0.4
    sig = np.array([[s1**2, rho * s1 * s2], [rho * s1 * s2, s2**2]])
    res = min_variance(sig, constraints=_UNCAPPED)
    w1 = (s2**2 - rho * s1 * s2) / (s1**2 + s2**2 - 2 * rho * s1 * s2)
    assert res.weights[0] == pytest.approx(w1, abs=_TOL)


def test_efficient_frontier_is_monotone() -> None:
    sig = _spd(6, seed=1)
    mu = np.linspace(0.03, 0.15, 6)
    rf = 0.02
    pts = [
        p for p in efficient_frontier(mu, sig, k=8, constraints=_UNCAPPED, rf=rf) if p.status in _OK
    ]
    assert len(pts) >= 5
    vols = [p.expected_vol for p in pts]
    rets = [p.expected_return for p in pts]
    assert all(b >= a - 1e-6 for a, b in pairwise(vols))
    assert all(b >= a - 1e-6 for a, b in pairwise(rets))
    # the tangency portfolio has the highest Sharpe of any point on the frontier
    tang = max_sharpe(mu, sig, rf=rf, constraints=_UNCAPPED)
    assert tang.sharpe is not None
    assert tang.sharpe >= max(p.sharpe or -1e9 for p in pts) - 1e-3


def test_target_volatility_hits_target_when_feasible() -> None:
    sig = _spd(5, seed=2)
    mu = np.array([0.04, 0.06, 0.08, 0.10, 0.12])
    mv = min_variance(sig, constraints=_UNCAPPED, mu=mu)
    target = mv.expected_vol * 1.5

    res = target_volatility_portfolio(mu, sig, target, constraints=_UNCAPPED)
    assert res.status in _OK
    assert res.expected_vol <= target + 1e-4
    assert res.expected_vol == pytest.approx(target, abs=5e-3)  # the cap binds


def test_target_volatility_falls_back_below_min_var() -> None:
    sig = _spd(5, seed=2)
    mu = np.array([0.04, 0.06, 0.08, 0.10, 0.12])
    mv = min_variance(sig, constraints=_UNCAPPED, mu=mu)

    infeasible = target_volatility_portfolio(mu, sig, mv.expected_vol * 0.5, constraints=_UNCAPPED)
    assert infeasible.status == "vol_infeasible"
    assert np.allclose(infeasible.weights, mv.weights, atol=1e-6)


def test_box_cap_binds() -> None:
    sig = np.diag([0.01, 0.20, 0.20, 0.20])  # unconstrained min-var wants ~all weight in asset 0
    res = min_variance(sig, constraints=Constraints(max_name_weight=0.4, max_sector_weight=None))
    assert res.weights.max() <= 0.4 + 1e-6
    assert res.weights.sum() == pytest.approx(1.0, abs=1e-6)


def test_sector_cap_binds() -> None:
    # 6 assets, 3 sectors; sigma wants sector 1 (assets 0,1) heavily. 3 * 0.4 >= 1.
    sig = np.diag([0.01, 0.01, 0.25, 0.25, 0.25, 0.25])
    ids = [10, 11, 12, 13, 14, 15]
    sector_of: dict[int, int | None] = {10: 1, 11: 1, 12: 2, 13: 2, 14: 3, 15: 3}
    res = min_variance(
        sig,
        constraints=Constraints(
            max_name_weight=None, max_sector_weight=0.40, sector_of=sector_of, asset_ids=ids
        ),
    )
    assert res.weights[0] + res.weights[1] <= 0.40 + 1e-6
    assert res.weights.sum() == pytest.approx(1.0, abs=1e-6)


def test_infeasible_box_raises() -> None:
    sig = _spd(3, seed=3)
    with pytest.raises(OptimizeError):
        min_variance(sig, constraints=Constraints(max_name_weight=0.2, max_sector_weight=None))
