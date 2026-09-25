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


def compute(
    stmts: Statements,
    period_key: str,
    prior_key: str | None = None,
    ttm: dict[str, float] | None = None,
) -> list[MetricResult]:
    """Leverage ratios. Net debt / EBITDA divides a stock by a flow, so on a 10-Q the EBITDA
    is trailing-twelve-months (*ttm*, T-105) -- a single quarter made it ~4x too high."""
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
    if ttm:
        op_ttm, da_ttm = ttm.get("operating_income"), ttm.get("depreciation_amortization")
        ebitda = op_ttm + da_ttm if op_ttm is not None and da_ttm is not None else None

    inputs = present(
        total_debt=debt,
        equity=equity,
        total_assets=assets,
        cash=cash,
        operating_income=operating,
        interest_expense=interest,
        depreciation_amortization=dep_amort,
        ebitda_ttm=ebitda if ttm else None,
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
