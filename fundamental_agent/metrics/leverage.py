"""Leverage ratios: how much debt sits in the capital structure."""

from __future__ import annotations

from fundamental_agent.metrics.base import MetricResult, present, safe_div
from fundamental_agent.statements import Statements

GROUP = "leverage"


def _total_debt(stmts: Statements, period_key: str) -> float | None:
    parts = [
        stmts.get("long_term_debt", period_key),
        stmts.get("short_term_debt", period_key),
    ]
    known = [p for p in parts if p is not None]
    return sum(known) if known else None


def compute(stmts: Statements, period_key: str, prior_key: str | None = None) -> list[MetricResult]:
    debt = _total_debt(stmts, period_key)
    equity = stmts.get("equity", period_key)
    assets = stmts.get("total_assets", period_key)
    cash = stmts.get("cash", period_key)
    operating = stmts.get("operating_income", period_key)
    interest = stmts.get("interest_expense", period_key)
    dep_amort = stmts.get("depreciation_amortization", period_key)

    net_debt = debt - cash if debt is not None and cash is not None else None
    ebitda = None
    if operating is not None and dep_amort is not None:
        ebitda = operating + dep_amort

    inputs = present(
        total_debt=debt,
        equity=equity,
        total_assets=assets,
        cash=cash,
        operating_income=operating,
        interest_expense=interest,
        depreciation_amortization=dep_amort,
    )
    return [
        MetricResult("debt_to_equity", safe_div(debt, equity), "x", inputs),
        MetricResult("debt_to_assets", safe_div(debt, assets), "ratio", inputs),
        MetricResult(
            "interest_coverage",
            safe_div(operating, abs(interest) if interest is not None else None),
            "x",
            inputs,
        ),
        MetricResult("net_debt_to_ebitda", safe_div(net_debt, ebitda), "x", inputs),
    ]
