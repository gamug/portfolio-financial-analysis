"""Final-round measurement: chained performance, tests and overfitting diagnostics (plan §4).

Pure numpy/scipy plus :mod:`cycle.backtest.stats`; no database and no I/O. The functions take
the per-window daily series already exported from the run copies and return aggregates only.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
from scipy.stats import kurtosis, norm, skew

from cycle.backtest import stats

WINDOWS = ("P1", "H1", "P3", "H2", "P2", "H3")
PERIODS = 252
_BLOCK = 16
_CHOOSE = 8
_TWO = 2


def chain(
    runs: Sequence[Mapping[str, Any]],
    arm: str,
) -> tuple[list[str], np.ndarray, np.ndarray]:
    """The six windows' daily series chained in the given calendar order, plus the RF series."""
    dates: list[str] = []
    returns: list[float] = []
    rf: list[float] = []
    for run in runs:
        daily = run["daily"]
        curve = run["rf"]
        series = daily[arm]
        for day in sorted(series):
            if dates and day <= dates[-1]:
                raise ValueError(
                    f"window {run['run']['window']} starts on {day}, before {dates[-1]}"
                )
            dates.append(day)
            returns.append(float(series[day]))
            rf.append(float(curve.get(day, 0.0)))
    return dates, np.asarray(returns, dtype=np.float64), np.asarray(rf, dtype=np.float64)


def _drawdown(equity: np.ndarray) -> tuple[float, int]:
    peak = np.maximum.accumulate(equity)
    drawdown = 1.0 - equity / peak
    under = equity < peak
    longest = current = 0
    for value in under:
        current = current + 1 if value else 0
        longest = max(longest, current)
    return float(drawdown.max()), longest


def _ols_alpha_beta(y: np.ndarray, x: np.ndarray) -> tuple[float, float, float, float]:
    """OLS ``y = alpha + beta x``: alpha, beta, plain t(alpha), Newey-West t(alpha)."""
    n = y.size
    design = np.column_stack([np.ones(n), x])
    coefficients = np.linalg.lstsq(design, y, rcond=None)[0]
    residuals = y - design @ coefficients
    sigma2 = float(residuals @ residuals) / (n - 2)
    covariance = sigma2 * np.linalg.inv(design.T @ design)
    t_plain = float(coefficients[0] / math.sqrt(covariance[0, 0]))
    lags = max(1, math.floor(4.0 * (n / 100.0) ** (2.0 / 9.0)))
    meat = (design * residuals[:, None]).T @ (design * residuals[:, None])
    for lag in range(1, lags + 1):
        weight = 1.0 - lag / (lags + 1.0)
        product = (design[lag:] * residuals[lag:, None]).T @ (
            design[:-lag] * residuals[:-lag, None]
        )
        meat += weight * (product + product.T)
    nw = np.linalg.inv(design.T @ design) @ meat @ np.linalg.inv(design.T @ design)
    t_nw = float(coefficients[0] / math.sqrt(max(nw[0, 0], 0.0)))
    return float(coefficients[0]), float(coefficients[1]), t_plain, t_nw


def _capture(returns: np.ndarray, spy: np.ndarray) -> tuple[float, float, float, float]:
    """Up/down capture: geometric and arithmetic mean on SPY-up / SPY-down sessions."""
    up = spy > 0.0
    down = spy < 0.0
    if not up.any() or not down.any():
        raise ValueError("the SPY series needs at least one up and one down session")

    def geometric(values: np.ndarray) -> float:
        return float(np.prod(1.0 + values) ** (1.0 / values.size) - 1.0)

    up_geo = geometric(returns[up]) / geometric(spy[up])
    down_geo = geometric(returns[down]) / geometric(spy[down])
    up_arith = float(np.mean(returns[up]) / np.mean(spy[up]))
    down_arith = float(np.mean(returns[down]) / np.mean(spy[down]))
    return up_geo, down_geo, up_arith, down_arith


