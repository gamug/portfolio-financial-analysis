"""T-159c's labels (no value changes) and the §2b provenance/cover-scale flags."""

from __future__ import annotations

from typing import Any

from fundamental_agent.metrics import cagr, efficiency, liquidity, provenance, roic, valuation
from fundamental_agent.metrics.base import MetricResult, TTMFlow
from fundamental_agent.statements import FiledFact, Statements

G = "us-gaap_"
FY = "2024-12-31 (FY)"
Q3 = "2024-09-30 (Q3)"
INSTANT = "2024-12-31"


def _row(concept: str, label: str, **periods: float) -> dict[str, Any]:
    return {
        "concept": G + concept,
        "label": label,
        "standard_concept": None,
        "abstract": False,
        "dimension": False,
        "preferred_sign": 1.0,
        **periods,
    }


def _stmts(
    *,
    income: list[dict[str, Any]] | None = None,
    balance: list[dict[str, Any]] | None = None,
    cash_flow: list[dict[str, Any]] | None = None,
) -> Statements:
    return Statements.from_payload(
        {
            "income_statement": income or [],
            "balance_sheet": balance or [],
            "cash_flow": cash_flow or [],
        }
    )


def _attached(stmts: Statements, group: str, results: list[Any], key: str = FY) -> list[Any]:
    pairs = [(group, result) for result in results]
    provenance.attach(pairs, stmts, key)
    return [result for _group, result in pairs]


def _loss_year() -> Statements:
    return _stmts(
        income=[
            _row("OperatingIncomeLoss", "Operating loss", **{FY: -50.0}),
            _row("IncomeTaxExpenseBenefit", "Income tax", **{FY: 10.0}),
            _row(
                "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                "Pre-tax loss",
                **{FY: -100.0},
            ),
            _row("NetIncomeLoss", "Net loss", **{FY: -110.0}),
        ],
        balance=[
            _row("Assets", "Assets", **{INSTANT: 500.0, "2023-12-31": 450.0}),
            _row("StockholdersEquity", "Equity", **{INSTANT: 300.0, "2023-12-31": 280.0}),
            _row("CashAndCashEquivalentsAtCarryingValue", "Cash", **{INSTANT: 50.0}),
            _row("LongTermDebtNoncurrent", "Long-term debt", **{INSTANT: 100.0}),
        ],
        cash_flow=[_row("NetCashProvidedByUsedInOperatingActivities", "OCF", **{FY: 20.0})],
    )


def test_effective_tax_rate_in_a_loss_year_is_denominator_not_positive() -> None:
    stmts = _loss_year()
    results = _attached(stmts, "roic", roic.compute(stmts, FY))
    rate = next(r for r in results if r.name == "effective_tax_rate")
    assert rate.value is None
    assert rate.inputs["provenance"]["empty_reason"] == provenance.DENOMINATOR_NOT_POSITIVE


def test_roic_at_the_effective_rate_in_a_loss_year_is_denominator_not_positive() -> None:
    stmts = _loss_year()
    results = _attached(stmts, "roic", roic.compute(stmts, FY))
    robust = next(r for r in results if r.name == "return_on_invested_capital_effective_rate")
    assert robust.value is None
    assert robust.inputs["provenance"]["empty_reason"] == provenance.DENOMINATOR_NOT_POSITIVE


def test_a_cagr_with_a_negative_end_is_growth_base_not_positive() -> None:
    stmts = _stmts(
        income=[
            _row("Revenues", "Revenue", **{"2022-12-31 (FY)": 100.0}),
            _row("Revenues", "Revenue", **{"2023-12-31 (FY)": 120.0}),
            _row("Revenues", "Revenue", **{FY: -5.0}),
            _row("NetIncomeLoss", "Net income", **{"2022-12-31 (FY)": 10.0}),
            _row("NetIncomeLoss", "Net income", **{"2023-12-31 (FY)": 12.0}),
            _row("NetIncomeLoss", "Net income", **{FY: 14.0}),
            _row("NetCashProvidedByUsedInOperatingActivities", "OCF", **{"2022-12-31 (FY)": 20.0}),
            _row("NetCashProvidedByUsedInOperatingActivities", "OCF", **{"2023-12-31 (FY)": 22.0}),
            _row("NetCashProvidedByUsedInOperatingActivities", "OCF", **{FY: 24.0}),
        ]
    )
    results = _attached(stmts, "cagr", cagr.compute(stmts, FY), key=FY)
    revenue = next(r for r in results if r.name == "revenue_cagr")
    assert revenue.value is None
    assert revenue.inputs["provenance"]["empty_reason"] == provenance.GROWTH_BASE_NOT_POSITIVE


def test_unclassified_balance_sheet_liquidity_is_not_applicable() -> None:
    stmts = _stmts(
        income=[_row("Revenues", "Revenue", **{FY: 100.0})],
        balance=[
            _row("Assets", "Assets", **{INSTANT: 500.0}),
            _row("StockholdersEquity", "Equity", **{INSTANT: 300.0}),
            _row("CashAndCashEquivalentsAtCarryingValue", "Cash", **{INSTANT: 50.0}),
        ],
    )
    results = _attached(stmts, "liquidity", liquidity.compute(stmts, FY))
    current = next(r for r in results if r.name == "current_ratio")
    assert current.value is None
    assert current.inputs["provenance"]["empty_reason"] == provenance.NOT_APPLICABLE


