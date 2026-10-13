"""Every metric carries a provenance block (T-151 part 3, D14)."""

from __future__ import annotations

import json
from typing import Any

from fundamental_agent.metrics import provenance
from fundamental_agent.metrics.base import MetricResult, TTMFlow
from fundamental_agent.metrics.cashflow import compute as cashflow_compute
from fundamental_agent.metrics.efficiency import compute as efficiency_compute
from fundamental_agent.metrics.leverage import compute as leverage_compute
from fundamental_agent.metrics.profitability import compute as profitability_compute
from fundamental_agent.metrics.roic import compute as roic_compute
from fundamental_agent.metrics.valuation import compute as valuation_compute
from fundamental_agent.statements import Statements
from kg_schema.market_cap import MarketCap

G = "us-gaap_"
KEY = "2024-12-31 (FY)"


def _mcap(value: float = 5_000.0) -> MarketCap:
    return MarketCap(
        asset_id=1,
        value=value,
        shares=100.0,
        share_count=100.0,
        share_as_of="2024-06-30",
        share_age_days=0,
        filing_id=1,
        close=value / 100.0,
        close_date="2024-06-30",
        n_classes=0,
        split_factor=1.0,
    )


def _row(concept: str, label: str, **periods: float) -> dict[str, Any]:
    return {
        "concept": concept,
        "label": label,
        "standard_concept": None,
        "abstract": False,
        "dimension": False,
        **periods,
    }


def _payload(*, prior_equity: float = 400.0, operating_income: float | None = 100.0) -> Statements:
    income = [
        _row(G + "NetIncomeLoss", "Net income", **{KEY: 80.0}),
    ]
    if operating_income is not None:
        income.append(
            _row(G + "OperatingIncomeLoss", "Operating income", **{KEY: operating_income})
        )
    stmts = Statements.from_payload(
        {
            "income_statement": income,
            "balance_sheet": [
                _row(G + "Assets", "Assets", **{"2024-12-31": 1_100.0, "2023-12-31": 1_000.0}),
                _row(
                    G + "StockholdersEquity",
                    "Equity",
                    **{"2024-12-31": 450.0, "2023-12-31": prior_equity},
                ),
            ],
            "cash_flow": [],
        }
    )
    stmts.accession = "0000320193-24-000001"
    return stmts


def _attached(stmts: Statements, group: str, results: list[Any]) -> list[Any]:
    pairs = [(group, result) for result in results]
    provenance.attach(pairs, stmts, KEY)
    return [result for _group, result in pairs]


def test_a_filled_metric_carries_its_provenance() -> None:
    stmts = _payload()
    results = _attached(stmts, "profitability", profitability_compute(stmts, KEY))
    roe = next(r for r in results if r.name == "return_on_equity")
    block = roe.inputs["provenance"]
    assert block["accession"] == "0000320193-24-000001"
    assert block["period"] == KEY
    assert "MD-10" in block["rule_ids"]
    assert block["empty_reason"] is None
    # G2: each item is a list of (basis, concept, how, flags) entries
    assert block["items"]["net_income"][0]["concept"] == G + "NetIncomeLoss"
    assert block["items"]["net_income"][0]["how"] == "concept"
    assert block["items"]["net_income"][0]["basis"] == "ttm"
    assert block["items"]["equity"][0]["concept"] == G + "StockholdersEquity"
    assert block["items"]["equity"][0]["basis"] == "prior_year"
    assert block["items"]["equity"][0]["source"] == "filing"


def test_an_empty_metric_carries_a_reason_from_the_fixed_list() -> None:
    stmts = _payload(prior_equity=-10.0)
    results = _attached(stmts, "profitability", profitability_compute(stmts, KEY))
    roe = next(r for r in results if r.name == "return_on_equity")
    assert roe.value is None
    block = roe.inputs["provenance"]
    assert block["empty_reason"] in provenance.EMPTY_REASONS
    assert block["empty_reason"] == provenance.NOT_APPLICABLE  # APP-07, negative book equity
    assert "flag_return_on_equity_negative_equity" in block["flags"]


def test_a_missing_input_reason_is_the_default() -> None:
    # inventory_turnover with no cogs and no inventory: a plain missing input, no flag
    stmts = _payload(operating_income=None)
    results = _attached(stmts, "efficiency", efficiency_compute(stmts, KEY))
    turnover = next(r for r in results if r.name == "inventory_turnover")
    assert turnover.value is None
    assert turnover.inputs["provenance"]["empty_reason"] == provenance.MISSING_INPUT


