"""Return on invested capital: NOPAT over the capital funding operations.

Companion SOP: ``skills/roic/SKILL.md``.
"""

from __future__ import annotations

from fundamental_agent.metrics.base import MetricResult, present, safe_div, sum_present
from fundamental_agent.statements import Statements

GROUP = "roic"

_DEFAULT_TAX_RATE = 0.21  # US federal statutory rate, used when the filing gives no usable rate
_MAX_TAX_RATE = 0.50


def effective_tax_rate(tax: float | None, pretax: float | None) -> float:
    """Cash tax / pre-tax income, clamped to ``[0, _MAX_TAX_RATE]``; statutory when unusable.

    Also reused by :mod:`fundamental_agent.metrics.valuation` for the unlevered FCF proxy.
    """
    if tax is None or pretax is None or pretax <= 0.0:
        return _DEFAULT_TAX_RATE
    return min(max(tax / pretax, 0.0), _MAX_TAX_RATE)


def compute(stmts: Statements, period_key: str, prior_key: str | None = None) -> list[MetricResult]:
    operating = stmts.get("operating_income", period_key)
    tax = stmts.get("income_tax", period_key)
    pretax = stmts.get("pretax_income", period_key)
    equity = stmts.get("equity", period_key)
    cash = stmts.get("cash", period_key)
    debt = sum_present(
        stmts.get("long_term_debt", period_key),
        stmts.get("short_term_debt", period_key),
    )

    rate = effective_tax_rate(tax, pretax)
    nopat = operating * (1.0 - rate) if operating is not None else None
    invested = sum_present(debt, equity)
    invested_ex_cash = invested - cash if invested is not None and cash is not None else invested

    inputs = present(
        operating_income=operating,
        income_tax=tax,
        pretax_income=pretax,
        effective_tax_rate=rate,
        total_debt=debt,
        equity=equity,
        cash=cash,
    )
    return [
        MetricResult("effective_tax_rate", rate, "ratio", inputs),
        MetricResult("nopat", nopat, "usd", inputs),
        MetricResult(
            "return_on_invested_capital",
            safe_div(nopat, invested_ex_cash),
            "ratio",
            inputs,
        ),
    ]
