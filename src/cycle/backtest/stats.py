"""Backtest statistics (WP-E; BACKTEST_SPEC §6 and §8.4).

Pure ``numpy`` / ``scipy``: no database, no I/O, no module-level state, and the inputs are never
mutated. The four public functions are the fixed interface WP-A calls:

* :func:`summarize` -- the per-arm, per-segment performance summary.
* :func:`memmel_jk` -- Jobson & Korkie (1981) test of equal Sharpe ratios, Memmel (2003) corrected.
* :func:`ledoit_wolf_sharpe_diff` -- Ledoit & Wolf (2008) studentized circular block bootstrap.
* :func:`hac_mean_diff` -- Newey & West (1987) HAC t-test on the mean return difference.

Conventions that hold everywhere in this module
-----------------------------------------------
* Returns are **simple** per-session decimals (``0.0012`` is +12 bp), not log returns, and the
  risk-free return ``rf`` is the return of the *same* session, in the same units.
* Sharpe ratios are **per session** unless a docstring says otherwise; multiply by
  ``sqrt(periods_per_year)`` to annualize. The bootstrap test works on the per-session value
  (studentizing makes the annualization cancel); :func:`memmel_jk` says why it does not cancel in
  its closed-form variance.
* ``max_drawdown`` is a **positive magnitude**: ``0.25`` means a 25 % peak-to-trough decline.
* Every public function validates its inputs and raises ``ValueError`` rather than return a number
  the data cannot support: series of unequal length, a non-finite value, or fewer than
  :data:`MIN_OBS` (= 30) observations.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from scipy.stats import norm

# The validation floor BACKTEST_PLAN WP-E fixes for every function here. The shortest reporting
# cut (P2a) is far above it, so the floor never binds on real output.
MIN_OBS = 30
_BPS = 10_000.0

# BACKTEST_SPEC §8.4 writes the parameters as ``Sequence[float]``. It also says WP-A passes numpy
# arrays, and a bare ``Sequence`` annotation makes every such call a mypy error at the call site,
# so the alias accepts both: a list/tuple and an ``ndarray``.
Series = Sequence[float] | np.ndarray


# -- validation --------------------------------------------------------------------------------


def _series(name: str, values: Series) -> np.ndarray:
    """*values* as a finite 1-D float array of at least :data:`MIN_OBS` observations."""
    try:
        arr = np.asarray(values, dtype=float)
    except (TypeError, ValueError) as exc:  # a non-numeric element (``None`` becomes ``nan``)
        raise ValueError(f"{name} is not a sequence of numbers: {exc}") from exc
    if arr.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional, got shape {arr.shape}")
    if arr.size < MIN_OBS:
        raise ValueError(f"{name} has {arr.size} observations; at least {MIN_OBS} are needed")
    if not np.all(np.isfinite(arr)):
        raise ValueError(f"{name} contains a non-finite value (nan or inf)")
    return arr


def _same_length(length: int, *named: tuple[str, np.ndarray]) -> None:
    """Every array in *named* must have *length* observations (paired series)."""
    for name, arr in named:
        if arr.size != length:
            raise ValueError(f"{name} has {arr.size} observations, expected {length}")


def _check_periods_per_year(periods_per_year: int) -> None:
    if not isinstance(periods_per_year, int) or periods_per_year <= 0:
        raise ValueError(f"periods_per_year must be a positive integer, got {periods_per_year!r}")


# -- summarize ---------------------------------------------------------------------------------


def summarize(
    net_returns: Series,
    rf: Series,
    turnover: Series,
    cost: Series,
    periods_per_year: int = 252,
) -> dict[str, float]:
    """Performance summary of one book's daily net returns over one segment.

    ``backtest_summary`` columns (§5) that this computes, with their formulas (``n`` sessions,
    ``r_t`` the net return of session ``t``, ``f_t`` the risk-free return of the same session):

    * ``cumulative_return`` = ``prod(1 + r_t) - 1`` (the terminal NAV minus one).
    * ``annualized_return`` = ``(1 + cumulative_return) ** (periods_per_year / n) - 1``
      (geometric, so it is the constant per-session rate that compounds to the same NAV).
    * ``annualized_vol`` = ``std(r, ddof=1) * sqrt(periods_per_year)`` (of the net returns, not
      of the excess returns).
    * ``ex_post_sharpe`` = ``mean(r - f) / std(r - f, ddof=1) * sqrt(periods_per_year)``, i.e. the
      realized (ex-post) Sharpe ratio on daily excess returns. ``nan`` when the excess returns are
      constant (zero standard deviation): the ratio is then undefined, and a constant excess series
      carries no risk-adjusted information. SQLite stores ``nan`` as NULL, so the column is simply
      empty for such a segment.
    * ``max_drawdown`` = ``max_t (1 - NAV_t / max(1, NAV_1..NAV_t))``, where
      ``NAV_t = prod_{s<=t}(1 + r_s)``. Positive; the starting NAV of 1.0 is the first peak, so a
      loss on the first session counts.
    * ``total_turnover`` = ``sum(turnover)`` (the engine records one net order per session, so the
      sum is the segment's traded fraction of NAV).
    * ``total_cost_bps`` = ``sum(cost) * 10_000`` (``cost`` is the per-session return fraction the
      engine charges on execution sessions).

    ``n_obs`` = ``n`` is the one key with no ``backtest_summary`` column: the table leaves the
    observation count implicit, the interface requires it here, and the persistence layer must
    therefore select columns explicitly instead of splatting the dict. ``n_rebalances`` and
    ``n_guard_actions`` are not derivable from these four series and come from the database.

    All four series must have the same length, finite values and ``n >= 30``; otherwise
    ``ValueError``. ``periods_per_year`` must be a positive integer (252 for daily NYSE sessions).
    """
    r = _series("net_returns", net_returns)
    f = _series("rf", rf)
    to = _series("turnover", turnover)
    c = _series("cost", cost)
    _check_periods_per_year(periods_per_year)
    n = r.size
    _same_length(n, ("rf", f), ("turnover", to), ("cost", c))

    growth = float(np.prod(1.0 + r))
    if not np.isfinite(growth) or growth <= 0.0:
        # A simple-return series cannot lose more than 100 % per session; a non-positive growth
        # factor means the input is not a return series and no annualized rate exists.
        raise ValueError(f"net_returns compound to a non-positive growth factor ({growth!r})")

    nav = np.cumprod(1.0 + r)
    peaks = np.maximum.accumulate(np.concatenate(([1.0], nav)))[1:]
    drawdown = np.clip(1.0 - nav / peaks, 0.0, None)

    excess = r - f
    excess_sd = float(np.std(excess, ddof=1))
    sharpe = (
        float(np.mean(excess)) / excess_sd * math.sqrt(periods_per_year)
        if excess_sd > 0.0
        else math.nan
    )

    return {
        "cumulative_return": growth - 1.0,
        "annualized_return": growth ** (periods_per_year / n) - 1.0,
        "annualized_vol": float(np.std(r, ddof=1)) * math.sqrt(periods_per_year),
        "ex_post_sharpe": sharpe,
        "max_drawdown": float(drawdown.max()),
        "total_turnover": float(np.sum(to)),
        "total_cost_bps": float(np.sum(c)) * _BPS,
        "n_obs": float(n),
    }


# -- Sharpe ratio tests ------------------------------------------------------------------------


def _sharpe(x: np.ndarray) -> float:
    """Sample Sharpe ratio ``mean(x) / std(x, ddof=1)`` of one excess-return series."""
    sd = float(np.std(x, ddof=1))
    if sd <= 0.0:
        raise ValueError("an excess-return series has zero standard deviation: Sharpe undefined")
    return float(np.mean(x)) / sd


def memmel_jk(r_a: Series, r_b: Series, rf: Series) -> tuple[float, float]:
    """Two-sided Jobson & Korkie (1981) test of ``H0: SR_a = SR_b``, Memmel (2003) corrected.

    Jobson, J.D. & Korkie, B. (1981), "Performance hypothesis testing with the Sharpe and Treynor
    measures", *Journal of Finance* 36(4), 889-908. CANDIDATE — verify page.
    Memmel, C. (2003), "Performance hypothesis testing with the Sharpe ratio", *Finance Letters*
    1, 21-24. CANDIDATE — verify page.

    With excess returns ``x = r_a - rf`` and ``y = r_b - rf``, per-session Sharpe ratios
    ``SR_a = mean(x) / std(x)`` and ``SR_b = mean(y) / std(y)`` (sample std, ``ddof=1``), Pearson
    correlation ``rho`` of ``x`` and ``y``, and ``n`` observations, the Memmel-corrected variance of
    the difference is::

        V = (1/n) * [ 2 (1 - rho) + 0.5 (SR_a^2 + SR_b^2 - 2 SR_a SR_b rho^2) ]
        z = (SR_a - SR_b) / sqrt(V)

    and the p-value is ``2 * (1 - Phi(|z|))`` with ``Phi`` the standard normal CDF (two-sided).
    ``V`` is the Memmel-corrected variance: the 1981 paper's expression is not used, and the cross
    term carries ``rho**2`` (see the derivation note at the end of this docstring).

    The test assumes i.i.d. normal returns. It uses **per-session** Sharpe ratios: annualizing both
    of them first is not equivalent, because ``2 (1 - rho)`` does not scale with the annualization
    factor and would be swamped. Neither series may have zero variance, and ``n >= 30``;
    otherwise ``ValueError``. Two exactly identical series have ``SR_a = SR_b``, ``rho = 1`` and
    ``V = 0``: the difference is exactly zero and ``(0.0, 1.0)`` is returned.

    Returns ``(z, p)``.

    Derivation of the ``rho**2`` (independent of the paper text): the Sharpe ratio's
    per-observation influence function is
    ``IF_i = (x_i - mu)/sigma - (mu/(2 sigma)) ((x_i - mu)^2/sigma^2 - 1)``. For joint normal
    returns ``Var(IF_a) = 1 + SR_a^2/2``, ``Var(IF_b) = 1 + SR_b^2/2`` and
    ``Cov(IF_a, IF_b) = rho + SR_a SR_b rho^2/2``, so ``n * Var(SR_a - SR_b)`` is exactly the
    bracket above. :func:`ledoit_wolf_sharpe_diff` studentizes the same influence function, but
    with a HAC long-run variance instead of the i.i.d. normal one.
    """
    a = _series("r_a", r_a)
    b = _series("r_b", r_b)
    f = _series("rf", rf)
    n = a.size
    _same_length(n, ("r_b", b), ("rf", f))

    x = a - f
    y = b - f
    sr_a = _sharpe(x)
    sr_b = _sharpe(y)
    rho = float(np.corrcoef(x, y)[0, 1])
    diff = sr_a - sr_b

    variance = (2.0 * (1.0 - rho) + 0.5 * (sr_a**2 + sr_b**2 - 2.0 * sr_a * sr_b * rho**2)) / n
    if variance <= 0.0:
        # Only reachable when the two excess series are the same up to a positive scale with equal
        # Sharpe ratios (rho = 1); the statistic is then 0/0 and the honest answer is "no evidence
        # of a difference". A non-zero difference with zero variance is arithmetically impossible,
        # so a negative variance can only be a rounding artefact.
        if diff == 0.0:
            return 0.0, 1.0
        raise ValueError(f"degenerate Memmel variance {variance!r} with a non-zero difference")

    z = diff / math.sqrt(variance)
    return z, float(2.0 * norm.sf(abs(z)))


def _sharpe_influence(x: np.ndarray) -> np.ndarray:
    """Influence function of the Sharpe ratio ``theta = mu / sigma``, one value per observation.

    ``IF_i = (x_i - mu)/sigma - (mu / (2 sigma)) * ((x_i - mu)^2 / sigma^2 - 1)``,
    the first-order expansion of ``theta`` in ``(mu, sigma^2)``. Sample moments are plugged in;
    Ledoit & Wolf (2008) state the population version, the difference is ``O(1/n)``.
    """
    mu = float(np.mean(x))
    sigma = float(np.std(x, ddof=1))
    if sigma <= 0.0:
        raise ValueError("an excess-return series has zero standard deviation: Sharpe undefined")
    z = (x - mu) / sigma
    return z - 0.5 * (mu / sigma) * (z * z - 1.0)


def _default_lags(n: int) -> int:
    """Newey & West's rule ``floor(4 (n/100) ** (2/9))`` used for every HAC estimate here."""
    rule: float = 4.0 * (n / 100.0) ** (2.0 / 9.0)
    return math.floor(rule)


def _newey_west_variance(z: np.ndarray, lags: int) -> float:
    """Long-run variance of the centred series *z*: Bartlett kernel, ``lags`` autocovariances.

    ``gamma_0 + 2 * sum_{j=1..L} (1 - j/(L+1)) * gamma_j`` with ``gamma_j = (1/n) sum_t z_t z_{t-j}``
    (Newey, W.K. & West, K.D. (1987), *Econometrica* 55(3), 703-708. CANDIDATE — verify page).
    """
    n = z.size
    e = z - np.mean(z)
    total = float(e @ e)
    for j in range(1, lags + 1):
        weight = 1.0 - j / (lags + 1.0)
        total += 2.0 * weight * float(e[j:] @ e[:-j])
    return total / n


def _sharpe_diff_se(x: np.ndarray, y: np.ndarray, lags: int) -> float:
    """HAC standard error of ``SR(x) - SR(y)`` from the influence-function series."""
    variance = _newey_west_variance(_sharpe_influence(x) - _sharpe_influence(y), lags)
    if not np.isfinite(variance) or variance <= 0.0:
        raise ValueError("non-positive HAC variance of the Sharpe difference")
    return math.sqrt(variance / x.size)


def _circular_indices(rng: np.random.Generator, n: int, block: int) -> np.ndarray:
    """One circular block bootstrap draw: ``ceil(n/block)`` blocks of *block* consecutive indices,
    wrapping at *n*, truncated to *n* observations."""
    n_blocks = math.ceil(n / block)
    starts = rng.integers(0, n, size=n_blocks)
    offsets = np.arange(block)
    return ((starts[:, None] + offsets[None, :]) % n).reshape(-1)[:n]


def ledoit_wolf_sharpe_diff(  # noqa: PLR0913 - the signature is fixed by BACKTEST_SPEC §8.4
    r_a: Series,
    r_b: Series,
    rf: Series,
    *,
    block: int = 5,
    n_boot: int = 4999,
    seed: int = 20261028,
) -> tuple[float, float]:
    """Two-sided Ledoit & Wolf (2008) studentized circular block bootstrap for ``H0: SR_a = SR_b``.

    Ledoit, O. & Wolf, M. (2008), "Robust performance hypothesis testing with the Sharpe ratio",
    *Journal of Empirical Finance* 15(5), 850-859. CANDIDATE — verify page.

    Procedure:

    1. ``x = r_a - rf``, ``y = r_b - rf``. ``diff = SR(x) - SR(y)``, per-session Sharpe ratios with
       the sample standard deviation (``ddof=1``).
    2. Studentize with a **plain** HAC standard error of ``diff``: the Sharpe ratio's influence
       function ``_sharpe_influence`` is formed for both series, their difference is taken, and its
       long-run variance is estimated by Newey & West (1987) with a Bartlett kernel and the lag
       ``floor(4 (n/100) ** (2/9))``. This is **not** the VAR(1)-prewhitened HAC estimator of
       Ledoit & Wolf (2008) §3 (Andrews & Monahan 1992); the paper offers both, and the plain
       version is the one declared here. ``T = diff / se``.
    3. ``n_boot`` times: draw one circular block bootstrap sample of the **paired** excess-return
       vectors ``(x_t, y_t)`` (blocks of ``block`` consecutive sessions, wrapping), recompute
       ``diff*`` and its HAC ``se*`` on the draw, and set ``T* = (diff* - diff) / se*``. Resampling
       the pairs preserves the contemporaneous correlation and the serial dependence of both
       series; a replication whose ``se*`` is not strictly positive is discarded (and reported in
       the denominator, so ``p`` stays a frequency over the usable draws).
    4. ``p = (1 + #{|T*| >= |T|}) / (1 + usable)`` -- the add-one bootstrap p-value, so ``p`` is
       never exactly 0 and identical series return exactly ``(0.0, 1.0)``.

    Deterministic for a given ``seed``: the generator is ``numpy.random.default_rng(seed)``
    (PCG64), created inside the call, with no other source of randomness. The returned ``diff`` is
    the per-session (daily) Sharpe difference; multiply by ``sqrt(252)`` to annualize.

    ``block >= 1`` and ``<= n``, ``n_boot >= 1``, both series and ``rf`` of equal length with
    finite values and ``n >= 30``; otherwise ``ValueError``.
    """
    a = _series("r_a", r_a)
    b = _series("r_b", r_b)
    f = _series("rf", rf)
    n = a.size
    _same_length(n, ("r_b", b), ("rf", f))
    if not isinstance(block, int) or not 1 <= block <= n:
        raise ValueError(f"block must be an integer in [1, {n}], got {block!r}")
    if not isinstance(n_boot, int) or n_boot < 1:
        raise ValueError(f"n_boot must be a positive integer, got {n_boot!r}")

    x = a - f
    y = b - f
    diff = _sharpe(x) - _sharpe(y)
    lags = _default_lags(n)
    try:
        se = _sharpe_diff_se(x, y, lags)
    except ValueError:
        if diff == 0.0:
            # The two excess series are identical (same Sharpe, zero variance of the difference):
            # no evidence of a difference, and no bootstrap distribution to draw.
            return 0.0, 1.0
        raise
    t_obs = diff / se

    rng = np.random.default_rng(seed)
    exceed = 0
    usable = 0
    for _ in range(n_boot):
        idx = _circular_indices(rng, n, block)
        xb, yb = x[idx], y[idx]
        try:
            diff_b = _sharpe(xb) - _sharpe(yb)
            se_b = _sharpe_diff_se(xb, yb, lags)
        except ValueError:
            continue
        usable += 1
        if abs((diff_b - diff) / se_b) >= abs(t_obs):
            exceed += 1
    if usable == 0:
        raise ValueError(f"all {n_boot} bootstrap replications were degenerate")

    return diff, (1.0 + exceed) / (1.0 + usable)


# -- Newey-West test on the mean difference -----------------------------------------------------


def hac_mean_diff(r_a: Series, r_b: Series, *, lags: int | None = None) -> tuple[float, float]:
    """Newey & West (1987) HAC t-test of ``H0: E[r_a - r_b] = 0``, two-sided.

    Newey, W.K. & West, K.D. (1987), "A simple, positive semi-definite, heteroskedasticity and
    autocorrelation consistent covariance matrix", *Econometrica* 55(3), 703-708.
    CANDIDATE — verify page.

    With ``d_t = r_a,t - r_b,t``, ``dbar = mean(d)`` and ``e_t = d_t - dbar``::

        lrvar = (1/n) [ sum_t e_t^2 + 2 sum_{j=1..L} (1 - j/(L+1)) sum_t e_t e_{t-j} ]
        se    = sqrt(lrvar / n)
        t     = dbar / se

    ``p = 2 * (1 - Phi(|t|))`` from the standard normal, the HAC asymptotic distribution (no
    small-sample t approximation is applied). ``L`` defaults to ``floor(4 (n/100) ** (2/9))``
    (Newey & West's rule, the same default as the Sharpe test); pass ``lags=0`` for the
    heteroskedasticity-only (White) standard error.

    The risk-free rate cancels in ``r_a - r_b``, so it is not a parameter here. Equal lengths,
    finite values, ``n >= 30``, and ``0 <= lags < n``; otherwise ``ValueError``. Identical series
    (``d`` constant zero) return ``(0.0, 1.0)``; a constant non-zero difference has zero variance
    and an unbounded statistic, so it raises ``ValueError`` instead of reporting ``inf``.

    Returns ``(t, p)``.
    """
    a = _series("r_a", r_a)
    b = _series("r_b", r_b)
    n = a.size
    _same_length(n, ("r_b", b))
    if lags is None:
        lags = _default_lags(n)
    if not isinstance(lags, int) or not 0 <= lags < n:
        raise ValueError(f"lags must be an integer in [0, {n - 1}], got {lags!r}")

    d = a - b
    # A constant series is exactly degenerate: floating-point subtraction would leave residuals
    # around 1e-19 and turn "undefined" into a huge finite t, so test constancy exactly instead.
    if bool(np.all(d == d[0])):
        if d[0] == 0.0:
            return 0.0, 1.0
        raise ValueError("zero HAC variance with a non-zero mean difference: t is unbounded")

    mean = float(np.mean(d))
    variance = _newey_west_variance(d, lags)
    if not np.isfinite(variance) or variance <= 0.0:
        if mean == 0.0:
            return 0.0, 1.0
        raise ValueError("zero HAC variance with a non-zero mean difference: t is unbounded")

    t = mean / math.sqrt(variance / n)
    return t, float(2.0 * norm.sf(abs(t)))