def performance(
    returns: np.ndarray,
    rf: np.ndarray,
    spy: np.ndarray,
    ew: np.ndarray,
) -> dict[str, float | int]:
    """Plan §4(b): the full-period metrics of one chained series."""
    n = returns.size
    if not (rf.size == spy.size == ew.size == n):
        raise ValueError("returns, rf, SPY and EW must be aligned")
    equity = np.concatenate([[1.0], np.cumprod(1.0 + returns)])
    cagr = float(equity[-1] ** (PERIODS / n) - 1.0)
    volatility = float(np.std(returns, ddof=1) * math.sqrt(PERIODS))
    excess = returns - rf
    sharpe = float(np.mean(excess) / np.std(excess, ddof=1) * math.sqrt(PERIODS))
    downside = np.minimum(excess, 0.0)
    sortino = float(
        np.mean(excess) * PERIODS / (math.sqrt(np.mean(downside**2)) * math.sqrt(PERIODS))
    )
    mdd, longest = _drawdown(equity)
    calmar = cagr / mdd if mdd > 0.0 else math.nan
    quantile = float(np.quantile(returns, 0.05))
    cvar = float(-np.mean(returns[returns <= quantile]))
    alpha, beta, t_plain, t_nw = _ols_alpha_beta(excess, spy - rf)
    up_geo, down_geo, up_arith, down_arith = _capture(returns, spy)
    ir_spy = float(np.mean(returns - spy) / np.std(returns - spy, ddof=1) * math.sqrt(PERIODS))
    ir_ew = float(np.mean(returns - ew) / np.std(returns - ew, ddof=1) * math.sqrt(PERIODS))
    return {
        "sessions": n,
        "years": n / PERIODS,
        "cagr": cagr,
        "vol": volatility,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "mdd": mdd,
        "longest_under_water": longest,
        "cvar95": cvar,
        "beta": beta,
        "alpha": alpha * PERIODS,
        "alpha_t_ols": t_plain,
        "alpha_t_nw": t_nw,
        "up_capture_geo": up_geo,
        "down_capture_geo": down_geo,
        "up_capture_arith": up_arith,
        "down_capture_arith": down_arith,
        "ir_spy": ir_spy,
        "ir_ew": ir_ew,
    }


def test_table(  # noqa: PLR0913 - the plan's four inputs and the bootstrap knobs
    a: np.ndarray,
    b: np.ndarray,
    rf: np.ndarray,
    *,
    block: int = 10,
    n_boot: int = 2999,
    seed: int = 7,
) -> dict[str, float | int]:
    """Plan §4(c): HAC mean difference, JK-Memmel, circular block bootstrap ΔMDD, min. detectable."""
    hac_t, hac_p = stats.hac_mean_diff(a, b)
    jk_z, jk_p = stats.memmel_jk(a, b, rf)
    mdd_diff, mdd_p = stats.bootstrap_mdd_diff(a, b, block=block, n_boot=n_boot, seed=seed)
    difference = a - b
    mean_difference = float(np.mean(difference))
    lags = max(1, math.floor(4.0 * (a.size / 100.0) ** (2.0 / 9.0)))
    # The HAC standard error of the mean difference, from its t statistic.
    standard_error = float("nan") if hac_t == 0.0 else mean_difference / hac_t
    detectable = 2.8 * abs(standard_error) * PERIODS
    return {
        "n": int(a.size),
        "mean_diff_annual": mean_difference * PERIODS,
        "hac_t": hac_t,
        "hac_p": hac_p,
        "jk_z": jk_z,
        "jk_p": jk_p,
        "mdd_diff": mdd_diff,
        "bootstrap_p": mdd_p,
        "min_detectable_annual": detectable,
        "lags": lags,
    }


# -- overfitting diagnostics -------------------------------------------------------------------


def _sharpe_columns(matrix: np.ndarray) -> np.ndarray:
    mean = matrix.mean(axis=0)
    std = matrix.std(axis=0, ddof=1)
    return np.divide(mean, std, out=np.zeros_like(mean), where=std > 0.0)


