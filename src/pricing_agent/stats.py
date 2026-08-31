"""Reduce a daily candle series to the window summary the collector stores."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from pricing_agent.pricing_client import Candle

TRADING_DAYS_PER_YEAR = 252
_MIN_RETURNS_FOR_STD = 2


@dataclass(frozen=True)
class WindowStats:
    """Start/end prices plus dispersion over one date window."""

    first_date: str
    last_date: str
    first_close: float
    last_close: float
    period_return: float
    trading_days: int
    daily_return_std: float | None  # sample std-dev of daily log returns
    annualized_volatility: float | None  # daily_return_std * sqrt(252)
    min_close: float
    max_close: float
    avg_volume: float


def summarize(candles: list[Candle]) -> WindowStats | None:
    """Summarize *candles*; ``None`` if the series is empty."""
    if not candles:
        return None
    ordered = sorted(candles, key=lambda candle: candle.date)
    closes = [candle.close for candle in ordered]
    first, last = ordered[0], ordered[-1]

    log_returns = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i - 1] > 0.0 and closes[i] > 0.0
    ]
    std = statistics.stdev(log_returns) if len(log_returns) >= _MIN_RETURNS_FOR_STD else None
    annualized = std * math.sqrt(TRADING_DAYS_PER_YEAR) if std is not None else None

    return WindowStats(
        first_date=first.date,
        last_date=last.date,
        first_close=first.close,
        last_close=last.close,
        period_return=(last.close / first.close - 1.0) if first.close > 0.0 else 0.0,
        trading_days=len(ordered),
        daily_return_std=std,
        annualized_volatility=annualized,
        min_close=min(closes),
        max_close=max(closes),
        avg_volume=statistics.fmean(candle.volume for candle in ordered),
    )


def slice_year(candles: list[Candle], year: int) -> list[Candle]:
    prefix = f"{year:04d}-"
    return [candle for candle in candles if candle.date.startswith(prefix)]
