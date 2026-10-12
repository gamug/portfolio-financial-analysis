"""Price-based valuation ratios, centred on free cash flow yield.

Unlike the other metric groups this one needs a market price -- the closing quote on
or near the filing's period-end date -- so it is computed separately by the agent
layer only when a :class:`~fundamental_agent.pricing.ClosePrice` is available.

Companion SOP: ``skills/free_cash_flow_yield/SKILL.md``.
"""

from __future__ import annotations

from datetime import date

from fundamental_agent.metrics.base import MetricResult, flag_inputs, present, safe_div
from fundamental_agent.metrics.cashflow import free_cash_flow
from fundamental_agent.metrics.roic import effective_tax_rate
from fundamental_agent.pricing import ClosePrice
from fundamental_agent.statements import Statements
from kg_schema.market_cap import MarketCap

GROUP = "valuation"


def _resolve_market_cap(
    price: ClosePrice | None,
    market_cap: MarketCap | None,
    market_cap_missing: str | None,
) -> tuple[
    float | None,
    float | None,
    float | None,
    float | None,
    float | None,
    float | None,
    bool,
    bool,
    str | None,
]:
    """Resolve (shares, share_price, market_cap_val, share_age_days, split_factor, n_classes,
    incomplete_classes, cover_member_not_common_excluded, missing_reason) strictly under MKT-01 to MKT-04."""
    if market_cap is not None:
        return (
            market_cap.shares,
            market_cap.close,
            market_cap.value,
            float(market_cap.share_age_days),
            market_cap.split_factor,
            float(market_cap.n_classes),
            market_cap.incomplete_classes,
            market_cap.cover_member_not_common_excluded,
            None,
        )
    if market_cap_missing is not None:
        p_close = price.close if price is not None else None
        return (None, p_close, None, None, None, None, False, False, market_cap_missing)

    p_close = price.close if price is not None else None
    return (None, p_close, None, None, None, None, False, False, "not_computed")


def compute(  # noqa: PLR0913 - the filing, its price, and keyword-only audit inputs
    stmts: Statements,
    period_key: str,
    price: ClosePrice | None = None,
    share_scale_factors: dict[str, float] | None = None,
    ttm: dict[str, float] | None = None,
    *,
    cik: str | None = None,
    market_cap: MarketCap | None = None,
    market_cap_missing: str | None = None,
) -> list[MetricResult]:
    """FCF-yield family for *period_key*, valued under MKT-01 to MKT-04 and PER-06."""
    _, fcfe = free_cash_flow(stmts, period_key)
    net_income = stmts.get("net_income", period_key)
    sbc = stmts.get("stock_based_compensation", period_key)
    annual = _AnnualFlows(stmts, period_key, ttm)
    fcfe_annual = annual.fcf()
    sbc_annual = annual.get("stock_based_compensation")

    (
        shares,
        share_price,
        market_cap_val,
        share_age_days,
        split_factor,
        n_classes,
        incomplete_classes,
        cover_member_not_common_excluded,
        missing_reason,
    ) = _resolve_market_cap(price, market_cap, market_cap_missing)

    cash_resolution = stmts.resolve("cash", period_key)
    cash = cash_resolution.value
    debt_resolution = stmts.total_debt(period_key)
    total_debt = debt_resolution.value
    enterprise_value = _enterprise_value(market_cap_val, total_debt, cash)

    interest_resolution = stmts.resolve("interest_expense", period_key)
    interest_rejected = any(f.endswith("_rejected") for f in interest_resolution.flags)
    fcff = _fcf_to_firm(
        stmts, period_key, fcfe, interest_resolution.value, interest_rejected=interest_rejected
    )
    fcff_annual = _fcf_to_firm(
        stmts,
        period_key,
        fcfe_annual,
        annual.get("interest_expense"),
        interest_rejected=interest_rejected,
    )
    sbc_adjusted_fcf = (
        fcfe_annual - sbc_annual if fcfe_annual is not None and sbc_annual is not None else None
    )

    inputs = present(
        free_cash_flow_to_equity=fcfe,
        free_cash_flow_to_firm=fcff,
        stock_based_compensation=sbc,
        net_income=net_income,
        shares=shares,
        share_price=share_price,
        market_capitalization=market_cap_val,
        total_debt=total_debt,
        cash=cash,
        enterprise_value=enterprise_value,
    )
    inputs.update(flag_inputs("total_debt", debt_resolution.flags))
    inputs.update(flag_inputs("cash", cash_resolution.flags))
    capex_resolution = stmts.resolve("capital_expenditure", period_key)
    inputs.update(flag_inputs("capital_expenditure", capex_resolution.flags))
    if price is not None:
        inputs["price_date_offset_days"] = _day_gap(price.date, period_key)
    if share_age_days is not None:
        inputs["share_age_days"] = share_age_days
    if split_factor is not None:
        inputs["split_factor"] = split_factor
    if n_classes is not None:
        inputs["n_classes"] = n_classes
    if missing_reason is not None:
        inputs[f"market_cap_{missing_reason}"] = 1.0
    if incomplete_classes:
        inputs["market_cap_incomplete_classes"] = 1.0
    if cover_member_not_common_excluded:
        inputs["cover_member_not_common_excluded"] = 1.0
    if share_scale_factors:
        inputs["share_scale_suspect"] = 1.0

    if annual.is_ttm:
        inputs.update(
            present(
                free_cash_flow_to_equity_ttm=fcfe_annual,
                free_cash_flow_to_firm_ttm=fcff_annual,
                stock_based_compensation_ttm=sbc_annual,
            )
        )

    return [
        MetricResult("market_capitalization", market_cap_val, "usd", inputs),
        MetricResult("enterprise_value", enterprise_value, "usd", inputs),
        MetricResult(
            "free_cash_flow_yield", safe_div(fcfe_annual, market_cap_val), "ratio", inputs
        ),
        MetricResult(
            "sbc_adjusted_fcf_yield", safe_div(sbc_adjusted_fcf, market_cap_val), "ratio", inputs
        ),
        MetricResult(
            "enterprise_fcf_yield", safe_div(fcff_annual, enterprise_value), "ratio", inputs
        ),
    ]


