"""The return panel aligns to a rectangular, gap-free (T, N) block."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from portfolio_common.db import Database

from quant.actions import backfill_corporate_actions
from quant.config import QuantSettings
from quant.panel import PanelError, build_return_panel
from quant.returns import run_build_returns


def _build_returns(conn: Database) -> None:
    s = QuantSettings(db_path=Path(":memory:"))
    backfill_corporate_actions(
        s, date_from="2000-01-01", date_to="2100-01-01", source="derive", conn=conn
    )
    run_build_returns(s, date_from="2000-01-01", date_to="2100-01-01", conn=conn)


def test_panel_excludes_short_history_and_has_no_nan(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=6, n_days=300, with_dividends=False)
    # asset 6: only ~50 obs
    conn.execute(
        "DELETE FROM price_daily WHERE asset_id = 6 AND date NOT IN "
        "(SELECT date FROM price_daily WHERE asset_id = 6 ORDER BY date DESC LIMIT 50)"
    )
    conn.commit()
    _build_returns(conn)

    as_of = conn.execute("SELECT MAX(obs_date) FROM quant_return_daily").fetchone()[0]
    panel = build_return_panel(conn, as_of=as_of, lookback_days=250, min_history_days=200)
    assert 6 not in panel.asset_ids
    assert panel.asset_ids == [1, 2, 3, 4, 5]
    assert panel.asset_ids == sorted(panel.asset_ids)
    assert panel.returns.shape == (len(panel.dates), 5)
    assert not np.isnan(panel.returns).any()
    assert len(panel.spec_sha256) == 64


def test_panel_respects_as_of_cutoff_and_fills_a_one_day_hole(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=3, n_days=280, with_dividends=False)
    _build_returns(conn)
    all_dates = [
        r[0]
        for r in conn.execute("SELECT DISTINCT obs_date FROM quant_return_daily ORDER BY obs_date")
    ]
    cutoff = all_dates[-10]
    # punch a single-day hole for asset 2, well inside the window
    hole = all_dates[-40]
    conn.execute(
        "UPDATE quant_return_daily SET tr_log_return = NULL WHERE asset_id = 2 AND obs_date = ?",
        (hole,),
    )
    conn.commit()

    panel = build_return_panel(conn, as_of=cutoff, lookback_days=200, min_history_days=150)
    assert max(panel.dates) == cutoff  # nothing after the cutoff
    assert not np.isnan(panel.returns).any()  # the hole was filled with 0.0
    j = panel.asset_ids.index(2)
    i = panel.dates.index(hole)
    assert panel.returns[i, j] == 0.0


def test_panel_drops_partial_coverage_column(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=5, n_days=300, with_dividends=False)
    _build_returns(conn)
    all_dates = [
        r[0]
        for r in conn.execute("SELECT DISTINCT obs_date FROM quant_return_daily ORDER BY obs_date")
    ]
    # asset 3: blank ~15% of the 200-day window (above the count floor, below min_coverage)
    holes = all_dates[-140:-110]
    conn.executemany(
        "UPDATE quant_return_daily SET tr_log_return = NULL WHERE asset_id = 3 AND obs_date = ?",
        [(d,) for d in holes],
    )
    conn.commit()

    panel = build_return_panel(
        conn, as_of=all_dates[-1], lookback_days=200, min_history_days=150, min_coverage=0.98
    )
    assert 3 not in panel.asset_ids
    assert not np.isnan(panel.returns).any()


def test_panel_raises_when_window_too_short(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=2, n_days=120, with_dividends=False)
    _build_returns(conn)
    as_of = conn.execute("SELECT MAX(obs_date) FROM quant_return_daily").fetchone()[0]
    with pytest.raises(PanelError):
        build_return_panel(conn, as_of=as_of, lookback_days=500, min_history_days=400)
