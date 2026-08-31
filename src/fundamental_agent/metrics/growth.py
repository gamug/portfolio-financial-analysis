"""Growth rates: current period versus the same period one year earlier."""

from __future__ import annotations

from fundamental_agent.metrics.base import MetricResult, present, safe_div
from fundamental_agent.metrics.cashflow import free_cash_flow
from fundamental_agent.statements import Statements

GROUP = "growth"


def _yoy(current: float | None, prior: float | None) -> float | None:
    ratio = safe_div(current, abs(prior) if prior is not None else None)
    return None if ratio is None else ratio - 1.0


def compute(stmts: Statements, period_key: str, prior_key: str | None = None) -> list[MetricResult]:
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
    return [
        MetricResult("revenue_growth", _yoy(revenue, revenue_prior), "ratio", inputs),
        MetricResult("operating_income_growth", _yoy(operating, operating_prior), "ratio", inputs),
        MetricResult("net_income_growth", _yoy(net, net_prior), "ratio", inputs),
        MetricResult("free_cash_flow_growth", _yoy(fcf, fcf_prior), "ratio", inputs),
    ]