def test_a_capture_failure_reason_comes_from_the_flags() -> None:
    # roic on a payload with no debt: the invested capital is a capture failure
    stmts = _payload()
    results = _attached(stmts, "roic", roic_compute(stmts, KEY))
    roic = next(r for r in results if r.name == "return_on_invested_capital")
    assert roic.value is None
    assert roic.inputs["provenance"]["empty_reason"] in provenance.EMPTY_REASONS


def test_attach_covers_every_result() -> None:
    stmts = _payload()
    results = _attached(stmts, "profitability", profitability_compute(stmts, KEY))
    for result in results:
        assert "provenance" in result.inputs


def test_each_metric_of_a_group_gets_its_own_provenance() -> None:
    # a group shares one inputs dict across its metrics: the block must be per metric, not the last
    # one written
    stmts = _payload()
    results = _attached(stmts, "profitability", profitability_compute(stmts, KEY))
    by_name = {r.name: r for r in results}
    assert by_name["net_margin"].inputs["provenance"]["rule_ids"] == ["MD-08", "ID-03"]
    assert by_name["return_on_equity"].inputs["provenance"]["rule_ids"] == [
        "MD-10",
        "MET-02",
        "APP-07",
    ]
    assert (
        by_name["net_margin"].inputs["provenance"]
        is not by_name["return_on_equity"].inputs["provenance"]
    )


def test_the_declarations_are_consistent() -> None:
    # every metric with items also has rule IDs, and vice versa
    assert set(provenance.ITEMS) == set(provenance.RULE_IDS)
    assert provenance.MISSING_INPUT in provenance.EMPTY_REASONS


def test_provenance_is_json_serialisable() -> None:
    stmts = _payload()
    results = _attached(stmts, "profitability", profitability_compute(stmts, KEY))
    for result in results:
        json.dumps(result.inputs)


def test_a_metric_with_no_declaration_gets_an_empty_item_map() -> None:
    # a hypothetical metric not in the declarations still gets a block (with no items)
    result = MetricResult("not_a_metric", 1.0, "ratio", {})
    block = provenance.build(_payload(), KEY, result)
    assert block["items"] == {}
    assert block["rule_ids"] == []
    assert block["empty_reason"] is None


# -- G2: the basis of each item ---------------------------------------------------------------


def test_a_ttm_flow_item_records_its_method_and_columns() -> None:
    stmts = _payload()
    # a 10-Q's FCF margin reads its flows on the TTM basis; here the flows are given explicitly
    flows = {"operating_cash_flow": TTMFlow(1_200.0, "ytd", ytd=900.0)}
    pairs = [
        ("cashflow", r)
        for r in cashflow_compute(stmts, KEY, None, {"operating_cash_flow": 1_200.0})
    ]
    provenance.attach(pairs, stmts, KEY, flows)
    result = next(r for _g, r in pairs if r.name == "free_cash_flow_margin")
    entry = result.inputs["provenance"]["items"]["operating_cash_flow"][0]
    assert entry["basis"] == "ttm"
    assert entry["method"] == "ytd"
    assert entry["ytd"] == 900.0


def test_a_prior_year_capital_item_records_its_source() -> None:
    stmts = _payload()
    pairs = [("roic", r) for r in roic_compute(stmts, KEY)]
    provenance.attach(pairs, stmts, KEY)
    result = next(r for _g, r in pairs if r.name == "return_on_invested_capital")
    entry = result.inputs["provenance"]["items"]["equity"][0]
    assert entry["basis"] == "prior_year"
    assert entry["source"] == "filing"
    assert entry["concept"] == G + "StockholdersEquity"


def test_md38_lists_the_working_capital_on_both_bases() -> None:
    stmts = _payload()
    pairs = [("valuation", r) for r in valuation_compute(stmts, KEY)]
    provenance.attach(pairs, stmts, KEY)
    result = next(r for _g, r in pairs if r.name == "enterprise_fcf_yield")
    bases = [entry["basis"] for entry in result.inputs["provenance"]["items"]["current_assets"]]
    assert bases == ["period", "prior_year"]


# -- G4: the empty reason's precedence ------------------------------------------------------------


