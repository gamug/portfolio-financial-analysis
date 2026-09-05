"""Total-return series: dividend days lift tr_log_return above the price return."""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path

import pytest
from portfolio_common.db import Database

from quant.actions import backfill_corporate_actions
from quant.config import QuantSettings
from quant.returns import _snap_to_calendar, build_total_return_series, run_build_returns


def test_snap_to_calendar_moves_events_forward_to_a_trading_day() -> None:
    cal = ["2024-01-02", "2024-01-03", "2024-01-08", "2024-01-09"]
    # 2024-01-06 is a Saturday -> snaps to 2024-01-08; two events on one snap accumulate
    snapped = _snap_to_calendar(cal, {"2024-01-06": 0.3, "2024-01-07": 0.2, "2024-01-03": 0.5})
    assert snapped == {"2024-01-08": pytest.approx(0.5), "2024-01-03": pytest.approx(0.5)}
    # an event after the last trading day is dropped
    assert _snap_to_calendar(cal, {"2024-02-01": 1.0}) == {}


def test_build_total_return_series_folds_in_a_dividend() -> None:
    closes = [
        ("2024-01-02", 100.0),
        ("2024-01-03", 101.0),  # ex-dividend day
        ("2024-01-04", 102.0),
    ]
    rows = build_total_return_series(closes, {"2024-01-03": 0.50}, {})

    assert rows[0].price_log_return is None and rows[0].tr_log_return is None
    assert rows[0].tr_index == 100.0

    # ex-div day: tr uses (C + D)
    price_lr, tr_lr = rows[1].price_log_return, rows[1].tr_log_return
    assert price_lr is not None and tr_lr is not None
    assert price_lr == pytest.approx(math.log(101.0 / 100.0))
    assert tr_lr == pytest.approx(math.log((101.0 + 0.50) / 100.0))
    assert tr_lr > price_lr
    assert rows[1].tr_index == pytest.approx(100.0 * (101.0 + 0.50) / 100.0)

    # non-div day: tr == price return exactly
    assert rows[2].tr_log_return == pytest.approx(rows[2].price_log_return)
    assert rows[2].cash_dividend == 0.0

    # back-adjusted close: last row is the raw split-adjusted close
    assert rows[-1].adj_close == pytest.approx(102.0)
    # earlier rows are scaled down by (1 - D/C) across the future dividend
    assert rows[0].adj_close == pytest.approx(100.0 * (1.0 - 0.50 / 101.0))


def test_run_build_returns_end_to_end(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=3, n_days=260)
    settings = QuantSettings(db_path=Path(":memory:"))
    backfill_corporate_actions(
        settings, date_from="2024-01-01", date_to="2025-12-31", source="derive", conn=conn
    )

    rep = run_build_returns(settings, date_from="2024-01-01", date_to="2025-12-31", conn=conn)
    assert rep.assets == 3
    assert rep.rows_written > 3 * 200
    assert rep.assets_with_dividends == 3

    n = conn.execute("SELECT COUNT(*) FROM quant_return_daily").fetchone()[0]
    assert n == rep.rows_written
    # tr_log_return never below price_log_return (dividends are non-negative)
    bad = conn.execute(
        "SELECT COUNT(*) FROM quant_return_daily "
        "WHERE tr_log_return IS NOT NULL AND tr_log_return < price_log_return - 1e-12"
    ).fetchone()[0]
    assert bad == 0
    # and strictly above on at least the seeded dividend ex-dates
    lifted = conn.execute(
        "SELECT COUNT(*) FROM quant_return_daily WHERE cash_dividend > 0 "
        "AND tr_log_return > price_log_return + 1e-9"
    ).fetchone()[0]
    assert lifted > 0

    # re-run is a no-op
    again = run_build_returns(settings, date_from="2024-01-01", date_to="2025-12-31", conn=conn)
    assert again.rows_written == 0
