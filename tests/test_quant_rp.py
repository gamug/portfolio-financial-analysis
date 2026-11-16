"""Round-4 risk-parity re-solve, hand-computed on a diagonal covariance (V4 plan §2b)."""

from __future__ import annotations

import numpy as np
import pytest

from quant.optimize import Constraints, risk_parity_slsqp


def _constraints(names: list[int], *, cap: float = 1.0) -> Constraints:
    return Constraints(
        max_name_weight=cap,
        max_sector_weight=None,
        sector_of=None,
        asset_ids=names,
    )


def test_diagonal_covariance_gives_weights_proportional_to_inverse_volatility() -> None:
    sigmas = np.array([0.10, 0.20, 0.40])
    result = risk_parity_slsqp(np.diag(sigmas**2), constraints=_constraints([1, 2, 3]))
    expected = (1.0 / sigmas) / (1.0 / sigmas).sum()
    assert np.allclose(result.weights, expected, atol=1e-8)
    # equal risk contributions: RC_i / (sum RC / n) is 1 for every name
    contributions = result.weights * (np.diag(sigmas**2) @ result.weights)
    relative = contributions / (contributions.sum() / 3.0)
    assert np.allclose(relative, 1.0, atol=1e-6)


def test_an_infeasible_cap_raises_instead_of_returning_a_book() -> None:
    with pytest.raises(ValueError):
        risk_parity_slsqp(np.eye(3), constraints=_constraints([1, 2, 3], cap=0.05))


def test_a_sector_cap_can_still_be_met() -> None:
    # unconstrained inverse-vol weights concentrate in sector 1 (sigma 0.05); the 0.40 cap
    # binds and the remaining 0.60 is split between the two higher-vol sectors.
    names = list(range(1, 7))
    sigmas = np.array([0.05, 0.05, 0.30, 0.30, 0.50, 0.50])
    result = risk_parity_slsqp(
        np.diag(sigmas**2),
        constraints=Constraints(
            max_name_weight=0.50,
            max_sector_weight=0.40,
            sector_of={1: 1, 2: 1, 3: 2, 4: 2, 5: 3, 6: 3},
            asset_ids=names,
        ),
    )
    assert min(result.weights) >= 0.0
    assert abs(float(result.weights[:2].sum()) - 0.40) < 1e-6
    assert result.weights[2:4].sum() <= 0.40 + 1e-8
    assert result.weights[4:].sum() <= 0.40 + 1e-8
    assert abs(float(result.weights.sum()) - 1.0) < 1e-8
