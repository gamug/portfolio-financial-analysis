"""Final-round satellite weighting methods (plan §3): known answers and feasibility."""

from __future__ import annotations

import numpy as np
import pytest

from quant.optimize import (
    Constraints,
    ensemble,
    equal_weight,
    hierarchical_risk_parity,
    max_diversification,
    min_cvar,
    project_onto_caps,
    volatility_timing,
)

_UNCAPPED = Constraints(max_name_weight=None, max_sector_weight=None)


def test_equal_weight_is_one_over_n() -> None:
    assert np.allclose(equal_weight(4), [0.25, 0.25, 0.25, 0.25])
    assert equal_weight(1).tolist() == [1.0]
    with pytest.raises(ValueError):
        equal_weight(0)


def test_volatility_timing_weights_by_inverse_variance() -> None:
    sigma = np.diag([0.04, 0.09, 0.16])
    expected = np.array([1 / 0.04, 1 / 0.09, 1 / 0.16])
    expected = expected / expected.sum()
    assert np.allclose(volatility_timing(sigma), expected)
    with pytest.raises(ValueError):
        volatility_timing(np.diag([0.0, 0.09]))


def test_max_diversification_diagonal_three_asset_closed_form() -> None:
    # With a diagonal covariance D(w) = sum(x)/||x||, x_i = w_i sigma_i; the optimum is x ∝ 1,
    # i.e. w_i ∝ 1/sigma_i.
    sigma = np.diag([0.04, 0.09, 0.16])
    expected = np.array([1 / 0.2, 1 / 0.3, 1 / 0.4])
    expected = expected / expected.sum()
    result = max_diversification(sigma, constraints=_UNCAPPED)
    assert result.weights.sum() == pytest.approx(1.0, abs=1e-8)
    assert np.allclose(result.weights, expected, atol=2e-5)


def test_max_diversification_respects_the_name_cap() -> None:
    sigma = np.diag([0.04, 0.09, 0.16])
    result = max_diversification(
        sigma, constraints=Constraints(max_name_weight=0.4, max_sector_weight=None)
    )
    assert result.weights.max() <= 0.4 + 1e-8
    assert result.weights.sum() == pytest.approx(1.0, abs=1e-8)
    assert result.projected is False


def test_hrp_on_lopez_de_prado_three_asset_correlation_example() -> None:
    # López de Prado (2016) §2: D_ij = sqrt((1-rho_ij)/2), then the Euclidean distance between
    # the columns of D, single linkage and recursive bisection. With the paper's correlation
    # example and the variances below the hand-computed split is
    # {1,2} vs {3} at 0.7594 / 0.2406 and {1} vs {2} at 0.6923 / 0.3077.
    corr = np.array([[1.0, 0.9, 0.8], [0.9, 1.0, 0.7], [0.8, 0.7, 1.0]])
    vols = np.array([0.2, 0.3, 0.4])
    sigma = corr * np.outer(vols, vols)
    result = hierarchical_risk_parity(sigma, constraints=_UNCAPPED)
    expected = np.array([0.52572456, 0.23365536, 0.24062009])
    assert np.allclose(result.weights, expected, atol=1e-6)
    assert result.weights.sum() == pytest.approx(1.0, abs=1e-9)


def test_hrp_respects_a_name_cap_by_projection() -> None:
    corr = np.array([[1.0, 0.9, 0.8], [0.9, 1.0, 0.7], [0.8, 0.7, 1.0]])
    vols = np.array([0.2, 0.3, 0.4])
    sigma = corr * np.outer(vols, vols)
    result = hierarchical_risk_parity(
        sigma, constraints=Constraints(max_name_weight=0.4, max_sector_weight=None)
    )
    assert result.weights.max() <= 0.4 + 1e-7
    assert result.projected is True


def test_min_cvar_avoids_the_bad_tail() -> None:
    # One asset carries a -10% tail in one of 100 scenarios; the other is a constant +0.1%.
    # At beta = 0.95 the CVaR of the first is far worse, so the LP should hold only the second.
    scenarios = np.column_stack([np.full(100, 0.001), np.full(100, 0.001)])
    scenarios[0, 0] = -0.10
    sigma = np.diag([0.04, 0.09])
    result = min_cvar(scenarios, sigma, beta=0.95, constraints=_UNCAPPED)
    assert result.weights[1] > 0.99
    assert result.weights.sum() == pytest.approx(1.0, abs=1e-7)


def test_min_cvar_respects_the_caps() -> None:
    scenarios = np.column_stack([np.full(50, 0.001), np.full(50, 0.001)])
    scenarios[0, 0] = -0.10
    sigma = np.diag([0.04, 0.09])
    result = min_cvar(
        scenarios,
        sigma,
        beta=0.95,
        constraints=Constraints(max_name_weight=0.6, max_sector_weight=None),
    )
    assert result.weights.max() <= 0.6 + 1e-7
    assert result.weights.sum() == pytest.approx(1.0, abs=1e-7)


def test_projection_lands_on_the_caps_and_is_the_identity_when_feasible() -> None:
    constraints = Constraints(max_name_weight=0.4, max_sector_weight=None)
    raw = np.array([0.6, 0.3, 0.1])
    projected = project_onto_caps(raw, constraints)
    assert np.allclose(projected, [0.4, 0.4, 0.2], atol=1e-5)
    assert projected.max() <= 0.4 + 1e-8
    feasible = np.array([0.25, 0.35, 0.4])
    assert np.array_equal(project_onto_caps(feasible, constraints), feasible)
    assert np.allclose(project_onto_caps(projected, constraints), projected, atol=1e-12)


def test_projection_respects_a_sector_cap() -> None:
    constraints = Constraints(
        max_name_weight=1.0,
        max_sector_weight=0.5,
        sector_of={1: 0, 2: 0, 3: 1},
        asset_ids=[1, 2, 3],
    )
    projected = project_onto_caps(np.array([0.4, 0.4, 0.2]), constraints)
    assert projected[:2].sum() <= 0.5 + 1e-8


def test_the_ensemble_average_is_feasible() -> None:
    vectors = [
        np.array([0.3, 0.4, 0.3]),
        np.array([0.4, 0.3, 0.3]),
        np.array([0.2, 0.4, 0.4]),
    ]
    averaged = ensemble(vectors)
    assert averaged.sum() == pytest.approx(1.0)
    assert averaged.max() <= 0.4 + 1e-12
    with pytest.raises(ValueError):
        ensemble([np.array([0.5, 0.5]), np.array([0.2, 0.2, 0.6])])
