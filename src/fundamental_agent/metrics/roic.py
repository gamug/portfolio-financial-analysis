"""Return on invested capital: NOPAT over the capital funding operations.

Companion SOP: ``skills/roic/SKILL.md``.
"""

from __future__ import annotations

from fundamental_agent.metrics import capital
from fundamental_agent.metrics.base import MetricResult, flag_inputs, present, safe_div
from fundamental_agent.metrics.tax import (
    STATUTORY_TAX_RATE,
    filing_effective_tax_rate,
    nopat,
    nopat_tax_rate,
)
from fundamental_agent.statements import Statements

GROUP = "roic"

# Backwards-compatible name for the clamped filing rate (valuation's unlevered-FCF proxy imports it).
_DEFAULT_TAX_RATE = STATUTORY_TAX_RATE
_MAX_TAX_RATE = 0.50


def effective_tax_rate(tax: float | None, pretax: float | None) -> float:
    """Cash tax / pre-tax income, clamped to ``[0, _MAX_TAX_RATE]``; statutory when unusable.

    Reused by :mod:`fundamental_agent.metrics.valuation` for the unlevered FCF proxy. **Not** the
    ``effective_tax_rate`` metric any more (T-151 part 2, D3): that is the filing's own rate, empty
    when pre-tax income is missing or <= 0 (:func:`fundamental_agent.metrics.tax.filing_effective_tax_rate`).
    """
    if tax is None or pretax is None or pretax <= 0.0:
        return _DEFAULT_TAX_RATE
    return min(max(tax / pretax, 0.0), _MAX_TAX_RATE)


def compute(
    stmts: Statements,
    period_key: str,
    prior_key: str | None = None,
    ttm: dict[str, float] | None = None,
) -> list[MetricResult]:
    """ROIC divides NOPAT (a flow) by invested capital (a stock) at the end of the prior fiscal year
    (MET-02), so on a 10-Q its NOPAT is trailing-twelve-months (*ttm*, T-105); the ``nopat`` metric
    itself stays the filing's own period value, as reported.

    NOPAT's tax is MET-03's hypothetical rate (:mod:`fundamental_agent.metrics.tax`), never the
    filing's own rate -- except for a REIT (APP-04c). The filing's own rate is reported as
    ``effective_tax_rate`` (empty when pre-tax income <= 0, L-05) and used only for the robustness
    ``return_on_invested_capital_effective_rate`` (D3, review 3.5). ROIC is used only when invested
    capital > 0; else empty, flagged ``invested_capital_not_positive`` (N15 / APP-07)."""
    operating = stmts.get("operating_income", period_key)
    tax_resolution = stmts.resolve("income_tax", period_key)
    tax = tax_resolution.value
    pretax = stmts.get("pretax_income", period_key)

    rate, rate_reason = nopat_tax_rate(stmts, period_key)
    filing_rate = filing_effective_tax_rate(stmts, period_key)
    operating_annual = ttm.get("operating_income") if ttm is not None else operating
    nopat_period = nopat(operating, rate)
    nopat_annual = nopat(operating_annual, rate)
    nopat_effective = nopat(operating_annual, filing_rate) if filing_rate is not None else None

    # MET-02: capital at the end of the prior fiscal year (D1/D2's sourcing, shared).
    cap = stmts.prior_year_capital(period_key)
    income_filed = stmts.nci_income_filed(period_key)
    invested = capital.invested_capital(cap, income_filed=income_filed)

    inputs = present(
        operating_income=operating,
        income_tax=tax,
        pretax_income=pretax,
        effective_tax_rate=filing_rate,
        nopat_tax_rate=rate,
        total_debt=cap.debt.value,
        equity=cap.equity.value,
        cash=cap.cash.value,
        short_term_investments=capital.net_short_term_investments(cap).value,
        nci=capital.nci(cap, income_filed=income_filed).value,
        invested_capital=invested.value,
        nopat_ttm=nopat_annual if ttm else None,
    )
    inputs.update(flag_inputs("income_tax", tax_resolution.flags))
    inputs.update(flag_inputs("total_debt", cap.debt.flags))
    inputs.update(capital.capital_flags(cap, "prior"))
    inputs[f"nopat_tax_rate_{rate_reason}"] = 1.0

    roic = None
    roic_effective = None
    if invested.value is None:
        # D2 step 3: the prior-year invested capital could not be captured.
        inputs["flag_return_on_invested_capital_prior_year_invested_capital_missing"] = 1.0
    elif invested.value <= 0.0:
        # N15 / APP-07: ROIC only when invested capital > 0.
        inputs["flag_return_on_invested_capital_invested_capital_not_positive"] = 1.0
    else:
        roic = safe_div(nopat_annual, invested.value)
        roic_effective = safe_div(nopat_effective, invested.value)

    return [
        MetricResult("effective_tax_rate", filing_rate, "ratio", inputs),
        MetricResult("nopat", nopat_period, "usd", inputs),
        MetricResult("return_on_invested_capital", roic, "ratio", inputs),
        MetricResult("return_on_invested_capital_effective_rate", roic_effective, "ratio", inputs),
    ]
