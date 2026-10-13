"""Profitability ratios: margins and returns on capital."""

from __future__ import annotations

from fundamental_agent.metrics import capital
from fundamental_agent.metrics.base import MetricResult, flag_inputs, present, safe_div
from fundamental_agent.metrics.tax import nopat, nopat_tax_rate
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

    # ROA/ROE divide a flow (net income / NOPAT) by an instantaneous balance-sheet stock
    # (assets/equity) -- on a 10-Q that flow is only 3 months, not annual, and must be measured on
    # the same annual basis as the stock first (F4, docs/model_fixes.md). `inputs["net_income"]`
    # above stays the raw, single-period value regardless -- it's what a later filing's own TTM
    # computation reads back as one of its trailing quarters.
    net_ttm = ttm.get("net_income") if ttm is not None else net
    operating_annual = ttm.get("operating_income") if ttm is not None else operating
    # N2 / MET-05 (T-149): the trailing-year net income the cycle's earnings yield divides by the
    # as-of market cap. A 10-K's is its fiscal year; a 10-Q's is the TTM (``annualized_x4`` in the
    # inputs says it was only one quarter times four, which the cycle then does not use).
    inputs = present(
        revenue=revenue,
        gross_profit=gross,
        operating_income=operating,
        net_income=net,
        total_assets=assets,
        equity=equity,
    )
    inputs.update(flag_inputs("net_income", net_resolution.flags))
    inputs.update(present(net_income_ttm=net_ttm))

    # MET-01 / MET-02 (D1/D2, T-151 part 2): capital at the end of the prior fiscal year -- the
    # filing's own comparative, else the stored prior 10-K. Never the ending capital.
    cap = stmts.prior_year_capital(period_key)
    rate, rate_reason = nopat_tax_rate(stmts, period_key)
    nopat_annual = nopat(operating_annual, rate)
    prior_assets = cap.total_assets.value
    prior_equity = cap.equity.value
    inputs.update(
        present(
            nopat_ttm=nopat_annual,
            prior_total_assets=prior_assets,
            prior_equity=prior_equity,
        )
    )
    inputs.update(capital.capital_flags(cap, "prior"))
    inputs[f"nopat_tax_rate_{rate_reason}"] = 1.0
    # D1/D2 step 3: the prior-year capital is empty -- flagged for what is missing.
    if prior_assets is None:
        inputs["flag_return_on_assets_prior_year_assets_missing"] = 1.0
    if prior_equity is None:
        inputs["flag_return_on_equity_prior_year_equity_missing"] = 1.0

    roe = None
    if prior_equity is not None:
        if prior_equity <= 0.0:
            # APP-07 / D6: ROE is not meaningful on negative book equity -- NA, never a veto.
            inputs["flag_return_on_equity_negative_equity"] = 1.0
        else:
            roe = safe_div(net_ttm, prior_equity)

    return [
        MetricResult("gross_margin", safe_div(gross, revenue), "ratio", inputs),
        MetricResult("operating_margin", safe_div(operating, revenue), "ratio", inputs),
        MetricResult("net_margin", safe_div(net, revenue), "ratio", inputs),
        MetricResult("return_on_assets", safe_div(nopat_annual, prior_assets), "ratio", inputs),
        MetricResult("return_on_equity", roe, "ratio", inputs),
    ]
