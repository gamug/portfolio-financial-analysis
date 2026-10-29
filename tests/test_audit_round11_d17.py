"""Audit round #11, D-17 (the user and reviewer A, 2026-10-29): operating income derived when the income statement
prints no operating-income line -- pre-tax income + the gross interest expense - the non-operating lines (S-X 5-03
items 7-9) - the equity-method income above the pre-tax line. The shapes are the control filings' (they print the
line, so the derivation can be checked against it): a single-step statement, NEE's "total other income (deductions)"
that holds the interest, a gain line presented inside revenue (EQT), EIX's wildfire claims among the operating costs,
an extension line read as non-operating by its caption, and the TTM's one-method rule.
"""

from __future__ import annotations

from typing import Any

from conftest import filed_after
from portfolio_common.db import Database

from fundamental_agent import db
from fundamental_agent.db import FilingKey, FilingMeta, YTDPair
from fundamental_agent.metrics import provenance
from fundamental_agent.metrics.base import MetricResult
from fundamental_agent.statements import EBIT_DERIVED_HOW, EBIT_DERIVED_INPUT, Statements

G = "us-gaap_"
FY = "2025-12-31 (FY)"
COL = "2025-12-31"


def _row(
    concept: str, label: str, val: float, sign: float = 1.0, column: str = FY
) -> dict[str, Any]:
    return {
        "concept": concept if "_" in concept else G + concept,
        "label": label,
        "abstract": False,
        "dimension": False,
        "preferred_sign": sign,
        column: val,
    }


def _stmts(income: list[dict[str, Any]], company_type: str | None = "operating") -> Statements:
    stmts = Statements.from_payload(
        {
            "income_statement": income,
            "balance_sheet": [_row("Assets", "Total assets", 1_000.0, column=COL)],
            "cash_flow": [],
            "corrections": [],
            "filed_facts": [],
        }
    )
    stmts.company_type = company_type
    return stmts


SINGLE_STEP = [
    _row("Revenues", "Total revenues", 1_000.0),
    _row("CostOfGoodsAndServicesSold", "Cost of sales", 600.0),
    _row("SellingGeneralAndAdministrativeExpense", "Selling, general and administrative", 200.0),
    _row("InterestExpense", "Interest expense", 30.0),
    _row("OtherNonoperatingIncomeExpense", "Other income, net", 10.0),
    _row(
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "Income before income taxes",
        180.0,
    ),
]


def test_a_single_step_statement_derives_the_operating_income() -> None:
    """180 + 30 interest - 10 other income = 200 = 1,000 - 600 - 200; flagged ``ebit_derived``."""
    res = _stmts(SINGLE_STEP).resolve("operating_income", FY)
    assert res.value == 200.0
    assert res.how == EBIT_DERIVED_HOW and res.concept is None and "ebit_derived" in res.flags


def test_a_printed_operating_income_line_is_read_never_derived() -> None:
    income = [
        *SINGLE_STEP[:3],
        _row("OperatingIncomeLoss", "Operating income", 205.0),
        *SINGLE_STEP[3:],
    ]
    res = _stmts(income).resolve("operating_income", FY)
    assert res.value == 205.0 and res.how != EBIT_DERIVED_HOW


def test_an_operating_income_line_the_gateway_dropped_is_never_derived() -> None:
    stmts = _stmts(SINGLE_STEP)
    stmts.corrections.append(
        {
            "statement": "income_statement",
            "concept": G + "OperatingIncomeLoss",
            "column": FY,
            "original": 200.0,
        }
    )
    assert stmts.resolve("operating_income", FY).how != EBIT_DERIVED_HOW


def test_financial_and_unknown_company_types_get_no_derived_operating_income() -> None:
    for company_type in ("article_9", "article_7", "other_financial", "multi_sector_holding", None):
        assert _stmts(SINGLE_STEP, company_type).resolve("operating_income", FY).value is None


def test_no_interest_expense_and_debt_not_proven_zero_leaves_it_empty() -> None:
    """Reviewer A (b): with no interest anywhere the interest is 0 only when the debt is proven 0 -- not here."""
    no_interest = [r for r in SINGLE_STEP if r["concept"] != G + "InterestExpense"]
    assert _stmts(no_interest).resolve("operating_income", FY).value is None


def test_a_net_interest_line_is_taken_out_whole() -> None:
    """Reviewer A (a), C7 p.9: "Interest expense, net" -30 (income 5 - expense 35) -- the whole line is taken out:
    180 + 30 - 10 = 200, flagged ``ebit_net_interest``."""
    net_only = [
        _row("InterestIncomeExpenseNonoperatingNet", "Interest expense, net", -30.0)
        if r["concept"] == G + "InterestExpense"
        else r
        for r in SINGLE_STEP
    ]
    res = _stmts(net_only).resolve("operating_income", FY)
    assert res.value == 200.0 and "ebit_net_interest" in res.flags


