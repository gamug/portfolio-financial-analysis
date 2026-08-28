"""Deterministic ratio math, asserted against known values from real filings."""

from __future__ import annotations

import pytest

from fundamental_agent.metrics import compute_group
from fundamental_agent.metrics.base import safe_div
from fundamental_agent.statements import Statements


def _flat(results: list) -> dict[str, float]:
    return {r.name: r.value for r in results if r.value is not None}


def test_safe_div_guards() -> None:
    assert safe_div(10.0, 2.0) == 5.0
    assert safe_div(1.0, 0.0) is None
    assert safe_div(None, 2.0) is None
    assert safe_div(1.0, None) is None


def test_profitability_matches_apple_fy2023(aapl_10k: Statements) -> None:
    key = "2023-09-30 (FY)"
    m = _flat(compute_group("profitability", aapl_10k, key))
    assert m["gross_margin"] == pytest.approx(0.4413, abs=1e-4)
    assert m["operating_margin"] == pytest.approx(0.2982, abs=1e-4)
    assert m["net_margin"] == pytest.approx(0.2531, abs=1e-4)
    assert m["return_on_equity"] == pytest.approx(1.5608, abs=1e-3)


def test_liquidity_matches_apple_fy2023(aapl_10k: Statements) -> None:
    key = "2023-09-30 (FY)"
    m = _flat(compute_group("liquidity", aapl_10k, key))
    assert m["current_ratio"] == pytest.approx(0.988, abs=1e-3)
    assert m["quick_ratio"] == pytest.approx(0.944, abs=1e-3)


def test_cashflow_free_cash_flow_is_ocf_minus_capex(aapl_10k: Statements) -> None:
    key = "2023-09-30 (FY)"
    m = _flat(compute_group("cashflow", aapl_10k, key))
    # OCF 110.543B, capex 10.959B, revenue 383.285B
    assert m["free_cash_flow_margin"] == pytest.approx((110543 - 10959) / 383285, abs=1e-4)
    assert m["capex_intensity"] == pytest.approx(10959 / 383285, abs=1e-4)


def test_growth_needs_a_prior_period(aapl_10k: Statements) -> None:
    key = "2023-09-30 (FY)"
    assert compute_group("growth", aapl_10k, key, None) == []
    prior = "2022-09-24 (FY)"
    m = _flat(compute_group("growth", aapl_10k, key, prior))
    assert m["revenue_growth"] == pytest.approx(383285 / 394328 - 1, abs=1e-4)


def test_nvidia_explosive_growth(nvda_10k: Statements) -> None:
    fy = nvda_10k.fy_periods()
    m = _flat(compute_group("growth", nvda_10k, fy[-1].key, fy[-2].key))
    assert m["revenue_growth"] > 1.0  # FY24 revenue more than doubled


def test_bank_profitability_skips_margins(jpm_10k: Statements) -> None:
    latest = jpm_10k.latest_fy()
    assert latest is not None
    key = latest.key
    m = _flat(compute_group("profitability", jpm_10k, key))
    assert "gross_margin" not in m  # no COGS / gross profit for a bank
    assert m["return_on_equity"] == pytest.approx(37676 / 292332, abs=1e-3)


def test_roic_apple_is_high_and_taxed(aapl_10k: Statements) -> None:
    m = _flat(compute_group("roic", aapl_10k, "2023-09-30 (FY)"))
    assert 0.0 < m["effective_tax_rate"] < 0.5
    assert m["return_on_invested_capital"] > 0.3  # Apple's invested-capital base is tiny


def test_roic_is_none_for_a_bank(jpm_10k: Statements) -> None:
    latest = jpm_10k.latest_fy()
    assert latest is not None
    m = _flat(compute_group("roic", jpm_10k, latest.key))
    assert "return_on_invested_capital" not in m  # no operating income -> no NOPAT


def test_cagr_is_multi_year_from_the_fy_columns(aapl_10k: Statements) -> None:
    m = _flat(compute_group("cagr", aapl_10k, "2023-09-30 (FY)"))
    # FY2021 365,817M -> FY2023 383,285M, n = 2 compounding periods
    assert m["revenue_cagr"] == pytest.approx((383285 / 365817) ** 0.5 - 1, abs=1e-4)


def test_cagr_is_empty_without_fiscal_year_columns(msft_10q: Statements) -> None:
    quarter = msft_10q.quarter_periods()[-1]
    assert compute_group("cagr", msft_10q, quarter.key) == []
