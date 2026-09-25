"""T-105: the 10-Q ratios F4 left quarterly -- FCF yields, net debt / EBITDA, ROIC -- use
trailing-twelve-month flows, and the TTM itself prefers the year-to-date identity
``FY(prior 10-K) - YTD(last year) + YTD(this year)``, which also covers a filer whose cash-flow
statement has no quarterly column (XOM reports only year-to-date).
"""

from __future__ import annotations

from typing import Any

import pytest
from portfolio_common.db import Database

from fundamental_agent import db
from fundamental_agent.agents import _flag_annualization
from fundamental_agent.db import FilingKey, FilingMeta, YTDPair
from fundamental_agent.metrics.base import MetricResult, TTMFlow
from fundamental_agent.metrics.leverage import compute as leverage_compute
from fundamental_agent.metrics.roic import compute as roic_compute
from fundamental_agent.metrics.valuation import compute as valuation_compute
from fundamental_agent.pipeline import _targets, _YearTask, _ytd_columns
from fundamental_agent.pricing import ClosePrice
from fundamental_agent.statements import Statements


def _asset(conn: Database) -> int:
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'XOM')")
    return 1


def _record(  # noqa: PLR0913, PLR0917 - one recorded metric row, all fields explicit
    conn: Database, form: str, period: str, period_end: str, group: str, name: str, inputs: Any
) -> None:
    fid = db.upsert_filing(
        conn, FilingKey(1, form, int(period_end[:4]), period), FilingMeta(period_end=period_end)
    )
    db.record_metrics(conn, fid, [(group, MetricResult(name, None, "r", dict(inputs)))])


# -- the TTM engine ----------------------------------------------------------------------------


def test_the_year_to_date_identity(memory_db: Database) -> None:
    """XOM's shape: FY2024 from the 10-K, both year-to-date columns from the 2025Q3 10-Q, no
    quarterly cash-flow column at all."""
    _asset(memory_db)
    _record(
        memory_db,
        "10-K",
        "FY2024",
        "2024-12-31",
        "cashflow",
        "free_cash_flow_margin",
        {"operating_cash_flow": 55_022.0},
    )
    flows = db.ttm_detail(
        memory_db,
        1,
        period_end="2025-09-30",
        current={},  # no (Q3) column for cash flows
        ytd={"operating_cash_flow": YTDPair(39_291.0, 42_793.0, "2024-09-30")},
    )
    assert flows == {"operating_cash_flow": TTMFlow(55_022.0 - 42_793.0 + 39_291.0, "ytd")}


def test_the_identity_is_preferred_over_four_quarters(memory_db: Database) -> None:
    _asset(memory_db)
    _record(
        memory_db,
        "10-K",
        "FY2024",
        "2024-12-31",
        "profitability",
        "return_on_assets",
        {"net_income": 100.0},
    )
    for label, end, ni in (("2025Q2", "2025-06-30", 20.0), ("2025Q1", "2025-03-31", 30.0)):
        _record(
            memory_db, "10-Q", label, end, "profitability", "return_on_assets", {"net_income": ni}
        )
    flows = db.ttm_detail(
        memory_db,
        1,
        period_end="2025-09-30",
        current={"net_income": 25.0},
        ytd={"net_income": YTDPair(75.0, 60.0, "2024-09-30")},
    )
    assert flows["net_income"] == TTMFlow(115.0, "ytd")


def test_a_10k_outside_the_year_is_not_used(memory_db: Database) -> None:
    """The prior fiscal year must end between last year's year-to-date and this period --
    otherwise it is not the year that year-to-date belongs to."""
    _asset(memory_db)
    _record(
        memory_db,
        "10-K",
        "FY2023",
        "2023-12-31",
        "profitability",
        "return_on_assets",
        {"net_income": 100.0},
    )
    flows = db.ttm_detail(
        memory_db,
        1,
        period_end="2025-09-30",
        current={"net_income": 25.0},
        ytd={"net_income": YTDPair(75.0, 60.0, "2024-09-30")},
    )
    assert flows["net_income"] == TTMFlow(100.0, "x4")  # no identity, no quarters: x4


