"""Window-summary math for the pricing collector."""

from __future__ import annotations

import math
import statistics

import pytest

from pricing_agent.pricing_client import Candle
from pricing_agent.stats import TRADING_DAYS_PER_YEAR, slice_year, summarize


def _candle(date: str, close: float, volume: float = 1_000.0) -> Candle:
    return Candle(
        date=date,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=volume,
        source="test",
    )


def test_summarize_empty_is_none() -> None:
    assert summarize([]) is None


def test_summarize_start_end_and_return() -> None:
    candles = [
        _candle("2022-01-05", 100.0),
        _candle("2022-01-03", 90.0),  # out of order on purpose
        _candle("2022-01-07", 108.0),
    ]
    stats = summarize(candles)
    assert stats is not None
    assert (stats.first_date, stats.first_close) == ("2022-01-03", 90.0)
    assert (stats.last_date, stats.last_close) == ("2022-01-07", 108.0)
    assert stats.period_return == pytest.approx(108.0 / 90.0 - 1.0)
    assert stats.trading_days == 3
    assert (stats.min_close, stats.max_close) == (90.0, 108.0)


def test_summarize_volatility_matches_stdev_of_log_returns() -> None:
    closes = [100.0, 110.0, 99.0, 108.9, 103.0]
    candles = [_candle(f"2022-02-0{i + 1}", c) for i, c in enumerate(closes)]
    stats = summarize(candles)
    assert stats is not None

    expected = statistics.stdev(
        [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    )
    assert stats.daily_return_std == pytest.approx(expected)
    assert stats.annualized_volatility == pytest.approx(expected * math.sqrt(TRADING_DAYS_PER_YEAR))


def test_summarize_single_bar_has_no_volatility() -> None:
    stats = summarize([_candle("2022-01-03", 100.0)])
    assert stats is not None
    assert stats.daily_return_std is None
    assert stats.annualized_volatility is None


def test_slice_year_filters_by_prefix() -> None:
    candles = [_candle("2022-12-30", 1.0), _candle("2023-01-03", 2.0), _candle("2023-06-01", 3.0)]
    assert [c.date for c in slice_year(candles, 2023)] == ["2023-01-03", "2023-06-01"]
    assert slice_year(candles, 2024) == []