def test_the_filers_total_is_split_and_its_interest_taken_back_out() -> None:
    """NEE's shape: interest, other and the total "other income (deductions)" below the operating costs. 220 pre-tax
    + 100 gross interest - (-80 total, less its -100 interest = 20) = 300 = 1,000 - 700."""
    income = [
        _row("Revenues", "Operating revenues", 1_000.0),
        _row("OperatingExpenses", "Total operating expenses", 700.0),
        _row("InterestExpense", "Interest expense", 100.0),
        _row("OtherNonoperatingIncomeExpense", "Other - net", 20.0),
        _row("NonoperatingIncomeExpense", "Total other income (deductions) - net", -80.0),
        _row(
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "Income before income taxes",
            220.0,
        ),
    ]
    assert _stmts(income).resolve("operating_income", FY).value == 300.0


def test_an_extension_line_inside_the_total_is_tried_both_ways() -> None:
    """NEE: an extension line of unknown direction inside the total -- the unique split decides its sign."""
    income = [
        _row("Revenues", "Operating revenues", 1_000.0),
        _row("OperatingExpenses", "Total operating expenses", 700.0),
        _row("nee_InterestExpenseAggregateExpenses", "Interest expense", 100.0),
        _row(
            "abc_ChangeInUnrealizedGainsOnDecommissioningFunds", "Change in unrealized gains", -7.0
        ),
        _row("OtherNonoperatingIncomeExpense", "Other - net", 20.0),
        _row("NonoperatingIncomeExpense", "Total other income (deductions) - net", -87.0),
        _row(
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "Income before income taxes",
            213.0,
        ),
    ]
    assert _stmts(income).resolve("operating_income", FY).value == 300.0


def test_the_equity_method_income_above_pretax_is_taken_out() -> None:
    """C7 p.46: the equity-method income above the pre-tax line is not operating."""
    income = [
        *SINGLE_STEP[:5],
        _row("IncomeLossFromEquityMethodInvestments", "Equity in earnings", 15.0),
    ]
    income.append(SINGLE_STEP[5] | {FY: 195.0})
    assert _stmts(income).resolve("operating_income", FY).value == 200.0


def test_the_equity_method_income_inside_revenue_is_taken_out_too() -> None:
    """XOM: "Income from equity affiliates" inside "Total revenues and other income" -- above the pre-tax line."""
    income = [
        _row(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Sales and other operating revenue",
            1_000.0,
        ),
        _row("IncomeLossFromEquityMethodInvestments", "Income from equity affiliates", 60.0),
        _row("Revenues", "Total revenues and other income", 1_060.0),
        *SINGLE_STEP[1:4],
        SINGLE_STEP[5] | {FY: 230.0},
    ]
    assert _stmts(income).resolve("operating_income", FY).value == 200.0


def test_a_line_inside_revenue_and_an_operating_cost_element_are_not_eliminated() -> None:
    """EQT: derivative gains presented inside revenue; EIX: wildfire claims (``LossFromCatastrophes``, a
    non-operating element in the calculation tree the controls place among operating costs) -- neither removed."""
    income = [
        _row("GainLossOnDerivativeInstrumentsNetPretax", "Gain on derivatives", 50.0),
        _row("Revenues", "Total revenues", 1_050.0),
        _row("CostOfGoodsAndServicesSold", "Cost of sales", 600.0),
        _row("LossFromCatastrophes", "Wildfire-related claims", 40.0),
        _row(
            "SellingGeneralAndAdministrativeExpense", "Selling, general and administrative", 200.0
        ),
        _row("InterestExpense", "Interest expense", 30.0),
        _row(
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "Income before income taxes",
            180.0,
        ),
    ]
    res = _stmts(income).resolve("operating_income", FY)
    assert res.value == 210.0 and "ebit_tree_element_kept" in res.flags


def test_an_extension_non_operating_line_is_counted_not_read() -> None:
    income = [
        *SINGLE_STEP[:4],
        _row("abc_OtherIncomeNet", "Other income, net", 10.0),
        SINGLE_STEP[5],
    ]
    res = _stmts(income).resolve("operating_income", FY)
    assert res.value == 210.0 and "ebit_extension_line_not_read" in res.flags


