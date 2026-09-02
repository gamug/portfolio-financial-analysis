"""Covariance + expected-return estimators for the mean-variance optimizer.

Covariance: Ledoit-Wolf (2004) linear shrinkage of the sample covariance toward a
constant-correlation target (``diagonal`` target also available), hand-rolled --
no scikit-learn. Expected returns: raw historical mean, a James-Stein shrink
toward the grand mean (the default -- raw-mean tangency is unstable), or the
reverse-optimized equilibrium return from cap weights.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import numpy.typing as npt

Mat = npt.NDArray[np.float64]
Vec = npt.NDArray[np.float64]

CovTarget = Literal["constant_correlation", "diagonal"]
MuModel = Literal["hist_mean", "james_stein", "equilibrium"]

_MATRIX_NDIM = 2
_MIN_ROWS = 2  # a covariance needs at least two observations
_MIN_ASSETS_FOR_CORR = 2  # 1-asset panel has no correlation target


def nearest_psd(sigma: Mat, *, eig_floor: float = 1e-10) -> Mat:
    """Symmetrize and floor eigenvalues so cvxpy gets a clean SPD matrix."""
    s = 0.5 * (sigma + sigma.T)
    w, v = np.linalg.eigh(s)
    w = np.maximum(w, eig_floor)
    out = (v * w) @ v.T
    return 0.5 * (out + out.T)


def sample_covariance(returns: Mat, *, annualize: bool = True, periods_per_year: int = 252) -> Mat:
    x = np.asarray(returns, dtype=np.float64)
    xc = x - x.mean(axis=0, keepdims=True)
    s = (xc.T @ xc) / x.shape[0]
    if annualize:
        s = s * periods_per_year
    return nearest_psd(s)


def ledoit_wolf_covariance(
    returns: Mat,
    *,
    target: CovTarget = "constant_correlation",
    annualize: bool = True,
    periods_per_year: int = 252,
) -> tuple[Mat, float]:
    """Return ``(Sigma, delta)`` -- the shrunk covariance and the shrinkage
    intensity ``delta in [0, 1]``. ``Sigma = delta * F + (1 - delta) * S`` before
    annualization / PSD projection."""
    x = np.asarray(returns, dtype=np.float64)
    if x.ndim != _MATRIX_NDIM or x.shape[0] < _MIN_ROWS or x.shape[1] < 1:
        raise ValueError(f"need a (T>=2, N>=1) return matrix, got {x.shape}")
    t, n = x.shape
    xc = x - x.mean(axis=0, keepdims=True)
    s = (xc.T @ xc) / t
    var = np.diag(s).copy()
    std = np.sqrt(var)
    std = np.where(std > 0, std, 1e-12)

    if target == "diagonal" or n < _MIN_ASSETS_FOR_CORR:
        prior: Mat = np.diag(var)
        r_bar = 0.0
    else:
        r_bar = (np.sum(s / np.outer(std, std)) - n) / (n * (n - 1))
        prior = r_bar * np.outer(std, std)
        np.fill_diagonal(prior, var)

    xc2 = xc**2
    pi_mat = (xc2.T @ xc2) / t - s**2
    pi_hat = float(pi_mat.sum())

    if target == "diagonal" or n < _MIN_ASSETS_FOR_CORR:
        rho_hat = float(np.diag(pi_mat).sum())
    else:
        xc3 = xc**3
        theta_ii = (xc3.T @ xc) / t - var[:, None] * s  # E[(x_i^2 - S_ii)(x_i x_j - S_ij)]
        theta_jj = (xc.T @ xc3) / t - var[None, :] * s  # E[(x_j^2 - S_jj)(x_i x_j - S_ij)]
        cross = 0.5 * (np.outer(1.0 / std, std) * theta_ii + np.outer(std, 1.0 / std) * theta_jj)
        np.fill_diagonal(cross, 0.0)
        rho_hat = float(np.diag(pi_mat).sum() + r_bar * cross.sum())

    gamma_hat = float(np.sum((prior - s) ** 2))
    kappa = (pi_hat - rho_hat) / gamma_hat if gamma_hat > 0 else 0.0
    delta = float(min(1.0, max(0.0, kappa / t)))

    shrunk = delta * prior + (1.0 - delta) * s
    if annualize:
        shrunk = shrunk * periods_per_year
    return nearest_psd(shrunk), delta


def historical_mean(returns: Mat, *, annualize: bool = True, periods_per_year: int = 252) -> Vec:
    mu = np.asarray(returns, dtype=np.float64).mean(axis=0)
    return mu * periods_per_year if annualize else mu


def james_stein_mean(returns: Mat, *, annualize: bool = True, periods_per_year: int = 252) -> Vec:
    """Shrink the sample mean toward the grand mean (Jorion-style intensity)."""
    x = np.asarray(returns, dtype=np.float64)
    t, n = x.shape
    mu_hat = x.mean(axis=0)
    mu0 = float(mu_hat.mean())
    dispersion = float(np.sum((mu_hat - mu0) ** 2))
    if dispersion <= 0 or n <= _MIN_ASSETS_FOR_CORR:
        shrunk = np.full(n, mu0)
    else:
        avg_est_var = float(x.var(axis=0, ddof=1).mean() / t)
        w = min(1.0, (n - 2) * avg_est_var / dispersion)
        shrunk = mu0 + (1.0 - w) * (mu_hat - mu0)
    return shrunk * periods_per_year if annualize else shrunk


def equilibrium_returns(
    sigma: Mat, cap_weights: Vec, *, risk_aversion: float, rf: float = 0.0
) -> Vec:
    """Reverse-optimized (Black-Litterman prior) returns: ``Pi = rf + lambda * Sigma * w_mkt``.
    *sigma* is expected annualized; *cap_weights* need not be normalized."""
    w = np.asarray(cap_weights, dtype=np.float64)
    total = w.sum()
    w = w / total if total > 0 else np.full(len(w), 1.0 / len(w))
    return rf + risk_aversion * (np.asarray(sigma, dtype=np.float64) @ w)