def _calmar_columns(matrix: np.ndarray) -> np.ndarray:
    out = np.zeros(matrix.shape[1], dtype=np.float64)
    for column in range(matrix.shape[1]):
        equity = np.concatenate([[1.0], np.cumprod(1.0 + matrix[:, column])])
        mdd, _ = _drawdown(equity)
        cagr = equity[-1] ** (PERIODS / matrix.shape[0]) - 1.0
        out[column] = cagr / mdd if mdd > 0.0 else 0.0
    return out


def cscv_pbo(
    returns: np.ndarray,
    *,
    blocks: int = _BLOCK,
    metric: str = "sharpe",
) -> dict[str, float | int]:
    """Bailey et al. (2017) CSCV PBO with S contiguous blocks and all C(S, S/2) splits."""
    if returns.ndim != _TWO or returns.shape[0] < blocks * _TWO:
        raise ValueError(f"cscv_pbo needs a (T>=2S, N) matrix, got {returns.shape}")
    if metric not in ("sharpe", "calmar"):
        raise ValueError(f"unknown metric {metric!r}")
    from itertools import combinations  # noqa: PLC0415 - only this diagnostic needs it

    chosen = _sharpe_columns if metric == "sharpe" else _calmar_columns
    n = returns.shape[1]
    edges = np.linspace(0, returns.shape[0], blocks + 1, dtype=int)
    chunks = [returns[edges[i] : edges[i + 1]] for i in range(blocks)]
    half = blocks // 2
    logits: list[float] = []
    in_sample: list[float] = []
    out_sample: list[float] = []
    for chosen_blocks in combinations(range(blocks), half):
        mask = np.zeros(blocks, dtype=bool)
        mask[list(chosen_blocks)] = True
        is_data = np.concatenate([chunks[i] for i in range(blocks) if mask[i]])
        oos_data = np.concatenate([chunks[i] for i in range(blocks) if not mask[i]])
        is_values = chosen(is_data)
        oos_values = chosen(oos_data)
        best = int(np.argmax(is_values))
        rank = 1 + int(np.sum(oos_values < oos_values[best]))
        logits.append(math.log(rank / (n + 1.0 - rank)))
        in_sample.append(float(is_values[best]) * math.sqrt(PERIODS))
        out_sample.append(float(oos_values[best]) * math.sqrt(PERIODS))
    logit_array = np.asarray(logits)
    return {
        "blocks": blocks,
        "splits": int(logit_array.size),
        "pbo": float(np.mean(logit_array <= 0.0)),
        "mean_is_sharpe": float(np.mean(in_sample)),
        "mean_oos_sharpe": float(np.mean(out_sample)),
    }


def _sr_variance(returns: np.ndarray) -> float:
    """Variance of the per-session Sharpe estimates across the trial columns."""
    mean = returns.mean(axis=0)
    std = returns.std(axis=0, ddof=1)
    sr = np.divide(mean, std, out=np.zeros_like(mean), where=std > 0.0)
    return float(np.var(sr, ddof=1))


def expected_max_sharpe(n_trials: int, sr_std: float) -> float:
    """Bailey et al. (2014), Proposition 1 / Theorem 2, with the Euler-Mascheroni constant."""
    if n_trials < _TWO:
        return 0.0
    gamma = 0.5772156649015329
    first = norm.ppf(1.0 - 1.0 / n_trials)
    second = norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    return float(sr_std * ((1.0 - gamma) * first + gamma * second))


def deflated_sharpe(
    returns: np.ndarray,
    *,
    n_trials: int,
    sr_std: float,
) -> dict[str, float]:
    """Bailey & López de Prado (2014), eq. (2)."""
    excess = np.asarray(returns, dtype=np.float64)
    n = excess.size
    sr_hat = float(np.mean(excess) / np.std(excess, ddof=1))
    skewness = float(skew(excess, bias=False))
    kurt = float(kurtosis(excess, fisher=False, bias=False))
    sr_zero = expected_max_sharpe(n_trials, sr_std)
    denominator = math.sqrt(max(1.0 - skewness * sr_hat + (kurt - 1.0) / 4.0 * sr_hat**2, 1e-12))
    dsr = float(norm.cdf((sr_hat - sr_zero) * math.sqrt(n - 1) / denominator))
    return {
        "sr_daily": sr_hat,
        "skew": skewness,
        "kurtosis": kurt,
        "sr_zero_daily": sr_zero,
        "dsr": dsr,
        "n_trials": n_trials,
    }


