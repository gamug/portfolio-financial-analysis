"""Efficiency ratios: how hard the asset base works."""

from __future__ import annotations

from fundamental_agent.metrics.base import MetricResult, present, safe_div
from fundamental_agent.statements import Statements

GROUP = "efficiency"


def compute(stmts: Statements, period_key: str, prior_key: str | None = None) -> list[MetricResult]:
    revenue = stmts.get("revenue", period_key)
    cogs = stmts.get("cogs", period_key)
    assets = stmts.get("total_assets", period_key)
    inventory = stmts.get("inventory", period_key)
    receivables = stmts.get("receivables", period_key)

    inputs = present(
        revenue=revenue,
        cogs=cogs,
        total_assets=assets,
        inventory=inventory,
        receivables=receivables,
    )
    return [
        MetricResult("asset_turnover", safe_div(revenue, assets), "x", inputs),
        MetricResult("inventory_turnover", safe_div(cogs, inventory), "x", inputs),
        MetricResult("receivables_turnover", safe_div(revenue, receivables), "x", inputs),
    ]
