"""T-151 part 2: the metric definitions (MET-01/02/03/04/09, MD-01/02/03/35/38; D1-D11).

Synthetic payloads exercise each formula, its empty/flag cases and its boundaries -- the beginning
capital (D1/D2), the 21% NOPAT tax and the filing's own effective rate (D3), growth from a base <= 0
and the adoption flags (D4), the non-positive denominators (D5), NCI and short-term investments in
invested capital and EV (D6/D7), MD-38 (D8), the persisted EBITDA (D9) and the FCF rename (D10).
"""

from __future__ import annotations

from typing import Any

import pytest

from fundamental_agent.metrics import capital
from fundamental_agent.metrics.base import safe_div
from fundamental_agent.metrics.cagr import compute as cagr_compute
from fundamental_agent.metrics.cashflow import compute as cashflow_compute
from fundamental_agent.metrics.growth import compute as growth_compute
from fundamental_agent.metrics.leverage import compute as leverage_compute
from fundamental_agent.metrics.profitability import compute as profitability_compute
from fundamental_agent.metrics.roic import compute as roic_compute
from fundamental_agent.metrics.tax import filing_effective_tax_rate, nopat_tax_rate
from fundamental_agent.metrics.valuation import compute as valuation_compute
from fundamental_agent.pricing import ClosePrice
from fundamental_agent.statements import Statements
from kg_schema.market_cap import MarketCap

G = "us-gaap_"


def _row(concept: str, label: str, **periods: float) -> dict[str, Any]:
    return {
        "concept": concept,
        "label": label,
        "standard_concept": None,
        "abstract": False,
        "dimension": False,
        **periods,
    }


FY = "2024-12-31 (FY)"
PRIOR_FY = "2023-12-31 (FY)"
Q = "2024-06-30 (Q2)"


def _flat(results: list[Any]) -> dict[str, Any]:
    return {r.name: r for r in results}


def _optional_rows(column: str, rows: dict[str, tuple[str, float | None]]) -> list[dict[str, Any]]:
    """One balance-sheet row per (concept, (label, value)) whose value is not ``None``."""
    return [
        _row(c, label, **{column: value}) for c, (label, value) in rows.items() if value is not None
    ]