def stationary_indices(rng: np.random.Generator, n: int, q: float) -> np.ndarray:
    """Politis & Romano (1994) stationary bootstrap indices: geometric blocks, mean 1/q."""
    if not 0.0 < q <= 1.0:
        raise ValueError(f"q must be in (0, 1], got {q!r}")
    indices = np.empty(n, dtype=np.int64)
    indices[0] = int(rng.integers(0, n))
    for i in range(1, n):
        if rng.random() < q:
            indices[i] = int(rng.integers(0, n))
        else:
            indices[i] = (indices[i - 1] + 1) % n
    return indices


def reality_check(
    differentials: np.ndarray,
    *,
    n_boot: int = 1000,
    q: float = 0.1,
    seed: int = 7,
) -> dict[str, float | int]:
    """White (2000) Reality Check with the stationary bootstrap (Politis & Romano 1994).

    ``differentials[t, k]`` is model k's return minus the benchmark's on session t. The
    statistic is ``V = max_k sqrt(n) mean_k``; the bootstrap is centred on each model's own
    mean. The p-value is the fraction of resamples whose centred maximum reaches V.
    """
    data = np.asarray(differentials, dtype=np.float64)
    if data.ndim != _TWO or data.shape[0] < _TWO:
        raise ValueError(f"reality_check needs a (T>=2, K) matrix, got {data.shape}")
    n, _k = data.shape
    means = data.mean(axis=0)
    observed = float(np.max(math.sqrt(n) * means))
    best = int(np.argmax(means))
    rng = np.random.default_rng(seed)
    hits = 0
    for _ in range(n_boot):
        indices = stationary_indices(rng, n, q)
        boot = data[indices].mean(axis=0)
        statistic = float(np.max(math.sqrt(n) * (boot - means)))
        hits += statistic >= observed
    return {
        "best_model": best,
        "best_mean_annual": float(means[best]) * PERIODS,
        "statistic": observed,
        "p_value": (1.0 + hits) / (1.0 + n_boot),
        "n_boot": n_boot,
        "q": q,
    }


def walk_forward(
    window_metrics: Mapping[str, Sequence[Mapping[str, float]]],
) -> list[dict[str, float | int | str]]:
    """Plan §4(e)5: after window k, pick the highest R over 1..k and rank it in window k+1.

    ``window_metrics[configuration]`` is a sequence in calendar order of dicts with
    ``cumulative_return`` and ``max_drawdown`` (one per window). R over windows 1..k is the
    mean cumulative divided by the mean MDD.
    """
    names = sorted(window_metrics)
    if not names:
        return []
    length = len(window_metrics[names[0]])
    rows: list[dict[str, float | int | str]] = []
    for k in range(2, length):
        scores: list[tuple[float, str]] = []
        for name in names:
            values = window_metrics[name][:k]
            mean_mdd = float(np.mean([v["max_drawdown"] for v in values]))
            mean_cum = float(np.mean([v["cumulative_return"] for v in values]))
            scores.append((mean_cum / mean_mdd if mean_mdd else math.nan, name))
        chosen = max(scores, key=lambda item: item[0])[1]
        next_scores = sorted(
            (
                (
                    float(v[k]["cumulative_return"]) / float(v[k]["max_drawdown"])
                    if v[k]["max_drawdown"]
                    else math.nan,
                    name,
                )
                for name, v in window_metrics.items()
            ),
            reverse=True,
        )
        chosen_rank = next(
            index for index, (_, name) in enumerate(next_scores, start=1) if name == chosen
        )
        rows.append(
            {
                "after_window": k,
                "chosen": chosen,
                "mean_cumulative": float(
                    np.mean([v["cumulative_return"] for v in window_metrics[chosen][:k]])
                ),
                "mean_mdd": float(np.mean([v["max_drawdown"] for v in window_metrics[chosen][:k]])),
                "next_window": WINDOWS[k],
                "next_rank": chosen_rank,
                "n_configurations": len(names),
            }
        )
    return rows
