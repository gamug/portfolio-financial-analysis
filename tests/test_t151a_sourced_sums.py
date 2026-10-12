"""T-151 part 1: the sourced D&A sum (C1), the sourced debt sums (C2) and the net capex element
(C3), all hermetic -- synthetic payloads plus the real fixtures the debt tests already carry.

C1 (MD-01, ID-02b): when no ``depreciation_amortization`` concept is filed, D&A is the sum of the
filed D&A-nature children of the FASB 2026 cash-flow arc -- ``Depreciation`` (required) plus
``Depletion`` / ``OtherDepreciationAndAmortization`` / ``AmortizationOfIntangibleAssets`` -- and
only when every other D&A-named cash-flow line is one of the four; else the sum is refused and D&A
stays empty. ``Depreciation`` alone is still refused.

C2 (MET-07, ID-02b): when no concept the resolver reads today is filed, long-term debt is the
filed children of ``LongTermDebtNoncurrent`` (sfp-cls), else of ``LongTermDebt`` (sfp-dbo, flagged
``includes_current_maturities``), and only when every debt-named balance-sheet line is a child of
the tree used or a short-term map concept; else refused and the debt stays a capture failure.

C3 (MET-08): ``PaymentsForProceedsFromProductiveAssets`` is read only when its filed value is > 0;
a net-proceeds value is skipped for the caption fallback.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fundamental_agent.metrics import cashflow, leverage
from fundamental_agent.statements import (
    LONG_TERM_DEBT_CHILDREN,
    LONG_TERM_DEBT_NONCURRENT_CHILDREN,
    SHORT_TERM_DEBT_LAST,
    SHORT_TERM_DEBT_MAP,
    G,
    Statements,
)

FIXTURES = Path(__file__).parent / "fixtures"
FY = "2025-12-31 (FY)"
Q2 = "2025-06-30 (Q2)"
YTD = "2025-06-30 (YTD)"


def _cashflow(*rows: dict[str, Any], reit: bool = False) -> Statements:
    return Statements.from_payload(
        {"income_statement": [], "balance_sheet": [], "cash_flow": list(rows), "corrections": []},
        reit=reit,
    )


def _row(concept: str, label: str, val: float | None = None, column: str = FY) -> dict[str, Any]:
    return {
        "concept": concept,
        "label": label,
        "abstract": False,
        "dimension": False,
        "preferred_sign": 1,
        column: val,
    }


# -- C1: the sourced D&A sum ---------------------------------------------------------------------


def test_c1_the_abbv_shape_depreciation_plus_intangibles_is_summed() -> None:
    """ABBV 10-Q 2022-06-30 files ``Depreciation`` (401M) and ``AmortizationOfIntangibleAssets``
    (3,704M) and no aggregate: the FASB 2026 arc's two D&A-nature children sum, flagged."""
    stmts = _cashflow(
        _row(G + "Depreciation", "Depreciation", 401_000_000.0),
        _row(
            G + "AmortizationOfIntangibleAssets",
            "Amortization of intangible assets",
            3_704_000_000.0,
        ),
    )
    res = stmts.resolve("depreciation_amortization", FY)
    assert res.value == 4_105_000_000.0
    assert res.how == "map" and res.flags == ("da_sourced_sum",)
    assert res.concept == f"{G}AmortizationOfIntangibleAssets + {G}Depreciation"


def test_c1_depreciation_alone_is_still_refused() -> None:
    """Depreciation alone is not D&A (condition (b)): empty, ``depreciation_only_not_da`` as today."""
    stmts = _cashflow(_row(G + "Depreciation", "Depreciation", 401_000_000.0))
    res = stmts.resolve("depreciation_amortization", FY)
    assert res.value is None and res.flags == ("depreciation_only_not_da",)


def test_c1_depreciation_plus_an_outside_amortization_is_not_d_and_a() -> None:
    """``AmortizationOfFinancingCosts`` is a child of ``AdjustmentForAmortization`` outside D&A:
    there is no D&A-nature amortization child, so the sum is not formed and D&A stays empty."""
    stmts = _cashflow(
        _row(G + "Depreciation", "Depreciation", 401_000_000.0),
        _row(G + "AmortizationOfFinancingCosts", "Amortization of loan fees", 12_000_000.0),
    )
    res = stmts.resolve("depreciation_amortization", FY)
    assert res.value is None


