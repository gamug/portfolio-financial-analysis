"""Price-based valuation ratios, centred on free cash flow yield.

Unlike the other metric groups this one needs a market price -- the closing quote on
or near the filing's period-end date -- so it is computed separately by the agent
layer only when a :class:`~fundamental_agent.pricing.ClosePrice` is available.

Companion SOP: ``skills/free_cash_flow_yield/SKILL.md``.
"""

from __future__ import annotations

from datetime import date

from fundamental_agent.metrics import capital
from fundamental_agent.metrics.base import MetricResult, flag_inputs, present, safe_div
from fundamental_agent.metrics.cashflow import free_cash_flow
from fundamental_agent.metrics.roic import effective_tax_rate
from fundamental_agent.metrics.tax import (
    REIT_TTM_RATE_MISSING,
    nopat,
    nopat_tax_rate,
    outside_unit_interval,
)
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
    annual = _AnnualFlows(stmts, period_key, ttm)
    fcfe_annual = annual.fcf()
    sbc_annual = annual.get("stock_based_compensation")
    mc = _resolve_market_cap(price, market_cap, market_cap_missing)
    (shares, share_price, market_cap_val, *_rest) = mc

    # MD-35 (D7): EV = market cap + debt + NCI - cash - short-term investments. Empty when market
    # cap, debt, cash or NCI is missing (MET-00). Preferred stock is not added (pending the second
    # author, Annex B).
    cap = stmts.capital_at(period_key)
    income_filed = stmts.nci_income_filed(period_key)
    nci_res = capital.nci(cap, income_filed=income_filed)
    sti_res = capital.net_short_term_investments(cap)
    enterprise_value = _enterprise_value(
        market_cap_val, cap.debt.value, cap.cash.value, nci_res.value, sti_res.value
    )

    interest_resolution = stmts.resolve("interest_expense", period_key)
    interest_rejected = any(f.endswith("_rejected") for f in interest_resolution.flags)
    fcff = _fcf_to_firm(
        stmts, period_key, fcfe, interest_resolution.value, interest_rejected=interest_rejected
    )
    # F7: the unlevered-FCF input on the ratios' basis -- master's TTM proxy on a 10-Q, not the
    # filing's single period labelled TTM.
    fcff_annual = _fcf_to_firm(
        stmts,
        period_key,
        fcfe_annual,
        annual.get("interest_expense"),
        interest_rejected=interest_rejected,
    )

    inputs = present(
        free_cash_flow=fcfe,
        free_cash_flow_to_firm=fcff,
        stock_based_compensation=stmts.get("stock_based_compensation", period_key),
        net_income=stmts.get("net_income", period_key),
        shares=shares,
        share_price=share_price,
        market_capitalization=market_cap_val,
        total_debt=cap.debt.value,
        cash=cap.cash.value,
        short_term_investments=sti_res.value,
        nci=nci_res.value,
        enterprise_value=enterprise_value,
    )
    inputs.update(flag_inputs("total_debt", cap.debt.flags))
    inputs.update(flag_inputs("cash", cap.cash.flags))
    inputs.update(capital.capital_flags(cap, "current"))
    # F4: the flags the NCI / short-term-investment / net-debt combinations raise.
    inputs.update(capital.combination_flags(cap, "current", income_filed=income_filed))
    capex_resolution = stmts.resolve("capital_expenditure", period_key)
    inputs.update(flag_inputs("capital_expenditure", capex_resolution.flags))
    _market_cap_inputs(inputs, mc, price, period_key, share_scale_factors)

    # MD-38 (D8): (NOPAT - (capex - D&A + Δ non-cash working capital)) / EV.
    fcff_md38 = _md38(stmts, period_key, cap, annual, inputs)
    # T-071 part 2b (D1): the numerator itself, so the VALORIZATION value-input rule input can
    # carry it -- the score prices MD-38 / EV on the analysis date, so it needs the numerator
    # independently of this period-end price. An audit input only; nothing else reads it.
    inputs.update(present(md38_numerator=fcff_md38))

    sbc_adjusted_fcf = (
        fcfe_annual - sbc_annual if fcfe_annual is not None and sbc_annual is not None else None
    )

    if annual.is_ttm:
        inputs.update(
            present(
                free_cash_flow_ttm=fcfe_annual,
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
            "enterprise_fcf_yield", safe_div(fcff_md38, enterprise_value), "ratio", inputs
        ),
    ]


_MarketCap = tuple[
    float | None,
    float | None,
    float | None,
    float | None,
    float | None,
    float | None,
    bool,
    bool,
    str | None,
]