def test_an_extension_equity_affiliates_line_is_counted_not_read() -> None:
    """CVX: "Income (loss) from equity affiliates" under an extension element, inside revenue's section."""
    income = [
        SINGLE_STEP[0],
        _row("cvx_EquityMethodInvestmentIncome", "Income (loss) from equity affiliates", 40.0),
        *SINGLE_STEP[1:4],
        SINGLE_STEP[5] | {FY: 210.0},
    ]
    res = _stmts(income).resolve("operating_income", FY)
    assert res.value == 240.0 and "ebit_extension_line_not_read" in res.flags


def test_a_pretax_line_under_another_element_places_the_lines() -> None:
    """COST: "INCOME BEFORE INCOME TAXES" tagged ``IncomeLossAttributableToParent``."""
    income = [
        *SINGLE_STEP[:5],
        _row("IncomeLossAttributableToParent", "INCOME BEFORE INCOME TAXES", 180.0),
    ]
    assert _stmts(income).resolve("operating_income", FY).value == 200.0


def test_every_metric_reading_a_derived_operating_income_is_flagged() -> None:
    stmts = _stmts(SINGLE_STEP)
    results = [
        ("profitability", MetricResult("operating_margin", 0.2, "ratio", {})),
        ("profitability", MetricResult("net_margin", 0.1, "ratio", {})),
    ]
    provenance.attach(results, stmts, FY)
    margin, net = results[0][1].inputs, results[1][1].inputs
    assert margin[EBIT_DERIVED_INPUT] == 1.0 and EBIT_DERIVED_INPUT in margin["provenance"]["flags"]
    assert margin["provenance"]["items"]["operating_income"][0]["how"] == EBIT_DERIVED_HOW
    assert EBIT_DERIVED_INPUT not in net


def _record(
    conn: Database, form: str, period: str, period_end: str, inputs: dict[str, Any]
) -> None:
    fid = db.upsert_filing(
        conn,
        FilingKey(1, form, int(period_end[:4]), period),
        FilingMeta(filing_date=filed_after(period_end), period_end=period_end),
    )
    db.record_metrics(
        conn, fid, [("leverage", MetricResult("net_debt_to_ebitda", None, "x", dict(inputs)))]
    )


def test_a_ttm_mixing_a_printed_and_a_derived_operating_income_is_refused(
    memory_db: Database,
) -> None:
    """D-17: the TTM's terms by one method -- the prior 10-K printed its line, this 10-Q's is derived."""
    memory_db.execute("INSERT INTO assets (id, ticker) VALUES (1, 'X')")
    _record(memory_db, "10-K", "FY2024", "2024-12-31", {"operating_income": 400.0})
    pair = {"operating_income": YTDPair(300.0, 280.0, "2024-09-30")}
    mixed = db.ttm_detail(
        memory_db,
        1,
        period_end="2025-09-30",
        current={},
        ytd=pair,
        derived=frozenset({"operating_income"}),
    )
    assert mixed["operating_income"].method == "refused"
    assert mixed["operating_income"].flags == ("ttm_ebit_method_mixed",)
    printed = db.ttm_detail(memory_db, 1, period_end="2025-09-30", current={}, ytd=pair)
    assert printed["operating_income"].value == 400.0 - 280.0 + 300.0


def test_a_ttm_of_derived_terms_is_built(memory_db: Database) -> None:
    memory_db.execute("INSERT INTO assets (id, ticker) VALUES (1, 'X')")
    _record(
        memory_db,
        "10-K",
        "FY2024",
        "2024-12-31",
        {"operating_income": 400.0, EBIT_DERIVED_INPUT: 1.0},
    )
    flows = db.ttm_detail(
        memory_db,
        1,
        period_end="2025-09-30",
        current={},
        ytd={"operating_income": YTDPair(300.0, 280.0, "2024-09-30")},
        derived=frozenset({"operating_income"}),
    )
    assert flows["operating_income"].method == "ytd" and flows["operating_income"].value == 420.0


# -- Part 2 (the 88 has_missed read one by one): XOM's inventory, FCX's open block --------------------------------


def _balance(rows: list[dict[str, Any]]) -> Statements:
    return Statements.from_payload(
        {
            "income_statement": [],
            "balance_sheet": rows,
            "cash_flow": [],
            "corrections": [],
            "filed_facts": [],
        }
    )


