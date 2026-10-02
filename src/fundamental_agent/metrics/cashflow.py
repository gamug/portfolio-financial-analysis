"""Cash-flow quality: operating cash, free cash flow and its conversion.

On a 10-Q these are measured over the trailing twelve months, on a 10-K over the fiscal year
(T-133). A 10-Q's cash-flow statement reports only year-to-date columns, so the quarter column
the metrics used to read is empty for every Q2 and Q3 (95% of them had no FCF margin), and where
it is filled it is one quarter -- a seasonal or lumpy outflow that the company-exclusion
NEGATIVE_FCF rule then read as the company's cash generation (WAT: one quarter of -$42M against
a positive trailing year).
"""

from __future__ import annotations

from fundamental_agent.metrics.base import MetricResult, present, safe_div
from fundamental_agent.statements import Statements

GROUP = "cashflow"


def free_cash_flow(stmts: Statements, period_key: str) -> tuple[float | None, float | None]:
    """Return ``(operating_cash_flow, free_cash_flow)`` for *period_key*."""
    ocf = stmts.get("operating_cash_flow", period_key)
    capex = stmts.get("capital_expenditure", period_key)
    return _free_cash_flow(ocf, capex)


def _free_cash_flow(ocf: float | None, capex: float | None) -> tuple[float | None, float | None]:
    if ocf is None:
        return None, None
    if capex is None:
        return ocf, None
    return ocf, ocf - abs(capex)


def compute(
    stmts: Statements,
    period_key: str,
    prior_key: str | None = None,
    ttm: dict[str, float] | None = None,
) -> list[MetricResult]:
    """The ratios for *period_key*. *ttm* is ``None`` for a 10-K, whose columns are already
    annual. A dict (even an empty one) means a 10-Q: every ratio is then taken from those
    trailing-twelve-month flows alone, so a flow the TTM could not be built for leaves its
    ratios empty rather than mixing a 12-month numerator with a 3-month denominator."""
    revenue = stmts.get("revenue", period_key)
    net_income = stmts.get("net_income", period_key)
    ocf, fcf = free_cash_flow(stmts, period_key)
    capex = stmts.get("capital_expenditure", period_key)

    # The raw single-period figures are the audit trail, and what a later filing's own TTM reads
    # back as one of its trailing quarters (`db._recorded_flow`) -- never replaced by TTM values.
    inputs = present(
        revenue=revenue,
        net_income=net_income,
        operating_cash_flow=ocf,
        free_cash_flow=fcf,
        capital_expenditure=capex,
    )
    if ttm is not None:
        revenue, net_income = ttm.get("revenue"), ttm.get("net_income")
        ocf, capex = ttm.get("operating_cash_flow"), ttm.get("capital_expenditure")
        ocf, fcf = _free_cash_flow(ocf, capex)
        inputs.update(
            present(
                revenue_ttm=revenue,
                net_income_ttm=net_income,
                operating_cash_flow_ttm=ocf,
                free_cash_flow_ttm=fcf,
                capital_expenditure_ttm=capex,
            )
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