def _market_cap_inputs(
    inputs: dict[str, float],
    mc: _MarketCap,
    price: ClosePrice | None,
    period_key: str,
    share_scale_factors: dict[str, float] | None,
) -> None:
    """Record how the market cap was read (MKT-01 to MKT-04): the price offset, the share age, the
    split factor, the class count, and why it is missing or incomplete when it is."""
    (
        _shares,
        _close,
        _value,
        share_age_days,
        split_factor,
        n_classes,
        incomplete_classes,
        cover_member_not_common_excluded,
        missing_reason,
    ) = mc
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


def _md38(
    stmts: Statements,
    period_key: str,
    cap: capital.Capital,
    annual: _AnnualFlows,
    inputs: dict[str, float],
) -> float | None:
    """MD-38 (D8): the unlevered free cash flow numerator of ``enterprise_fcf_yield`` =
    NOPAT - (capex - D&A + Δ non-cash working capital), or ``None`` when a term is missing or the
    balance sheet is not classified (MET-00). The Δ is the prior fiscal year end (a 10-K) or the
    prior year's same quarter (a 10-Q, ``prior_wc_missing`` when it is not stored)."""
    # F1: the rate is decided on the same basis as the flow it taxes (the TTM on a 10-Q).
    operating_annual = annual.get("operating_income")
    rate_missing = bool(
        annual.is_ttm
        and (
            annual.get("flag_income_tax_ttm_not_built")
            or annual.get("flag_pretax_income_ttm_not_built")
        )
    )
    rate, rate_reason = nopat_tax_rate(
        operating_annual,
        reit=stmts.reit,
        tax=annual.get("income_tax"),
        pretax=annual.get("pretax_income"),
        rate_missing=rate_missing,
    )
    nopat_annual = nopat(operating_annual, rate)
    capex_annual = annual.get("capital_expenditure")
    da_annual = annual.get("depreciation_amortization")
    wc_current = capital.working_capital(cap)
    prior_cap = (
        stmts.prior_quarter_capital_at(period_key)
        if annual.is_ttm
        else stmts.prior_year_capital(period_key)
    )
    wc_prior = capital.working_capital(prior_cap)
    delta_wc = (
        wc_current.value - wc_prior.value
        if wc_current.value is not None and wc_prior.value is not None
        else None
    )
    inputs[f"nopat_tax_rate_{rate_reason}"] = 1.0
    if rate_reason == REIT_TTM_RATE_MISSING:
        inputs["flag_nopat_reit_ttm_rate_missing"] = 1.0
    # H7 (APP-04c, director's predicate): the flag fires only when NOPAT is formed -- operating
    # income present on the ratio's basis -- and the rate applied to it is outside [0, 1].
    if operating_annual is not None and outside_unit_interval(rate):
        inputs["flag_nopat_tax_rate_outside_unit_interval"] = 1.0
    inputs.update(
        present(
            nopat=nopat_annual,
            noncash_working_capital=wc_current.value,
            noncash_working_capital_prior=wc_prior.value,
            delta_working_capital=delta_wc,
        )
    )
    inputs.update(capital.capital_flags(prior_cap, "prior"))
    inputs.update(
        capital.combination_flags(
            prior_cap, "prior", income_filed=stmts.nci_income_filed(period_key)
        )
    )
    # F10: distinguish a prior column that is absent (nothing stored) from one that is stored but
    # whose balance sheet is not classified (a REIT, an insurer, a bank). The current column always
    # exists, so its empty working capital is the unclassified case.
    if wc_current.value is None:
        inputs["flag_enterprise_fcf_yield_unclassified_balance_sheet"] = 1.0
    if wc_prior.value is None:
        if prior_cap.source is None:
            inputs["flag_enterprise_fcf_yield_prior_wc_missing"] = 1.0
        else:
            inputs["flag_enterprise_fcf_yield_unclassified_balance_sheet"] = 1.0
    if nopat_annual is None or capex_annual is None or da_annual is None or delta_wc is None:
        return None
    return nopat_annual - (capex_annual - da_annual + delta_wc)


def _enterprise_value(
    market_cap: float | None,
    total_debt: float | None,
    cash: float | None,
    nci_value: float | None,
    sti_value: float | None,
) -> float | None:
    """MD-35 (D7): market cap + debt + NCI - cash - short-term investments. MET-00: a missing
    market cap, debt, cash or NCI (a capture failure, D6) leaves the enterprise value empty, never
    read as 0."""
    if market_cap is None or total_debt is None or cash is None or nci_value is None:
        return None
    return market_cap + total_debt + nci_value - cash - (sti_value or 0.0)


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
