"""F4 (docs/model_fixes.md): TTM-annualizing a 10-Q's quarterly flow numerators
(net income, revenue, cogs) before they're divided by an instantaneous
balance-sheet stock (assets, equity, inventory, receivables) -- a 3-month flow
divided by an annual-basis stock otherwise understates ROA/ROE/turnover by
~4x, exactly as verified against the live production database.

``db.ttm_flows`` reads each trailing quarter's raw, single-period flow back
from the *already-recorded* ``fundamental_metrics.inputs_json`` of an earlier
filing (never re-deriving concept resolution) -- so these tests seed that
table directly via :func:`fundamental_agent.db.record_metrics`, the same
entry point the real pipeline uses.

The prior quarters are found by **period-end date**, not by the ``fiscal_period`` label
(T-094): the labels line up on the calendar only for a December year-end.
"""

from __future__ import annotations

from datetime import date

import pytest
from conftest import filed_after
from portfolio_common.db import Database

from fundamental_agent import db
from fundamental_agent.db import FilingKey, FilingMeta
from fundamental_agent.metrics.base import MetricResult
from fundamental_agent.metrics.efficiency import compute as efficiency_compute
from fundamental_agent.metrics.profitability import compute as profitability_compute
from fundamental_agent.statements import Statements
from kg_schema.queries import UniverseMember


def _company(symbol: str) -> UniverseMember:
    return UniverseMember(
        symbol=symbol,
        security=symbol,
        cik="0000000001",
        gics_sector="Consumer",
        gics_sub_industry="Sub",
        hq_location=None,
        date_added=None,
        founded=None,
        valid_from="2020-01-01",
        valid_to=None,
    )


def _seed_quarter(
    conn: Database, asset_id: int, fiscal_period: str, period_end: str, flows: dict[str, float]
) -> None:
    """Record one 10-Q's profitability/efficiency metrics the way
    ``pipeline._analyze_one`` does -- inputs_json is what a later filing's own
    TTM lookup reads back. *flows* holds whichever of net_income/revenue/cogs
    apply. *period_end* is the real quarter-end date: the lookup finds quarters by it."""
    year = int(fiscal_period[:4])
    filing_id = db.upsert_filing(
        conn,
        FilingKey(asset_id, "10-Q", year, fiscal_period),
        FilingMeta(filing_date=filed_after(period_end), period_end=period_end),
    )
    _record_flow_metrics(conn, filing_id, flows)


def _seed_fy(
    conn: Database, asset_id: int, fiscal_period: str, period_end: str, flows: dict[str, float]
) -> None:
    """Record one 10-K's FY totals the same way."""
    filing_id = db.upsert_filing(
        conn,
        FilingKey(asset_id, "10-K", int(fiscal_period[2:]), fiscal_period),
        FilingMeta(filing_date=filed_after(period_end), period_end=period_end),
    )
    _record_flow_metrics(conn, filing_id, flows)


def _record_flow_metrics(conn: Database, filing_id: int, flows: dict[str, float]) -> None:
    inputs = {"net_income": flows["net_income"], "revenue": flows["revenue"]}
    eff_inputs = {"revenue": flows["revenue"], "cogs": flows["cogs"]}
    db.record_metrics(
        conn,
        filing_id,
        [
            ("profitability", MetricResult("return_on_assets", None, "ratio", inputs)),
            ("efficiency", MetricResult("asset_turnover", None, "x", eff_inputs)),
        ],
    )


def _flows(net_income: float, revenue: float = 100.0, cogs: float = 40.0) -> dict[str, float]:
    return {"net_income": net_income, "revenue": revenue, "cogs": cogs}


def _asset(conn: Database, symbol: str) -> int:
    db.sync_universe(conn, [_company(symbol)])
    return int(db.load_universe(conn)[0]["id"])


@pytest.mark.parametrize(
    ("iso", "months", "expected"),
    [
        ("2023-08-31", 3, "2023-05-31"),  # a month-end stays a month-end
        ("2024-05-31", 3, "2024-02-29"),  # ... including into a leap February
        ("2024-02-29", 3, "2023-11-30"),
        ("2023-11-30", 9, "2023-02-28"),
        ("2024-01-15", 3, "2023-10-15"),  # a mid-month date keeps its day
        ("2024-09-28", 3, "2024-06-28"),  # a 52/53-week Saturday: within tolerance of the real end
        ("2024-03-31", 12, "2023-03-31"),
    ],
)
def test_months_before_keeps_month_ends_and_crosses_years(
    iso: str, months: int, expected: str
) -> None:
    assert db._months_before(iso, months).isoformat() == expected


