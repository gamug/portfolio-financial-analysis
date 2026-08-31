"""Per-(asset, day) price analytics -- the ``price_observation`` series.

Mirrors :mod:`pricing_agent.stats` in spirit: a frozen dataclass plus pure functions
over a ``list[Candle]``. Raw OHLCV stays in ``price_daily``; this module only derives
close-to-close return, Wilder ATR, rolling realized volatility, rolling max drawdown
and simple momentum. Warm-up rows (before a window is full) carry ``None`` for the
fields that need history.
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


def true_range(high: float, low: float, prev_close: float | None) -> float:
    """Classic true range; falls back to the day's range when there is no prior close."""
    if prev_close is None:
        return high - low
    return max(high - low, abs(high - prev_close), abs(prev_close - low))


def _annualized_vol(log_returns: list[float]) -> float | None:
    if len(log_returns) < 2:  # noqa: PLR2004 - stdev needs two points
        return None
    return statistics.stdev(log_returns) * math.sqrt(TRADING_DAYS_PER_YEAR)


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
    for symmetry with the writer (it keys the immutable row) and is not used here."""
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
            )
        )
    return out
