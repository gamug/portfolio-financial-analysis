"""Derived per-day price analytics: ATR recursion, rolling windows, warm-up NULLs."""

from __future__ import annotations

import math
import sqlite3

from pricing_agent import db
from pricing_agent.observations import ATR_PERIOD, build_observations, true_range
from pricing_agent.pricing_client import Candle


def _series(closes: list[float], *, spread: float = 1.0, volume: float = 1_000.0) -> list[Candle]:
    out = []
    for i, close in enumerate(closes):
        out.append(
            Candle(
                date=f"2022-{1 + i // 28:02d}-{1 + i % 28:02d}",
                open=close,
                high=close + spread,
                low=close - spread,
                close=close,
                volume=volume,
                source="test",
            )
        )
    return out


def test_true_range_first_bar_has_no_prev_close() -> None:
    assert true_range(11.0, 9.0, None) == 2.0
    assert true_range(11.0, 9.0, 5.0) == 6.0  # |high - prev_close| dominates


def test_first_observation_has_no_history() -> None:
    obs = build_observations(_series([100.0, 101.0, 102.0]), engine_version="v1")
    first = obs[0]
    assert first.prev_close is None
    assert first.log_return is None
    assert first.atr_14 is None
    assert first.realized_vol_21d is None
    assert first.momentum_21d is None
    assert obs[1].log_return == math.log(101.0 / 100.0)
    assert obs[1].prev_close == 100.0


def test_atr_is_wilder_smoothed() -> None:
    # constant spread => every true range is 2.0 => ATR is exactly 2.0 once seeded
    obs = build_observations(_series([100.0] * 40, spread=1.0), engine_version="v1")
    assert obs[ATR_PERIOD - 2].atr_14 is None
    assert obs[ATR_PERIOD - 1].atr_14 == 2.0
    assert obs[-1].atr_14 == 2.0


def test_rolling_vol_and_momentum_activate_after_warmup() -> None:
    closes = [100.0 * (1.001**i) for i in range(120)]
    obs = build_observations(_series(closes), engine_version="v1")
    assert obs[20].realized_vol_21d is None
    assert obs[21].realized_vol_21d is not None
    assert obs[89].realized_vol_90d is None
    assert obs[90].realized_vol_90d is not None
    # steady compounding => 21-day momentum is ~1.001**21 - 1
    assert obs[30].momentum_21d == closes[30] / closes[9] - 1.0


def test_max_drawdown_is_non_positive_and_windowed() -> None:
    closes = [100.0] * 95 + [80.0] + [100.0] * 20  # a 20% dip at index 95
    obs = build_observations(_series(closes), engine_version="v1")
    assert obs[88].max_drawdown_90d is None  # 90-day window not full yet
    dip_day = obs[95]
    assert dip_day.max_drawdown_90d is not None
    assert dip_day.max_drawdown_90d < -0.19  # roughly -0.2
    assert obs[90].max_drawdown_90d == 0.0  # first full window, still flat


def test_writer_is_immutable_per_engine_version(memory_pricing_db: sqlite3.Connection) -> None:
    conn = memory_pricing_db
    conn.execute("INSERT INTO assets (ticker) VALUES ('AAPL')")
    conn.commit()
    aid = conn.execute("SELECT id FROM assets WHERE ticker = 'AAPL'").fetchone()[0]
    obs = build_observations(_series([100.0, 101.0, 102.0, 103.0]), engine_version="v1")

    db.upsert_price_observations(conn, aid, obs, engine_version="v1")
    db.upsert_price_observations(conn, aid, obs, engine_version="v1")  # no-op
    assert conn.execute("SELECT COUNT(*) FROM price_observation").fetchone()[0] == 4

    db.upsert_price_observations(conn, aid, obs, engine_version="v2")  # parallel row set
    assert conn.execute("SELECT COUNT(*) FROM price_observation").fetchone()[0] == 8
    # v_price_observation exposes only the newest engine_version per (asset, day)
    versions = {r[0] for r in conn.execute("SELECT engine_version FROM v_price_observation")}
    assert versions == {"v2"}
