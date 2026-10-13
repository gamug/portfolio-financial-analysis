"""Growth rates: current period versus the same period one year earlier."""

from __future__ import annotations

from datetime import date, timedelta

from fundamental_agent.metrics.base import MetricResult, present
from fundamental_agent.metrics.cashflow import free_cash_flow
from fundamental_agent.statements import Statements

GROUP = "growth"

# PER-12 to PER-14 (D4): the effective dates for public business entities that are SEC filers.
# ASC 842 (leases): fiscal years beginning after 2018-12-15. ASC 326 (CECL): fiscal years beginning
# after 2019-12-15, for banks and lenders only. A growth whose base fiscal year began before the
# effective date and whose later fiscal year began after it straddles the adoption: the value is
# kept and flagged (the prior periods are not restated, B5_FASB_TIG_Accounting_Changes p.9).
ASC842_FIRST_YEAR_BEGINS = date(2018, 12, 16)
CECL_FIRST_YEAR_BEGINS = date(2019, 12, 16)
# T-147's lender set (`scripts/sec_xcheck/period_measures.py`, PER-14): the sub-industries whose
# provisioning base changes at CECL. The Article type is T-071's; the code knows the GICS snapshot
# only (L-03).
LENDER_SUB_INDUSTRIES = frozenset(
    {
        "Diversified Banks",
        "Regional Banks",
        "Consumer Finance",
        "Investment Banking & Brokerage",
        "Asset Management & Custody Banks",
        "Commercial & Residential Mortgage Finance",
        "Specialized Finance",
        "Multi-Sector Holdings",
        "Diversified Financial Services",
        "Mortgage REITs",
    }
)


def fy_start(end_date: str) -> date:
    """The approximate start of the fiscal year that ends on *end_date* (T-147's ``- 364``)."""
    return date.fromisoformat(end_date[:10]) - timedelta(days=364)


def crosses_adoption(stmts: Statements, earlier_key: str, later_key: str, effective: date) -> bool:
    """Whether the two compared periods straddle *effective*: the earlier fiscal year began before
    it and the later one after (PER-12)."""
    earlier = next((p for p in stmts.periods if p.key == earlier_key), None)
    later = next((p for p in stmts.periods if p.key == later_key), None)
    if earlier is None or later is None:
        return False
    return fy_start(earlier.date) < effective < fy_start(later.date)


def _yoy(current: float | None, prior: float | None) -> float | None:
    """(current - prior) / prior, empty when the base is <= 0 (MET-04, D4). Damodaran's
    absolute-value fix is not adopted: a ratio over a negative base has the wrong sign."""
    if current is None or prior is None or prior <= 0.0:
        return None
    return current / prior - 1.0


def compute(
    stmts: Statements,
    period_key: str,
    prior_key: str | None = None,
    ttm: dict[str, float] | None = None,  # uniform ComputeFn signature, unused here
) -> list[MetricResult]:
    if prior_key is None:
        return []

    revenue = stmts.get("revenue", period_key)
    revenue_prior = stmts.get("revenue", prior_key)
    operating = stmts.get("operating_income", period_key)
    operating_prior = stmts.get("operating_income", prior_key)
    net = stmts.get("net_income", period_key)
    net_prior = stmts.get("net_income", prior_key)
    _, fcf = free_cash_flow(stmts, period_key)
    _, fcf_prior = free_cash_flow(stmts, prior_key)

    inputs = present(
        revenue=revenue,
        revenue_prior=revenue_prior,
        operating_income=operating,
        operating_income_prior=operating_prior,
        net_income=net,
        net_income_prior=net_prior,
        free_cash_flow=fcf,
        free_cash_flow_prior=fcf_prior,
    )
    # MET-04 (D4): a base <= 0 leaves the growth empty, flagged growth_base_not_positive.
    base_flags: dict[str, float] = {}
    if revenue_prior is not None and revenue_prior <= 0.0:
        base_flags["flag_revenue_growth_growth_base_not_positive"] = 1.0
    if operating_prior is not None and operating_prior <= 0.0:
        base_flags["flag_operating_income_growth_growth_base_not_positive"] = 1.0
    if net_prior is not None and net_prior <= 0.0:
        base_flags["flag_net_income_growth_growth_base_not_positive"] = 1.0
    if fcf_prior is not None and fcf_prior <= 0.0:
        base_flags["flag_free_cash_flow_growth_growth_base_not_positive"] = 1.0
    inputs.update(base_flags)

    asc842 = crosses_adoption(stmts, prior_key, period_key, ASC842_FIRST_YEAR_BEGINS)
    cecl = stmts.sub_industry in LENDER_SUB_INDUSTRIES and crosses_adoption(
        stmts, prior_key, period_key, CECL_FIRST_YEAR_BEGINS
    )
    if asc842:
        inputs["flag_crosses_asc842"] = 1.0
    if cecl:
        inputs["flag_net_income_growth_crosses_cecl"] = 1.0
    return [
        MetricResult("revenue_growth", _yoy(revenue, revenue_prior), "ratio", inputs),
        MetricResult("operating_income_growth", _yoy(operating, operating_prior), "ratio", inputs),
        MetricResult("net_income_growth", _yoy(net, net_prior), "ratio", inputs),
        MetricResult("free_cash_flow_growth", _yoy(fcf, fcf_prior), "ratio", inputs),
    ]