def test_two_inventory_components_one_net_of_reserves_are_summed_in_a_closed_block() -> None:
    """XOM 2023-Q1: "Crude oil, products and merchandise" + "Materials and supplies" (``...NetOfReserves``), no total,
    between receivables and other current assets: 19,458 + 4,184."""
    rows = [
        _row("ReceivablesNetCurrent", "Notes and accounts receivable - net", 38_808.0, column=COL),
        _row("EnergyRelatedInventory", "Crude oil, products and merchandise", 19_458.0, column=COL),
        _row(
            "InventoryPartsAndComponentsNetOfReserves",
            "Materials and supplies",
            4_184.0,
            column=COL,
        ),
        _row("OtherAssetsCurrent", "Other current assets", 2_098.0, column=COL),
    ]
    res = _balance(rows).resolve("inventory", COL)
    assert res.value == 23_642.0 and "inventory_components_summed" in res.flags


def test_a_net_of_reserves_sum_with_an_unnamed_neighbour_line_is_refused() -> None:
    """FCX 2022: "Product" sits in the inventory block under an extension element that does not name inventory --
    the two named lines (3,347) would leave it out, so nothing is read."""
    rows = [
        _row("AccountsReceivableNetCurrent", "Trade accounts receivable", 1_336.0, column=COL),
        _row(
            "InventoryRawMaterialsAndSuppliesNetOfReserves",
            "Total materials and supplies, net",
            1_964.0,
            column=COL,
        ),
        _row(
            "fcx_InventoryMillandStockpilesonLeachPadsCurrent",
            "Mill and leach stockpiles",
            1_383.0,
            column=COL,
        ),
        _row("fcx_Product", "Product", 1_833.0, column=COL),
        _row("OtherAssetsCurrent", "Other current assets", 492.0, column=COL),
    ]
    res = _balance(rows).resolve("inventory", COL)
    assert res.value is None and "inventory_components_block_open" in res.flags


# -- the independent review (B1, B3, minors 1-4) -------------------------------------------------------------------

PRETAX_ROW = SINGLE_STEP[5]


def test_splits_that_agree_on_the_interest_are_one_split() -> None:
    """Review B1 (GPC's shape): a zero line of unknown direction inside the total gives two fitting sign choices
    that agree -- the total is split, its interest taken back out: 220 + 100 - (-80 + 100) = 300."""
    income = [
        _row("Revenues", "Operating revenues", 1_000.0),
        _row("OperatingExpenses", "Total operating expenses", 700.0),
        _row("InterestExpense", "Interest expense", 100.0),
        _row("abc_SeveranceCosts", "Severance", 0.0),
        _row("OtherNonoperatingIncomeExpense", "Other - net", 20.0),
        _row("NonoperatingIncomeExpense", "Total other income (deductions) - net", -80.0),
        PRETAX_ROW | {FY: 220.0},
    ]
    assert _stmts(income).resolve("operating_income", FY).value == 300.0


def test_no_revenue_line_leaves_it_empty() -> None:
    """Review B3: without the revenue line the non-operating window is not placed."""
    income = [r for r in SINGLE_STEP if r["concept"] != G + "Revenues"]
    res = _stmts(income).resolve("operating_income", FY)
    assert res.value is None


def test_the_pretax_line_is_the_row_the_pretax_value_was_read_from() -> None:
    """Review minor 1: a zero line above the pre-tax line, with a pre-tax income of zero, is not the pre-tax line."""
    income = [
        *SINGLE_STEP[:3],
        _row("InterestExpense", "Interest expense", 30.0),
        _row("abc_Other", "Other", 0.0),
        _row("OtherNonoperatingIncomeExpense", "Other income, net", 10.0),
        PRETAX_ROW | {FY: 0.0},
    ]
    income[0] = income[0] | {FY: 800.0}
    assert _stmts(income).resolve("operating_income", FY).value == 20.0


def test_a_ttm_term_with_no_recorded_anchor_row_is_not_called_mixed(memory_db: Database) -> None:
    """Review minor 2: a prior quarter that recorded no anchor row supplies no term -- not a method mix."""
    memory_db.execute("INSERT INTO assets (id, ticker) VALUES (1, 'X')")
    _record(
        memory_db,
        "10-K",
        "FY2024",
        "2024-12-31",
        {"operating_income": 400.0, EBIT_DERIVED_INPUT: 1.0},
    )
    fid = db.upsert_filing(
        memory_db,
        FilingKey(1, "10-Q", 2025, "2025Q1"),
        FilingMeta(filing_date=filed_after("2025-03-31"), period_end="2025-03-31"),
    )
    assert fid
    flows = db.ttm_detail(
        memory_db,
        1,
        period_end="2025-09-30",
        current={},
        ytd={"operating_income": YTDPair(300.0, 280.0, "2024-09-30")},
        derived=frozenset({"operating_income"}),
    )
    assert flows["operating_income"].method == "ytd"


