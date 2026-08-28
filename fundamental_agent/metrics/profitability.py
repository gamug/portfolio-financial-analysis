"""Profitability ratios: margins and returns on capital."""

from __future__ import annotations

from fundamental_agent.metrics.base import MetricResult, present, safe_div
from fundamental_agent.statements import Statements

GROUP = "profitability"


def compute(stmts: Statements, period_key: str, prior_key: str | None = None) -> list[MetricResult]:
    revenue = stmts.get("revenue", period_key)
    cogs = stmts.get("cogs", period_key)
    gross = stmts.get("gross_profit", period_key)
    if gross is None and revenue is not None and cogs is not None:
        gross = revenue - cogs
    operating = stmts.get("operating_income", period_key)
    net = stmts.get("net_income", period_key)
    assets = stmts.get("total_assets", period_key)
    equity = stmts.get("equity", period_key)

    inputs = present(
        revenue=revenue,
        gross_profit=gross,
        operating_income=operating,
        net_income=net,
        total_assets=assets,
        equity=equity,
    )
    return [
        MetricResult("gross_margin", safe_div(gross, revenue), "ratio", inputs),
        MetricResult("operating_margin", safe_div(operating, revenue), "ratio", inputs),
        MetricResult("net_margin", safe_div(net, revenue), "ratio", inputs),
        MetricResult("return_on_assets", safe_div(net, assets), "ratio", inputs),
        MetricResult("return_on_equity", safe_div(net, equity), "ratio", inputs),
    ]
