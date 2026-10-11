"""Profitability ratios: margins and returns on capital."""

from __future__ import annotations

from fundamental_agent.metrics.base import MetricResult, flag_inputs, present, safe_div
from fundamental_agent.statements import Statements

GROUP = "profitability"


def compute(
    stmts: Statements,
    period_key: str,
    prior_key: str | None = None,
    ttm: dict[str, float] | None = None,
) -> list[MetricResult]:
    revenue = stmts.get("revenue", period_key)
    cogs = stmts.get("cogs", period_key)
    gross = stmts.get("gross_profit", period_key)
    if gross is None and revenue is not None and cogs is not None:
        gross = revenue - cogs
    operating = stmts.get("operating_income", period_key)
    net_resolution = stmts.resolve("net_income", period_key)
    net = net_resolution.value
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
    inputs.update(flag_inputs("net_income", net_resolution.flags))
    # ROA/ROE divide a flow (net income) by an instantaneous balance-sheet stock
    # (assets/equity) -- on a 10-Q that flow is only 3 months, not annual, and
    # must be measured on the same annual basis as the stock first (F4,
    # docs/model_fixes.md). `inputs["net_income"]` above stays the raw,
    # single-period value regardless -- it's what a later filing's own TTM
    # computation reads back as one of its trailing quarters.
    net_ttm = (ttm or {}).get("net_income", net)
    # N2 / MET-05 (T-149): the trailing-year net income the cycle's earnings yield divides by the
    # as-of market cap. A 10-K's is its fiscal year; a 10-Q's is the TTM (``annualized_x4`` in the
    # inputs says it was only one quarter times four, which the cycle then does not use).
    inputs.update(present(net_income_ttm=net_ttm))
    return [
        MetricResult("gross_margin", safe_div(gross, revenue), "ratio", inputs),
        MetricResult("operating_margin", safe_div(operating, revenue), "ratio", inputs),
        MetricResult("net_margin", safe_div(net, revenue), "ratio", inputs),
        MetricResult("return_on_assets", safe_div(net_ttm, assets), "ratio", inputs),
        MetricResult("return_on_equity", safe_div(net_ttm, equity), "ratio", inputs),
    ]
