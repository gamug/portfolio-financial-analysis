"""Tests for T-148 (e): revenue under D-02, bank net revenue map, N20, N19, N3, N14/D7.

Rules tested:
- D-02: sum of >= 2 revenue streams taken only when no other valued non-abstract,
  non-dimensional income-statement row reads as revenue.
- D2: bank net revenue map (InterestIncomeExpenseNet + NoninterestIncome) when no total/components.
- N20 / L-09: net-anchor test in rebuild_total refuses anchor when value less between rows
  is below a valued revenue-concept row.
- N19: _LABEL_TOTAL_EXCLUDE_RE matches costs? so "TOTAL COSTS OF REVENUES" is never an anchor.
- N3: cash flow filing both NetCashProvidedByUsedInOperatingActivities and continuing-operations
  reads the total.
- N14 / D7: depreciation_amortization concept and unread flags, and propagation in leverage metrics.
"""

from __future__ import annotations

from typing import Any

from fundamental_agent.metrics import leverage
from fundamental_agent.statements import REGISTRY, Statements

FY = "2025-12-31 (FY)"


def _income(*rows: dict[str, Any]) -> Statements:
    return Statements.from_payload(
        {
            "income_statement": list(rows),
            "balance_sheet": [],
            "cash_flow": [],
            "corrections": [],
        }
    )


def _row(concept: str, label: str, val: float | None = None) -> dict[str, Any]:
    return {
        "concept": concept,
        "label": label,
        "abstract": False,
        "dimension": False,
        FY: val,
    }


def test_d02_cpt_sum_taken_with_revenue_606_plus_non606_map() -> None:
    """CPT FY2025 (acc 0001628280-26-007697):
    OperatingLeaseLeaseIncome 'Property revenues' 1,573,544,000 +
    RevenueFromContractWithCustomerExcludingAssessedTax 'Fee and asset management' 12,967,000.
    With other lines like 'Total property expenses', 'Interest and other income',
    and 'Total non-property income (loss)' which do not read as revenue -> 1,586,511,000.
    """
    stmts = _income(
        _row(
            "us-gaap_OperatingLeaseLeaseIncome",
            "Property revenues",
            1_573_544_000.0,
        ),
        _row(
            "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
            "Fee and asset management",
            12_967_000.0,
        ),
        _row("us-gaap_OperatingExpenses", "Total property expenses", 450_000_000.0),
        _row("us-gaap_InvestmentIncomeNonoperating", "Interest and other income", 5_000_000.0),
        _row(
            "us-gaap_NonoperatingIncomeExpense", "Total non-property income (loss)", -10_000_000.0
        ),
    )
    res = stmts.resolve("revenue", FY)
    assert res.value == 1_586_511_000.0
    assert res.how == "map"
    assert "revenue_606_plus_non606_map" in res.flags


def test_d02_revenue_sum_refused_when_extra_revenue_row_present() -> None:
    """When an income statement has an extra row that reads as revenue (e.g. 'Other revenues'),
    the 2-stream sum is refused because it does not represent all revenue lines."""
    stmts = _income(
        _row(
            "us-gaap_OperatingLeaseLeaseIncome",
            "Property revenues",
            1_573_544_000.0,
        ),
        _row(
            "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
            "Fee and asset management",
            12_967_000.0,
        ),
        _row("custom_OtherRevenue", "Other revenues", 50_000_000.0),
    )
    res = stmts.resolve("revenue", FY)
    assert res.value is None
    assert "revenue_sum_not_only_revenue_lines" in res.flags


def test_d2_bank_net_revenue_map_for_bny_mellon() -> None:
    """BNY Mellon layout: InterestIncomeExpenseNet + NoninterestIncome
    mapped to revenue when no total concepts or component streams match."""
    stmts = _income(
        _row("us-gaap_InterestIncomeExpenseNet", "Net interest revenue", 2_618_000_000.0),
        _row("us-gaap_NoninterestIncome", "Noninterest income", 13_313_000_000.0),
    )
    res = stmts.resolve("revenue", FY)
    assert res.value == 15_931_000_000.0
    assert res.how == "map"
    assert res.concept == "us-gaap_InterestIncomeExpenseNet + us-gaap_NoninterestIncome"
    assert "bank_net_revenue_map" in res.flags


