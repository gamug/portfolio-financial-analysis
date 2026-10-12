"""Free-cash-flow-yield family, asserted against real filings + a fixed price."""

from __future__ import annotations

import pytest

from fundamental_agent.metrics import valuation
from fundamental_agent.metrics.base import sum_present
from fundamental_agent.pricing import ClosePrice
from fundamental_agent.statements import CoverShares, Statements


def _flat(results: list) -> dict[str, float]:
    return {r.name: r.value for r in results if r.value is not None}


def _num(value: float | None) -> float:
    assert value is not None
    return value


def test_equity_fcf_yield_matches_apple_fy2023(aapl_10k: Statements) -> None:
    key = "2023-09-30 (FY)"
    price = ClosePrice("2023-09-29", 171.21)
    aapl_10k.cover_shares = [CoverShares(15_550_061_000.0, "2023-10-20")]
    m = _flat(valuation.compute(aapl_10k, key, price))

    fcfe = 110_543e6 - 10_959e6  # OCF - CapEx
    shares = 15_550_061_000.0
    market_cap = shares * 171.21

    assert m["market_capitalization"] == pytest.approx(market_cap, rel=1e-9)
    assert m["free_cash_flow_yield"] == pytest.approx(fcfe / market_cap, rel=1e-6)
    # SBC (10.833B) is a real dilutive cost -> the adjusted yield must be lower.
    assert m["sbc_adjusted_fcf_yield"] == pytest.approx((fcfe - 10_833e6) / market_cap, rel=1e-6)
    assert m["sbc_adjusted_fcf_yield"] < m["free_cash_flow_yield"]


def test_enterprise_value_and_yield_use_net_debt(aapl_10k: Statements) -> None:
    key = "2023-09-30 (FY)"
    price = ClosePrice("2023-09-29", 171.21)
    aapl_10k.cover_shares = [CoverShares(15_550_061_000.0, "2023-10-20")]
    results = valuation.compute(aapl_10k, key, price)
    m = _flat(results)

    market_cap = 15_550_061_000.0 * 171.21
    debt = _num(
        sum_present(aapl_10k.get("long_term_debt", key), aapl_10k.get("short_term_debt", key))
    )
    cash = _num(aapl_10k.get("cash", key))
    assert m["enterprise_value"] == pytest.approx(market_cap + debt - cash, rel=1e-9)
    # net cash or modest net debt -> enterprise yield close to but not equal to equity yield
    assert m["enterprise_fcf_yield"] > 0.0
    assert m["enterprise_fcf_yield"] != m["free_cash_flow_yield"]


def test_price_offset_is_recorded_in_inputs(aapl_10k: Statements) -> None:
    key = "2023-09-30 (FY)"
    aapl_10k.cover_shares = [CoverShares(15_550_061_000.0, "2023-10-20")]
    results = valuation.compute(aapl_10k, key, ClosePrice("2023-09-29", 171.21))
    inputs = results[0].inputs
    assert inputs["price_date_offset_days"] == 1.0
    assert inputs["shares"] == 15_550_061_000.0


def test_bank_without_cover_shares_leaves_market_cap_empty_under_mkt04(jpm_10k: Statements) -> None:
    key = jpm_10k.latest_fy()
    assert key is not None
    price = ClosePrice(key.date, 150.0)
    results = valuation.compute(jpm_10k, key.key, price)
    m = {r.name: r for r in results}

    assert jpm_10k.cover_shares == []
    mc = m["market_capitalization"]
    assert mc.value is None
    assert mc.inputs["market_cap_no_cover_shares"] == 1.0


def test_missing_cash_flow_leaves_yield_none_but_still_prices_the_equity(
    jpm_10k: Statements,
) -> None:
    key = jpm_10k.latest_fy()
    assert key is not None
    jpm_10k.cover_shares = [CoverShares(2_970_000_000.0, "2023-01-15")]
    results = valuation.compute(jpm_10k, key.key, ClosePrice(key.date, 150.0))
    m = {r.name: r.value for r in results}
    # a bank payload carries no capex line -> FCFE cannot be formed
    assert m["free_cash_flow_yield"] is None
    assert m["market_capitalization"] == pytest.approx(2_970_000_000.0 * 150.0, rel=1e-9)


def _diluted_payload(period_key: str, value: float) -> dict:
    """A minimal income_statement-only payload, matching a real EDGAR-gateway
    diluted-shares row (see tests/fixtures/financials_*.json)."""
    return {
        "income_statement": [
            {
                "concept": "us-gaap_WeightedAverageNumberOfDilutedSharesOutstanding",
                "label": "Diluted (in shares)",
                "standard_concept": "SharesFullyDilutedAverage",
                "abstract": False,
                "dimension": False,
                period_key: value,
            }
        ],
        "balance_sheet": [],
        "cash_flow": [],
    }


def _shares_outstanding_payload(instant_date: str, value: float) -> dict:
    """A minimal balance_sheet-only payload for the primary share-count concept."""
    return {
        "income_statement": [],
        "balance_sheet": [
            {
                "concept": "us-gaap_CommonStockSharesOutstanding",
                "label": "Common stock, shares outstanding",
                "standard_concept": "SharesYearEnd",
                "abstract": False,
                "dimension": False,
                instant_date: value,
            }
        ],
        "cash_flow": [],
    }


def test_diluted_shares_never_used_and_scale_defect_flags_suspect_under_id18() -> None:
    """MKT-04 & ID-18: diluted shares are never used for market capitalization.
    When a scale defect is detected, it flags share_scale_suspect instead of correcting."""
    key = "2025-12-31 (FY)"
    stmts = Statements.from_payload(_diluted_payload(key, 751.8))
    price = ClosePrice("2025-12-30", 300.0)

    results = valuation.compute(stmts, key, price, {"diluted_shares": 1_000_000.0})
    m = {r.name: r for r in results}

    assert m["market_capitalization"].value is None
    assert m["market_capitalization"].inputs["share_scale_suspect"] == 1.0
    assert m["market_capitalization"].inputs["market_cap_no_cover_shares"] == 1.0


def test_no_cover_shares_leaves_cap_empty_without_fallback() -> None:
    """MKT-04: without cover shares, market cap is empty and flagged no_cover_shares,
    never falling back to diluted shares."""
    key = "2025-12-31 (FY)"
    stmts = Statements.from_payload(_diluted_payload(key, 751_800_000.0))
    price = ClosePrice("2025-12-30", 300.0)

    results = valuation.compute(stmts, key, price)  # share_scale_factors omitted
    m = {r.name: r for r in results}

    assert m["market_capitalization"].value is None
    assert m["market_capitalization"].inputs["market_cap_no_cover_shares"] == 1.0


def test_balance_sheet_shares_outstanding_never_used_under_mkt04() -> None:
    """MKT-04: balance sheet shares outstanding is never used as fallback for market cap."""
    key = "2025-12-31 (FY)"
    stmts = Statements.from_payload(_shares_outstanding_payload("2025-12-31", 209.271))
    price = ClosePrice("2025-12-30", 1000.0)

    results = valuation.compute(stmts, key, price, {"shares_outstanding": 1_000_000.0})
    m = {r.name: r for r in results}

    assert m["market_capitalization"].value is None
    assert m["market_capitalization"].inputs["market_cap_no_cover_shares"] == 1.0
    assert m["market_capitalization"].inputs["share_scale_suspect"] == 1.0