def test_no_operating_income_with_a_refused_debt_is_missing_input_for_roic() -> None:
    """G4: the numerator's absence is the first failing term, before the refused debt (a capture
    failure) or the prior column (which is present here)."""
    stmts = Statements.from_payload(
        {
            "income_statement": [
                _row(G + "NetIncomeLoss", "Net income", **{KEY: 80.0}),
                _row(
                    G
                    + "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                    "Pretax",
                    **{KEY: 120.0},
                ),
                _row(G + "IncomeTaxExpenseBenefit", "Income tax", **{KEY: 20.0}),
            ],
            "balance_sheet": [
                _row(G + "Assets", "Assets", **{"2024-12-31": 1_100.0, "2023-12-31": 1_000.0}),
                _row(
                    G + "StockholdersEquity", "Equity", **{"2024-12-31": 450.0, "2023-12-31": 400.0}
                ),
                _row(
                    G + "CashAndCashEquivalentsAtCarryingValue",
                    "Cash",
                    **{"2024-12-31": 60.0, "2023-12-31": 50.0},
                ),
                # a debt-named line outside the sourced children: the long-term sum refuses
                _row(
                    G + "SeniorNotes", "Senior notes", **{"2024-12-31": 210.0, "2023-12-31": 200.0}
                ),
            ],
            "cash_flow": [],
        }
    )
    stmts.accession = "0000000000-24-000001"
    pairs = [("roic", r) for r in roic_compute(stmts, KEY)]
    provenance.attach(pairs, stmts, KEY)
    roic = next(r for _g, r in pairs if r.name == "return_on_invested_capital")
    assert roic.value is None
    assert roic.inputs["provenance"]["empty_reason"] == provenance.MISSING_INPUT


def test_an_absent_prior_column_is_prior_year_missing() -> None:
    stmts = Statements.from_payload(
        {
            "income_statement": [
                _row(G + "OperatingIncomeLoss", "Operating income", **{KEY: 100.0}),
                _row(G + "NetIncomeLoss", "Net income", **{KEY: 80.0}),
                _row(
                    G
                    + "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                    "Pretax",
                    **{KEY: 120.0},
                ),
                _row(G + "IncomeTaxExpenseBenefit", "Income tax", **{KEY: 20.0}),
            ],
            "balance_sheet": [
                # only the current column: no comparative, and no prior 10-K
                _row(G + "Assets", "Assets", **{"2024-12-31": 1_100.0}),
                _row(G + "StockholdersEquity", "Equity", **{"2024-12-31": 450.0}),
                _row(G + "CashAndCashEquivalentsAtCarryingValue", "Cash", **{"2024-12-31": 60.0}),
                _row(G + "LongTermDebt", "Debt", **{"2024-12-31": 210.0}),
            ],
            "cash_flow": [],
        }
    )
    pairs = [("roic", r) for r in roic_compute(stmts, KEY)]
    provenance.attach(pairs, stmts, KEY)
    roic = next(r for _g, r in pairs if r.name == "return_on_invested_capital")
    assert roic.value is None
    assert roic.inputs["provenance"]["empty_reason"] == provenance.PRIOR_YEAR_MISSING


# -- H1: an optional item is never a missing_input ---------------------------------------------


def _h1_payload(*, short_term_debt: float | None = None) -> Statements:
    """A payload whose only debt line is ``SeniorNotes`` -- outside the sourced long-term sum, so
    the debt refuses (``debt_sum_refused_other_lines``)."""
    balance = [
        _row(G + "Assets", "Assets", **{"2024-12-31": 1_100.0, "2023-12-31": 1_000.0}),
        _row(G + "StockholdersEquity", "Equity", **{"2024-12-31": 450.0, "2023-12-31": 400.0}),
        _row(
            G + "CashAndCashEquivalentsAtCarryingValue",
            "Cash",
            **{"2024-12-31": 60.0, "2023-12-31": 50.0},
        ),
        _row(G + "SecuredDebt", "Secured debt", **{"2024-12-31": 200.0, "2023-12-31": 190.0}),
        _row(G + "SeniorNotes", "Senior notes", **{"2024-12-31": 210.0, "2023-12-31": 200.0}),
    ]
    if short_term_debt is not None:
        balance.append(
            _row(G + "LongTermDebtCurrent", "ST debt", **{"2024-12-31": short_term_debt})
        )
    return Statements.from_payload(
        {
            "income_statement": [
                _row(G + "OperatingIncomeLoss", "Operating income", **{KEY: 100.0}),
                _row(G + "NetIncomeLoss", "Net income", **{KEY: 80.0}),
            ],
            "balance_sheet": balance,
            "cash_flow": [],
        }
    )