def test_ttm_sums_four_real_quarters_for_a_calendar_year_filer(memory_db: Database) -> None:
    """Q3's trailing four are itself + the same year's Q1/Q2 + the prior fiscal year's derived
    Q4 (FY total minus that year's three 10-Qs), all found by period-end date."""
    aid = _asset(memory_db, "AAA")
    _seed_quarter(memory_db, aid, "2023Q1", "2023-03-31", _flows(10.0, 100.0, 40.0))
    _seed_quarter(memory_db, aid, "2023Q2", "2023-06-30", _flows(12.0, 110.0, 42.0))
    _seed_fy(memory_db, aid, "FY2022", "2022-12-31", _flows(44.0, 420.0, 168.0))
    _seed_quarter(memory_db, aid, "2022Q1", "2022-03-31", _flows(9.0, 95.0, 38.0))
    _seed_quarter(memory_db, aid, "2022Q2", "2022-06-30", _flows(11.0, 105.0, 41.0))
    _seed_quarter(memory_db, aid, "2022Q3", "2022-09-30", _flows(10.0, 100.0, 40.0))
    # derived 2022 Q4: net_income 44-9-11-10 = 14, revenue 420-95-105-100 = 120, cogs 168-38-41-40 = 49

    result = db.ttm_flows(
        memory_db,
        aid,
        period_end="2023-09-30",
        current={"net_income": 13.0, "revenue": 115.0, "cogs": 44.0},
    )

    assert result["net_income"] == 13.0 + 12.0 + 10.0 + 14.0
    assert result["revenue"] == 115.0 + 110.0 + 100.0 + 120.0
    assert result["cogs"] == 44.0 + 42.0 + 40.0 + 49.0


def test_ttm_for_a_february_year_end_never_reads_another_fiscal_years_quarters(
    memory_db: Database,
) -> None:
    """T-094. STZ's fiscal year ends in February: ``FY2024`` (labelled by the year it ends in)
    covers Mar-2023..Feb-2024, and its own quarters are labelled ``2023Q1``-``Q3`` (by the
    calendar year *each* ends in). The old lookup derived Q4 as ``FY2024 - 2024Q1..Q3`` --
    the *next* fiscal year's quarters. Here those are poisoned (a -1,199 impairment quarter):
    a correct TTM must never touch them."""
    aid = _asset(memory_db, "STZ")
    # FY2023 (ends 2023-02-28) and its quarters, for the Q4 that precedes FY2024
    _seed_fy(memory_db, aid, "FY2023", "2023-02-28", _flows(900.0))
    _seed_quarter(memory_db, aid, "2022Q1", "2022-05-31", _flows(180.0))
    _seed_quarter(memory_db, aid, "2022Q2", "2022-08-31", _flows(220.0))
    _seed_quarter(memory_db, aid, "2022Q3", "2022-11-30", _flows(250.0))
    # FY2024 (ends 2024-02-29) and *its* quarters, labelled 2023Q1-Q3
    _seed_fy(memory_db, aid, "FY2024", "2024-02-29", _flows(1000.0))
    _seed_quarter(memory_db, aid, "2023Q1", "2023-05-31", _flows(200.0))
    _seed_quarter(memory_db, aid, "2023Q2", "2023-08-31", _flows(250.0))
    _seed_quarter(memory_db, aid, "2023Q3", "2023-11-30", _flows(300.0))
    # FY2025's quarters, labelled 2024Q1-Q3 -- the ones the buggy Q4 derivation read
    _seed_quarter(memory_db, aid, "2024Q1", "2024-05-31", _flows(400.0))
    _seed_quarter(memory_db, aid, "2024Q2", "2024-08-31", _flows(-1199.0))
    _seed_quarter(memory_db, aid, "2024Q3", "2024-11-30", _flows(615.9))
    q4_fy2023 = 900.0 - 180.0 - 220.0 - 250.0  # 250
    q4_fy2024 = 1000.0 - 200.0 - 250.0 - 300.0  # 250

    # Q3 of FY2024 (2023-11-30): itself + Q2 + Q1 + the *previous* fiscal year's Q4
    ttm_q3 = db.ttm_flows(memory_db, aid, period_end="2023-11-30", current={"net_income": 300.0})
    assert ttm_q3["net_income"] == 300.0 + 250.0 + 200.0 + q4_fy2023

    # Q1 of FY2025 (2024-05-31): itself + Q4 of FY2024 (FY2024 minus 2023Q1..Q3) + Q3 + Q2
    ttm_q1 = db.ttm_flows(memory_db, aid, period_end="2024-05-31", current={"net_income": 400.0})
    assert ttm_q1["net_income"] == 400.0 + q4_fy2024 + 300.0 + 250.0

    # Q2 of FY2025 (2024-08-31): the impairment quarter is *itself* here, never a prior quarter
    ttm_q2 = db.ttm_flows(memory_db, aid, period_end="2024-08-31", current={"net_income": -1199.0})
    assert ttm_q2["net_income"] == -1199.0 + 400.0 + q4_fy2024 + 300.0

    # ... and it is never read as a *prior* quarter of any other filing
    assert -1199.0 not in (ttm_q1["net_income"], ttm_q3["net_income"])


