"""Multi-year compound annual growth rate over the fiscal-year columns in a 10-K.

Companion SOP: ``skills/cagr/SKILL.md``. A 10-K ``financials`` payload carries ~3
annual columns, so this yields a 2-3 year CAGR with no extra fetch; a 10-Q payload
has no FY columns and yields nothing.
"""

from __future__ import annotations

from fundamental_agent.metrics.base import MetricResult, present
from fundamental_agent.metrics.growth import (
    ASC842_FIRST_YEAR_BEGINS,
    CECL_FIRST_YEAR_BEGINS,
    LENDER_SUB_INDUSTRIES,
    crosses_adoption,
)
from fundamental_agent.statements import Statements

GROUP = "cagr"

_SERIES = (
    ("revenue", "revenue_cagr"),
    ("net_income", "net_income_cagr"),
    ("operating_cash_flow", "operating_cash_flow_cagr"),
)


def _cagr(begin: float | None, end: float | None, years: int) -> float | None:
    # Undefined / uninformative unless BOTH endpoints are positive (SKILL.md
    # "Zero/Negative Boundary"; MET-04, D4). A negative `end` over a positive `begin` also makes
    # the fractional power complex, which float() then rejects.
    if begin is None or end is None or begin <= 0.0 or end <= 0.0 or years < 1:
        return None
    return float((end / begin) ** (1.0 / years) - 1.0)


def compute(
    stmts: Statements,
    period_key: str,
    prior_key: str | None = None,
    ttm: dict[str, float] | None = None,  # uniform ComputeFn signature, unused here
) -> list[MetricResult]:
    fy = stmts.fy_periods()
    if not fy:
        return []
    target = next((p for p in fy if p.key == period_key), fy[-1])
    earlier = [p for p in fy if p.date < target.date]
    if not earlier:
        return []
    begin_period = earlier[0]
    years = target.year - begin_period.year

    # PER-12 to PER-14 (D4): the base fiscal year began before the adoption of ASC 842 (every CAGR)
    # or CECL (a lender's net-income CAGR). The value is kept and flagged.
    inputs: dict[str, float] = {"years": float(years)}
    asc842 = crosses_adoption(stmts, begin_period.key, target.key, ASC842_FIRST_YEAR_BEGINS)
    cecl = stmts.sub_industry in LENDER_SUB_INDUSTRIES and crosses_adoption(
        stmts, begin_period.key, target.key, CECL_FIRST_YEAR_BEGINS
    )
    if asc842:
        inputs["flag_crosses_asc842"] = 1.0

    results: list[MetricResult] = []
    for item, name in _SERIES:
        begin = stmts.get(item, begin_period.key)
        end = stmts.get(item, target.key)
        item_inputs = dict(inputs)
        item_inputs.update(present(begin=begin, end=end))
        # F5 / D4: a CAGR from a base <= 0 is empty and flagged, like the growth group's metrics.
        if begin is not None and begin <= 0.0:
            item_inputs[f"flag_{name}_growth_base_not_positive"] = 1.0
        # F5: CECL changes the provision, which is in net income -- the flag belongs to the
        # net-income CAGR alone, never to the shared inputs of all three series.
        if name == "net_income_cagr" and cecl:
            item_inputs["flag_net_income_cagr_crosses_cecl"] = 1.0
        results.append(MetricResult(name, _cagr(begin, end, years), "ratio", item_inputs))
    return results