def test_h1_a_refused_debt_is_a_capture_failure_not_a_missing_input() -> None:
    stmts = _h1_payload()  # no short-term debt line at all
    pairs = [("leverage", r) for r in leverage_compute(stmts, KEY)]
    provenance.attach(pairs, stmts, KEY)
    d_to_e = next(r for _g, r in pairs if r.name == "debt_to_equity")
    assert d_to_e.value is None
    # the short-term debt is optional (statements._total_debt: lt + (st or 0)), so the cause is the
    # refused long-term sum, not a missing short-term line
    assert d_to_e.inputs["provenance"]["empty_reason"] == provenance.CAPTURE_FAILURE


def test_h1_absent_nci_lines_do_not_make_an_enterprise_value_missing() -> None:
    stmts = Statements.from_payload(
        {
            "income_statement": [_row(G + "NetIncomeLoss", "Net income", **{KEY: 80.0})],
            "balance_sheet": [
                _row(G + "Assets", "Assets", **{"2024-12-31": 1_100.0, "2023-12-31": 1_000.0}),
                _row(
                    G + "StockholdersEquity", "Equity", **{"2024-12-31": 450.0, "2023-12-31": 400.0}
                ),
                _row(
                    G + "CashAndCashEquivalentsAtCarryingValue",
                    "Cash",
                    **{"2024-12-31": 60.0, "2023-12-31": 50.0},
                ),
                _row(G + "LongTermDebt", "Debt", **{"2024-12-31": 210.0, "2023-12-31": 200.0}),
            ],
            "cash_flow": [],
        }
    )
    # a market cap makes the EV computable; the NCI lines are absent and no NCI income is filed
    pairs = [("valuation", r) for r in valuation_compute(stmts, KEY, market_cap=_mcap())]
    provenance.attach(pairs, stmts, KEY)
    ev = next(r for _g, r in pairs if r.name == "enterprise_value")
    assert ev.value is not None  # the NCI lines being absent is not a missing input
    assert ev.inputs["provenance"]["empty_reason"] is None


# -- H2: a TTM that was not formed --------------------------------------------------------------


def test_h2_a_first_year_10q_is_prior_year_missing() -> None:
    q = "2024-06-30 (Q2)"
    stmts = Statements.from_payload(
        {
            "income_statement": [
                _row(
                    G + "OperatingIncomeLoss",
                    "Operating income",
                    **{q: 25.0, "2024-06-30 (YTD)": 50.0},
                ),
                _row(G + "NetIncomeLoss", "Net income", **{q: 20.0, "2024-06-30 (YTD)": 40.0}),
                _row(
                    G
                    + "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                    "Pretax",
                    **{q: 30.0, "2024-06-30 (YTD)": 60.0},
                ),
                _row(
                    G + "IncomeTaxExpenseBenefit",
                    "Income tax",
                    **{q: 5.0, "2024-06-30 (YTD)": 10.0},
                ),
            ],
            "balance_sheet": [
                _row(G + "Assets", "Assets", **{"2024-06-30": 1_100.0, "2023-12-31": 1_000.0}),
                _row(
                    G + "StockholdersEquity", "Equity", **{"2024-06-30": 450.0, "2023-12-31": 400.0}
                ),
                _row(
                    G + "CashAndCashEquivalentsAtCarryingValue",
                    "Cash",
                    **{"2024-06-30": 60.0, "2023-12-31": 50.0},
                ),
                _row(G + "LongTermDebt", "Debt", **{"2024-06-30": 210.0, "2023-12-31": 200.0}),
            ],
            "cash_flow": [],
        }
    )
    # the metric's TTM is empty (the flow was not formed) and the provenance is told why
    pairs = [("roic", r) for r in roic_compute(stmts, q, None, {})]
    flows = {"operating_income": TTMFlow(100.0, "x4")}
    provenance.attach(pairs, stmts, q, flows)
    roic = next(r for _g, r in pairs if r.name == "return_on_invested_capital")
    assert roic.value is None
    assert roic.inputs["provenance"]["empty_reason"] == provenance.PRIOR_YEAR_MISSING


# -- H3: the prior basis follows the metric's own rule -------------------------------------------