def test_ttm_tolerates_52_53_week_quarter_ends(memory_db: Database) -> None:
    """AAPL-shaped: quarters end on a Saturday, so they drift a day or two from "exactly three
    months earlier". The lookup matches within a tolerance window, never by exact date."""
    aid = _asset(memory_db, "APL")
    _seed_fy(memory_db, aid, "FY2023", "2023-09-30", _flows(1000.0))
    _seed_quarter(memory_db, aid, "2022Q1", "2022-12-31", _flows(240.0))
    _seed_quarter(memory_db, aid, "2023Q2", "2023-04-01", _flows(250.0))
    _seed_quarter(memory_db, aid, "2023Q3", "2023-07-01", _flows(260.0))
    _seed_quarter(memory_db, aid, "2023Q1", "2023-12-30", _flows(300.0))  # FY2024 Q1
    _seed_quarter(memory_db, aid, "2024Q2", "2024-03-30", _flows(310.0))  # FY2024 Q2
    q4_fy2023 = 1000.0 - 240.0 - 250.0 - 260.0  # 250

    # FY2024 Q3 ends Saturday 2024-06-29
    result = db.ttm_flows(memory_db, aid, period_end="2024-06-29", current={"net_income": 320.0})

    assert result["net_income"] == 320.0 + 310.0 + 300.0 + q4_fy2023


def test_ttm_falls_back_to_times_four_when_a_quarter_is_genuinely_missing(
    memory_db: Database,
) -> None:
    aid = _asset(memory_db, "MIS")
    _seed_quarter(memory_db, aid, "2023Q1", "2023-03-31", _flows(10.0))
    # 2023Q2 (2023-06-30) was never ingested
    _seed_fy(memory_db, aid, "FY2022", "2022-12-31", _flows(44.0))
    _seed_quarter(memory_db, aid, "2022Q1", "2022-03-31", _flows(9.0))
    _seed_quarter(memory_db, aid, "2022Q2", "2022-06-30", _flows(11.0))
    _seed_quarter(memory_db, aid, "2022Q3", "2022-09-30", _flows(10.0))

    result = db.ttm_flows(memory_db, aid, period_end="2023-09-30", current={"net_income": 13.0})

    assert result == {"net_income": 52.0}  # 13 x 4: a gap is not silently skipped


def test_ttm_flows_falls_back_to_times_four_with_incomplete_history(memory_db: Database) -> None:
    """A fresh ticker's first-ever 10-Q has no prior quarters at all -- every
    item falls back to `current * 4` rather than raising or silently omitting
    the item."""
    aid = _asset(memory_db, "BBB")

    result = db.ttm_flows(
        memory_db,
        aid,
        period_end="2024-03-31",
        current={"net_income": 5.0, "revenue": 50.0, "cogs": 20.0},
    )

    assert result == {"net_income": 20.0, "revenue": 200.0, "cogs": 80.0}


def test_ttm_flows_falls_back_when_prior_fy_q4_cannot_be_derived(memory_db: Database) -> None:
    """Q1 needs the prior fiscal year's Q4, derived from FY-(Q1+Q2+Q3) -- if the
    prior FY's 10-K hasn't been ingested yet, Q4 can't be derived even though
    the prior year's three quarters are all present, so the fallback applies."""
    aid = _asset(memory_db, "CCC")
    _seed_quarter(memory_db, aid, "2022Q1", "2022-03-31", _flows(9.0, 95.0, 38.0))
    _seed_quarter(memory_db, aid, "2022Q2", "2022-06-30", _flows(11.0, 105.0, 41.0))
    _seed_quarter(memory_db, aid, "2022Q3", "2022-09-30", _flows(10.0, 100.0, 40.0))
    # no 2022 10-K seeded -- Q4 is underivable

    result = db.ttm_flows(
        memory_db, aid, period_end="2023-03-31", current={"net_income": 13.0, "revenue": 115.0}
    )

    assert result == {"net_income": 52.0, "revenue": 460.0}


