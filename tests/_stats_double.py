"""A spec-shaped stand-in for ``cycle.backtest.stats`` (WP-E), for this branch only.

Installed into ``sys.modules`` by the driver test only when the real module is absent.
Nothing under ``src/`` imports it.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence


def summarize(
    net_returns: Sequence[float],
    rf: Sequence[float],
    turnover: Sequence[float],
    cost: Sequence[float],
    periods_per_year: int = 252,
) -> dict[str, float]:
    nav = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in net_returns:
        nav *= 1.0 + value
        peak = max(peak, nav)
        max_drawdown = max(max_drawdown, 1.0 - nav / peak)
    n = len(net_returns)
    cumulative = nav - 1.0
    annualized = (1.0 + cumulative) ** (periods_per_year / n) - 1.0 if n else 0.0
    volatility = statistics.stdev(net_returns) * math.sqrt(periods_per_year) if n > 1 else 0.0
    excess = [r - f for r, f in zip(net_returns, rf, strict=True)]
    std = statistics.stdev(excess) if len(excess) > 1 else 0.0
    sharpe = statistics.mean(excess) / std * math.sqrt(periods_per_year) if std else 0.0
    return {
        "cumulative_return": cumulative,
        "annualized_return": annualized,
        "annualized_vol": volatility,
        "ex_post_sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "total_turnover": float(sum(turnover)),
        "total_cost_bps": float(sum(cost)) * 10_000.0,
        "n_obs": float(n),
    }


def memmel_jk(
    r_a: Sequence[float], r_b: Sequence[float], rf: Sequence[float]
) -> tuple[float, float]:
    a = [x - f for x, f in zip(r_a, rf, strict=True)]
    b = [x - f for x, f in zip(r_b, rf, strict=True)]
    n = len(a)
    mean_a, mean_b = statistics.mean(a), statistics.mean(b)
    sd_a, sd_b = statistics.stdev(a), statistics.stdev(b)
    rho = statistics.correlation(a, b)
    sr_a, sr_b = mean_a / sd_a, mean_b / sd_b
    variance = (2 * (1 - rho) + 0.5 * (sr_a**2 + sr_b**2 - 2 * sr_a * sr_b * rho**2)) / n
    if variance <= 0:
        return 0.0, 1.0
    z = (sr_a - sr_b) / math.sqrt(variance)
    return z, 2 * (1 - statistics.NormalDist().cdf(abs(z)))


def ledoit_wolf_sharpe_diff(  # noqa: PLR0913 - the §8.4 signature
    r_a: Sequence[float],
    r_b: Sequence[float],
    rf: Sequence[float],
    *,
    block: int = 5,
    n_boot: int = 4999,
    seed: int = 20261028,
) -> tuple[float, float]:
    del block, n_boot, seed
    a = [x - f for x, f in zip(r_a, rf, strict=True)]
    b = [x - f for x, f in zip(r_b, rf, strict=True)]
    diff = statistics.mean(a) / statistics.stdev(a) - statistics.mean(b) / statistics.stdev(b)
    return diff, 1.0


def hac_mean_diff(
    r_a: Sequence[float], r_b: Sequence[float], *, lags: int | None = None
) -> tuple[float, float]:
    del lags
    diff = [x - y for x, y in zip(r_a, r_b, strict=True)]
    n = len(diff)
    mean = statistics.mean(diff)
    variance = statistics.variance(diff) / n
    if variance <= 0:
        return 0.0, 1.0
    t = mean / math.sqrt(variance)
    return t, 2 * (1 - statistics.NormalDist().cdf(abs(t)))
