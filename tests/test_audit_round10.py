"""Audit round #10 (reviewer B, 2026-10-28; user-approved Q1-Q9, samples E1-E7 checked on EDGAR).

Each case is a shape the whole-denominator inventory (``audit/r10``) met: a face line under an element the engine does
not read (COST's pretax under ``IncomeLossAttributableToParent``, AT&T's inventories under ``PrepaidExpenseCurrent``),
inventory shown as component lines (DTE, KR's FIFO less LIFO reserve, MKC's gross total), the figure filed only in the
notes, the net interest line, D&A as its two face lines (HON) or one (CSX), a line that bundles impairment (NKE), an
NCI income filed in parts with the face's proof (SRE), a gain on a stake sold read as NCI income (ITW), and ROIC on a
near-zero capital base (PLTR FY2023).
"""

from __future__ import annotations

from typing import Any

from fundamental_agent.metrics import roic
from fundamental_agent.statements import Statements

G = "us-gaap_"
FY = "2025-12-31 (FY)"
COL = "2025-12-31"
PRIOR = "2024-12-31"


def _row(concept: str, label: str, column: str, val: float, sign: float = 1.0) -> dict[str, Any]:
    return {
        "concept": concept if "_" in concept else G + concept,
        "label": label,
        "abstract": False,
        "dimension": False,
        "preferred_sign": sign,
        column: val,
    }


def _fact(concept: str, value: float, end: str = COL, start: str | None = None) -> dict[str, Any]:
    return {
        "concept": "us-gaap:" + concept,
        "value": value,
        "unit": "USD",
        "period_start": start,
        "period_end": end,
    }


BASE_FACTS = [_fact("Revenues", 500.0, start="2025-01-01")]


def _stmts(
    income: list[dict[str, Any]] | None = None,
    balance: list[dict[str, Any]] | None = None,
    cash_flow: list[dict[str, Any]] | None = None,
    facts: list[dict[str, Any]] | None = None,
) -> Statements:
    return Statements.from_payload(
        {
            "income_statement": income or [_row("Revenues", "Revenue", FY, 500.0)],
            "balance_sheet": balance or [_row("Assets", "Total assets", COL, 1_000.0)],
            "cash_flow": cash_flow or [],
            "corrections": [],
            "filed_facts": BASE_FACTS if facts is None else facts,
        }
    )


def test_a_pretax_line_under_a_net_income_element_is_the_pretax_income() -> None:
    """R1 (E1, COST): "INCOME BEFORE INCOME TAXES" tagged ``IncomeLossAttributableToParent`` is the pre-tax income."""
    stmts = _stmts(
        income=[_row("IncomeLossAttributableToParent", "INCOME BEFORE INCOME TAXES", FY, 2_306.0)]
    )
    res = stmts.resolve("pretax_income", FY)
    assert (
        res.value == 2_306.0 and res.how == "face_line" and "face_line_other_element" in res.flags
    )


def test_an_allowlisted_element_whose_caption_names_another_item_is_not_read() -> None:
    """The same element captioned as net income is not the pre-tax income."""
    stmts = _stmts(
        income=[
            _row("IncomeLossAttributableToParent", "Net income attributable to parent", FY, 1_798.0)
        ]
    )
    assert stmts.resolve("pretax_income", FY).value is None


def test_a_cash_line_under_a_restricted_cash_element_needs_no_restricted_cash_shown() -> None:
    """R1 + Q6: MMM's "Cash and cash equivalents" under the restricted-cash total element is cash when the filing shows
    no restricted cash; PODD's, with a restricted-cash amount filed, stays refused."""
    element = (
        G
        + "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations"
    )
    line = _row(element, "Cash and cash equivalents", COL, 4_564.0)
    assert _stmts(balance=[line]).resolve("cash", COL).value == 4_564.0
    refused = _stmts(
        balance=[line], facts=[*BASE_FACTS, _fact("RestrictedCashCurrent", 12.0)]
    ).resolve("cash", COL)
    assert refused.value is None and "face_line_restricted_cash_refused" in refused.flags


def test_inventory_component_lines_without_a_total_are_summed() -> None:
    """Q3 (DTE): "Fuel and gas" + "Materials, supplies, and other"; the gas inventory equalization is not inventory."""
    stmts = _stmts(
        balance=[
            _row("EnergyRelatedInventory", "Fuel and gas", COL, 368.0),
            _row("InventoryRawMaterialsAndSupplies", "Materials, supplies, and other", COL, 490.0),
            _row("dte_GasInventoryEqualizationCurrent", "Gas inventory equalization", COL, 108.0),
        ]
    )
    res = stmts.resolve("inventory", COL)
    assert res.value == 858.0 and "inventory_components_summed" in res.flags


