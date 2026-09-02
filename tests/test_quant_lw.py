"""Ledoit-Wolf shrinkage covariance + mu estimators."""

from __future__ import annotations

import numpy as np
import pytest

from quant.risk import (
    equilibrium_returns,
    historical_mean,
    james_stein_mean,
    ledoit_wolf_covariance,
    nearest_psd,
    sample_covariance,
)


def _returns(t: int = 400, n: int = 5, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    factor = rng.normal(0, 0.01, size=(t, 1))
    idio = rng.normal(0, 0.008, size=(t, n))
    loadings = np.linspace(0.6, 1.4, n)
    return factor * loadings + idio


def test_lw_blend_identity_and_bounds() -> None:
    x = _returns()
    sigma, delta = ledoit_wolf_covariance(x, annualize=False)
    assert 0.0 <= delta <= 1.0

    # recompute S (MLE) and the constant-correlation target F independently
    xc = x - x.mean(axis=0, keepdims=True)
    s = (xc.T @ xc) / x.shape[0]
    var = np.diag(s)
    std = np.sqrt(var)
    n = x.shape[1]
    r_bar = (np.sum(s / np.outer(std, std)) - n) / (n * (n - 1))
    f = r_bar * np.outer(std, std)
    np.fill_diagonal(f, var)

    expected = delta * f + (1.0 - delta) * s
    assert np.allclose(sigma, expected, atol=1e-9)
    assert np.allclose(sigma, sigma.T)
    assert np.linalg.eigvalsh(sigma).min() > -1e-10


def test_lw_annualizes_and_stays_psd() -> None:
    x = _returns()
    daily, _ = ledoit_wolf_covariance(x, annualize=False)
    annual, _ = ledoit_wolf_covariance(x, annualize=True, periods_per_year=252)
    assert np.allclose(np.diag(annual), np.diag(daily) * 252, rtol=1e-6)
    assert np.linalg.eigvalsh(annual).min() > -1e-10


def test_lw_more_shrinkage_when_data_is_scarce() -> None:
    _, d_scarce = ledoit_wolf_covariance(_returns(t=40, n=12, seed=2))
    _, d_ample = ledoit_wolf_covariance(_returns(t=2000, n=12, seed=2))
    assert d_scarce > d_ample
    assert d_scarce > 0.3


def test_diagonal_target_is_diagonal() -> None:
    x = _returns()
    sigma, delta = ledoit_wolf_covariance(x, target="diagonal", annualize=False)
    xc = x - x.mean(axis=0, keepdims=True)
    s = (xc.T @ xc) / x.shape[0]
    off = sigma - np.diag(np.diag(sigma))
    s_off = s - np.diag(np.diag(s))
    # off-diagonals are shrunk toward zero by (1 - delta)
    assert np.allclose(off, (1.0 - delta) * s_off, atol=1e-9)


def test_nearest_psd_lifts_negative_eigenvalues() -> None:
    bad = np.array([[1.0, 0.9, 0.9], [0.9, 1.0, 0.9], [0.9, 0.9, -0.5]])
    fixed = nearest_psd(bad)
    assert np.linalg.eigvalsh(fixed).min() >= 0.0
    assert np.allclose(fixed, fixed.T)


def test_sample_covariance_matches_numpy() -> None:
    x = _returns(seed=3)
    got = sample_covariance(x, annualize=False)
    xc = x - x.mean(axis=0, keepdims=True)
    assert np.allclose(got, (xc.T @ xc) / x.shape[0], atol=1e-9)


def test_mu_estimators_shapes_and_shrinkage() -> None:
    x = _returns(seed=4)
    hist = historical_mean(x, periods_per_year=252)
    js = james_stein_mean(x, periods_per_year=252)
    assert hist.shape == (x.shape[1],)
    assert js.shape == (x.shape[1],)
    # James-Stein pulls the spread in toward the grand mean
    assert np.std(js) <= np.std(hist) + 1e-12

    sigma, _ = ledoit_wolf_covariance(x)
    caps = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    eq = equilibrium_returns(sigma, caps, risk_aversion=2.5)
    assert eq.shape == (x.shape[1],)
    assert np.all(np.isfinite(eq))
    # Pi = lambda * Sigma @ w_mkt, exactly
    w = caps / caps.sum()
    assert np.allclose(eq, 2.5 * (sigma @ w), atol=1e-12)


def test_lw_handles_a_zero_variance_column() -> None:
    x = _returns(n=4, seed=5)
    x[:, 2] = 0.0  # a constant column
    sigma, delta = ledoit_wolf_covariance(x)
    assert np.all(np.isfinite(sigma))
    assert 0.0 <= delta <= 1.0
    assert np.linalg.eigvalsh(sigma).min() > -1e-9


def test_lw_returns_pair() -> None:
    out = ledoit_wolf_covariance(_returns())
    assert isinstance(out, tuple)
    assert len(out) == 2
    assert isinstance(out[1], float)
    with pytest.raises(ValueError, match="return matrix"):
        ledoit_wolf_covariance(np.empty((0, 0)))