def _enterprise_value(
    market_cap: float | None, total_debt: float | None, cash: float | None
) -> float | None:
    # MET-00 / D10: a missing debt or cash leaves the enterprise value empty, never read as 0
    if market_cap is None or total_debt is None or cash is None:
        return None
    return market_cap + total_debt - cash


class _AnnualFlows:
    """The flows a yield needs, on an annual basis: a 10-Q's TTM values when *ttm* is provided
    (and then *only* those -- never a raw quarter beside a TTM one, PER-06), else the filing's own
    period values, which on a 10-K are already annual."""

    def __init__(self, stmts: Statements, period_key: str, ttm: dict[str, float] | None) -> None:
        self._stmts, self._key, self._ttm = stmts, period_key, ttm

    @property
    def is_ttm(self) -> bool:
        return self._ttm is not None

    def get(self, item: str) -> float | None:
        if self._ttm is not None:
            return self._ttm.get(item)
        return self._stmts.get(item, self._key)

    def fcf(self) -> float | None:
        if self._ttm is not None:
            ocf, capex = self.get("operating_cash_flow"), self.get("capital_expenditure")
            if ocf is None or capex is None:
                return None
            return ocf - capex
        return free_cash_flow(self._stmts, self._key)[1]


def _fcf_to_firm(
    stmts: Statements,
    period_key: str,
    fcfe: float | None,
    interest: float | None,
    *,
    interest_rejected: bool = False,
) -> float | None:
    """Unlevered FCF proxy: FCFE plus after-tax interest expense (no working-capital delta)."""
    if fcfe is None or interest_rejected:
        return None
    if interest is None:
        return fcfe
    if interest < 0.0:
        # MET-09 (T-148): a negative filed interest expense is left out, not added back and not
        # turned positive with abs() (ID-11): the unlevered flow is empty, as the coverage is
        return None
    rate = effective_tax_rate(
        stmts.get("income_tax", period_key), stmts.get("pretax_income", period_key)
    )
    return fcfe + interest * (1.0 - rate)


def _day_gap(price_date: str, period_key: str) -> float:
    """Whole days between the close used and the requested period-end (>= 0)."""

    def _ord(iso: str) -> int:
        year, month, day = (int(part) for part in iso[:10].split("-"))
        return date(year, month, day).toordinal()

    return float(abs(_ord(period_key) - _ord(price_date)))
