"""Leverage ratios: how much debt sits in the capital structure."""

from __future__ import annotations

from fundamental_agent.metrics.base import MetricResult, flag_inputs, present, safe_div
from fundamental_agent.statements import Statements

GROUP = "leverage"


def compute(
    stmts: Statements,
    period_key: str,
    prior_key: str | None = None,
    ttm: dict[str, float] | None = None,
) -> list[MetricResult]:
    """Leverage ratios. Net debt / EBITDA divides a stock by a flow, so on a 10-Q the EBITDA
    is trailing-twelve-months (*ttm*, T-105) -- a single quarter made it ~4x too high."""
    debt_resolution = stmts.total_debt(period_key)
    debt = debt_resolution.value
    equity = stmts.get("equity", period_key)
    assets = stmts.get("total_assets", period_key)
    cash = stmts.get("cash", period_key)
    operating = stmts.get("operating_income", period_key)
    interest_resolution = stmts.resolve("interest_expense", period_key)
    interest = interest_resolution.value
    da_resolution = stmts.resolve("depreciation_amortization", period_key)
    dep_amort = da_resolution.value

    net_debt = debt - cash if debt is not None and cash is not None else None
    ebitda = None
    if ttm is not None:
        op_ttm, da_ttm = ttm.get("operating_income"), ttm.get("depreciation_amortization")
        ebitda = op_ttm + da_ttm if op_ttm is not None and da_ttm is not None else None
    elif operating is not None and dep_amort is not None:
        ebitda = operating + dep_amort

    inputs = present(
        total_debt=debt,
        equity=equity,
        total_assets=assets,
        cash=cash,
        operating_income=operating,
        interest_expense=interest,
        depreciation_amortization=dep_amort,
        ebitda_ttm=ebitda if ttm is not None else None,
    )

    inputs.update(flag_inputs("total_debt", debt_resolution.flags))
    inputs.update(flag_inputs("interest_expense", interest_resolution.flags))
    inputs.update(flag_inputs("depreciation_amortization", da_resolution.flags))
    # MET-09: interest coverage is empty when interest expense is not positive. Zero is not
    # defined; a negative filed value (a net interest income, a wrong sign the resolver did not
    # reject) is flagged, never turned positive with abs() (ID-11).
    if interest is not None and interest < 0.0:
        inputs["flag_interest_expense_negative"] = 1.0
    coverage = safe_div(operating, interest) if interest is not None and interest > 0.0 else None
    return [
        MetricResult("debt_to_equity", safe_div(debt, equity), "x", inputs),
        MetricResult("debt_to_assets", safe_div(debt, assets), "ratio", inputs),
        MetricResult("interest_coverage", coverage, "x", inputs),
        MetricResult("net_debt_to_ebitda", safe_div(net_debt, ebitda), "x", inputs),
    ]
