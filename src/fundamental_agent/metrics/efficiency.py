"""Efficiency ratios: how hard the asset base works."""

from __future__ import annotations

from fundamental_agent.metrics.base import MetricResult, present, safe_div
from fundamental_agent.statements import Statements

GROUP = "efficiency"


def compute(
    stmts: Statements,
    period_key: str,
    prior_key: str | None = None,
    ttm: dict[str, float] | None = None,
) -> list[MetricResult]:
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
    # Every turnover ratio here divides a flow (revenue/cogs) by an instantaneous
    # balance-sheet stock -- same 10-Q quarter-vs-annual mismatch as ROA/ROE
    # (F4, docs/model_fixes.md); `inputs` above keeps the raw, single-period
    # values regardless, for a later filing's own TTM computation to read back.
    revenue_ttm = (ttm or {}).get("revenue", revenue)
    cogs_ttm = (ttm or {}).get("cogs", cogs)
    return [
        MetricResult("asset_turnover", safe_div(revenue_ttm, assets), "x", inputs),
        MetricResult("inventory_turnover", safe_div(cogs_ttm, inventory), "x", inputs),
        MetricResult("receivables_turnover", safe_div(revenue_ttm, receivables), "x", inputs),
    ]
