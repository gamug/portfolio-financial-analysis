"""T-133: cash-flow quality on a trailing-twelve-month basis, capex recognition beyond
``PaymentsToAcquirePropertyPlantAndEquipment``, and the first-quarter year-to-date detection that
makes WAT's first quarter (tagged ``(Q2)``) resolvable.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from fundamental_agent.agents import FilingContext, FundamentalAnalyst, _group_ttm
from fundamental_agent.metrics.base import TTMFlow
from fundamental_agent.metrics.cashflow import compute
from fundamental_agent.pipeline import _is_first_quarter, _targets, _YearTask, _ytd_columns
from fundamental_agent.statements import Statements


def _row(concept: str, label: str, **columns: float) -> dict[str, Any]:
    return {"concept": concept, "label": label, "abstract": False, "dimension": False, **columns}


def _stmts(
    cash_flow: list[dict[str, Any]],
    income: list[dict[str, Any]] | None = None,
    balance: list[dict[str, Any]] | None = None,
) -> Statements:
    return Statements.from_payload(
        {
            "income_statement": income or [],
            "balance_sheet": balance or [],
            "cash_flow": cash_flow,
        }
    )


def _metrics(stmts: Statements, key: str, ttm: dict[str, float] | None) -> dict[str, Any]:
    return {m.name: m for m in compute(stmts, key, None, ttm)}


# -- the ratios -------------------------------------------------------------------------------

Q = "2025-09-30 (Q3)"


def _q3_filing() -> Statements:
    """A Q3 10-Q the way EDGAR files it: income statement by quarter, cash flow year-to-date only."""
    return _stmts(
        [
            _row(
                "us-gaap_NetCashProvidedByUsedInOperatingActivities",
                "OCF",
                **{"2025-09-30 (YTD)": 900.0},
            ),
            _row(
                "us-gaap_PaymentsToAcquirePropertyPlantAndEquipment",
                "Capex",
                **{"2025-09-30 (YTD)": -300.0},
            ),
        ],
        income=[
            _row("us-gaap_Revenues", "Revenue", **{Q: 1_000.0}),
            _row("us-gaap_NetIncomeLoss", "NI", **{Q: 100.0}),
        ],
    )


def test_a_q3_filing_has_no_quarter_cash_flow_so_the_ratios_come_from_the_ttm() -> None:
    ttm = {
        "operating_cash_flow": 1_200.0,
        "capital_expenditure": -400.0,
        "revenue": 4_000.0,
        "net_income": 500.0,
    }
    got = _metrics(_q3_filing(), Q, ttm)
    assert got["free_cash_flow_margin"].value == pytest.approx(800.0 / 4_000.0)
    assert got["operating_cash_flow_margin"].value == pytest.approx(0.3)
    assert got["free_cash_flow_conversion"].value == pytest.approx(800.0 / 500.0)
    assert got["capex_intensity"].value == pytest.approx(0.1)
    inputs = got["free_cash_flow_margin"].inputs
    assert inputs["free_cash_flow_ttm"] == 800.0
    assert inputs["revenue_ttm"] == 4_000.0
    # the raw single-period figures stay the audit trail a later filing's own TTM reads back
    assert inputs["revenue"] == 1_000.0
    assert "operating_cash_flow" not in inputs  # there is no quarter column to record


def test_a_ttm_that_lacks_a_flow_leaves_the_ratio_empty_rather_than_mixing_bases() -> None:
    """Revenue is a 3-month figure here; dividing a 12-month cash flow by it would be 4x too big."""
    got = _metrics(_q3_filing(), Q, {"operating_cash_flow": 1_200.0, "capital_expenditure": -400.0})
    assert got["free_cash_flow_margin"].value is None
    assert got["operating_cash_flow_margin"].value is None


def test_no_capex_means_no_free_cash_flow_but_still_an_operating_margin() -> None:
    got = _metrics(_q3_filing(), Q, {"operating_cash_flow": 1_200.0, "revenue": 4_000.0})
    assert got["operating_cash_flow_margin"].value == pytest.approx(0.3)
    assert got["free_cash_flow_margin"].value is None


def test_a_10q_with_no_real_ttm_gets_no_ratio_even_where_a_quarter_column_exists() -> None:
    """An empty TTM is a 10-Q whose year could not be built, not a 10-K: it must not fall back to
    the quarter -- that is the single-quarter reading the veto must never see."""
    q1 = "2026-04-04 (Q2)"
    stmts = _stmts(
        [
            _row("us-gaap_NetCashProvidedByUsedInOperatingActivities", "OCF", **{q1: -3.0}),
            _row("us-gaap_PaymentsToAcquireProductiveAssets", "Capex", **{q1: -39.0}),
        ],
        income=[_row("us-gaap_Revenues", "Revenue", **{q1: 1_267.0})],
    )
    assert _metrics(stmts, q1, {})["free_cash_flow_margin"].value is None
    assert _metrics(stmts, q1, None)["free_cash_flow_margin"].value is not None


def test_a_10k_is_measured_on_its_own_annual_columns() -> None:
    fy = "2025-12-31 (FY)"
    stmts = _stmts(
        [
            _row("us-gaap_NetCashProvidedByUsedInOperatingActivities", "OCF", **{fy: 500.0}),
            _row("us-gaap_PaymentsToAcquirePropertyPlantAndEquipment", "Capex", **{fy: -100.0}),
        ],
        income=[_row("us-gaap_Revenues", "Revenue", **{fy: 2_000.0})],
    )
    got = _metrics(stmts, fy, None)
    assert got["free_cash_flow_margin"].value == pytest.approx(0.2)
    assert "free_cash_flow_ttm" not in got["free_cash_flow_margin"].inputs


def test_one_negative_quarter_does_not_make_the_trailing_year_negative() -> None:
    """WAT's first quarter of 2026: FCF -$42M on $1,267M of revenue, while the year is positive."""
    q1 = "2026-04-04 (Q2)"
    stmts = _stmts(
        [
            _row("us-gaap_NetCashProvidedByUsedInOperatingActivities", "OCF", **{q1: -3.0}),
            _row("us-gaap_PaymentsToAcquireProductiveAssets", "Capex", **{q1: -39.0}),
        ],
        income=[_row("us-gaap_Revenues", "Revenue", **{q1: 1_267.0})],
    )
    quarter = _metrics(stmts, q1, None)["free_cash_flow_margin"].value
    assert quarter is not None and quarter < 0
    ttm = {"operating_cash_flow": 860.0, "capital_expenditure": -170.0, "revenue": 3_000.0}
    assert _metrics(stmts, q1, ttm)["free_cash_flow_margin"].value == pytest.approx(690.0 / 3_000.0)


