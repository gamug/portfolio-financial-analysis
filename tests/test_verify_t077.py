"""``scripts/verify_t077.py``: the chained-series arithmetic the T-077 verification reports."""

from __future__ import annotations

import math

import pytest
from verify_t077 import chain_returns, chain_stats

DAILY = {
    "2026-01-05": {1: 0.01, 2: -0.01},
    "2026-01-06": {1: 0.02, 2: 0.00},
    "2026-01-07": {1: 0.00, 2: 0.03},
    "2026-01-08": {1: -0.02, 2: 0.01},
}


def test_each_book_pays_its_turnover_once_on_its_first_day() -> None:
    books = [("2026-01-02", {1: 1.0}), ("2026-01-06", {1: 0.5, 2: 0.5})]
    series, turns = chain_returns(books, DAILY, "2026-01-08", bps=10.0)
    assert turns == [pytest.approx(1.0), pytest.approx(1.0)]  # buy 1.0; then sell 0.5, buy 0.5
    days = dict(series)
    assert list(days) == ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
    assert days["2026-01-05"] == pytest.approx(0.01 - 0.0010)  # first book: bps * sum|w|
    assert days["2026-01-06"] == pytest.approx(
        0.02
    )  # the first book is held through the next as-of
    assert days["2026-01-07"] == pytest.approx(
        0.5 * 0.0 + 0.5 * 0.03 - 0.0010
    )  # second book's day 1
    assert days["2026-01-08"] == pytest.approx(0.5 * -0.02 + 0.5 * 0.01)  # ... and nothing after


def test_a_missing_name_contributes_zero_with_its_weight_kept() -> None:
    series, _ = chain_returns([("2026-01-02", {1: 0.5, 9: 0.5})], DAILY, "2026-01-05", bps=0.0)
    assert dict(series)["2026-01-05"] == pytest.approx(0.5 * 0.01)


def test_stats() -> None:
    series = [(f"d{i}", r) for i, r in enumerate([0.01, -0.02, 0.015, 0.0, 0.01])]
    s = chain_stats(series, [1.0, 0.2, 0.4], rf=0.03)
    wealth = 1.01 * 0.98 * 1.015 * 1.0 * 1.01
    assert s.ann_return == pytest.approx(wealth ** (252 / 5) - 1)
    assert s.max_drawdown == pytest.approx(-0.02)
    assert s.mean_turnover == pytest.approx(0.3) and s.first_turnover == 1.0 and s.n_days == 5
    mean = sum(r for _, r in series) / 5
    sd = math.sqrt(sum((r - mean) ** 2 for _, r in series) / 4) * math.sqrt(252)
    assert s.ann_vol == pytest.approx(sd) and s.sharpe == pytest.approx((mean * 252 - 0.03) / sd)
