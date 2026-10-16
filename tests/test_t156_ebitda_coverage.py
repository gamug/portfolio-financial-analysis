"""T-156 phase 2: the EBITDA-coverage recoveries E1-E4, all hermetic.

E1 (MD-01, ID-02b): when the cash-flow statement files no D&A fact at all, the same us-gaap
elements the D&A resolver accepts are read from the income statement (B6's definitions name no
statement), flagged ``da_income_statement``. The two statements are never mixed in one value.

E2 (N14/D7 stays): a depreciation-only filer recovers through the existing sourced sum when its
amortization child is on the income statement, every condition of that sum met as the code states
it today; flagged ``da_sum_cross_statement``.

E3 (ID-01, A1 5.6.1): a company extension on the cash-flow statement whose caption equals the
standard label of an accepted us-gaap D&A element is read through the shared caption fallback,
flagged ``caption_fallback`` and ``extension_concept``.

E4 (ID-02b): operating income formed from the FASB 2026 ``soi`` calculation identities when every
term is filed, flagged ``oi_from_calc_identity``.
"""

from __future__ import annotations

from typing import Any

from fundamental_agent.metrics import leverage
from fundamental_agent.statements import G, Statements

FY = "2025-12-31 (FY)"
Q2 = "2025-06-30 (Q2)"
YTD = "2025-06-30 (YTD)"


def _row(concept: str, label: str, val: float | None = None, column: str = FY) -> dict[str, Any]:
    return {
        "concept": concept,
        "label": label,
        "abstract": False,
        "dimension": False,
        "preferred_sign": 1,
        column: val,
    }


def _stmts(
    income: list[dict[str, Any]] | None = None,
    cash: list[dict[str, Any]] | None = None,
    balance: list[dict[str, Any]] | None = None,
) -> Statements:
    return Statements.from_payload(
        {
            "income_statement": income or [],
            "balance_sheet": balance or [],
            "cash_flow": cash or [],
            "corrections": [],
        }
    )


# -- E1: D&A from the income statement when the cash-flow statement has none ---------------------


def test_e1_the_dal_shape_reads_the_income_statement_aggregate() -> None:
    """DAL's 10-Q files ``DepreciationDepletionAndAmortization`` on the income statement and no D&A
    fact at all on the cash-flow statement: E1 reads the same element, flagged."""
    stmts = _stmts(
        income=[
            _row(
                G + "DepreciationDepletionAndAmortization",
                "Depreciation and amortization",
                1_291_000_000.0,
                YTD,
            )
        ],
        cash=[_row(G + "NetIncomeLoss", "Net income", 500_000_000.0, YTD)],
    )
    res = stmts.resolve("depreciation_amortization", YTD)
    assert res.value == 1_291_000_000.0
    assert res.concept == G + "DepreciationDepletionAndAmortization" and res.how == "concept"
    assert res.flags == ("da_income_statement",)


def test_e1_a_cash_flow_d_a_fact_in_any_column_keeps_today_behaviour() -> None:
    """Never mix: the cash-flow statement carries a D&A fact (in another column), so the income
    statement is not read and D&A stays empty."""
    stmts = _stmts(
        income=[
            _row(
                G + "DepreciationDepletionAndAmortization",
                "Depreciation and amortization",
                1_291_000_000.0,
                FY,
            )
        ],
        cash=[_row(G + "Depreciation", "Depreciation", 401_000_000.0, YTD)],
    )
    res = stmts.resolve("depreciation_amortization", FY)
    assert res.value is None
    assert "da_income_statement" not in res.flags


def test_e1_an_empty_income_statement_stays_empty() -> None:
    stmts = _stmts(cash=[_row(G + "NetIncomeLoss", "Net income", 1.0, FY)])
    res = stmts.resolve("depreciation_amortization", FY)
    assert res.value is None


def test_e1_the_income_statement_sourced_sum_is_available_too() -> None:
    """The same elements: an income statement filing ``Depreciation`` + an amortization child and no
    aggregate resolves through the existing sourced sum, on the income statement."""
    stmts = _stmts(
        income=[
            _row(G + "Depreciation", "Depreciation", 400_000_000.0, FY),
            _row(
                G + "AmortizationOfIntangibleAssets",
                "Amortization of intangibles",
                100_000_000.0,
                FY,
            ),
        ],
        cash=[_row(G + "NetIncomeLoss", "Net income", 1.0, FY)],
    )
    res = stmts.resolve("depreciation_amortization", FY)
    assert res.value == 500_000_000.0
    assert "da_income_statement" in res.flags and "da_sourced_sum" in res.flags


def test_e1_a_negative_income_statement_aggregate_is_rejected_not_repaired() -> None:
    """ID-13 / ID-11: a negative ``DepreciationDepletionAndAmortization`` (an expense displayed
    negative) is rejected, never made positive."""
    stmts = _stmts(
        income=[
            _row(
                G + "DepreciationDepletionAndAmortization",
                "Depreciation and amortization",
                -363_000_000.0,
                FY,
            )
        ],
        cash=[_row(G + "NetIncomeLoss", "Net income", 1.0, FY)],
    )
    res = stmts.resolve("depreciation_amortization", FY)
    assert res.value is None
    assert "wrong_sign_rejected" in res.flags


def test_e1_the_flag_reaches_the_metric_inputs() -> None:
    """The flag travels as ``flag_depreciation_amortization_da_income_statement`` (D11/T-151 C6)."""
    stmts = _stmts(
        income=[
            _row(
                G + "DepreciationDepletionAndAmortization",
                "Depreciation and amortization",
                1_291_000_000.0,
                FY,
            ),
            _row(G + "OperatingIncomeLoss", "Operating income", 2_000_000_000.0, FY),
        ],
        balance=[
            _row(G + "Assets", "Total assets", 10.0, "2025-12-31"),
            _row(G + "StockholdersEquity", "Total equity", 5.0, "2025-12-31"),
            _row(G + "CashAndCashEquivalentsAtCarryingValue", "Cash", 1.0, "2025-12-31"),
            _row(G + "LongTermDebtNoncurrent", "Long-term debt", 2.0, "2025-12-31"),
        ],
        cash=[_row(G + "NetIncomeLoss", "Net income", 1.0, FY)],
    )
    results = leverage.compute(stmts, FY)
    ebitda = next(r for r in results if r.name == "ebitda")
    assert ebitda.value == 3_291_000_000.0
    assert ebitda.inputs["flag_depreciation_amortization_da_income_statement"] == 1.0
