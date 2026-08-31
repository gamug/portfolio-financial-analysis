"""Multi-year compound annual growth rate over the fiscal-year columns in a 10-K.

Companion SOP: ``skills/cagr/SKILL.md``. A 10-K ``financials`` payload carries ~3
annual columns, so this yields a 2-3 year CAGR with no extra fetch; a 10-Q payload
has no FY columns and yields nothing.
"""

from __future__ import annotations

from fundamental_agent.metrics.base import MetricResult, present
from fundamental_agent.statements import Statements

GROUP = "cagr"

_SERIES = (
    ("revenue", "revenue_cagr"),
    ("net_income", "net_income_cagr"),
    ("operating_cash_flow", "operating_cash_flow_cagr"),
)


def _cagr(begin: float | None, end: float | None, years: int) -> float | None:
    # Undefined / uninformative unless BOTH endpoints are positive (SKILL.md
    # "Zero/Negative Boundary"). A negative `end` over a positive `begin` also makes
    # the fractional power complex, which float() then rejects.
    if begin is None or end is None or begin <= 0.0 or end <= 0.0 or years < 1:
        return None
    return float((end / begin) ** (1.0 / years) - 1.0)


def compute(stmts: Statements, period_key: str, prior_key: str | None = None) -> list[MetricResult]:
    fy = stmts.fy_periods()
    if not fy:
        return []
    target = next((p for p in fy if p.key == period_key), fy[-1])
    earlier = [p for p in fy if p.date < target.date]
    if not earlier:
        return []
    begin_period = earlier[0]
    years = target.year - begin_period.year

    results: list[MetricResult] = []
    for item, name in _SERIES:
        begin = stmts.get(item, begin_period.key)
        end = stmts.get(item, target.key)
        results.append(
            MetricResult(
                name,
                _cagr(begin, end, years),
                "ratio",
                present(begin=begin, end=end, years=float(years)),
            )
        )
    return results