# -- which flows a group is given --------------------------------------------------------------


def _ctx(form: str, flows: dict[str, TTMFlow]) -> FilingContext:
    return FilingContext(
        ticker="X",
        company_name="X",
        form=form,
        fiscal_period="2025Q3",
        stmts=_stmts([]),
        period_key=Q,
        prior_key=None,
        ttm_flows=flows,
    )


def test_cash_flow_takes_real_ttm_flows_only_and_other_groups_keep_the_x4_fallback() -> None:
    flows = {
        "operating_cash_flow": TTMFlow(1_200.0, "ytd"),
        "capital_expenditure": TTMFlow(-400.0, "x4"),
        "net_income": TTMFlow(500.0, "quarters"),
    }
    ctx = _ctx("10-Q", flows)
    assert _group_ttm("cashflow", ctx) == {"operating_cash_flow": 1_200.0, "net_income": 500.0}
    assert _group_ttm("profitability", ctx) == ctx.ttm
    assert ctx.ttm["capital_expenditure"] == -400.0


def test_a_10k_is_given_no_ttm_for_cash_flow_but_a_10q_with_nothing_real_is_given_an_empty_one() -> (
    None
):
    assert _group_ttm("cashflow", _ctx("10-K", {})) is None
    assert _group_ttm("cashflow", _ctx("10-Q", {"revenue": TTMFlow(4.0, "x4")})) == {}


# -- capex recognition -------------------------------------------------------------------------

FY = "2025-12-31 (FY)"


def _capex(*rows: dict[str, Any]) -> float | None:
    return _stmts(list(rows)).get("capital_expenditure", FY)


def test_the_standard_capex_concept_is_unchanged() -> None:
    assert (
        _capex(_row("us-gaap_PaymentsToAcquirePropertyPlantAndEquipment", "Capex", **{FY: -5.0}))
        == -5.0
    )