def test_fifo_inventory_less_the_lifo_reserve_is_the_inventory() -> None:
    """Q3 (KR): "FIFO inventory" 8,353 less "LIFO reserve" (1,570, displayed negative with a negating label)."""
    stmts = _stmts(
        balance=[
            _row("FIFOInventoryAmount", "FIFO inventory", COL, 8_353.0),
            _row("InventoryLIFOReserve", "LIFO reserve", COL, -1_570.0, sign=-1.0),
        ]
    )
    assert stmts.resolve("inventory", COL).value == 6_783.0


def test_a_gross_total_with_no_reserve_line_is_the_inventory() -> None:
    """Q4 (MKC): "Total inventory" (``InventoryGross``) equals its two component lines and no reserve is shown."""
    stmts = _stmts(
        balance=[
            _row("InventoryFinishedGoods", "Finished products", COL, 571.5),
            _row(
                "InventoryWorkInProcessAndRawMaterials",
                "Raw materials and work-in-process",
                COL,
                672.7,
            ),
            _row("InventoryGross", "Total inventory", COL, 1_244.2),
        ]
    )
    res = stmts.resolve("inventory", COL)
    assert res.value == 1_244.2 and res.concept == G + "InventoryGross"


def test_a_figure_filed_only_in_the_notes_is_read_last() -> None:
    """Q7: the interest expense the filing files only in its notes."""
    facts = [*BASE_FACTS, _fact("InterestExpense", 42.0, start="2025-01-01")]
    res = _stmts(facts=facts).resolve("interest_expense", FY)
    assert res.value == 42.0 and "filed_fact_from_notes" in res.flags


def test_a_net_interest_line_is_the_interest_expense_when_no_gross_is_filed() -> None:
    """Q1 (CAG): "Interest expense, net" displayed 94.9 with a negating label -> the filed net element is -94.9."""
    income = [
        _row("Revenues", "Revenue", FY, 500.0),
        _row("InterestIncomeExpenseNet", "Interest expense, net", FY, 94.9, sign=-1.0),
    ]
    res = _stmts(income=income).resolve("interest_expense", FY)
    assert res.value == 94.9 and res.how == "net_interest" and "interest_net" in res.flags


def test_da_shown_as_depreciation_and_amortization_lines_is_their_sum() -> None:
    """Q2b (HON): "Depreciation" + "Amortization" on the cash-flow face; a debt-cost line is not D&A."""
    cash_flow = [
        _row("Depreciation", "Depreciation", FY, 659.0),
        _row("AdjustmentForAmortization", "Amortization", FY, 517.0),
        _row("AmortizationOfDebtDiscountPremium", "Amortization of debt discount", FY, 3.0),
    ]
    res = _stmts(cash_flow=cash_flow).resolve("depreciation_amortization", FY)
    assert res.value == 1_176.0 and "da_face_parts" in res.flags


def test_one_depreciation_line_is_the_da_when_no_amortization_is_filed() -> None:
    """Q2b (CSX): "Depreciation" alone, and no amortization amount anywhere in the filing."""
    res = _stmts(cash_flow=[_row("Depreciation", "Depreciation", FY, 1_420.0)]).resolve(
        "depreciation_amortization", FY
    )
    assert res.value == 1_420.0 and "da_single_part" in res.flags


def test_an_amortization_line_that_adds_impairment_refuses_the_parts() -> None:
    """Q2a (NKE): "Amortization, impairment and other" beside "Depreciation" -- the bundled line is not a part."""
    cash_flow = [
        _row("Depreciation", "Depreciation", FY, 717.0),
        _row("nke_AmortizationImpairmentAndOther", "Amortization, impairment and other", FY, 123.0),
    ]
    res = _stmts(cash_flow=cash_flow).resolve("depreciation_amortization", FY)
    assert res.value is None and "da_parts_bundled" in res.flags