def _payload(**fields: Any) -> dict[str, Any]:
    """A 10-K-shaped payload with a prior fiscal year end (2023-12-31). A ``None`` field omits its
    row; the prior-column values default to the fixture's own."""
    pretax_concept = (
        G
        + "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"
    )
    income = [
        _row(
            G + "OperatingIncomeLoss",
            "Operating income",
            **{FY: fields.get("operating_income", 100.0)},
        ),
        _row(G + "NetIncomeLoss", "Net income", **{FY: fields.get("net_income", 80.0)}),
        _row(G + "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenue", **{FY: 500.0}),
    ]
    income += _optional_rows(
        FY,
        {
            pretax_concept: ("Pretax", fields.get("pretax_income", 120.0)),
            G + "IncomeTaxExpenseBenefit": ("Income tax", fields.get("income_tax", 20.0)),
            G + "NetIncomeLossAttributableToNoncontrollingInterest": (
                "NCI income",
                fields.get("nci_income"),
            ),
        },
    )
    balance: list[dict[str, Any]] = [
        _row(G + "Assets", "Assets", **{"2024-12-31": 1_100.0}),
        _row(G + "StockholdersEquity", "Equity", **{"2024-12-31": 450.0}),
        _row(G + "LongTermDebt", "Long-term debt", **{"2024-12-31": 210.0}),
        _row(G + "CashAndCashEquivalentsAtCarryingValue", "Cash", **{"2024-12-31": 60.0}),
        _row(
            G + "AssetsCurrent",
            "Current assets",
            **{"2024-12-31": fields.get("current_assets", 300.0)},
        ),
        _row(
            G + "LiabilitiesCurrent",
            "Current liabilities",
            **{"2024-12-31": fields.get("current_liabilities", 150.0)},
        ),
        _row(
            G + "LongTermDebtCurrent",
            "ST debt",
            **{"2024-12-31": fields.get("current_short_term_debt", 20.0)},
        ),
    ]
    balance += _optional_rows(
        "2024-12-31",
        {
            G + "ShortTermInvestments": ("STI", fields.get("current_sti")),
            G + "MinorityInterest": ("NCI", fields.get("minority_interest")),
        },
    )
    balance += _optional_rows(
        "2023-12-31",
        {
            G + "Assets": ("Assets", fields.get("prior_assets", 1_000.0)),
            G + "StockholdersEquity": ("Equity", fields.get("prior_equity", 400.0)),
            G + "LongTermDebt": ("Long-term debt", fields.get("prior_debt", 200.0)),
            G + "CashAndCashEquivalentsAtCarryingValue": ("Cash", fields.get("prior_cash", 50.0)),
            G + "ShortTermInvestments": ("Short-term investments", fields.get("prior_sti")),
            G + "MinorityInterest": ("NCI", fields.get("minority_interest")),
            G + "RedeemableNoncontrollingInterestEquityCarryingAmount": (
                "Redeemable NCI",
                fields.get("redeemable_nci"),
            ),
        },
    )
    balance += [
        _row(
            G + "AssetsCurrent",
            "Current assets",
            **{"2023-12-31": fields.get("prior_current_assets", 280.0)},
        ),
        _row(
            G + "LiabilitiesCurrent",
            "Current liabilities",
            **{"2023-12-31": fields.get("prior_current_liabilities", 140.0)},
        ),
        _row(
            G + "LongTermDebtCurrent",
            "ST debt",
            **{"2023-12-31": fields.get("prior_short_term_debt", 20.0)},
        ),
    ]
    cash_flow = [
        _row(G + "NetCashProvidedByUsedInOperatingActivities", "OCF", **{FY: 90.0}),
        _row(
            G + "PaymentsToAcquirePropertyPlantAndEquipment",
            "Capex",
            **{FY: fields.get("capex", 30.0)},
        ),
        _row(G + "DepreciationDepletionAndAmortization", "D&A", **{FY: fields.get("da", 10.0)}),
    ]
    return {"income_statement": income, "balance_sheet": balance, "cash_flow": cash_flow}


def _stmts(**kwargs: Any) -> Statements:
    return Statements.from_payload(_payload(**kwargs))


# -- D1: MET-01, ROA over the prior fiscal year end -------------------------------------------------


def test_roa_uses_nopat_over_the_prior_fiscal_year_end() -> None:
    m = _flat(profitability_compute(_stmts(), FY))
    # NOPAT = 100 x (1 - 21%) = 79; prior assets 1,000
    assert m["return_on_assets"].value == pytest.approx(79.0 / 1_000.0)


def test_roa_is_empty_when_the_prior_year_end_is_missing_not_the_ending_assets() -> None:
    m = _flat(profitability_compute(_stmts(prior_assets=None), FY))
    assert m["return_on_assets"].value is None
    assert m["return_on_assets"].inputs["flag_return_on_assets_prior_year_assets_missing"] == 1.0


# -- D2: MET-02, ROE / ROIC over the prior fiscal year end ------------------------------------------


def test_roe_divides_parent_net_income_by_prior_year_equity() -> None:
    m = _flat(profitability_compute(_stmts(), FY))
    assert m["return_on_equity"].value == pytest.approx(80.0 / 400.0)


def test_roe_is_empty_and_flagged_on_negative_prior_equity() -> None:
    m = _flat(profitability_compute(_stmts(prior_equity=-10.0), FY))
    assert m["return_on_equity"].value is None
    assert m["return_on_equity"].inputs["flag_return_on_equity_negative_equity"] == 1.0


def test_roic_uses_invested_capital_at_the_prior_year_end() -> None:
    m = _flat(roic_compute(_stmts(), FY))
    # IC = debt 200 + equity 400 + NCI 0 - cash 50 - STI 0 = 550
    assert m["return_on_invested_capital"].value == pytest.approx(100 * 0.79 / 550.0)