def test_a_fiscal_q4_needs_all_three_of_its_years_quarters(memory_db: Database) -> None:
    """Q4 is FY minus its three 10-Qs. With one of the three missing it is *not derivable* --
    it must not be computed as if the missing quarter were zero."""
    aid = _asset(memory_db, "Q4M")
    _seed_fy(memory_db, aid, "FY2022", "2022-12-31", _flows(44.0))
    _seed_quarter(memory_db, aid, "2022Q1", "2022-03-31", _flows(9.0))
    _seed_quarter(memory_db, aid, "2022Q3", "2022-09-30", _flows(10.0))  # 2022Q2 is missing
    assert db._quarter_flow_ending(memory_db, aid, date(2022, 12, 31), "net_income") is None
    _seed_quarter(memory_db, aid, "2022Q2", "2022-06-30", _flows(11.0))
    assert db._quarter_flow_ending(memory_db, aid, date(2022, 12, 31), "net_income") == 14.0


def test_a_10q_is_never_mistaken_for_the_q4_of_the_year_it_precedes(memory_db: Database) -> None:
    """A fiscal Q4 has no 10-Q, so the 10-K (matched by *form*) supplies it. A 10-Q ending near
    the same date must win, and a 10-K must not be read as a quarter."""
    aid = _asset(memory_db, "FRM")
    _seed_fy(memory_db, aid, "FY2023", "2023-06-30", _flows(999.0))  # a 10-K ending near the target

    assert db._quarter_flow_ending(memory_db, aid, date(2023, 6, 30), "net_income") is None
    _seed_quarter(memory_db, aid, "2023Q2", "2023-06-30", _flows(12.0))
    assert db._quarter_flow_ending(memory_db, aid, date(2023, 6, 30), "net_income") == 12.0


def _income_row(concept: str, label: str, **periods: float) -> dict:
    return {
        "concept": concept,
        "label": label,
        "standard_concept": None,
        "abstract": False,
        "dimension": False,
        **periods,
    }


def _quarterly_payload(
    *, revenue: float, cogs: float, net_income: float, total_assets: float
) -> dict:
    """A synthetic 10-Q-shaped payload carrying every field ROA/asset_turnover
    need, so the ratio itself (not just its inputs) is exercisable -- the real
    MSFT fixture's balance sheet doesn't carry `total_assets` for this period."""
    key = "2024-06-30 (Q3)"
    return {
        "income_statement": [
            _income_row(
                "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenue",
                **{key: revenue},
            ),
            _income_row("us-gaap_CostOfRevenue", "Cost of revenue", **{key: cogs}),
            _income_row("us-gaap_NetIncomeLoss", "Net income", **{key: net_income}),
        ],
        "balance_sheet": [
            _income_row("us-gaap_Assets", "Total assets", **{"2024-06-30": total_assets}),
        ],
        "cash_flow": [],
    }


def test_profitability_compute_uses_ttm_net_income_for_roa_roe_only() -> None:
    """ROA/ROE pick up the TTM-adjusted net income; net_margin (a flow-over-flow
    ratio, not flow-over-stock) and the recorded `inputs` stay on the filing's
    own raw single-quarter value."""
    key = "2024-06-30 (Q3)"
    stmts = Statements.from_payload(
        _quarterly_payload(revenue=100.0, cogs=40.0, net_income=10.0, total_assets=500.0)
    )

    baseline = {r.name: r for r in profitability_compute(stmts, key)}
    ttm_results = {r.name: r for r in profitability_compute(stmts, key, None, {"net_income": 40.0})}

    assert baseline["return_on_assets"].value == pytest.approx(10.0 / 500.0)
    assert ttm_results["return_on_assets"].value == pytest.approx(40.0 / 500.0)
    assert ttm_results["return_on_assets"].inputs["net_income"] == 10.0  # audit trail unchanged
    assert ttm_results["net_margin"].value == baseline["net_margin"].value  # untouched by ttm


def test_efficiency_compute_uses_ttm_revenue_and_cogs() -> None:
    key = "2024-06-30 (Q3)"
    stmts = Statements.from_payload(
        _quarterly_payload(revenue=100.0, cogs=40.0, net_income=10.0, total_assets=500.0)
    )

    results = {
        r.name: r for r in efficiency_compute(stmts, key, None, {"revenue": 400.0, "cogs": 160.0})
    }

    assert results["asset_turnover"].value == pytest.approx(400.0 / 500.0)
    assert results["asset_turnover"].inputs["revenue"] == 100.0  # audit trail unchanged
