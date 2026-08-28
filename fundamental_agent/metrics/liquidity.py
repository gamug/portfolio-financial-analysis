"""Liquidity ratios: ability to cover current obligations."""

from __future__ import annotations

from fundamental_agent.metrics.base import MetricResult, present, safe_div
from fundamental_agent.statements import Statements

GROUP = "liquidity"


def compute(stmts: Statements, period_key: str, prior_key: str | None = None) -> list[MetricResult]:
    current_assets = stmts.get("current_assets", period_key)
    current_liabilities = stmts.get("current_liabilities", period_key)
    cash = stmts.get("cash", period_key)
    short_term_investments = stmts.get("short_term_investments", period_key)
    inventory = stmts.get("inventory", period_key)

    quick_assets = None
    if current_assets is not None and inventory is not None:
        quick_assets = current_assets - inventory
    cash_like = cash
    if cash is not None and short_term_investments is not None:
        cash_like = cash + short_term_investments

    inputs = present(
        current_assets=current_assets,
        current_liabilities=current_liabilities,
        cash=cash,
        short_term_investments=short_term_investments,
        inventory=inventory,
    )
    return [
        MetricResult(
            "current_ratio",
            safe_div(current_assets, current_liabilities),
            "x",
            inputs,
        ),
        MetricResult("quick_ratio", safe_div(quick_assets, current_liabilities), "x", inputs),
        MetricResult("cash_ratio", safe_div(cash_like, current_liabilities), "x", inputs),
    ]
