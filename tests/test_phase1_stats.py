"""The Phase-1 family statistics: the DSR's Sharpe is the report's excess Sharpe.

Synthetic series only: the point is the definition, not a market number."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

_SPEC = importlib.util.spec_from_file_location(
    "phase1_stats",
    Path(__file__).resolve().parent.parent / "scripts" / "phase1_stats.py",
)
assert _SPEC and _SPEC.loader
phase1_stats = importlib.util.module_from_spec(_SPEC)
sys.modules["phase1_stats"] = phase1_stats
_SPEC.loader.exec_module(phase1_stats)


def test_the_dsr_sharpe_equals_the_metrics_sharpe_on_the_same_excess_series() -> None:
    days = [f"2024-01-{index:02d}" for index in range(1, 23)]
    net = {day: 0.001 + 0.01 * ((index % 7) - 3) / 3 for index, day in enumerate(days)}
    rf = {day: 0.0002 for day in days}
    report = phase1_stats.metrics(net, rf)
    excess = np.asarray([net[day] - rf[day] for day in days])
    dsr = phase1_stats.deflated_sharpe(excess, 30, np.full(30, 0.02))
    assert dsr["sharpe_annual"] == pytest.approx(report["sharpe"], abs=1e-12)
    assert dsr["sharpe_daily"] * np.sqrt(phase1_stats.PERIODS) == pytest.approx(
        report["sharpe"], abs=1e-12
    )


def test_mdd_pivot_p_is_one_on_identical_series() -> None:
    days = [f"2024-02-{index:02d}" for index in range(1, 42)]
    a = {day: 0.001 * ((index % 5) - 2) for index, day in enumerate(days)}
    result = phase1_stats.block_mdd_diff(a, dict(a), block=5, n_boot=199, seed=7)
    assert result["observed"] == 0.0
    assert result["bootstrap_mean"] == 0.0
    assert result["p_value_pivot"] == 1.0
    assert result["p_value_mean_centred"] == 1.0


def test_mdd_pivot_recentres_on_the_observed_difference() -> None:
    days = [f"2024-03-{index:02d}" for index in range(1, 42)]
    a = {day: 0.002 + 0.004 * ((index % 7) - 3) / 3 for index, day in enumerate(days)}
    b = {day: 0.001 + 0.003 * ((index % 5) - 2) / 2 for index, day in enumerate(days)}
    result = phase1_stats.block_mdd_diff(a, b, block=5, n_boot=199, seed=7)
    observed, diffs = phase1_stats.mdd_bootstrap_distribution(a, b, block=5, n_boot=199, seed=7)
    assert result["observed"] == pytest.approx(observed)
    assert result["bootstrap_mean"] == pytest.approx(float(diffs.mean()))
    expected = (np.sum(np.abs(diffs - observed) >= abs(observed) - 1e-15) + 1) / (len(diffs) + 1)
    assert result["p_value_pivot"] == pytest.approx(float(expected))
    assert np.mean(diffs - observed) == pytest.approx(float(diffs.mean()) - observed)