def test_a_flow_with_nothing_to_annualize_is_absent(memory_db: Database) -> None:
    _asset(memory_db)
    flows = db.ttm_detail(
        memory_db,
        1,
        period_end="2025-09-30",
        current={},
        ytd={"operating_cash_flow": YTDPair(39_291.0, 42_793.0, "2024-09-30")},
    )
    assert flows == {}


# -- which columns are "year to date" ---------------------------------------------------------


def _stmts(*columns: str) -> Statements:
    row = {
        "concept": "us-gaap_NetIncomeLoss",
        "label": "Net income",
        "abstract": False,
        "dimension": False,
        **dict.fromkeys(columns, 1.0),
    }
    return Statements.from_payload(
        {"income_statement": [row], "balance_sheet": [], "cash_flow": []}
    )


def test_ytd_columns_are_this_year_s_and_last_year_s() -> None:
    stmts = _stmts("2025-09-30 (Q3)", "2025-09-30 (YTD)", "2024-09-30 (Q3)", "2024-09-30 (YTD)")
    target = _targets(stmts, _YearTask(1, "X", "X", "10-Q", 2025))[0]
    assert _ytd_columns(stmts, target) == ("2025-09-30 (YTD)", "2024-09-30 (YTD)", "2024-09-30")


def test_a_first_quarter_s_year_to_date_is_its_quarter() -> None:
    stmts = _stmts("2025-03-29 (Q1)", "2024-03-30 (Q1)")  # a 52/53-week calendar
    target = _targets(stmts, _YearTask(1, "X", "X", "10-Q", 2025))[0]
    assert _ytd_columns(stmts, target) == ("2025-03-29 (Q1)", "2024-03-30 (Q1)", "2024-03-30")


def test_no_year_ago_column_means_no_identity() -> None:
    stmts = _stmts("2025-09-30 (Q3)", "2025-09-30 (YTD)")
    target = _targets(stmts, _YearTask(1, "X", "X", "10-Q", 2025))[0]
    assert _ytd_columns(stmts, target) == (None, None, None)


# -- the ratios -------------------------------------------------------------------------------


def _quarter() -> Statements:
    key = "2025-06-30 (Q2)"

    def row(concept: str, value: float, statement: str = "cash_flow") -> tuple[str, Any]:
        return statement, {
            "concept": concept,
            "label": concept,
            "abstract": False,
            "dimension": False,
            key: value,
        }

    rows = [
        row("us-gaap_NetCashProvidedByUsedInOperatingActivities", 1_000.0),
        row("us-gaap_PaymentsToAcquirePropertyPlantAndEquipment", -200.0),
        row("us-gaap_ShareBasedCompensation", 50.0),
        row("us-gaap_OperatingIncomeLoss", 900.0, "income_statement"),
        row("us-gaap_DepreciationDepletionAndAmortization", 100.0),
        row("us-gaap_InterestExpense", 40.0, "income_statement"),
    ]
    balance = [
        {
            "concept": "us-gaap_CommonStockSharesOutstanding",
            "label": "shares",
            "abstract": False,
            "dimension": False,
            "2025-06-30": 100.0,
        },
        {
            "concept": "us-gaap_LongTermDebt",
            "label": "debt",
            "abstract": False,
            "dimension": False,
            "2025-06-30": 10_000.0,
        },
        {
            "concept": "us-gaap_CashAndCashEquivalentsAtCarryingValue",
            "label": "cash",
            "abstract": False,
            "dimension": False,
            "2025-06-30": 2_000.0,
        },
        {
            "concept": "us-gaap_StockholdersEquity",
            "label": "equity",
            "abstract": False,
            "dimension": False,
            "2025-06-30": 20_000.0,
        },
    ]
    payload: dict[str, list[Any]] = {
        "income_statement": [],
        "balance_sheet": balance,
        "cash_flow": [],
    }
    for statement, r in rows:
        payload[statement].append(r)
    return Statements.from_payload(payload)


