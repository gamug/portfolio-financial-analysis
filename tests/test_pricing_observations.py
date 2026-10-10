"""Derived per-day price analytics: ATR recursion, rolling windows, warm-up NULLs."""

from __future__ import annotations

import math

import pytest
from portfolio_common.db import Database

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


def test_writer_is_immutable_per_engine_version(memory_pricing_db: Database) -> None:
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


# -- priceobs-v2 (T-070): sma_200, ret_5d, vol_5d and the 60-day baseline ----------------------


def _wiggly(n: int) -> list[float]:
    """A deterministic series with non-trivial, distinct daily returns."""
    closes = [100.0]
    for i in range(1, n):
        closes.append(closes[-1] * (1.0 + 0.01 * math.sin(i * 1.3) + 0.002 * (i % 7 - 3)))
    return closes


def _returns(obs: list, first: int, last: int) -> list[float]:
    """The daily log returns of ``obs[first:last]`` (none of them is undefined in these series)."""
    return [r for o in obs[first:last] if (r := o.log_return) is not None]


def _sd(values: list[float]) -> float:
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def test_the_engine_version_is_priceobs_v2() -> None:
    assert db.PRICE_OBSERVATION_ENGINE_VERSION == "priceobs-v2"


def test_sma_200_is_the_mean_of_the_last_200_closes_and_null_until_full() -> None:
    closes = _wiggly(260)
    obs = build_observations(_series(closes), engine_version="v2")
    assert obs[198].sma_200 is None  # 199 closes
    assert obs[199].sma_200 == pytest.approx(sum(closes[:200]) / 200)  # the first full window
    assert obs[259].sma_200 == pytest.approx(sum(closes[60:260]) / 200)  # drops the oldest 60


def test_ret_5d_is_the_five_day_log_return_and_null_until_six_closes() -> None:
    closes = _wiggly(12)
    obs = build_observations(_series(closes), engine_version="v2")
    assert obs[4].ret_5d is None  # only 5 closes: no close five days back
    assert obs[5].ret_5d == pytest.approx(math.log(closes[5] / closes[0]))
    assert obs[11].ret_5d == pytest.approx(math.log(closes[11] / closes[6]))
    # the sum of the last five daily log returns
    assert obs[11].ret_5d == pytest.approx(sum(_returns(obs, 7, 12)))


def test_vol_5d_is_the_sd_of_the_last_five_daily_log_returns_not_annualized() -> None:
    closes = _wiggly(12)
    obs = build_observations(_series(closes), engine_version="v2")
    assert obs[4].vol_5d is None  # four daily returns so far
    last_five = _returns(obs, 1, 6)
    assert obs[5].vol_5d == pytest.approx(_sd(last_five))  # n - 1, daily
    assert obs[11].vol_5d == pytest.approx(_sd(_returns(obs, 7, 12)))


def test_the_baseline_is_the_60_returns_ending_5_sessions_before_and_null_until_full() -> None:
    closes = _wiggly(80)
    obs = build_observations(_series(closes), engine_version="v2")
    rets = [0.0, *_returns(obs, 1, 80)]  # index 0 has no return; rets[i] is bar i's
    assert obs[64].mu_60d_base is None and obs[64].vol_60d_base is None  # 59 returns available
    # i = 65: returns 1..60 -- they END at i - 5 = 60 and the five most recent (61..65) are out
    window = rets[1:61]
    assert obs[65].mu_60d_base == pytest.approx(sum(window) / 60)
    assert obs[65].vol_60d_base == pytest.approx(_sd(window))
    # i = 79: returns 15..74
    assert obs[79].mu_60d_base == pytest.approx(sum(rets[15:75]) / 60)
    assert obs[79].vol_60d_base == pytest.approx(_sd(rets[15:75]))


def test_a_shock_in_the_recent_window_does_not_dilute_its_own_baseline() -> None:
    calm = [100.0 * (1.0 + 0.001 * ((-1) ** i)) for i in range(70)]
    shocked = [*calm, 60.0, 61.0, 59.0, 62.0, 58.0]  # five violent days at the end
    base = build_observations(_series(calm), engine_version="v2")[-1]
    after = build_observations(_series(shocked), engine_version="v2")[-1]
    # the baseline of the last (shocked) day ends 5 sessions earlier: the calm days only
    assert after.vol_60d_base == pytest.approx(base.vol_60d_base)
    assert after.vol_5d is not None and after.vol_60d_base is not None
    assert after.vol_5d / after.vol_60d_base > 2.5
    # ... which is what ret_5d against the baseline picks up
    assert after.ret_5d is not None and after.ret_5d < -0.3


def test_a_gap_in_a_window_nulls_the_field_that_needs_it() -> None:
    closes = _wiggly(80)
    closes[40] = 0.0  # a bad bar: the log returns around it are undefined
    obs = build_observations(_series(closes), engine_version="v2")
    assert obs[43].vol_5d is None  # its five returns include one of the two undefined ones
    assert obs[79].vol_5d is not None  # the bad bar is out of the recent window by now
    assert obs[79].vol_60d_base is None  # ... but still inside the 60-day baseline


def test_the_v2_fields_are_written_and_a_rerun_touches_nothing(
    memory_pricing_db: Database,
) -> None:
    conn = memory_pricing_db
    conn.execute("INSERT INTO assets (ticker) VALUES ('AAPL')")
    conn.commit()
    aid = conn.execute("SELECT id FROM assets WHERE ticker = 'AAPL'").fetchone()[0]
    obs = build_observations(
        _series(_wiggly(80)), engine_version=db.PRICE_OBSERVATION_ENGINE_VERSION
    )
    db.upsert_price_observations(conn, aid, obs)
    row = conn.execute(
        "SELECT * FROM price_observation WHERE obs_date = ?", (obs[79].obs_date,)
    ).fetchone()
    assert row["engine_version"] == "priceobs-v2"
    assert (row["sma_200"], row["ret_5d"]) == (None, obs[79].ret_5d)
    assert row["vol_5d"] == obs[79].vol_5d
    assert row["mu_60d_base"] == obs[79].mu_60d_base and row["vol_60d_base"] == obs[79].vol_60d_base
    computed = conn.execute("SELECT computed_at FROM price_observation WHERE id = ?", (row["id"],))
    stamp = computed.fetchone()[0]
    db.upsert_price_observations(conn, aid, obs)
    again = conn.execute("SELECT computed_at FROM price_observation WHERE id = ?", (row["id"],))
    assert again.fetchone()[0] == stamp  # unchanged inputs: the row is not rewritten