def test_roic_is_empty_and_flagged_when_invested_capital_is_not_positive() -> None:
    m = _flat(roic_compute(_stmts(prior_debt=0.0, prior_equity=0.0, prior_cash=100.0), FY))
    assert m["return_on_invested_capital"].value is None
    assert (
        m["return_on_invested_capital"].inputs[
            "flag_return_on_invested_capital_invested_capital_not_positive"
        ]
        == 1.0
    )


# -- D3: MET-03 / APP-04c, the NOPAT tax and the filing's own effective rate ------------------------


def test_nopat_tax_is_21_percent_for_a_non_reit() -> None:
    rate, reason = nopat_tax_rate(_stmts(), FY)
    assert rate == pytest.approx(0.21)
    assert reason == "statutory_21"
    assert _flat(roic_compute(_stmts(), FY))["nopat"].value == pytest.approx(79.0)


def test_nopat_tax_is_zero_on_an_operating_loss() -> None:
    rate, reason = nopat_tax_rate(_stmts(operating_income=-50.0), FY)
    assert rate == 0.0
    assert reason == "operating_loss"
    assert _flat(roic_compute(_stmts(operating_income=-50.0), FY))["nopat"].value == pytest.approx(
        -50.0
    )


def test_a_reit_is_taxed_at_its_own_rate_never_21_percent() -> None:
    stmts = _stmts()
    stmts.reit = True
    rate, reason = nopat_tax_rate(stmts, FY)
    assert rate == pytest.approx(20.0 / 120.0)  # the filing's own rate
    assert reason == "reit_effective"
    assert rate != pytest.approx(0.21)


def test_a_reit_with_nonpositive_pretax_income_is_taxed_at_zero() -> None:
    stmts = _stmts(pretax_income=-5.0)
    stmts.reit = True
    rate, reason = nopat_tax_rate(stmts, FY)
    assert rate == 0.0
    assert reason == "reit_no_rate"


def test_effective_tax_rate_metric_is_empty_when_pretax_is_missing_or_nonpositive() -> None:
    assert filing_effective_tax_rate(_stmts(), FY) == pytest.approx(20.0 / 120.0)
    assert filing_effective_tax_rate(_stmts(pretax_income=0.0), FY) is None
    assert filing_effective_tax_rate(_stmts(pretax_income=-1.0), FY) is None
    assert filing_effective_tax_rate(_stmts(pretax_income=None), FY) is None
    m = _flat(roic_compute(_stmts(pretax_income=0.0), FY))
    assert m["effective_tax_rate"].value is None
    # the robustness ROIC at the effective rate is empty when the rate is empty
    assert m["return_on_invested_capital_effective_rate"].value is None


def test_roic_at_the_effective_rate_is_reported_as_robustness() -> None:
    m = _flat(roic_compute(_stmts(), FY))
    assert m["return_on_invested_capital_effective_rate"].value == pytest.approx(
        100 * (1 - 20.0 / 120.0) / 550.0
    )


# -- D4: MET-04 / PER-12-14, growth -----------------------------------------------------------------


def test_growth_is_empty_and_flagged_when_the_base_is_nonpositive() -> None:
    payload = _payload()
    # make the prior-year revenue zero and the prior-year net income negative
    for row in payload["income_statement"]:
        if row["concept"] == G + "RevenueFromContractWithCustomerExcludingAssessedTax":
            row[PRIOR_FY] = 0.0
        if row["concept"] == G + "NetIncomeLoss":
            row[PRIOR_FY] = -5.0
    m = _flat(growth_compute(Statements.from_payload(payload), FY, PRIOR_FY))
    assert m["revenue_growth"].value is None
    assert m["revenue_growth"].inputs["flag_revenue_growth_growth_base_not_positive"] == 1.0
    assert m["net_income_growth"].value is None
    assert m["net_income_growth"].inputs["flag_net_income_growth_growth_base_not_positive"] == 1.0