def test_c1_depreciation_intangibles_and_an_extension_da_line_refuses_the_sum() -> None:
    """Condition (c) "only lines": an extension D&A line beside the four refuses the sum
    (``da_sum_refused_other_lines``) and D&A stays empty."""
    stmts = _cashflow(
        _row(G + "Depreciation", "Depreciation", 401_000_000.0),
        _row(
            G + "AmortizationOfIntangibleAssets",
            "Amortization of intangible assets",
            3_704_000_000.0,
        ),
        _row("x_AmortizationOfContentAssets", "Amortization of content assets", 100_000_000.0),
    )
    res = stmts.resolve("depreciation_amortization", FY)
    assert res.value is None and "da_sum_refused_other_lines" in res.flags


def test_c1_a_filed_total_is_the_total_and_no_sum_is_attempted() -> None:
    """Tier 1 is unchanged: a filed ``DepreciationDepletionAndAmortization`` wins outright."""
    stmts = _cashflow(
        _row(
            G + "DepreciationDepletionAndAmortization", "Depreciation and amortization", 9_000_000.0
        ),
        _row(G + "Depreciation", "Depreciation", 401_000_000.0),
        _row(
            G + "AmortizationOfIntangibleAssets", "Amortization of intangible assets", 100_000_000.0
        ),
    )
    res = stmts.resolve("depreciation_amortization", FY)
    assert res.value == 9_000_000.0 and res.how == "concept"
    assert "da_sourced_sum" not in res.flags


def test_c1_the_year_to_date_column_works_through_the_same_map() -> None:
    """A 10-Q's YTD column: the same sourced sum, keyed on the column (T-105's TTM reads it)."""
    stmts = Statements.from_payload(
        {
            "income_statement": [],
            "balance_sheet": [],
            "cash_flow": [
                _row(G + "Depreciation", "Depreciation", 200_000_000.0, YTD),
                _row(G + "Depletion", "Depletion", 50_000_000.0, YTD),
            ],
            "corrections": [],
        }
    )
    res = stmts.resolve("depreciation_amortization", YTD)
    assert res.value == 250_000_000.0 and res.flags == ("da_sourced_sum",)
    assert stmts.resolve("depreciation_amortization", Q2).value is None


# -- C2: the sourced debt sums -------------------------------------------------------------------


def _balance(*rows: dict[str, Any]) -> Statements:
    return Statements.from_payload(
        {"income_statement": [], "balance_sheet": list(rows), "cash_flow": [], "corrections": []}
    )


def _inst(concept: str, label: str, val: float | None = None) -> dict[str, Any]:
    return {
        "concept": concept,
        "label": label,
        "abstract": False,
        "dimension": False,
        "preferred_sign": 1,
        "2025-12-31": val,
    }


def test_c2_the_noncurrent_tree_sums_without_the_current_maturities_flag() -> None:
    """The sfp-cls arc: ``SecuredLongTermDebt`` + ``UnsecuredLongTermDebt`` (both noncurrent) sum,
    and the map is not flagged ``includes_current_maturities``."""
    stmts = _balance(
        _inst(G + "SecuredLongTermDebt", "Secured long-term debt", 1_000_000.0),
        _inst(G + "UnsecuredLongTermDebt", "Unsecured long-term debt", 2_000_000.0),
    )
    res = stmts.resolve("long_term_debt", FY)
    assert res.value == 3_000_000.0
    assert res.flags == ("debt_sourced_sum",)


