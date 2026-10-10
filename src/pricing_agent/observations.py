"""Per-(asset, day) price analytics -- the ``price_observation`` series.

Mirrors :mod:`pricing_agent.stats` in spirit: a frozen dataclass plus pure functions
over a ``list[Candle]``. Raw OHLCV stays in ``price_daily``; this module only derives
close-to-close return, Wilder ATR, rolling realized volatility, rolling max drawdown
and simple momentum, plus (``priceobs-v2``, T-070) the 200-day simple moving average, the
5-day log return, the 5-day realized volatility, and the mean and volatility of a 60-day
*baseline* of daily log returns. Warm-up rows (before a window is full) carry ``None`` for the
fields that need history.

The baseline window is declared here, once: ``mu_60d_base`` / ``vol_60d_base`` are the mean and
sample standard deviation (n - 1) of the 60 daily log returns that END ``SHOCK_WINDOW`` (5)
trading days before ``obs_date`` -- returns ``i-64 .. i-5`` for the bar at index ``i``. The
recent window (``i-4 .. i``) is excluded on purpose, so a shock does not dilute its own
baseline. ``vol_5d`` and the baseline volatility are *daily* (not annualized), so their ratio
is dimensionless and ``ret_5d`` is comparable with ``5 * mu_60d_base`` and
``vol_60d_base * sqrt(5)``. Every one of these fields is ``None`` until its window is full.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from pricing_agent.pricing_client import Candle

TRADING_DAYS_PER_YEAR = 252
ATR_PERIOD = 14
VOL_SHORT = 21
VOL_LONG = 90
DRAWDOWN_WINDOW = 90
SMA_LONG = 200
SHOCK_WINDOW = 5
BASELINE_WINDOW = 60
_MOMENTUM_LAGS = {"momentum_21d": 21, "momentum_63d": 63, "momentum_252d": 252}


@dataclass(frozen=True)
class Observation:
    """One trading day's derived analytics for a single asset."""

    obs_date: str
    close: float
    prev_close: float | None
    log_return: float | None
    true_range: float | None
    atr_14: float | None
    realized_vol_21d: float | None
    realized_vol_90d: float | None
    max_drawdown_90d: float | None
    momentum_21d: float | None
    momentum_63d: float | None
    momentum_252d: float | None
    dollar_volume: float | None
    # priceobs-v2 (T-070)
    sma_200: float | None = None
    ret_5d: float | None = None
    vol_5d: float | None = None
    mu_60d_base: float | None = None
    vol_60d_base: float | None = None


def true_range(high: float, low: float, prev_close: float | None) -> float:
    """Classic true range; falls back to the day's range when there is no prior close."""
    if prev_close is None:
        return high - low
    return max(high - low, abs(high - prev_close), abs(prev_close - low))


def _annualized_vol(log_returns: list[float]) -> float | None:
    if len(log_returns) < 2:  # noqa: PLR2004 - stdev needs two points
        return None
    return statistics.stdev(log_returns) * math.sqrt(TRADING_DAYS_PER_YEAR)


def _mean_sd(values: list[float]) -> tuple[float, float] | None:
    """Mean and sample standard deviation (n - 1) of *values*; ``None`` under two points."""
    n = len(values)
    if n < 2:  # noqa: PLR2004 - stdev needs two points
        return None
    mean = math.fsum(values) / n
    return mean, math.sqrt(math.fsum((v - mean) ** 2 for v in values) / (n - 1))


def _full_window(log_returns: list[float | None], first: int, last: int) -> list[float] | None:
    """``log_returns[first .. last]`` (inclusive) when the window is in range and has no gap."""
    if first < 1 or last >= len(log_returns):
        return None
    window = log_returns[first : last + 1]
    return None if any(r is None for r in window) else [r for r in window if r is not None]


def _max_drawdown(closes: list[float]) -> float | None:
    """Largest peak-to-trough decline over *closes*, as a non-positive fraction."""
    if len(closes) < 2:  # noqa: PLR2004
        return None
    peak = closes[0]
    worst = 0.0
    for price in closes:
        peak = max(peak, price)
        if peak > 0:
            worst = min(worst, price / peak - 1.0)
    return worst


def build_observations(candles: list[Candle], *, engine_version: str) -> list[Observation]:
    """Derive the full observation series for one asset. *engine_version* is accepted
    for symmetry with the writer (it keys the row) and is not used here."""
    _ = engine_version
    ordered = sorted(candles, key=lambda c: c.date)
    closes = [c.close for c in ordered]
    log_returns: list[float | None] = [None]
    for i in range(1, len(ordered)):
        prev, cur = closes[i - 1], closes[i]
        log_returns.append(math.log(cur / prev) if prev > 0 and cur > 0 else None)

    trs: list[float] = []
    atr: list[float | None] = []
    for i, c in enumerate(ordered):
        pc = closes[i - 1] if i > 0 else None
        tr = true_range(c.high, c.low, pc)
        trs.append(tr)
        if len(trs) < ATR_PERIOD:
            atr.append(None)
        elif len(trs) == ATR_PERIOD:
            atr.append(sum(trs) / ATR_PERIOD)
        else:
            prev_atr = atr[-1]
            atr.append(
                (prev_atr * (ATR_PERIOD - 1) + tr) / ATR_PERIOD if prev_atr is not None else None
            )

    out: list[Observation] = []
    for i, c in enumerate(ordered):
        window_returns = [
            r for r in log_returns[max(0, i - VOL_SHORT + 1) : i + 1] if r is not None
        ]
        window_returns_long = [
            r for r in log_returns[max(0, i - VOL_LONG + 1) : i + 1] if r is not None
        ]
        dd_closes = closes[max(0, i - DRAWDOWN_WINDOW + 1) : i + 1]
        momentum = {
            name: (closes[i] / closes[i - lag] - 1.0 if i >= lag and closes[i - lag] > 0 else None)
            for name, lag in _MOMENTUM_LAGS.items()
        }
        recent = _full_window(log_returns, i - SHOCK_WINDOW + 1, i)
        base = _full_window(log_returns, i - SHOCK_WINDOW - BASELINE_WINDOW + 1, i - SHOCK_WINDOW)
        recent_stats = _mean_sd(recent) if recent is not None else None
        base_stats = _mean_sd(base) if base is not None else None
        out.append(
            Observation(
                obs_date=c.date,
                close=c.close,
                prev_close=closes[i - 1] if i > 0 else None,
                log_return=log_returns[i],
                true_range=trs[i],
                atr_14=atr[i],
                realized_vol_21d=_annualized_vol(window_returns) if i >= VOL_SHORT else None,
                realized_vol_90d=_annualized_vol(window_returns_long) if i >= VOL_LONG else None,
                max_drawdown_90d=_max_drawdown(dd_closes) if i >= DRAWDOWN_WINDOW - 1 else None,
                momentum_21d=momentum["momentum_21d"],
                momentum_63d=momentum["momentum_63d"],
                momentum_252d=momentum["momentum_252d"],
                dollar_volume=c.close * c.volume if c.volume else None,
                sma_200=math.fsum(closes[i - SMA_LONG + 1 : i + 1]) / SMA_LONG
                if i >= SMA_LONG - 1
                else None,
                ret_5d=math.log(closes[i] / closes[i - SHOCK_WINDOW])
                if i >= SHOCK_WINDOW and closes[i] > 0 and closes[i - SHOCK_WINDOW] > 0
                else None,
                vol_5d=recent_stats[1] if recent_stats is not None else None,
                mu_60d_base=base_stats[0] if base_stats is not None else None,
                vol_60d_base=base_stats[1] if base_stats is not None else None,
            )
        )
    return out
