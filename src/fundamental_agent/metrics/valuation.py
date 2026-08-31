"""Price-based valuation ratios, centred on free cash flow yield.

Unlike the other metric groups this one needs a market price -- the closing quote on
or near the filing's period-end date -- so it is computed separately by the agent
layer only when a :class:`~fundamental_agent.pricing.ClosePrice` is available.

Companion SOP: ``skills/free_cash_flow_yield/SKILL.md``.
"""

from __future__ import annotations

from datetime import date

from fundamental_agent.metrics.base import MetricResult, present, safe_div, sum_present
from fundamental_agent.metrics.cashflow import free_cash_flow
from fundamental_agent.metrics.roic import effective_tax_rate
from fundamental_agent.pricing import ClosePrice
from fundamental_agent.statements import Statements

GROUP = "valuation"


def _share_count(stmts: Statements, period_key: str) -> tuple[float | None, bool]:
    """Shares for market cap: point-in-time if reported, else weighted-average diluted.

    Returns ``(shares, used_diluted_fallback)``.
    """
    shares = stmts.get("shares_outstanding", period_key)
    if shares is not None and shares > 0.0:
        return shares, False
    diluted = stmts.get("diluted_shares", period_key)
    if diluted is not None and diluted > 0.0:
        return diluted, True
    return None, False


def compute(stmts: Statements, period_key: str, price: ClosePrice) -> list[MetricResult]:
    """FCF-yield family for *period_key*, valued at *price* (a period-end close)."""
    _, fcfe = free_cash_flow(stmts, period_key)
    net_income = stmts.get("net_income", period_key)
    sbc = stmts.get("stock_based_compensation", period_key)
    shares, used_diluted = _share_count(stmts, period_key)

    market_cap = shares * price.close if shares is not None else None

    cash = stmts.get("cash", period_key)
    total_debt = sum_present(
        stmts.get("long_term_debt", period_key),
        stmts.get("short_term_debt", period_key),
    )
    enterprise_value = _enterprise_value(market_cap, total_debt, cash)

    fcff = _fcf_to_firm(stmts, period_key, fcfe)
    sbc_adjusted_fcf = fcfe - abs(sbc) if fcfe is not None and sbc is not None else None

    inputs = present(
        free_cash_flow_to_equity=fcfe,
        free_cash_flow_to_firm=fcff,
        stock_based_compensation=sbc,
        net_income=net_income,
        shares=shares,
        share_price=price.close,
        market_capitalization=market_cap,
        total_debt=total_debt,
        cash=cash,
        enterprise_value=enterprise_value,
    )
    inputs["price_date_offset_days"] = _day_gap(price.date, period_key)
    inputs["shares_are_diluted_average"] = float(used_diluted)

    return [
        MetricResult("market_capitalization", market_cap, "usd", inputs),
        MetricResult("enterprise_value", enterprise_value, "usd", inputs),
        MetricResult("free_cash_flow_yield", safe_div(fcfe, market_cap), "ratio", inputs),
        MetricResult(
            "sbc_adjusted_fcf_yield", safe_div(sbc_adjusted_fcf, market_cap), "ratio", inputs
        ),
        MetricResult("enterprise_fcf_yield", safe_div(fcff, enterprise_value), "ratio", inputs),
    ]


def _enterprise_value(
    market_cap: float | None, total_debt: float | None, cash: float | None
) -> float | None:
    if market_cap is None:
        return None
    return market_cap + (total_debt or 0.0) - (cash or 0.0)


def _fcf_to_firm(stmts: Statements, period_key: str, fcfe: float | None) -> float | None:
    """Unlevered FCF proxy: FCFE plus after-tax interest expense (no working-capital delta)."""
    if fcfe is None:
        return None
    interest = stmts.get("interest_expense", period_key)
    if interest is None:
        return fcfe
    rate = effective_tax_rate(
        stmts.get("income_tax", period_key), stmts.get("pretax_income", period_key)
    )
    return fcfe + abs(interest) * (1.0 - rate)


def _day_gap(price_date: str, period_key: str) -> float:
    """Whole days between the close used and the requested period-end (>= 0)."""

    def _ord(iso: str) -> int:
        year, month, day = (int(part) for part in iso[:10].split("-"))
        return date(year, month, day).toordinal()

    return float(abs(_ord(period_key) - _ord(price_date)))