def test_a_gain_on_a_stake_sold_is_not_nci_income() -> None:
    """R3 (E6, ITW): "Gain on sale of noncontrolling interest in Wilsonart" -> ``ProfitLoss`` is the net income."""
    income = [
        _row("ProfitLoss", "Net Income", FY, 3_488.0),
        _row(
            "EquityMethodInvestmentRealizedGainLossOnDisposal",
            "Gain on sale of noncontrolling interest in Wilsonart International Holdings LLC",
            FY,
            363.0,
        ),
    ]
    res = _stmts(income=income).resolve("net_income", FY)
    assert res.value == 3_488.0 and "no_nci_income_filed" in res.flags


def test_nci_income_in_parts_is_their_sum_when_the_face_proves_it() -> None:
    """R3 (SRE): ``ProfitLoss`` less the nonredeemable and redeemable NCI lines equals the face's parent line."""
    income = [
        _row("ProfitLoss", "Net income", FY, 942.0),
        _row(
            "NetIncomeLossAttributableToNonredeemableNoncontrollingInterest",
            "Earnings attributable to noncontrolling interests",
            FY,
            141.0,
        ),
        _row(
            "NetIncomeLossAttributableToRedeemableNoncontrollingInterest",
            "Earnings attributable to redeemable noncontrolling interest",
            FY,
            4.0,
        ),
        _row(
            "NetIncomeLossAvailableToCommonStockholdersBasic",
            "Earnings attributable to Sempra",
            FY,
            797.0,
        ),
    ]
    res = _stmts(income=income).resolve("net_income", FY)
    assert res.value == 797.0 and "nci_parts_proved" in res.flags


def test_nci_income_in_parts_without_the_proof_stays_refused() -> None:
    """R3: no face row equals profit less the parts -- a part may be missing (APA's Egypt NCI): empty."""
    income = [
        _row("ProfitLoss", "Net income", FY, 1_313.0),
        _row(
            "NetIncomeLossAttributableToRedeemableNoncontrollingInterest",
            "Net income attributable to Altus Preferred Unit limited partners",
            FY,
            162.0,
        ),
        _row(
            "NetIncomeLossAvailableToCommonStockholdersBasic",
            "Net income attributable to common stock",
            FY,
            973.0,
        ),
    ]
    res = _stmts(income=income).resolve("net_income", FY)
    assert res.value is None and "profit_loss_nci_income_not_total" in res.flags


def test_roic_on_a_near_zero_capital_base_is_not_meaningful() -> None:
    """Q9 / D-14 (PLTR FY2023): invested capital 8.76 against 4,522 of total assets (0.2 %) -> empty, flagged."""
    stmts = _stmts(
        income=[
            _row("Revenues", "Revenue", FY, 2_225.0),
            _row("OperatingIncomeLoss", "Income from operations", FY, 120.0),
        ],
        balance=[
            _row("Assets", "Total assets", PRIOR, 4_522.0),
            _row("StockholdersEquity", "Total stockholders' equity", PRIOR, 3_475.0),
            _row(
                "CashAndCashEquivalentsAtCarryingValue", "Cash and cash equivalents", PRIOR, 2_598.0
            ),
            _row("ShortTermInvestments", "Marketable securities", PRIOR, 868.24),
            _row("AccountsPayableCurrent", "Accounts payable", PRIOR, 25.0),
        ],
    )
    results = {r.name: r for r in roic.compute(stmts, FY)}
    row = results["return_on_invested_capital"]
    assert row.value is None
    assert row.inputs.get("flag_return_on_invested_capital_invested_capital_not_meaningful") == 1.0


def test_a_debt_figure_only_in_the_notes_is_not_read() -> None:
    """Q7's exclusion: a balance the face's other lines may contain (PHM's senior notes inside "Notes payable", BE's
    verified debt lines) is never read from the notes -- it would be counted twice."""
    facts = [*BASE_FACTS, _fact("LongTermDebt", 250.0)]
    res = _stmts(facts=facts).resolve("long_term_debt", COL)
    assert res.value is None and "filed_fact_from_notes" not in res.flags


def test_a_notes_figure_is_refused_when_the_face_shows_the_item_itself() -> None:
    """Q7's guard (WEC): the face shows "Common shareholders' equity" under an element the engine does not read; the
    notes figure that differs from it is another scope (equity including NCI), never the item."""
    balance = [
        _row("Assets", "Total assets", COL, 1_000.0),
        _row("wec_TotalCommonShareholdersEquity", "Common shareholders' equity", COL, 600.0),
    ]
    facts = [
        *BASE_FACTS,
        _fact("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", 640.0),
    ]
    res = _stmts(balance=balance, facts=facts).resolve("equity", COL)
    assert res.value is None and "filed_fact_refused_face_line_differs" in res.flags
