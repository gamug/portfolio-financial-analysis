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
"""

from __future__ import annotations

import pytest
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
    conn: Database, asset_id: int, fiscal_period: str, flows: dict[str, float]
) -> None:
    """Record one 10-Q's profitability/efficiency metrics the way
    ``pipeline._analyze_one`` does -- inputs_json is what a later filing's own
    TTM lookup reads back. *flows* holds whichever of net_income/revenue/cogs
    apply."""
    year = int(fiscal_period[:4])
    filing_id = db.upsert_filing(
        conn,
        FilingKey(asset_id, "10-Q", year, fiscal_period),
        FilingMeta(period_end=f"{year}-06-30"),
    )
    _record_flow_metrics(conn, filing_id, flows)


def _seed_fy(conn: Database, asset_id: int, fiscal_year: int, flows: dict[str, float]) -> None:
    """Record one 10-K's FY totals the same way."""
    filing_id = db.upsert_filing(
        conn,
        FilingKey(asset_id, "10-K", fiscal_year, f"FY{fiscal_year}"),
        FilingMeta(period_end=f"{fiscal_year}-12-31"),
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


def test_ttm_flows_sums_four_real_quarters_within_the_same_fiscal_year(memory_db: Database) -> None:
    """Q3's trailing four are itself + the same fiscal year's Q1/Q2 + the prior
    fiscal year's derived Q4 -- exercised in isolation first with a synthetic
    prior Q4 already seeded directly (no FY/Q1-3 needed for this one)."""
    db.sync_universe(memory_db, [_company("AAA")])
    asset_id = db.load_universe(memory_db)[0]["id"]
    _seed_quarter(
        memory_db, asset_id, "2023Q1", {"net_income": 10.0, "revenue": 100.0, "cogs": 40.0}
    )
    _seed_quarter(
        memory_db, asset_id, "2023Q2", {"net_income": 12.0, "revenue": 110.0, "cogs": 42.0}
    )
    _seed_fy(memory_db, asset_id, 2022, {"net_income": 44.0, "revenue": 420.0, "cogs": 168.0})
    _seed_quarter(memory_db, asset_id, "2022Q1", {"net_income": 9.0, "revenue": 95.0, "cogs": 38.0})
    _seed_quarter(
        memory_db, asset_id, "2022Q2", {"net_income": 11.0, "revenue": 105.0, "cogs": 41.0}
    )
    _seed_quarter(
        memory_db, asset_id, "2022Q3", {"net_income": 10.0, "revenue": 100.0, "cogs": 40.0}
    )
    # derived 2022 Q4 = 44 - 9 - 11 - 10 = 14.0 (net_income); 420-95-105-100=120.0 (revenue);
    # 168-38-41-40=49.0 (cogs)

    result = db.ttm_flows(
        memory_db,
        asset_id,
        fiscal_year=2023,
        quarter=3,
        current={"net_income": 13.0, "revenue": 115.0, "cogs": 44.0},
    )

    # trailing 4 = 2023Q3(current) + 2023Q2 + 2023Q1 + derived 2022Q4
    assert result["net_income"] == 13.0 + 12.0 + 10.0 + 14.0
    assert result["revenue"] == 115.0 + 110.0 + 100.0 + 120.0
    assert result["cogs"] == 44.0 + 42.0 + 40.0 + 49.0


def test_ttm_flows_falls_back_to_times_four_with_incomplete_history(memory_db: Database) -> None:
    """A fresh ticker's first-ever 10-Q has no prior quarters at all -- every
    item falls back to `current * 4` rather than raising or silently omitting
    the item."""
    db.sync_universe(memory_db, [_company("BBB")])
    asset_id = db.load_universe(memory_db)[0]["id"]

    result = db.ttm_flows(
        memory_db,
        asset_id,
        fiscal_year=2024,
        quarter=1,
        current={"net_income": 5.0, "revenue": 50.0, "cogs": 20.0},
    )

    assert result == {"net_income": 20.0, "revenue": 200.0, "cogs": 80.0}


def test_ttm_flows_falls_back_when_prior_fy_q4_cannot_be_derived(memory_db: Database) -> None:
    """Q1 needs the prior fiscal year's Q4, derived from FY-(Q1+Q2+Q3) -- if the
    prior FY's 10-K hasn't been ingested yet, Q4 can't be derived even though
    the prior year's three quarters are all present, so the fallback applies."""
    db.sync_universe(memory_db, [_company("CCC")])
    asset_id = db.load_universe(memory_db)[0]["id"]
    _seed_quarter(memory_db, asset_id, "2022Q1", {"net_income": 9.0, "revenue": 95.0, "cogs": 38.0})
    _seed_quarter(
        memory_db, asset_id, "2022Q2", {"net_income": 11.0, "revenue": 105.0, "cogs": 41.0}
    )
    _seed_quarter(
        memory_db, asset_id, "2022Q3", {"net_income": 10.0, "revenue": 100.0, "cogs": 40.0}
    )
    # no 2022 10-K seeded -- Q4 is underivable

    result = db.ttm_flows(
        memory_db,
        asset_id,
        fiscal_year=2023,
        quarter=1,
        current={"net_income": 13.0, "revenue": 115.0},
    )

    assert result == {"net_income": 52.0, "revenue": 460.0}


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