def test_c2_a_debt_line_outside_the_tree_refuses_the_sum() -> None:
    """Condition (c2) "only lines": ``SeniorNotes`` is filed beside the tree's children and is no
    child of either tree nor a short-term map concept -- the sum is refused and the debt is a
    capture failure, as today."""
    stmts = _balance(
        _inst(G + "SecuredLongTermDebt", "Secured long-term debt", 1_000_000.0),
        _inst(G + "UnsecuredLongTermDebt", "Unsecured long-term debt", 2_000_000.0),
        _inst(G + "SeniorNotes", "Senior notes", 500_000.0),
    )
    res = stmts.resolve("long_term_debt", FY)
    assert res.value is None and "debt_sum_refused_other_lines" in res.flags
    total = stmts.total_debt(FY)
    assert total.value is None and "capture_failure" in total.flags


def test_c2_a_filed_long_term_debt_noncurrent_wins_and_no_sum_is_attempted() -> None:
    stmts = _balance(
        _inst(G + "LongTermDebtNoncurrent", "Long-term debt", 7_000_000.0),
        _inst(G + "SecuredDebt", "Secured debt", 1_000_000.0),
    )
    res = stmts.resolve("long_term_debt", FY)
    assert res.value == 7_000_000.0 and res.how == "concept" and res.flags == ()


def test_c2_the_short_term_map_is_allowed_beside_the_tree() -> None:
    """A short-term map concept beside the tree is not an "other line": the sum stands and the
    short-term term is added to the total (MET-07)."""
    stmts = _balance(
        _inst(G + "SecuredLongTermDebt", "Secured long-term debt", 1_000_000.0),
        _inst(G + "UnsecuredLongTermDebt", "Unsecured long-term debt", 2_000_000.0),
        _inst(G + "ShortTermBorrowings", "Short-term borrowings", 400_000.0),
        _inst(G + "Assets", "Total assets", 9_000_000.0),
        _inst(G + "StockholdersEquity", "Equity", 5_000_000.0),
    )
    total = stmts.total_debt(FY)
    assert total.value == 3_400_000.0
    assert "debt_sourced_sum" in total.flags and "includes_current_maturities" not in total.flags


def test_c2_a_reit_fixture_sums_the_secured_and_unsecured_pair() -> None:
    """The real UDR capture: ``SecuredDebt`` + ``UnsecuredDebt`` sum to the filed values."""
    data = json.loads(
        (FIXTURES / "financials_UDR_10-K_2026_acc-0000074208-26-000013.json").read_text()
    )["data"]
    stmts = Statements.from_payload(data)
    key = stmts.fy_periods()[-1].key
    res = stmts.resolve("long_term_debt", key)
    assert res.value == 5_821_369_000.0
    assert res.flags == ("debt_sourced_sum", "includes_current_maturities")


def test_c2_the_no_debt_line_zero_rule_is_unchanged() -> None:
    """ID-02b's zero: no debt line, a complete balance sheet (assets, equity, cash), no interest
    expense -- still 0, flagged ``no_debt_lines``, with the sourced maps read first and empty."""
    stmts = _balance(
        _inst(G + "Assets", "Total assets", 1_000.0),
        _inst(G + "StockholdersEquity", "Equity", 600.0),
        _inst(G + "CashAndCashEquivalentsAtCarryingValue", "Cash", 100.0),
    )
    total = stmts.total_debt(FY)
    assert total.value == 0.0 and total.flags == ("no_debt_lines",)


# -- C3: the net capex element -------------------------------------------------------------------


def test_c3_a_negative_net_element_is_skipped_for_the_caption_fallback() -> None:
    """COP 2022Q1: ``PaymentsForProceedsFromProductiveAssets`` is filed at -2,332M (a caption that
    does not name proceeds, so the sign rule decides) -- net proceeds, not capex. The tier is
    skipped, flagged, and the caption fallback resolves the filer's own "Capital expenditures and
    investments" line."""
    stmts = _cashflow(
        _row(
            G + "PaymentsForProceedsFromProductiveAssets",
            "Payments for productive assets, net",
            -2_332_000_000.0,
        ),
        _row(
            "cop_PaymentToAcquireProductiveAssetsAndInvestments",
            "Capital expenditures and investments",
            2_000_000_000.0,
        ),
    )
    res = stmts.resolve("capital_expenditure", FY)
    assert res.value == 2_000_000_000.0
    assert res.concept == "cop_PaymentToAcquireProductiveAssetsAndInvestments"
    assert "net_element_net_proceeds" in res.flags and "caption_fallback" in res.flags