def test_an_oil_and_gas_producer_s_development_spend_is_capex() -> None:
    """APA: 'Additions to oil and gas property' is the whole of its capital spending."""
    row = _row(
        "us-gaap_PaymentsToExploreAndDevelopOilAndGasProperties",
        "Additions to oil and gas property",
        **{FY: -2_740.0},
    )
    assert _capex(row) == -2_740.0


def test_development_beats_an_acquisition_of_reserves_whatever_the_row_order() -> None:
    """FANG: $8.9B of acquisitions listed ahead of $2.9B of development is not capital spending."""
    acquire = _row("us-gaap_PaymentsToAcquireOilAndGasProperty", "Acquisitions", **{FY: -8_920.0})
    develop = _row(
        "us-gaap_PaymentsToExploreAndDevelopOilAndGasProperties", "Development", **{FY: -2_867.0}
    )
    assert _capex(acquire, develop) == -2_867.0
    assert _capex(acquire) == -8_920.0  # sole line: the best that is known


def test_an_oil_and_gas_filer_s_other_property_line_joins_its_oil_and_gas_spend() -> None:
    """EOG: $6,115M of oil and gas additions and, on its own line, $479M of other PP&E."""
    og = _row(
        "us-gaap_PaymentsToAcquireOilAndGasPropertyAndEquipment",
        "Additions to oil and gas",
        **{FY: -6_115.0},
    )
    other = _row(
        "us-gaap_PaymentsToAcquireOtherPropertyPlantAndEquipment",
        "Additions to other PP&E",
        **{FY: -479.0},
    )
    assert _capex(og, other) == -6_594.0
    assert _capex(other, og) == -6_594.0  # whatever the row order
    assert _capex(og) == -6_115.0  # the line is optional


def test_other_property_alone_is_not_oil_and_gas_capex() -> None:
    """The addend rides on an oil and gas tier; on its own it is not a recognised capex line."""
    other = _row(
        "us-gaap_PaymentsToAcquireOtherPropertyPlantAndEquipment", "Other PP&E", **{FY: -479.0}
    )
    assert _capex(other) is None


def test_the_net_additions_line_takes_no_addend() -> None:
    net = _row("us-gaap_PaymentsForProceedsFromProductiveAssets", "Additions, net", **{FY: -100.0})
    other = _row(
        "us-gaap_PaymentsToAcquireOtherPropertyPlantAndEquipment", "Other PP&E", **{FY: -50.0}
    )
    assert _capex(net, other) == -100.0


def test_a_standard_capex_row_is_never_displaced_by_a_fallback() -> None:
    plain = _row("us-gaap_PaymentsToAcquirePropertyPlantAndEquipment", "Capex", **{FY: -10.0})
    develop = _row(
        "us-gaap_PaymentsToExploreAndDevelopOilAndGasProperties", "Development", **{FY: -999.0}
    )
    assert _capex(develop, plain) == -10.0


def test_a_custom_concept_is_found_by_its_caption() -> None:
    """PSX: ``psx_CapitalExpendituresAndInvestments`` -- no stable tag, only the line's caption."""
    row = _row(
        "psx_CapitalExpendituresAndInvestments",
        "Capital expenditures and investments",
        **{FY: -2_233.0},
    )
    assert _capex(row) == -2_233.0


def test_non_cash_capex_reconciling_rows_are_not_cash_spent() -> None:
    accrual = _row(
        "x_CapexNotPaid", "Increase (decrease) in capital expenditures not yet paid", **{FY: 40.0}
    )
    included = _row(
        "x_CapexInAp", "Capital expenditures included in accounts payable", **{FY: 25.0}
    )
    assert _capex(accrual, included) is None


@pytest.mark.parametrize(
    "label",
    [
        "(Decrease) increase in accounts payable related to capital expenditures",
        "Change in capital expenditures funded by vendors",
        "Proceeds from capital expenditure reimbursements",
        "Reimbursement of capital expenditures",
        "Sale of capital expenditure assets",
    ],
)
def test_reconciling_and_proceeds_captions_are_not_capex(label: str) -> None:
    assert _capex(_row("x_Other", label, **{FY: 12.0})) is None