def test_h3_md38_on_a_10q_without_the_prior_quarter_is_prior_year_missing() -> None:
    q = "2024-06-30 (Q2)"
    stmts = Statements.from_payload(
        {
            "income_statement": [
                _row(
                    G + "OperatingIncomeLoss",
                    "Operating income",
                    **{q: 25.0, "2024-06-30 (YTD)": 50.0},
                ),
                _row(
                    G
                    + "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                    "Pretax",
                    **{q: 30.0, "2024-06-30 (YTD)": 60.0},
                ),
                _row(
                    G + "IncomeTaxExpenseBenefit",
                    "Income tax",
                    **{q: 5.0, "2024-06-30 (YTD)": 10.0},
                ),
            ],
            "balance_sheet": [
                _row(G + "Assets", "Assets", **{"2024-06-30": 1_100.0}),
                _row(G + "AssetsCurrent", "Current assets", **{"2024-06-30": 300.0}),
                _row(G + "LiabilitiesCurrent", "Current liabilities", **{"2024-06-30": 150.0}),
                _row(G + "CashAndCashEquivalentsAtCarryingValue", "Cash", **{"2024-06-30": 60.0}),
                _row(G + "LongTermDebt", "Debt", **{"2024-06-30": 210.0}),
                _row(G + "StockholdersEquity", "Equity", **{"2024-06-30": 450.0}),
            ],
            "cash_flow": [
                _row(
                    G + "PaymentsToAcquirePropertyPlantAndEquipment",
                    "Capex",
                    **{q: 10.0, "2024-06-30 (YTD)": 20.0},
                ),
                _row(
                    G + "DepreciationDepletionAndAmortization",
                    "D&A",
                    **{q: 5.0, "2024-06-30 (YTD)": 10.0},
                ),
            ],
        }
    )
    # the TTM flows are formed (the YTD columns are filed), so the only failing term is the prior
    # working capital
    flows = {
        "operating_income": TTMFlow(50.0, "ytd"),
        "income_tax": TTMFlow(10.0, "ytd"),
        "pretax_income": TTMFlow(60.0, "ytd"),
        "capital_expenditure": TTMFlow(20.0, "ytd"),
        "depreciation_amortization": TTMFlow(10.0, "ytd"),
    }
    pairs = [
        ("valuation", r)
        for r in valuation_compute(
            stmts, q, market_cap=_mcap(), ttm={k: v.value for k, v in flows.items()}
        )
    ]
    provenance.attach(pairs, stmts, q, flows)
    efy = next(r for _g, r in pairs if r.name == "enterprise_fcf_yield")
    assert efy.value is None
    block = efy.inputs["provenance"]
    # MD-38 on a 10-Q reads the prior year's same quarter, which is not stored here
    bases = [entry["basis"] for entry in block["items"]["current_assets"]]
    assert bases == ["period", "prior_quarter"]
    prior = block["items"]["current_assets"][1]
    assert prior["source"] is None
    assert block["empty_reason"] == provenance.PRIOR_YEAR_MISSING


# -- H4: the TTM entry records its value and the columns it combined -------------------------------


def test_h4_a_ytd_ttm_entry_gives_the_value_from_its_columns() -> None:

    stmts = _payload()
    q = "2024-06-30 (Q2)"
    flow = TTMFlow(
        120.0,
        "ytd",
        alt=118.0,
        ytd=40.0,
        terms=(("prior_fiscal_year", 100.0), ("prior_ytd", 20.0), ("ytd", 40.0)),
        columns=(
            ("prior_fiscal_year", "2023-12-31", "0000000000-24-000001"),
            ("prior_ytd", "2023-06-30 (YTD)", "0000000000-24-000002"),
            ("ytd", "2024-06-30 (YTD)", "0000000000-24-000002"),
        ),
    )
    pairs = [
        ("cashflow", r) for r in cashflow_compute(stmts, q, None, {"operating_cash_flow": 120.0})
    ]
    provenance.attach(pairs, stmts, q, {"operating_cash_flow": flow})
    result = next(r for _g, r in pairs if r.name == "free_cash_flow_margin")
    entry = result.inputs["provenance"]["items"]["operating_cash_flow"][0]
    assert entry["value"] == 120.0
    assert entry["four_quarter_check"] == 118.0
    terms = dict(entry["terms"])
    assert terms["prior_fiscal_year"] - terms["prior_ytd"] + terms["ytd"] == entry["value"]
    columns = {label: (key, accession) for label, key, accession in entry["columns"]}
    assert columns["prior_fiscal_year"] == ("2023-12-31", "0000000000-24-000001")
    assert columns["ytd"] == ("2024-06-30 (YTD)", "0000000000-24-000002")