def test_a_growth_across_asc842_adoption_is_kept_and_flagged() -> None:
    # a non-calendar (June) filer: the FY2019 base began before 2018-12-16, the FY2021 end after
    payload = _payload()
    income = [
        _row(
            G + "OperatingIncomeLoss",
            "Operating income",
            **{"2021-06-30 (FY)": 100.0, "2019-06-30 (FY)": 50.0},
        ),
        _row(
            G + "NetIncomeLoss", "Net income", **{"2021-06-30 (FY)": 80.0, "2019-06-30 (FY)": 40.0}
        ),
    ]
    payload["income_statement"] = income
    stmts = Statements.from_payload(payload)
    m = _flat(cagr_compute(stmts, "2021-06-30 (FY)"))
    assert m["net_income_cagr"].value is not None
    assert m["net_income_cagr"].inputs["flag_crosses_asc842"] == 1.0


def test_a_calendar_filers_cagr_does_not_cross_asc842() -> None:
    payload = _payload()
    payload["income_statement"] = [
        _row(
            G + "NetIncomeLoss", "Net income", **{"2021-12-31 (FY)": 80.0, "2019-12-31 (FY)": 40.0}
        ),
    ]
    stmts = Statements.from_payload(payload)
    m = _flat(cagr_compute(stmts, "2021-12-31 (FY)"))
    assert "flag_crosses_asc842" not in m["net_income_cagr"].inputs


def test_the_cecl_flag_is_only_for_a_lender() -> None:
    payload = _payload()
    payload["income_statement"] = [
        _row(
            G + "NetIncomeLoss", "Net income", **{"2021-12-31 (FY)": 80.0, "2019-12-31 (FY)": 40.0}
        ),
    ]
    stmts = Statements.from_payload(payload)
    # a January FY end: FY2019 began 2018-01-01 (before 2019-12-16), FY2021 began 2020-01-01 (after)
    m = _flat(cagr_compute(stmts, "2021-12-31 (FY)"))
    assert "flag_crosses_cecl" not in m["net_income_cagr"].inputs
    stmts.sub_industry = "Regional Banks"
    m = _flat(cagr_compute(stmts, "2021-12-31 (FY)"))
    assert m["net_income_cagr"].inputs["flag_crosses_cecl"] == 1.0


# -- D5: MET-09, non-positive denominators -----------------------------------------------------------


def test_net_debt_to_ebitda_is_empty_on_a_nonpositive_ebitda() -> None:
    zero = _flat(leverage_compute(_stmts(operating_income=0.0, da=0.0), FY))
    assert zero["net_debt_to_ebitda"].value is None
    assert "flag_net_debt_to_ebitda_denominator_negative" not in zero["net_debt_to_ebitda"].inputs
    negative = _flat(leverage_compute(_stmts(operating_income=-100.0, da=10.0), FY))
    assert negative["net_debt_to_ebitda"].value is None
    assert (
        negative["net_debt_to_ebitda"].inputs["flag_net_debt_to_ebitda_denominator_negative"] == 1.0
    )


def test_net_cash_with_positive_ebitda_is_a_valid_negative_value() -> None:
    m = _flat(leverage_compute(_stmts(prior_debt=10.0), FY))
    # net debt = 210 - 60 = 150 (current); make it negative by a small debt
    payload = _payload()
    for row in payload["balance_sheet"]:
        if row["concept"] == G + "LongTermDebt":
            row["2024-12-31"] = 10.0
    m = _flat(leverage_compute(Statements.from_payload(payload), FY))
    assert m["net_debt_to_ebitda"].value is not None
    assert m["net_debt_to_ebitda"].value < 0.0
    assert m["net_debt_to_ebitda"].inputs["flag_net_debt_to_ebitda_net_cash"] == 1.0


