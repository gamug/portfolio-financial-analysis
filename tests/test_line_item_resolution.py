"""T-148 part 1 (a)-(c): line items in the filed sign, concept precedence, parent net income and
income tax -- on the real gateway captures in ``tests/fixtures`` (every rule is cited by its
checklist ID, ``docs/checklist_sec/checklist_v1.3.md``).

* ID-11 / ID-12 -- a value is read in the sign it was filed in (displayed value times
  ``preferred_sign``); a wrong sign is rejected and flagged, never repaired with ``abs()``.
* ID-02a -- exact concepts first, in the spec's order; a label or standard match never outranks them.
* ID-03 (N13) -- parent earnings are ``NetIncomeLoss``; else ``ProfitLoss`` less the NCI income;
  never the "available to common" line.
* ID-02b / D-06(c) -- income tax is the total or current + deferred, never one component.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from fundamental_agent.metrics.cashflow import free_cash_flow
from fundamental_agent.metrics.roic import effective_tax_rate
from fundamental_agent.statements import REGISTRY, LineItem, Statements, filed_value, iter_facts

FIXTURES = Path(__file__).parent / "fixtures"


def _payload(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())["data"]
    return data


def _stmts(name: str) -> Statements:
    return Statements.from_payload(_payload(name))


def _latest(stmts: Statements, *, quarter: bool = False) -> str:
    periods = stmts.quarter_periods() if quarter else stmts.fy_periods()
    return periods[-1].key


def _row(payload: dict[str, Any], statement: str, concept: str) -> dict[str, Any]:
    return next(
        r
        for r in payload[statement]
        if r["concept"] == concept and not r.get("dimension") and not r.get("abstract")
    )


STZ = "financials_STZ_10-Q_2023_acc-0000016918-23-000190.json"
AAPL = "financials_AAPL_10-K_2023.json"
DUKE = "financials_DUK_10-K_2025_acc-0001326160-25-000072.json"
APA_Q = "financials_APA_10-Q_2021_acc-0001673379-21-000012.json"
APA_K = "financials_APA_10-K_2022_acc-0001784031-22-000009.json"
WAT = "financials_WAT_10-Q_2023_acc-0001193125-23-271909.json"
WAT_21 = "financials_WAT_10-Q_2021_acc-0001193125-21-319595.json"
APO = "financials_APO_10-K_2023_acc-0001858681-23-000007.json"
PM = "financials_PM_10-K_2026_acc-0001628280-26-005939.json"
HPE = "financials_HPE_10-K_2024_acc-0001645590-24-000139.json"
PNC = "financials_PNC_10-K_2024_acc-0000713676-24-000028.json"
ALLE_Q = "financials_ALLE_10-Q_2025_acc-0001579241-25-000030.json"
MA = "financials_MA_10-K_2025_acc-0001141391-25-000011.json"
ETR = "financials_ETR_10-K_2023_acc-0000065984-23-000014.json"


# -- (a) the filed sign: ID-11, ID-12 ----------------------------------------------------------


def test_stz_income_tax_and_cost_of_goods_are_read_in_the_filed_sign() -> None:
    """Constellation Brands' 10-Q displays the expense lines negated (``preferred_sign = -1``):
    the tax is shown as -147.2M and the cost of goods as -1,386.9M. Filed, both are positive --
    read as displayed, the tax rate was forced to 0 and inventory turnover came out negative."""
    payload = _payload(STZ)
    key = _latest(Statements.from_payload(payload), quarter=True)
    tax_row = _row(payload, "income_statement", "us-gaap_IncomeTaxExpenseBenefit")
    cogs_row = _row(payload, "income_statement", "us-gaap_CostOfGoodsAndServicesSold")
    assert tax_row["preferred_sign"] == cogs_row["preferred_sign"] == -1.0
    assert tax_row[key] == -147_200_000.0 and cogs_row[key] == -1_386_900_000.0  # as displayed

    stmts = Statements.from_payload(payload)
    assert stmts.get("income_tax", key) == 147_200_000.0
    assert stmts.get("cogs", key) == 1_386_900_000.0
    assert stmts.resolve("income_tax", key).flags == ()
    assert filed_value(tax_row, key) == 147_200_000.0


def test_apo_tax_benefit_is_negative_as_filed_not_the_displayed_positive() -> None:
    """Apollo's FY2022 10-K (accession 0001858681-23-000007)
    shows "income tax (provision) benefit" as +1,069M beside a pre-tax loss of 5,815M: a benefit,
    displayed positive (``preferred_sign = -1``). Filed it is -1,069M. Read as displayed, the
    benefit was a 1,069M expense (17 of APO's production filings, with the tax rate clamped to 0
    or 21% by it); and ``abs()`` would have made it one again."""
    payload = _payload(APO)
    stmts = Statements.from_payload(payload)
    key = _latest(stmts)
    row = _row(payload, "income_statement", "us-gaap_IncomeTaxExpenseBenefit")
    assert row["preferred_sign"] == -1.0 and row[key] == 1_069_000_000.0
    assert stmts.get("income_tax", key) == -1_069_000_000.0
    assert stmts.get("pretax_income", key) == -5_815_000_000.0


def test_wat_interest_expense_displayed_negative_is_filed_positive() -> None:
    """Waters displays its interest expense negated (-30.4M, ``preferred_sign = -1``): 202 of the
    319 production values were stored negative for that reason (N23). Filed, it is positive."""
    stmts = _stmts(WAT)
    key = _latest(stmts, quarter=True)
    row = _row(_payload(WAT), "income_statement", "us-gaap_InterestExpense")
    assert row["preferred_sign"] == -1.0 and row[key] < 0
    assert stmts.get("interest_expense", key) == -row[key] == 30_442_000.0


def test_capex_is_the_positive_amount_paid_and_free_cash_flow_subtracts_it() -> None:
    """Apple's cash-flow statement shows capex as -10,959M (``preferred_sign = -1``); the filed
    value is the +10,959M paid. Free cash flow is operating cash flow less that, no ``abs()``."""
    stmts = _stmts(AAPL)
    key = _latest(stmts)
    assert (
        _row(_payload(AAPL), "cash_flow", "us-gaap_PaymentsToAcquirePropertyPlantAndEquipment")[key]
        == -10_959_000_000.0
    )
    assert stmts.get("capital_expenditure", key) == 10_959_000_000.0
    ocf, fcf = free_cash_flow(stmts, key)
    assert ocf == 110_543_000_000.0
    assert fcf == 110_543_000_000.0 - 10_959_000_000.0


def test_a_negative_filed_capex_is_rejected_and_flagged_never_made_positive() -> None:
    """ID-13 (DQC_0015 lists the capex payment elements) and ID-11: a payment filed negative is a
    wrong sign. The row is rejected -- the item is empty, so is free cash flow -- and the flag
    says why. Before T-148 ``abs()`` turned it into a plausible capex."""
    payload = copy.deepcopy(_payload(AAPL))
    _row(payload, "cash_flow", "us-gaap_PaymentsToAcquirePropertyPlantAndEquipment")[
        "preferred_sign"
    ] = 1.0  # displayed -10,959M, filed -10,959M
    stmts = Statements.from_payload(payload)
    key = _latest(stmts)
    resolution = stmts.resolve("capital_expenditure", key)
    assert resolution.value is None
    assert resolution.flags == ("wrong_sign_rejected",)
    assert free_cash_flow(stmts, key) == (110_543_000_000.0, None)


def test_a_row_without_a_preferred_sign_reads_as_displayed_and_is_flagged() -> None:
    """A value stored before T-148, or sent by a gateway without the field, has no sign hint: it
    reads as displayed and says so -- there is no backfill, and no guess."""
    payload = copy.deepcopy(_payload(AAPL))
    for rows in payload.values():
        for row in rows:
            row.pop("preferred_sign", None)
    stmts = Statements.from_payload(payload)
    key = _latest(stmts)
    tax = stmts.resolve("income_tax", key)
    assert tax.value == 16_741_000_000.0 and tax.flags == ("sign_unverified",)
    # displayed -10,959M cannot be told from a wrong sign: rejected, not read as a payment
    capex = stmts.resolve("capital_expenditure", key)
    assert capex.value is None
    assert set(capex.flags) == {"sign_unverified", "sign_unknown_negative_rejected"}


def test_a_genuine_negative_is_kept_when_it_is_the_filed_sign() -> None:
    """ID-11: a negative filed value is valid unless a DQC rule lists the element. A tax benefit
    (-189.7M, Twitter FY2021) is a benefit, not an error."""
    stmts = _stmts("financials_TWTR_10-K_2022_acc-0001418091-22-000029.json")
    assert stmts.get("income_tax", "2021-12-31 (FY)") == -189_704_000.0


def test_the_facts_are_stored_with_their_sign_hint() -> None:
    """The stored fact keeps the displayed value and the hint, so the filed sign can be rebuilt
    from storage (the audit's replay) without a second source."""
    stmts = _stmts(STZ)
    key = _latest(stmts, quarter=True)
    by_concept = {(f["statement"], f["concept"], f["period_key"]): f for f in iter_facts(stmts)}
    fact = by_concept[("income_statement", "us-gaap_IncomeTaxExpenseBenefit", key)]
    assert fact["value"] == -147_200_000.0 and fact["preferred_sign"] == -1.0


# -- (b) precedence: ID-02a, ID-03 ---------------------------------------------------------------


def _balance_row(concept: str, label: str, value: float, sign: float = 1.0) -> dict[str, Any]:
    return {
        "concept": concept,
        "label": label,
        "standard_concept": "AllEquityBalance",
        "abstract": False,
        "dimension": False,
        "preferred_sign": sign,
        "2025-12-31": value,
    }


def test_pm_equity_is_the_stockholders_equity_row_not_the_label_match() -> None:
    """Philip Morris International FY2025 (accession 0001628280-26-005939):
    ``StockholdersEquityBeforeTreasuryStock``, "Total stockholders' equity before treasury stock"
    (+25,557M), precedes the real equity row in the statement and matches the ``total stockholders``
    label hint, so it was read as equity (D2: +22.6B against -12.6B in the audit's year).
    ``StockholdersEquity`` -- "Total PMI stockholders' deficit", -9,994M -- is the narrowest
    definition and wins wherever it sits (ID-02a); the label is only a fallback."""
    stmts = _stmts(PM)
    key = _latest(stmts)
    payload = _payload(PM)
    rows = [r["concept"] for r in payload["balance_sheet"] if not r.get("dimension")]
    assert rows.index("us-gaap_StockholdersEquityBeforeTreasuryStock") < rows.index(
        "us-gaap_StockholdersEquity"
    )  # the wrong row comes first in the document
    resolution = stmts.resolve("equity", key)
    assert resolution.value == -9_994_000_000.0
    assert resolution.concept == "us-gaap_StockholdersEquity" and resolution.how == "concept"


def test_etr_capex_resolves_tier1_under_met08() -> None:
    """MET-08 Tier 1: Entergy's FY2022 10-K files PaymentsForCapitalImprovements (5,065M) in statement
    order before PaymentsToAcquireProductiveAssets (106M). Tier 1 takes PaymentsForCapitalImprovements (5.07B),
    leaving PaymentsToAcquireProductiveAssets unread."""
    payload = _payload(ETR)
    key = "2022-12-31 (FY)"
    filed = {
        r["concept"]: r[key]
        for r in payload["cash_flow"]
        if r["concept"].startswith("us-gaap_Payments")
        and not r.get("dimension")
        and r.get(key) is not None
    }
    assert filed["us-gaap_PaymentsToAcquireProductiveAssets"] == 106_193_000.0
    resolution = Statements.from_payload(payload).resolve("capital_expenditure", key)
    assert resolution.value == 5_065_126_000.0
    assert resolution.concept == "us-gaap_PaymentsForCapitalImprovements"
    assert resolution.how == "concept"


def test_a_label_match_is_used_only_when_no_listed_concept_is_filed() -> None:
    stmts = Statements.from_payload(
        {
            "balance_sheet": [
                _balance_row("x_Equity", "Total shareholders' equity", 123.0),
            ]
        }
    )
    resolution = stmts.resolve("equity", "2025-12-31 (FY)")
    assert (resolution.value, resolution.how) == (123.0, "label")


def test_concepts_are_tried_in_the_spec_s_order_not_the_document_s() -> None:
    """The spec lists ``StockholdersEquity`` before the NCI-inclusive total; a document that
    files the wider one first still resolves to the first."""
    stmts = Statements.from_payload(
        {
            "balance_sheet": [
                _balance_row(
                    "us-gaap_StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
                    "Total equity",
                    900.0,
                ),
                _balance_row("us-gaap_StockholdersEquity", "Parent equity", 800.0),
            ]
        }
    )
    assert stmts.get("equity", "2025-12-31 (FY)") == 800.0


def test_duke_net_income_is_the_parent_s_not_profit_loss() -> None:
    """ID-03: Duke files ``NetIncomeLoss`` (4,524M, attributable to Duke Energy Corporation) beside
    ``ProfitLoss`` (4,614M, 90M of which is noncontrolling interest) and the "available to common"
    line (4,402M, after preferred dividends). The first is the regression: ``ProfitLoss`` was read
    in 2,247 production filings."""
    stmts = _stmts(DUKE)
    key = _latest(stmts)
    payload = _payload(DUKE)
    assert _row(payload, "income_statement", "us-gaap_ProfitLoss")[key] == 4_614_000_000.0
    assert stmts.get("net_income_to_common", key) == 4_402_000_000.0
    resolution = stmts.resolve("net_income", key)
    assert resolution.value == 4_524_000_000.0
    assert resolution.concept == "us-gaap_NetIncomeLoss"


def test_a_filing_with_only_profit_loss_takes_it_less_the_nci_income() -> None:
    """ID-03 / CON-19: ``ProfitLoss`` = ``NetIncomeLoss`` + NCI income, so with only ``ProfitLoss``
    the parent's earnings are the difference. Apache's first-quarter 2021 10-Q."""
    stmts = _stmts(APA_Q)
    key = "2021-03-31 (Q1)"
    payload = _payload(APA_Q)
    profit = _row(payload, "income_statement", "us-gaap_ProfitLoss")[key]
    nci = _row(
        payload, "income_statement", "us-gaap_NetIncomeLossAttributableToNoncontrollingInterest"
    )[key]
    resolution = stmts.resolve("net_income", key)
    assert resolution.value == profit - nci == 408_000_000.0
    assert resolution.how == "map"


def test_profit_loss_with_nci_income_filed_only_in_parts_is_empty() -> None:
    """ID-03 step (iv): Apache's FY2021 10-K files ``ProfitLoss`` 1,313M (including noncontrolling
    interests), the common stock's 973M under the "available to common" line, and its NCI income
    only as the redeemable variant (162M) -- no ``NetIncomeLossAttributableToNoncontrollingInterest``
    total. NCI income *is* filed, so ``ProfitLoss`` is not parent earnings, and the difference
    cannot be taken: the item is empty and flagged."""
    stmts = _stmts(APA_K)
    key = "2021-12-31 (FY)"
    assert stmts.get("net_income_to_common", key) == 973_000_000.0
    resolution = stmts.resolve("net_income", key)
    assert resolution.value is None
    assert resolution.flags == ("profit_loss_nci_income_not_total",)


def test_id03_i_net_income_loss_on_another_statement_is_the_same_fact() -> None:
    """ID-03 step (i): Hewlett Packard Enterprise's income statement files no ``NetIncomeLoss`` --
    only the cash-flow statement does ("Net earnings attributable to HPE", 2,579M). The same fact,
    same filing and period: it is the parent's net income."""
    payload = _payload(HPE)
    key = "2024-10-31 (FY)"
    assert not any(
        r["concept"] == "us-gaap_NetIncomeLoss" and r.get(key) is not None
        for r in payload["income_statement"]
    )
    resolution = Statements.from_payload(payload).resolve("net_income", key)
    assert resolution.value == 2_579_000_000.0
    assert resolution.concept == "us-gaap_NetIncomeLoss" and resolution.flags == ()


def test_id03_ii_profit_loss_when_no_nci_income_is_filed() -> None:
    """ID-03 step (ii): Allegion's 10-Q files ``ProfitLoss`` ("Net earnings", 159.7M) and an NCI
    income row with no value for the quarter: no income attributable to the noncontrolling interest
    is filed, so CON-19 makes ``ProfitLoss`` the parent's. Flagged."""
    stmts = _stmts(ALLE_Q)
    resolution = stmts.resolve("net_income", "2025-06-30 (Q2)")
    assert resolution.value == 159_700_000.0
    assert resolution.concept == "us-gaap_ProfitLoss" and resolution.how == "concept"
    assert resolution.flags == ("no_nci_income_filed",)


def test_id03_iii_profit_loss_less_the_nci_income_total() -> None:
    """ID-03 step (iii): PNC files ``ProfitLoss`` on the cash-flow statement (5,647M) and the NCI
    income total on the income statement (69M): the parent's earnings are the difference."""
    stmts = _stmts(PNC)
    resolution = stmts.resolve("net_income", "2023-12-31 (FY)")
    assert resolution.value == 5_647_000_000.0 - 69_000_000.0
    assert resolution.how == "map" and resolution.flags == ()


def _allegion_with(row: dict[str, Any], statement: str = "income_statement") -> Statements:
    payload = copy.deepcopy(_payload(ALLE_Q))
    payload[statement].append({"abstract": False, "dimension": False, **row})
    return Statements.from_payload(payload)


@pytest.mark.parametrize(
    ("row", "statement"),
    [
        # an extension that names the interest and an income
        (
            {
                "concept": "alle_IncomeAllocatedToMinorityInterestHolders",
                "label": "Income allocated to minority interest holders",
            },
            "income_statement",
        ),
        # a us-gaap line with the filer's own caption
        (
            {
                "concept": "us-gaap_IncomeLossFromContinuingOperationsAttributableToNoncontrollingEntity",
                "label": "Net income attributable to noncontrolling interests",
            },
            "income_statement",
        ),
        # the same NCI income shown only in the cash-flow reconciliation
        (
            {
                "concept": "us-gaap_NetIncomeLossAttributableToRedeemableNoncontrollingInterest",
                "label": "Add: noncontrolling interests",
            },
            "cash_flow",
        ),
    ],
)
def test_id03_a_row_that_names_nci_income_keeps_profit_loss_from_being_the_parent_s(
    row: dict[str, Any], statement: str
) -> None:
    """The conservative inference of step (ii) (``docs/model_fixes.md``): any row whose concept and
    caption name a noncontrolling interest and an income counts as NCI income filed. With no NCI
    total to subtract, the item is empty."""
    key = "2025-06-30 (Q2)"
    stmts = _allegion_with({**row, key: 1_000_000.0}, statement)
    resolution = stmts.resolve("net_income", key)
    assert resolution.value is None and resolution.flags == ("profit_loss_nci_income_not_total",)


@pytest.mark.parametrize(
    ("row", "statement"),
    [
        # the taxonomy names its pre-tax elements after the interest
        (
            {
                "concept": "us-gaap_IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                "label": "Income before income taxes",
            },
            "income_statement",
        ),
        # a total that includes the interest
        (
            {
                "concept": "us-gaap_ComprehensiveIncomeNetOfTaxIncludingPortionAttributableToNoncontrollingInterest",
                "label": "Total comprehensive income",
            },
            "income_statement",
        ),
        # flows with the interest are not its income
        (
            {
                "concept": "us-gaap_PaymentsToMinorityShareholders",
                "label": "Distributions to noncontrolling interests",
            },
            "cash_flow",
        ),
        (
            {
                "concept": "us-gaap_GainLossOnInvestments",
                "label": "(Gain) loss on minority equity investments, net",
            },
            "cash_flow",
        ),
    ],
)
def test_id03_a_row_that_only_names_the_interest_is_not_nci_income(
    row: dict[str, Any], statement: str
) -> None:
    key = "2025-06-30 (Q2)"
    stmts = _allegion_with({**row, key: 1_000_000.0}, statement)
    resolution = stmts.resolve("net_income", key)
    assert resolution.value == 159_700_000.0 and resolution.flags == ("no_nci_income_filed",)


def test_id03_ii_reads_only_the_statements_the_filing_stores() -> None:
    """Declared limitation of step (ii): Mastercard's face statements carry no NCI income (it files
    9-21M a year, 0.1-0.2% of its 8.7-15.0B ``ProfitLoss``, only in its notes), so ``ProfitLoss``
    stands, flagged -- the flag is what a reader sees."""
    stmts = _stmts(MA)
    resolution = stmts.resolve("net_income", "2024-12-31 (FY)")
    assert resolution.value == 12_874_000_000.0 and resolution.flags == ("no_nci_income_filed",)


def test_an_nci_income_filed_as_zero_makes_profit_loss_the_parent_s() -> None:
    """ID-03: ``ProfitLoss`` is parent earnings when the NCI income is zero."""
    payload = copy.deepcopy(_payload(DUKE))
    income = payload["income_statement"]
    payload["income_statement"] = [
        r for r in income if r["concept"] != "us-gaap_NetIncomeLoss" or r.get("dimension")
    ]
    nci = _row(
        payload, "income_statement", "us-gaap_NetIncomeLossAttributableToNoncontrollingInterest"
    )
    for column in [c for c in nci if c.endswith("(FY)")]:
        nci[column] = 0.0
    stmts = Statements.from_payload(payload)
    key = _latest(stmts)
    assert stmts.get("net_income", key) == 4_614_000_000.0


def test_net_income_is_never_the_available_to_common_line() -> None:
    """N13: Waters' income statement files only "Net income" under
    ``NetIncomeLossAvailableToCommonStockholdersBasic`` -- the line after preferred dividends, which
    is not the parent's earnings. The item is empty and flagged, in each of the 83 production
    filings (15 companies) that took that line."""
    for name in (WAT, WAT_21):
        stmts = _stmts(name)
        key = _latest(stmts, quarter=True)
        resolution = stmts.resolve("net_income", key)
        assert resolution.value is None and resolution.flags == ("net_income_not_filed",)
        assert (
            stmts.get("net_income_to_common", key) is not None
        )  # the EPS cross-check still has it


# -- (c) income tax: ID-02b, D-06(c) -------------------------------------------------------------


def test_apa_tax_is_current_plus_deferred_when_the_total_is_not_on_the_statement() -> None:
    """B6: ``IncomeTaxExpenseBenefit`` is "current income tax expense (benefit) and deferred income
    tax expense (benefit) pertaining to continuing operations". Apache files both and no total
    (19 production filings): the sum is the tax, flagged as a map."""
    stmts = _stmts(APA_K)
    key = "2021-12-31 (FY)"
    payload = _payload(APA_K)
    current = _row(payload, "income_statement", "us-gaap_CurrentIncomeTaxExpenseBenefit")[key]
    deferred = _row(payload, "income_statement", "us-gaap_DeferredIncomeTaxExpenseBenefit")[key]
    resolution = stmts.resolve("income_tax", key)
    assert resolution.value == current + deferred == 578_000_000.0
    assert resolution.how == "map" and resolution.flags == ("current_plus_deferred",)


def test_the_filed_tax_total_beats_the_components() -> None:
    payload = copy.deepcopy(_payload(AAPL))
    template = _row(payload, "income_statement", "us-gaap_IncomeTaxExpenseBenefit")
    for concept, value in (
        ("us-gaap_CurrentIncomeTaxExpenseBenefit", 1.0),
        ("us-gaap_DeferredIncomeTaxExpenseBenefit", 2.0),
    ):
        part = {**template, "concept": concept}
        for column in [c for c in part if c.endswith("(FY)")]:
            part[column] = value
        payload["income_statement"].append(part)
    stmts = Statements.from_payload(payload)
    resolution = stmts.resolve("income_tax", _latest(stmts))
    assert resolution.value == 16_741_000_000.0
    assert resolution.concept == "us-gaap_IncomeTaxExpenseBenefit"


def test_a_single_tax_component_is_never_the_tax() -> None:
    """The gateway maps ``CurrentIncomeTaxExpenseBenefit`` to the standard concept "IncomeTaxes"
    too; a ``standard`` match took the current tax alone as the total (55 production filings, 9
    companies). With the deferred line missing the item is empty, whatever the standard concept."""
    payload = copy.deepcopy(_payload(APA_K))
    payload["income_statement"] = [
        r
        for r in payload["income_statement"]
        if r["concept"] != "us-gaap_DeferredIncomeTaxExpenseBenefit"
    ]
    current = _row(payload, "income_statement", "us-gaap_CurrentIncomeTaxExpenseBenefit")
    current["standard_concept"] = "IncomeTaxes"
    stmts = Statements.from_payload(payload)
    resolution = stmts.resolve("income_tax", "2021-12-31 (FY)")
    assert resolution.value is None
    assert resolution.flags == ("tax_component_not_total",)
    # and the default rate applies downstream: no tax, no effective rate
    assert effective_tax_rate(resolution.value, 1_000.0) == pytest.approx(0.21)


# -- generic flag support: T-148 step 2.1 (ID-01) ------------------------------------------------


def test_concept_flags_raised_when_value_comes_from_concept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """concept_flags: when the value comes from that concept, raise the flag."""
    spec = LineItem(
        name="test_item",
        statements=("income_statement",),
        concepts=("us-gaap_TestConceptA", "us-gaap_TestConceptB"),
        concept_flags=(("us-gaap_TestConceptA", "flag_a_used"),),
    )
    monkeypatch.setitem(REGISTRY, "test_item", spec)

    payload = {
        "income_statement": [
            {"concept": "us-gaap_TestConceptA", "2024-12-31 (FY)": 100.0, "preferred_sign": 1.0}
        ]
    }
    stmts = Statements.from_payload(payload)
    res = stmts.resolve("test_item", "2024-12-31 (FY)")
    assert res.value == 100.0
    assert res.concept == "us-gaap_TestConceptA"
    assert "flag_a_used" in res.flags

    # When concept B is resolved, flag_a_used is not raised
    payload_b = {
        "income_statement": [
            {"concept": "us-gaap_TestConceptB", "2024-12-31 (FY)": 200.0, "preferred_sign": 1.0}
        ]
    }
    stmts_b = Statements.from_payload(payload_b)
    res_b = stmts_b.resolve("test_item", "2024-12-31 (FY)")
    assert res_b.value == 200.0
    assert "flag_a_used" not in res_b.flags


def test_unread_flags_raised_when_item_empty_and_concept_filed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """unread_flags: when the item resolves empty and that concept is filed (valued,
    non-dimensional) in the column, raise the flag."""
    spec = LineItem(
        name="test_item",
        statements=("cash_flow",),
        concepts=("us-gaap_DepreciationDepletionAndAmortization",),
        unread_flags=(("us-gaap_Depreciation", "depreciation_only_not_da"),),
    )
    monkeypatch.setitem(REGISTRY, "test_item", spec)

    # Depreciation is filed on cash flow, but target concept is not
    payload = {
        "cash_flow": [
            {"concept": "us-gaap_Depreciation", "2024-12-31 (FY)": 50.0, "preferred_sign": 1.0}
        ]
    }
    stmts = Statements.from_payload(payload)
    res = stmts.resolve("test_item", "2024-12-31 (FY)")
    assert res.value is None
    assert "depreciation_only_not_da" in res.flags

    # If the row is dimensional or abstract, it should not trigger unread_flags
    payload_dim = {
        "cash_flow": [
            {
                "concept": "us-gaap_Depreciation",
                "2024-12-31 (FY)": 50.0,
                "preferred_sign": 1.0,
                "dimension": "some_dim",
            }
        ]
    }
    stmts_dim = Statements.from_payload(payload_dim)
    res_dim = stmts_dim.resolve("test_item", "2024-12-31 (FY)")
    assert res_dim.value is None
    assert "depreciation_only_not_da" not in res_dim.flags


def test_id01_standard_concept_match_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """ID-01: raise standard_concept_match when how == 'standard'."""
    spec = LineItem(
        name="test_item",
        statements=("income_statement",),
        concepts=("us-gaap_NonexistentConcept",),
        standard=("StandardRevenue",),
    )
    monkeypatch.setitem(REGISTRY, "test_item", spec)

    payload = {
        "income_statement": [
            {
                "concept": "us-gaap_AlternateRevenue",
                "standard_concept": "StandardRevenue",
                "2024-12-31 (FY)": 500.0,
                "preferred_sign": 1.0,
            }
        ]
    }
    stmts = Statements.from_payload(payload)
    res = stmts.resolve("test_item", "2024-12-31 (FY)")
    assert res.value == 500.0
    assert res.how == "standard"
    assert "standard_concept_match" in res.flags


def test_id01_label_match_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """ID-01: raise label_match when how in ('label', 'label_fallback')."""
    # 1. how == "label"
    spec_label = LineItem(
        name="test_item_label",
        statements=("income_statement",),
        concepts=("us-gaap_Nonexistent",),
        label_contains=("custom revenues",),
    )
    monkeypatch.setitem(REGISTRY, "test_item_label", spec_label)
    payload_label = {
        "income_statement": [
            {
                "concept": "us-gaap_SomeRevenueLine",
                "label": "Total custom revenues from ops",
                "2024-12-31 (FY)": 300.0,
                "preferred_sign": 1.0,
            }
        ]
    }
    stmts_label = Statements.from_payload(payload_label)
    res_label = stmts_label.resolve("test_item_label", "2024-12-31 (FY)")
    assert res_label.value == 300.0
    assert res_label.how == "label"
    assert "label_match" in res_label.flags

    # 2. how == "label_fallback"
    spec_fallback = LineItem(
        name="test_item_fallback",
        statements=("cash_flow",),
        concepts=("us-gaap_Nonexistent",),
        label_fallback=r"^capital expenditures",
    )
    monkeypatch.setitem(REGISTRY, "test_item_fallback", spec_fallback)
    payload_fallback = {
        "cash_flow": [
            {
                "concept": "us-gaap_SomeCapexConcept",
                "label": "Capital expenditures incurred",
                "2024-12-31 (FY)": 400.0,
                "preferred_sign": 1.0,
            }
        ]
    }
    stmts_fallback = Statements.from_payload(payload_fallback)
    res_fallback = stmts_fallback.resolve("test_item_fallback", "2024-12-31 (FY)")
    assert res_fallback.value == 400.0
    assert res_fallback.how == "label_fallback"
    assert "label_match" in res_fallback.flags


def test_id01_extension_concept_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """ID-01: raise extension_concept when concept does not start with us-gaap_ or dei_."""
    spec = LineItem(
        name="test_item_ext",
        statements=("income_statement",),
        concepts=("custom_SpecialRevenue", "us-gaap_StandardRev", "dei_EntityRev"),
    )
    monkeypatch.setitem(REGISTRY, "test_item_ext", spec)

    # 1. Custom extension concept
    payload_custom = {
        "income_statement": [
            {"concept": "custom_SpecialRevenue", "2024-12-31 (FY)": 700.0, "preferred_sign": 1.0}
        ]
    }
    stmts_custom = Statements.from_payload(payload_custom)
    res_custom = stmts_custom.resolve("test_item_ext", "2024-12-31 (FY)")
    assert res_custom.value == 700.0
    assert "extension_concept" in res_custom.flags

    # 2. us-gaap concept: no extension_concept flag
    payload_gaap = {
        "income_statement": [
            {"concept": "us-gaap_StandardRev", "2024-12-31 (FY)": 800.0, "preferred_sign": 1.0}
        ]
    }
    stmts_gaap = Statements.from_payload(payload_gaap)
    res_gaap = stmts_gaap.resolve("test_item_ext", "2024-12-31 (FY)")
    assert res_gaap.value == 800.0
    assert "extension_concept" not in res_gaap.flags

    # 3. dei concept: no extension_concept flag
    payload_dei = {
        "income_statement": [
            {"concept": "dei_EntityRev", "2024-12-31 (FY)": 900.0, "preferred_sign": 1.0}
        ]
    }
    stmts_dei = Statements.from_payload(payload_dei)
    res_dei = stmts_dei.resolve("test_item_ext", "2024-12-31 (FY)")
    assert res_dei.value == 900.0
    assert "extension_concept" not in res_dei.flags