def test_a_noncurrent_net_of_reserves_element_is_not_a_component() -> None:
    """Review minor 4: only the "Reserve" of a net-of-reserves name is excused."""
    rows = [
        _row("ReceivablesNetCurrent", "Receivables", 100.0, column=COL),
        _row("EnergyRelatedInventory", "Crude oil and products", 50.0, column=COL),
        _row("InventoryNoncurrentNetOfReserves", "Long-term materials", 30.0, column=COL),
        _row("OtherAssetsCurrent", "Other current assets", 10.0, column=COL),
    ]
    assert _balance(rows).resolve("inventory", COL).value != 80.0


def test_an_interest_read_from_the_notes_inside_the_other_line_is_not_added_back() -> None:
    """E11-2 (PFE FY2021): no interest line on the face, the notes' 1,291 sits inside "Other (income)/deductions -- net"
    (income 4,878 net) -- 24,311 - 4,878 = 19,433, not 20,724."""
    income = [
        _row("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", 81_288.0),
        _row("CostOfGoodsAndServicesSold", "Cost of sales", 30_821.0),
        _row(
            "SellingGeneralAndAdministrativeExpense",
            "Selling, informational and administrative",
            12_703.0,
        ),
        _row(
            "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
            "Research and development",
            13_829.0,
        ),
        _row("pfe_AmortizationOfIntangibleAssets", "Amortization of intangible assets", 3_700.0),
        _row("pfe_RestructuringCharges", "Restructuring charges", 802.0),
        _row("OtherNonoperatingIncomeExpense", "Other (income)/deductions--net", 4_878.0),
        _row(
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "Income from continuing operations before provision for taxes on income",
            24_311.0,
        ),
    ]
    stmts = Statements.from_payload(
        {
            "income_statement": income,
            "balance_sheet": [],
            "cash_flow": [],
            "corrections": [],
            "filed_facts": [
                {
                    "concept": "us-gaap:InterestExpense",
                    "value": 1_291.0,
                    "unit": "USD",
                    "period_start": "2021-01-01",
                    "period_end": COL,
                },
            ],
        }
    )
    stmts.company_type = "operating"
    res = stmts.resolve("operating_income", FY)
    assert res.value == 19_433.0 and "ebit_interest_inside_nonoperating" in res.flags


def test_a_deconsolidation_gain_is_taken_out() -> None:
    """Reviewer A: DeconsolidationGainOrLossAmount is a one-time item (C7 p.37) like the sale of a subsidiary's stock."""
    income = [
        *SINGLE_STEP[:4],
        _row("DeconsolidationGainOrLossAmount", "Gain on deconsolidation of subsidiary", 600.0),
    ]
    income.append(SINGLE_STEP[5] | {FY: 770.0})
    res = _stmts(income).resolve("operating_income", FY)
    assert res.value == 200.0 and "ebit_tree_element_kept" not in res.flags


def test_no_interest_anywhere_and_debt_proven_zero_reads_the_interest_as_zero() -> None:
    """Reviewer A (b): no interest concept in the facts or the rows and the debt proven 0 (ID-02b) -- 170 - 10 = 160,
    flagged ``ebit_zero_interest_proven``; with any debt line the value stays empty."""
    income = [r for r in SINGLE_STEP if r["concept"] != G + "InterestExpense"]
    income[-1] = income[-1] | {FY: 170.0}
    balance = [
        _row("CashAndCashEquivalentsAtCarryingValue", "Cash", 300.0, column=COL),
        _row("Assets", "Total assets", 1_000.0, column=COL),
        _row("StockholdersEquity", "Total equity", 800.0, column=COL),
        _row(
            "LiabilitiesAndStockholdersEquity", "Total liabilities and equity", 1_000.0, column=COL
        ),
    ]
    facts = [
        {
            "concept": "us-gaap:Revenues",
            "value": 1_000.0,
            "unit": "USD",
            "period_start": "2025-01-01",
            "period_end": COL,
        }
    ]
    payload = {
        "income_statement": income,
        "balance_sheet": balance,
        "cash_flow": [],
        "corrections": [],
        "filed_facts": facts,
    }
    stmts = Statements.from_payload(payload)
    stmts.company_type = "operating"
    res = stmts.resolve("operating_income", FY)
    assert res.value == 160.0 and "ebit_zero_interest_proven" in res.flags
    with_debt = Statements.from_payload(
        payload
        | {
            "balance_sheet": [
                *balance,
                _row("LongTermDebtNoncurrent", "Long-term debt", 100.0, column=COL),
            ]
        }
    )
    with_debt.company_type = "operating"
    assert with_debt.resolve("operating_income", FY).value is None
