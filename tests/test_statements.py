"""Statement parsing and line-item resolution against real EDGAR captures."""

from __future__ import annotations

import pytest

from fundamental_agent.statements import INSTANT, Period, Statements, _parse_period, iter_facts


def _tag(column: str) -> str | None:
    parsed = _parse_period(column)
    return parsed.tag if parsed else None


def _need(period: Period | None) -> Period:
    assert period is not None
    return period


def test_parses_duration_and_instant_columns() -> None:
    assert _tag("2023-09-30 (FY)") == "FY"
    assert _tag("2024-06-29 (Q3)") == "Q3"
    assert _tag("2024-06-29 (YTD)") == "YTD"
    assert _tag("2023-09-30") == INSTANT
    assert _parse_period("total assets") is None


def test_period_helpers(aapl_10k: Statements) -> None:
    fy = aapl_10k.fy_periods()
    assert [p.date for p in fy] == ["2021-09-25", "2022-09-24", "2023-09-30"]
    assert _need(aapl_10k.latest_fy()).date == "2023-09-30"
    assert _need(aapl_10k.prior_of(fy[-1])).date == "2022-09-24"
    assert aapl_10k.instant_periods()  # balance sheet columns are bare dates


def test_income_line_items_resolve(aapl_10k: Statements) -> None:
    key = "2023-09-30 (FY)"
    assert aapl_10k.get("revenue", key) == 383285000000.0
    assert aapl_10k.get("cogs", key) == 214137000000.0
    assert aapl_10k.get("gross_profit", key) == 169148000000.0
    assert aapl_10k.get("operating_income", key) == 114301000000.0
    assert aapl_10k.get("net_income", key) == 96995000000.0


def test_balance_sheet_resolves_via_instant_translation(aapl_10k: Statements) -> None:
    # asked with a duration key; balance-sheet items must map to the instant column.
    key = "2023-09-30 (FY)"
    assert aapl_10k.get("total_assets", key) == 352583000000.0
    assert aapl_10k.get("current_assets", key) == 143566000000.0
    assert aapl_10k.get("current_liabilities", key) == 145308000000.0
    assert aapl_10k.get("equity", key) == 62146000000.0
    # cash must be the balance-sheet stock, not a cash-flow reconciliation line.
    assert aapl_10k.get("cash", key) == 29965000000.0


def test_balance_sheet_items_are_never_negative(nvda_10k: Statements) -> None:
    key = _need(nvda_10k.latest_fy()).key
    for item in ("inventory", "receivables", "short_term_investments", "long_term_debt"):
        value = nvda_10k.get(item, key)
        assert value is not None
        assert value >= 0.0


def test_bank_has_no_operating_income_or_current_split(jpm_10k: Statements) -> None:
    key = _need(jpm_10k.latest_fy()).key
    assert jpm_10k.get("operating_income", key) is None
    assert jpm_10k.get("current_liabilities", key) is None
    # but a bank still reports revenue, net income, assets and equity.
    assert jpm_10k.get("revenue", key) == pytest.approx(128695000000.0)
    assert jpm_10k.get("equity", key) == pytest.approx(292332000000.0)


def test_quarterly_prior_period_detected_in_same_payload(msft_10q: Statements) -> None:
    quarters = msft_10q.quarter_periods()
    latest = quarters[-1]
    prior = msft_10q.prior_of(latest)
    assert prior is not None
    assert prior.tag == latest.tag
    assert prior.year == latest.year - 1


def test_iter_facts_yields_only_numeric_period_cells(aapl_10k: Statements) -> None:
    facts = list(iter_facts(aapl_10k))
    assert facts
    assert all(isinstance(f["value"], float) for f in facts)
    assert all(f["statement"] in aapl_10k.raw for f in facts)
    assert {f["statement"] for f in facts} == set(aapl_10k.raw)
