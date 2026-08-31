"""Cash-flow quality: operating cash, free cash flow and its conversion."""

from __future__ import annotations

from fundamental_agent.metrics.base import MetricResult, present, safe_div
from fundamental_agent.statements import Statements

GROUP = "cashflow"


def free_cash_flow(stmts: Statements, period_key: str) -> tuple[float | None, float | None]:
    """Return ``(operating_cash_flow, free_cash_flow)`` for *period_key*."""
    ocf = stmts.get("operating_cash_flow", period_key)
    capex = stmts.get("capital_expenditure", period_key)
    if ocf is None:
        return None, None
    if capex is None:
        return ocf, None
    return ocf, ocf - abs(capex)


def compute(stmts: Statements, period_key: str, prior_key: str | None = None) -> list[MetricResult]:
    revenue = stmts.get("revenue", period_key)
    net_income = stmts.get("net_income", period_key)
    ocf, fcf = free_cash_flow(stmts, period_key)
    capex = stmts.get("capital_expenditure", period_key)

    inputs = present(
        revenue=revenue,
        net_income=net_income,
        operating_cash_flow=ocf,
        free_cash_flow=fcf,
        capital_expenditure=capex,
    )
    return [
        MetricResult("operating_cash_flow_margin", safe_div(ocf, revenue), "ratio", inputs),
        MetricResult("free_cash_flow_margin", safe_div(fcf, revenue), "ratio", inputs),
        MetricResult("free_cash_flow_conversion", safe_div(fcf, net_income), "x", inputs),
        MetricResult(
            "capex_intensity",
            safe_div(abs(capex) if capex is not None else None, revenue),
            "ratio",
            inputs,
        ),
    ]