TTM = {
    "operating_cash_flow": 4_100.0,
    "capital_expenditure": -800.0,
    "stock_based_compensation": 190.0,
    "operating_income": 3_500.0,
    "depreciation_amortization": 400.0,
    "interest_expense": 160.0,
}
KEY = "2025-06-30 (Q2)"
PRICE = ClosePrice(date="2025-06-30", close=400.0)  # market cap 40,000


def _by_name(results: list[MetricResult]) -> dict[str, MetricResult]:
    return {r.name: r for r in results}


def test_fcf_yields_divide_a_year_of_cash_flow_by_the_price() -> None:
    out = _by_name(valuation_compute(_quarter(), KEY, PRICE, None, TTM))
    assert out["free_cash_flow_yield"].value == pytest.approx((4_100 - 800) / 40_000)
    assert out["sbc_adjusted_fcf_yield"].value == pytest.approx((4_100 - 800 - 190) / 40_000)
    inputs = out["free_cash_flow_yield"].inputs
    assert inputs["free_cash_flow_to_equity"] == 800.0  # the raw quarter stays on record
    assert inputs["free_cash_flow_to_equity_ttm"] == 3_300.0
    # without TTM (a 10-K) the filing's own period is the year
    tenk = _by_name(valuation_compute(_quarter(), KEY, PRICE, None, None))
    assert tenk["free_cash_flow_yield"].value == pytest.approx(800 / 40_000)


def test_a_ttm_yield_never_mixes_in_a_raw_quarter() -> None:
    partial = {k: v for k, v in TTM.items() if k != "capital_expenditure"}
    out = _by_name(valuation_compute(_quarter(), KEY, PRICE, None, partial))
    assert out["free_cash_flow_yield"].value is None


def test_net_debt_to_ebitda_uses_a_year_of_ebitda() -> None:
    out = _by_name(leverage_compute(_quarter(), KEY, None, TTM))
    assert out["net_debt_to_ebitda"].value == pytest.approx((10_000 - 2_000) / (3_500 + 400))
    assert out["net_debt_to_ebitda"].inputs["ebitda_ttm"] == 3_900.0
    quarterly = _by_name(leverage_compute(_quarter(), KEY, None, None))
    assert quarterly["net_debt_to_ebitda"].value == pytest.approx(8_000 / 1_000)


def test_roic_uses_a_year_of_nopat_and_nopat_stays_the_quarter() -> None:
    out = _by_name(roic_compute(_quarter(), KEY, None, TTM))
    rate = out["effective_tax_rate"].value
    assert rate is not None
    assert out["return_on_invested_capital"].value == pytest.approx(
        3_500 * (1 - rate) / (10_000 + 20_000 - 2_000)
    )
    assert out["nopat"].value == pytest.approx(900 * (1 - rate))


def test_each_group_is_stamped_with_how_it_was_annualized() -> None:
    lev = MetricResult("net_debt_to_ebitda", 1.0, "x", {})
    val = MetricResult("free_cash_flow_yield", 0.03, "ratio", {})
    liq = MetricResult("current_ratio", 1.2, "x", {})
    flows = {
        "operating_income": TTMFlow(1.0, "ytd"),
        "depreciation_amortization": TTMFlow(1.0, "ytd"),
        "operating_cash_flow": TTMFlow(1.0, "ytd"),
        "capital_expenditure": TTMFlow(1.0, "x4"),
    }
    _flag_annualization([("leverage", lev), ("valuation", val), ("liquidity", liq)], flows)
    assert lev.inputs == {"annualized_ttm": 1.0}
    assert val.inputs == {"annualized_x4": 1.0}  # one crude input taints the group
    assert liq.inputs == {}