def test_bank_with_filed_total_keeps_total_concept() -> None:
    """A bank that explicitly files RevenuesNetOfInterestExpense keeps the total concept."""
    stmts = _income(
        _row("us-gaap_InterestIncomeExpenseNet", "Net interest revenue", 2_618_000_000.0),
        _row("us-gaap_NoninterestIncome", "Noninterest income", 13_313_000_000.0),
        _row(
            "us-gaap_RevenuesNetOfInterestExpense",
            "Total net revenues",
            15_931_000_000.0,
        ),
    )
    res = stmts.resolve("revenue", FY)
    assert res.value == 15_931_000_000.0
    assert res.how == "total"
    assert res.concept == "us-gaap_RevenuesNetOfInterestExpense"
    assert "bank_net_revenue_map" not in res.flags


def test_n20_net_anchor_refused_when_below_valued_revenue_component() -> None:
    """N20 / L-09: ICE shape ('Total revenues, less transaction-based expenses').
    When anchor value less rows between is below a valued revenue-concept row,
    rebuild_total refuses with reason 'net anchor'."""
    spec = REGISTRY["revenue"]
    stmts = _income(
        _row(
            "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            5_000_000_000.0,
        ),
        _row("x_TransactionFees", "Transaction expenses", 1_000_000_000.0),
        _row(
            "custom_TotalRevLessExpenses",
            "Total revenues, less transaction-based expenses",
            4_000_000_000.0,
        ),
    )
    rebuild = stmts.rebuild_total(spec, FY)
    assert rebuild is not None
    assert rebuild.value is None
    assert rebuild.refusal == "net anchor"
    assert stmts.get("revenue", FY) == 5_000_000_000.0


def test_n19_total_costs_of_revenues_is_never_an_anchor() -> None:
    """N19: _LABEL_TOTAL_EXCLUDE_RE matches costs? so 'TOTAL COSTS OF REVENUES' is never an anchor."""
    spec = REGISTRY["revenue"]
    stmts = _income(
        _row(
            "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            10_000_000_000.0,
        ),
        _row("custom_TotalCosts", "TOTAL COSTS OF REVENUES", 6_000_000_000.0),
    )
    rebuild = stmts.rebuild_total(spec, FY)
    assert rebuild is None


def test_n3_cash_flow_total_preferred_over_continuing_operations() -> None:
    """N3: a cash-flow statement filing both NetCashProvidedByUsedInOperatingActivities
    and NetCashProvidedByUsedInOperatingActivitiesContinuingOperations reads the total."""
    stmts = Statements.from_payload(
        {
            "income_statement": [],
            "balance_sheet": [],
            "cash_flow": [
                _row(
                    "us-gaap_NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
                    "Continuing operations OCF",
                    800_000_000.0,
                ),
                _row(
                    "us-gaap_NetCashProvidedByUsedInOperatingActivities",
                    "Operating cash flow",
                    1_000_000_000.0,
                ),
            ],
            "corrections": [],
        }
    )
    res = stmts.resolve("operating_cash_flow", FY)
    assert res.value == 1_000_000_000.0
    assert res.concept == "us-gaap_NetCashProvidedByUsedInOperatingActivities"


def test_n14_d7_depreciation_amortization_flags() -> None:
    """N14 / D7: us-gaap_Depreciation is removed from concepts;
    raises depreciation_only_not_da when filed unread.
    us-gaap_DepreciationAndAmortization raises da_element_nonproduction."""
    # Case 1: only us-gaap_Depreciation filed in cash_flow -> resolves empty, flag raised
    stmts_dep = Statements.from_payload(
        {
            "income_statement": [],
            "balance_sheet": [],
            "cash_flow": [
                _row("us-gaap_Depreciation", "Depreciation", 100_000_000.0),
            ],
            "corrections": [],
        }
    )
    res_dep = stmts_dep.resolve("depreciation_amortization", FY)
    assert res_dep.value is None
    assert "depreciation_only_not_da" in res_dep.flags

    # Case 2: us-gaap_DepreciationAndAmortization filed in cash_flow -> resolves, concept flag raised
    stmts_da = Statements.from_payload(
        {
            "income_statement": [],
            "balance_sheet": [],
            "cash_flow": [
                _row(
                    "us-gaap_DepreciationAndAmortization",
                    "Depreciation and amortization",
                    200_000_000.0,
                ),
            ],
            "corrections": [],
        }
    )
    res_da = stmts_da.resolve("depreciation_amortization", FY)
    assert res_da.value == 200_000_000.0
    assert "da_element_nonproduction" in res_da.flags

    # Verify flags propagate to leverage metrics inputs
    res_lev = leverage.compute(stmts_da, FY)
    assert res_lev[0].inputs.get("flag_depreciation_amortization_da_element_nonproduction") == 1.0