def test_fcf_conversion_is_empty_and_flagged_on_nonpositive_net_income() -> None:
    m = _flat(cashflow_compute(_stmts(net_income=-5.0), FY))
    assert m["free_cash_flow_conversion"].value is None
    assert (
        m["free_cash_flow_conversion"].inputs["flag_free_cash_flow_conversion_denominator_negative"]
        == 1.0
    )
    zero = _flat(cashflow_compute(_stmts(net_income=0.0), FY))
    assert zero["free_cash_flow_conversion"].value is None


def test_interest_coverage_is_empty_only_when_interest_is_nonpositive() -> None:
    payload = _payload()
    payload["income_statement"].append(_row(G + "InterestExpense", "Interest", **{FY: 25.0}))
    m = _flat(leverage_compute(Statements.from_payload(payload), FY))
    assert m["interest_coverage"].value == pytest.approx(100.0 / 25.0)
    payload["income_statement"][-1][FY] = -25.0
    m = _flat(leverage_compute(Statements.from_payload(payload), FY))
    assert (
        m["interest_coverage"].value is None
    )  # an operating loss with positive interest stays valid


# -- D6: MD-02, invested capital with NCI and short-term investments ---------------------------------


def test_invested_capital_adds_nci_and_nets_short_term_investments() -> None:
    stmts = _stmts(prior_sti=30.0, minority_interest=40.0)
    cap = stmts.prior_year_capital(FY)
    ic = capital.invested_capital(cap, income_filed=True)
    assert ic.value == pytest.approx(200.0 + 400.0 + 40.0 - 50.0 - 30.0)


def test_nci_is_the_equity_nci_plus_the_redeemable_nci() -> None:
    cap = _stmts(minority_interest=40.0, redeemable_nci=15.0).prior_year_capital(FY)
    assert capital.nci(cap, income_filed=True).value == pytest.approx(55.0)


def test_nci_uses_con04_when_no_nci_balance_is_filed() -> None:
    payload = _payload(nci_income=10.0)
    payload["balance_sheet"].append(
        _row(
            G + "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            "Equity incl NCI",
            **{"2023-12-31": 430.0},
        )
    )
    cap = Statements.from_payload(payload).prior_year_capital(FY)
    res = capital.nci(cap, income_filed=True)
    assert res.value == pytest.approx(430.0 - 400.0)
    assert "nci_con04_identity" in res.flags


def test_nci_is_zero_only_when_no_nci_income_is_filed() -> None:
    cap = _stmts().prior_year_capital(FY)
    assert capital.nci(cap, income_filed=False).value == 0.0
    assert capital.nci(cap, income_filed=True).value is None
    assert "nci_capture_failure" in capital.nci(cap, income_filed=True).flags


def test_short_term_investments_inside_the_cash_line_are_not_counted_twice() -> None:
    # the prior column files only the combined "cash, cash equivalents and short-term investments"
    # line beside a separate STI line: the STI is already inside cash and is not netted again.
    payload = _payload(prior_cash=None, prior_sti=30.0)
    payload["balance_sheet"].append(
        _row(
            G + "CashCashEquivalentsAndShortTermInvestments",
            "Cash, cash equivalents and short-term investments",
            **{"2023-12-31": 80.0},
        )
    )
    stmts = Statements.from_payload(payload)
    cap = stmts.prior_year_capital(FY)
    sti = capital.net_short_term_investments(cap)
    assert sti.value == 0.0
    assert "short_term_investments_in_cash" in sti.flags


# -- D7: MD-35, enterprise value --------------------------------------------------------------------


def _mcap(value: float = 5_000.0) -> MarketCap:
    return MarketCap(
        asset_id=1,
        value=value,
        shares=100.0,
        share_count=100.0,
        share_as_of="2024-12-31",
        share_age_days=0,
        filing_id=1,
        close=value / 100.0,
        close_date="2024-12-31",
        n_classes=0,
        split_factor=1.0,
    )