def test_c3_a_positive_net_element_is_kept_as_the_last_resort() -> None:
    """A positive net element is a net payment: the tier still resolves it, flagged as before."""
    stmts = _cashflow(
        _row(
            G + "PaymentsForProceedsFromProductiveAssets",
            "Payments for productive assets, net",
            120_000_000.0,
        )
    )
    res = stmts.resolve("capital_expenditure", FY)
    assert res.value == 120_000_000.0
    assert "net_element_last_resort" in res.flags
    assert "net_element_net_proceeds" not in res.flags


def test_c3_a_zero_net_element_is_proceeds_and_the_tier_is_skipped() -> None:
    stmts = _cashflow(
        _row(G + "PaymentsForProceedsFromProductiveAssets", "Productive assets, net", 0.0),
        _row("x_CapitalExpenditures", "Capital expenditures", 55_000_000.0),
    )
    res = stmts.resolve("capital_expenditure", FY)
    assert res.value == 55_000_000.0
    assert "net_element_net_proceeds" in res.flags


def test_c3_the_earlier_tiers_are_unchanged_by_the_negative_net_element() -> None:
    """A filed PP&E line still wins before the net element is ever reached."""
    stmts = _cashflow(
        _row(
            G + "PaymentsToAcquirePropertyPlantAndEquipment", "Capital expenditures", 300_000_000.0
        ),
        _row(
            G + "PaymentsForProceedsFromProductiveAssets",
            "Proceeds from asset dispositions",
            -10_000_000.0,
        ),
    )
    res = stmts.resolve("capital_expenditure", FY)
    assert res.value == 300_000_000.0 and "net_element_net_proceeds" not in res.flags


def test_c3_the_new_capex_flag_reaches_the_metric_inputs() -> None:
    """C6: every new flag is a ``flag_<item>_<flag>`` input of the metric that reads the item, as
    T-148's flags are -- here the capex flag on the cash-flow group."""
    stmts = _cashflow(
        _row(
            G + "PaymentsForProceedsFromProductiveAssets",
            "Proceeds from asset dispositions",
            -2_332_000_000.0,
        ),
        _row("x_CapitalExpenditures", "Capital expenditures", 2_000_000_000.0),
        _row(G + "NetCashProvidedByUsedInOperatingActivities", "Operating cash flow", 5_000_000.0),
        _row(G + "NetCashProvidedByUsedInInvestingActivities", "Investing", -2_000_000_000.0),
    )
    results = cashflow.compute(stmts, FY)
    inputs = results[0].inputs
    assert inputs["flag_capital_expenditure_net_element_captioned_proceeds"] == 1.0
    assert inputs["flag_capital_expenditure_caption_fallback"] == 1.0


def test_c3_a_line_captioned_proceeds_is_skipped_whatever_its_sign() -> None:
    """C3 addition: COP's 2021-03-31 comparative on "Proceeds from asset dispositions" is displayed
    -17,000,000 with ``preferred_sign`` -1, a filed **+17,000,000** the sign test alone would read as
    capex. The caption decides: skipped, flagged ``net_element_captioned_proceeds``, caption
    fallback tried."""
    row = _row(
        G + "PaymentsForProceedsFromProductiveAssets",
        "Proceeds from asset dispositions",
        17_000_000.0,
    )
    row["preferred_sign"] = -1  # the displayed -17,000,000 negated by the filed sign
    stmts = _cashflow(
        {**row, "2025-12-31 (FY)": -17_000_000.0},
        _row(
            "cop_PaymentToAcquireProductiveAssetsAndInvestments",
            "Capital expenditures and investments",
            1_900_000_000.0,
        ),
    )
    res = stmts.resolve("capital_expenditure", FY)
    assert res.value == 1_900_000_000.0
    assert "net_element_captioned_proceeds" in res.flags
    assert "net_element_last_resort" not in res.flags
    assert "caption_fallback" in res.flags