def test_unclassified_balance_sheet_receivables_turnover_is_not_applicable() -> None:
    stmts = _stmts(
        income=[_row("Revenues", "Revenue", **{FY: 100.0})],
        balance=[
            _row("Assets", "Assets", **{INSTANT: 500.0}),
            _row("StockholdersEquity", "Equity", **{INSTANT: 300.0}),
        ],
    )
    results = _attached(stmts, "efficiency", efficiency.compute(stmts, FY))
    turnover = next(r for r in results if r.name == "receivables_turnover")
    assert turnover.value is None
    assert turnover.inputs["provenance"]["empty_reason"] == provenance.NOT_APPLICABLE


def test_a_bank_receivables_turnover_is_not_applicable() -> None:
    stmts = _stmts(
        income=[_row("Revenues", "Revenue", **{FY: 100.0})],
        balance=[
            _row("Assets", "Assets", **{INSTANT: 500.0}),
            _row("StockholdersEquity", "Equity", **{INSTANT: 300.0}),
        ],
    )
    stmts.sub_industry = "Diversified Banks"
    results = _attached(stmts, "efficiency", efficiency.compute(stmts, FY))
    turnover = next(r for r in results if r.name == "receivables_turnover")
    assert turnover.value is None
    assert turnover.inputs["provenance"]["empty_reason"] == provenance.NOT_APPLICABLE


def _ebitda_statements(key: str) -> Statements:
    return _stmts(
        income=[
            _row("Revenues", "Revenue", **{key: 1_000.0}),
            _row("OperatingIncomeLoss", "Operating income", **{key: 120.0}),
        ],
        balance=[_row("Assets", "Assets", **{INSTANT: 500.0})],
        cash_flow=[
            _row("NetCashProvidedByUsedInOperatingActivities", "OCF", **{key: 200.0}),
            _row("DepreciationDepletionAndAmortization", "D&A", **{key: 30.0}),
        ],
    )


def test_ebitda_provenance_labels_the_ttm_basis_on_a_10q() -> None:
    stmts = _ebitda_statements(Q3)
    flows = {
        "operating_income": TTMFlow(120.0, "ytd"),
        "depreciation_amortization": TTMFlow(30.0, "ytd"),
    }
    block = provenance.build(stmts, Q3, MetricResult("ebitda", 150.0, "usd", {}), flows)
    assert [entry["basis"] for entry in block["items"]["operating_income"]] == ["ttm"]
    assert [entry["basis"] for entry in block["items"]["depreciation_amortization"]] == ["ttm"]


def test_ebitda_provenance_stays_period_on_a_10k() -> None:
    stmts = _ebitda_statements(FY)
    block = provenance.build(stmts, FY, MetricResult("ebitda", 150.0, "usd", {}))
    assert [entry["basis"] for entry in block["items"]["operating_income"]] == ["period"]
    assert [entry["basis"] for entry in block["items"]["depreciation_amortization"]] == ["period"]


def test_a_refused_filed_fact_stays_missing_input_not_not_reported() -> None:
    stmts = _stmts(
        balance=[
            _row("Assets", "Assets", **{INSTANT: 500.0}),
            _row("StockholdersEquity", "Equity", **{INSTANT: 300.0}),
        ],
    )
    stmts.filed_facts = (
        # a zero is refused (Rule C) -- the notes figure of audit round #10 (Q7) is not one
        FiledFact(
            concept="us-gaap:LongTermDebt",
            value=0.0,
            unit="USD",
            period_start=None,
            period_end=INSTANT,
        ),
    )
    block = provenance.build(stmts, FY, MetricResult("debt_to_assets", None, "x", {}))
    assert block["empty_reason"] == provenance.MISSING_INPUT
    assert "filed_fact_refused" in block["items"]["long_term_debt"][0]["flags"]


def test_a_refused_input_through_a_metric_is_missing_input() -> None:
    stmts = _stmts(
        balance=[
            _row("Assets", "Assets", **{INSTANT: 500.0}),
            _row("Other", "Something else", **{INSTANT: 100.0}),
        ],
    )
    stmts.filed_facts = (
        FiledFact(
            concept="us-gaap:LongTermDebt",
            value=100.0,
            unit="USD",
            period_start=None,
            period_end=INSTANT,
        ),
    )
    block = provenance.build(stmts, FY, MetricResult("debt_to_assets", None, "x", {}))
    assert block["empty_reason"] == provenance.MISSING_INPUT


def test_a_missing_current_input_outranks_a_debt_capture_failure() -> None:
    stmts = _stmts(
        balance=[
            _row("Assets", "Assets", **{"2023-12-31": 500.0}),
            _row("Other", "Something else", **{INSTANT: 1.0}),
            _row("SeniorNotes", "Senior notes", **{INSTANT: 50.0}),
            _row("MortgagesPayable", "Mortgages payable", **{INSTANT: 100.0}),
        ],
    )
    block = provenance.build(stmts, FY, MetricResult("debt_to_assets", None, "x", {}))
    assert block["empty_reason"] == provenance.MISSING_INPUT


def test_vno_s_statement_refusal_is_still_capture_failure() -> None:
    stmts = _stmts(
        balance=[
            _row("Assets", "Assets", **{INSTANT: 500.0}),
            _row("SeniorNotes", "Senior notes", **{INSTANT: 50.0}),
            _row("MortgagesPayable", "Mortgages payable", **{INSTANT: 100.0}),
        ],
    )
    block = provenance.build(stmts, FY, MetricResult("debt_to_assets", None, "x", {}))
    assert block["empty_reason"] == provenance.CAPTURE_FAILURE


def test_a_cover_scale_suspect_is_a_stored_flag_on_the_valuation_rows() -> None:
    inputs: dict[str, float] = {}
    valuation._market_cap_inputs(
        inputs,
        (None, None, None, None, None, None, False, False, "no_cover_shares"),
        None,
        FY,
        None,
        cover_scale_suspect=True,
    )
    assert inputs["flag_market_capitalization_cover_scale_suspect"] == 1.0