def test_enterprise_value_adds_nci_and_nets_short_term_investments() -> None:
    payload = _payload(minority_interest=40.0, current_sti=25.0)
    out = _flat(
        valuation_compute(
            Statements.from_payload(payload), FY, ClosePrice("2024-12-31", 50.0), market_cap=_mcap()
        )
    )
    # EV = mcap 5,000 + debt 210 + NCI 40 - cash 60 - STI 25
    assert out["enterprise_value"].value == pytest.approx(5_000.0 + 210.0 + 40.0 - 60.0 - 25.0)


def test_enterprise_value_is_empty_when_nci_cannot_be_captured() -> None:
    payload = _payload(
        nci_income=10.0
    )  # NCI income filed but no NCI balance and no CON-04 identity
    out = _flat(
        valuation_compute(
            Statements.from_payload(payload), FY, ClosePrice("2024-12-31", 50.0), market_cap=_mcap()
        )
    )
    assert out["enterprise_value"].value is None


# -- D8: MD-38, enterprise FCF yield ----------------------------------------------------------------


def test_enterprise_fcf_yield_is_the_c10_ebit_form() -> None:
    stmts = _stmts()
    out = _flat(valuation_compute(stmts, FY, ClosePrice("2024-12-31", 50.0), market_cap=_mcap()))
    # WC = (300 - 60 - 0) - (150 - 20) = 110; prior WC = (280 - 50) - (140 - 20) = 110; Δ = 0
    # FCFF = NOPAT 79 - (30 - 10 + 0) = 59; EV = 5,000 + 210 - 60 = 5,150
    assert out["enterprise_fcf_yield"].value == pytest.approx(59.0 / 5_150.0)
    assert out["enterprise_fcf_yield"].inputs["delta_working_capital"] == pytest.approx(0.0)


def test_enterprise_fcf_yield_is_empty_when_the_balance_sheet_is_not_classified() -> None:
    payload = _payload()
    payload["balance_sheet"] = [
        r
        for r in payload["balance_sheet"]
        if r["concept"] not in (G + "AssetsCurrent", G + "LiabilitiesCurrent")
    ]
    out = _flat(
        valuation_compute(
            Statements.from_payload(payload), FY, ClosePrice("2024-12-31", 50.0), market_cap=_mcap()
        )
    )
    assert out["enterprise_fcf_yield"].value is None


def test_a_10q_without_the_prior_quarter_balance_sheet_is_flagged() -> None:
    payload = _payload()
    income = [
        _row(G + "OperatingIncomeLoss", "Operating income", **{Q: 25.0}),
        _row(G + "NetIncomeLoss", "Net income", **{Q: 20.0}),
    ]
    payload["income_statement"] = income
    ttm = {
        "operating_income": 100.0,
        "capital_expenditure": 30.0,
        "depreciation_amortization": 10.0,
    }
    out = _flat(
        valuation_compute(
            Statements.from_payload(payload),
            Q,
            ClosePrice("2024-06-30", 50.0),
            ttm=ttm,
            market_cap=_mcap(),
        )
    )
    assert out["enterprise_fcf_yield"].value is None
    assert out["enterprise_fcf_yield"].inputs["flag_enterprise_fcf_yield_prior_wc_missing"] == 1.0


# -- D9: EBITDA persisted ---------------------------------------------------------------------------


def test_ebitda_is_persisted_as_operating_income_plus_da() -> None:
    m = _flat(leverage_compute(_stmts(operating_income=100.0, da=10.0), FY))
    assert m["ebitda"].value == pytest.approx(110.0)


# -- D10: the FCF rename ----------------------------------------------------------------------------


def test_free_cash_flow_is_not_named_free_cash_flow_to_equity() -> None:
    out = _flat(valuation_compute(_stmts(), FY, ClosePrice("2024-12-31", 50.0), market_cap=_mcap()))
    inputs = out["free_cash_flow_yield"].inputs
    assert "free_cash_flow" in inputs
    assert "free_cash_flow_to_equity" not in inputs


def test_safe_div_is_still_the_shared_guard() -> None:
    assert safe_div(1.0, 2.0) == 0.5