def test_c3_a_caption_naming_a_sale_or_disposition_is_also_skipped() -> None:
    for caption in (
        "Net proceeds from sale of assets",
        "Gain on disposition of assets",
        "Proceeds",
    ):
        stmts = _cashflow(_row(G + "PaymentsForProceedsFromProductiveAssets", caption, 5_000_000.0))
        res = stmts.resolve("capital_expenditure", FY)
        assert res.value is None and "net_element_captioned_proceeds" in res.flags, caption


def test_c3_a_positive_net_payment_caption_is_still_kept() -> None:
    """A caption that names neither proceeds nor a sale/disposition keeps the sign test: a positive
    net element is a net payment (the last resort, as before)."""
    stmts = _cashflow(
        _row(
            G + "PaymentsForProceedsFromProductiveAssets",
            "Payments for productive assets, net",
            120_000_000.0,
        )
    )
    res = stmts.resolve("capital_expenditure", FY)
    assert res.value == 120_000_000.0
    assert "net_element_last_resort" in res.flags
    assert "net_element_captioned_proceeds" not in res.flags


def test_c1_and_c2_the_new_flags_reach_the_metric_inputs() -> None:
    """C6: the D&A sourced sum and the debt sum reach the leverage group's flags for their items
    (``flag_depreciation_amortization_*`` / ``flag_total_debt_*``), where cycle and the company card
    read them."""
    da = _cashflow(
        _row(G + "Depreciation", "Depreciation", 401_000_000.0),
        _row(
            G + "AmortizationOfIntangibleAssets", "Amortization of intangible assets", 100_000_000.0
        ),
        _row(
            G + "NetCashProvidedByUsedInOperatingActivities", "Operating cash flow", 5_000_000_000.0
        ),
        _row(G + "Revenues", "Revenues", 8_000_000_000.0),
    )
    inputs = {r.name: r.inputs for r in leverage.compute(da, FY)}
    assert any("flag_depreciation_amortization_da_sourced_sum" in i for i in inputs.values())

    data = json.loads(
        (FIXTURES / "financials_UDR_10-K_2026_acc-0000074208-26-000013.json").read_text()
    )["data"]
    stmts = Statements.from_payload(data)
    key = stmts.fy_periods()[-1].key
    debt_inputs = {r.name: r.inputs for r in leverage.compute(stmts, key)}
    assert any("flag_total_debt_debt_sourced_sum" in i for i in debt_inputs.values())


def test_the_long_term_sum_and_the_short_term_terms_never_share_a_concept() -> None:
    """The double-count hard stop (director decision after hard stop 4'): no concept may be in both
    the long-term sum and the short-term terms of the same total debt. The two trees and the
    short-term map are disjoint by construction, and a real shape counts each concept once."""
    cls, dbo = set(LONG_TERM_DEBT_NONCURRENT_CHILDREN), set(LONG_TERM_DEBT_CHILDREN)
    short = {c for _term, cs in SHORT_TERM_DEBT_MAP for c in cs} | {SHORT_TERM_DEBT_LAST}
    assert not (cls & dbo)
    assert not (cls & short)
    assert not (dbo & short)

    stmts = _balance(
        _inst(G + "SecuredLongTermDebt", "Secured long-term debt", 1_000_000.0),
        _inst(G + "UnsecuredLongTermDebt", "Unsecured long-term debt", 2_000_000.0),
        _inst(G + "LongTermDebtCurrent", "Current maturities", 300_000.0),
        _inst(G + "ShortTermBorrowings", "Short-term borrowings", 400_000.0),
        _inst(G + "Assets", "Total assets", 9_000_000.0),
        _inst(G + "StockholdersEquity", "Equity", 5_000_000.0),
    )
    long_term = stmts.resolve("long_term_debt", FY)
    total = stmts.total_debt(FY)
    lt_concepts = set((long_term.concept or "").split(" + "))
    total_concepts = set((total.concept or "").split(" + "))
    assert lt_concepts <= total_concepts
    assert not (lt_concepts & (total_concepts - lt_concepts))
    assert long_term.value == 3_000_000.0 and total.value == 3_700_000.0