def test_a_filer_reporting_capex_in_parts_has_no_capex_rather_than_a_fraction() -> None:
    """NEE: 'Capital expenditures of FPL' and 'Capital expenditures' (the same $9.1B) beside
    'Other capital expenditures' -- any one of them alone understates the total."""
    rows = [
        _row("nee_CapitalExpendituresOfFPL", "Capital expenditures of FPL", **{FY: -9_067.0}),
        _row("nee_CapitalExpendituresOfPublicUtility", "Capital expenditures", **{FY: -9_067.0}),
        _row("nee_OtherCapitalExpenditures", "Other capital expenditures", **{FY: -452.0}),
    ]
    assert _capex(*rows) is None


def test_the_caption_fallback_ignores_other_statements() -> None:
    income_row = _row("x_Capex", "Capital expenditures", **{FY: -7.0})
    stmts = Statements.from_payload(
        {"income_statement": [income_row], "balance_sheet": [], "cash_flow": []}
    )
    assert stmts.get("capital_expenditure", FY) is None


# -- the first quarter's year-to-date ----------------------------------------------------------


def _balance(*instants: str) -> list[dict[str, Any]]:
    return [_row("us-gaap_Assets", "Total assets", **dict.fromkeys(instants, 1.0))]


def _target(stmts: Statements) -> Any:
    return _targets(stmts, _YearTask(1, "X", "X", "10-Q", 2026))[0]


def _waters(balance: list[dict[str, Any]]) -> Statements:
    """A first quarter the gateway tags (Q2) beside a prior-year (Q1): no (YTD) column at all."""
    cols = {"2026-04-04 (Q2)": 1.0, "2025-03-29 (Q1)": 1.0}
    return _stmts(
        [_row("us-gaap_NetCashProvidedByUsedInOperatingActivities", "OCF", **cols)],
        balance=balance,
    )


def test_a_first_quarter_tagged_by_the_calendar_is_recognised_from_the_balance_sheet() -> None:
    stmts = _waters(_balance("2025-12-31", "2026-04-04"))
    assert _is_first_quarter(stmts, _target(stmts))
    assert _ytd_columns(stmts, _target(stmts)) == (
        "2026-04-04 (Q2)",
        "2025-03-29 (Q1)",
        "2025-03-29",
    )


def test_a_later_quarter_with_only_quarter_columns_is_not_taken_for_year_to_date() -> None:
    """A Q2 whose comparative balance sheet is six months back: its quarter is not its YTD."""
    stmts = _stmts(
        [
            _row(
                "us-gaap_NetCashProvidedByUsedInOperatingActivities",
                "OCF",
                **{"2026-06-30 (Q2)": 1.0, "2025-06-30 (Q2)": 1.0},
            )
        ],
        balance=_balance("2025-12-31", "2026-06-30"),
    )
    assert not _is_first_quarter(stmts, _target(stmts))
    assert _ytd_columns(stmts, _target(stmts)) == (None, None, None)


# -- wiring ------------------------------------------------------------------------------------


def _analyst_margin(ctx: FilingContext) -> float | None:
    analyst = FundamentalAnalyst(object(), "test-model")  # type: ignore[arg-type]  # never called
    computed = analyst._compute_all(ctx)
    return next(
        r.value for g, r in computed if g == "cashflow" and r.name == "free_cash_flow_margin"
    )


def test_the_analyst_feeds_a_10q_s_real_ttm_flows_to_the_cash_flow_group() -> None:
    flows = {
        "operating_cash_flow": TTMFlow(1_200.0, "ytd"),
        "capital_expenditure": TTMFlow(-400.0, "ytd"),
        "revenue": TTMFlow(4_000.0, "ytd"),
    }
    ctx = replace(_ctx("10-Q", flows), stmts=_q3_filing())
    assert _analyst_margin(ctx) == pytest.approx(0.2)


def test_a_quarter_times_four_is_not_a_trailing_year_so_it_yields_no_margin() -> None:
    """The veto reads this margin: a crude annualization of one bad quarter must not be able to
    raise it."""
    flows = {
        "operating_cash_flow": TTMFlow(-12.0, "x4"),
        "capital_expenditure": TTMFlow(-156.0, "x4"),
        "revenue": TTMFlow(5_068.0, "x4"),
    }
    ctx = replace(_ctx("10-Q", flows), stmts=_q3_filing())
    assert _analyst_margin(ctx) is None
