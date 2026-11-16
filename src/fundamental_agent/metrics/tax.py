"""NOPAT's hypothetical tax and the filing's own effective rate (MET-03, APP-04c, MD-30, L-05).

Two different rates (T-151 part 2, D3; part 2 fix 1, F1):

* :func:`nopat_tax_rate` -- what NOPAT is taxed at, decided **on the same basis as the flow it
  taxes** (F1): the 21% federal marginal rate for every company except equity REITs; a REIT's own
  effective rate when its pre-tax income is positive, else 0 (the 21% rate is never applied to a
  REIT, APP-04c). With operating income <= 0 the tax is 0, so NOPAT = operating income (MET-03's
  loss inference). Actual taxes paid are never used. On a 10-Q the caller passes the **TTM** operating
  income, tax and pre-tax income for the ratios, and the filing's own period values for the ``nopat``
  metric.
* :func:`filing_effective_tax_rate` -- the ``effective_tax_rate`` *metric*: the filing's own period
  rate (income tax / pre-tax income, both in the filed sign), empty when pre-tax income is missing or
  <= 0 (L-05). It is reported only; NOPAT does not use it (except for a REIT, where APP-04c does).
"""

from __future__ import annotations

from fundamental_agent.statements import Statements

# The statutory federal corporate rate, in force for taxable years beginning after 2017-12-31
# (E1, 26 U.S.C. §11(b), 21%) -- the whole data window (MET-03).
STATUTORY_TAX_RATE = 0.21

# The rate the internal unlevered-FCF proxy (valuation._fcf_to_firm) clamps its tax shield to; the
# NOPAT *metric* never clamps (it reports the filing's own number, D3).
_MAX_TAX_RATE = 0.50

# F1: the reason a REIT's NOPAT is empty because its own rate could not be built on the basis of the
# flow (a TTM tax or pre-tax income that is refused or x4).
REIT_TTM_RATE_MISSING = "reit_ttm_rate_missing"


def filing_effective_tax_rate_from(tax: float | None, pretax: float | None) -> float | None:
    """MD-30 / L-05: income tax (filed sign, ID-02b's map) / pre-tax income; empty when pre-tax
    income is missing or <= 0. Never the 21% default (that was defect N10)."""
    if tax is None or pretax is None or pretax <= 0.0:
        return None
    return tax / pretax


def filing_effective_tax_rate(stmts: Statements, period_key: str) -> float | None:
    """The filing's own rate for its own period (the ``effective_tax_rate`` metric)."""
    return filing_effective_tax_rate_from(
        stmts.get("income_tax", period_key), stmts.get("pretax_income", period_key)
    )


def clamped_filing_tax_rate(stmts: Statements, period_key: str) -> float:
    """The filing's own rate clamped to ``[0, _MAX_TAX_RATE]``, the statutory rate when unusable --
    the conservative input of the unlevered-FCF proxy, not the reported metric."""
    rate = filing_effective_tax_rate(stmts, period_key)
    if rate is None:
        return STATUTORY_TAX_RATE
    return min(max(rate, 0.0), _MAX_TAX_RATE)


def nopat_tax_rate(
    operating_income: float | None,
    *,
    reit: bool,
    tax: float | None,
    pretax: float | None,
    rate_missing: bool = False,
) -> tuple[float | None, str]:
    """The rate NOPAT is taxed at, decided on the basis of *operating_income* -- the value it taxes
    (MET-03, APP-04c; F1), with the reason it was chosen.

    ``operating_income`` <= 0 -> ``0.0`` (``operating_loss``: the tax is 0, NOPAT = operating income).
    An equity REIT -> its own effective rate from *tax* / *pretax* on the same basis when pre-tax
    income > 0 (``reit_effective``), else 0 (``reit_no_rate``); when *rate_missing* (the REIT's tax or
    pre-tax income could not be built on this basis), ``None`` (``reit_ttm_rate_missing``), so NOPAT
    is empty. Every other company -> the 21% federal marginal rate (``statutory_21``)."""
    if operating_income is not None and operating_income <= 0.0:
        return 0.0, "operating_loss"
    if reit:
        if rate_missing:
            return None, REIT_TTM_RATE_MISSING
        rate = filing_effective_tax_rate_from(tax, pretax)
        if rate is None:
            return 0.0, "reit_no_rate"
        return rate, "reit_effective"
    return STATUTORY_TAX_RATE, "statutory_21"


def nopat(operating_income: float | None, rate: float | None) -> float | None:
    """NOPAT = operating income x (1 - rate) (MD-05); empty when the rate could not be formed (F1)."""
    if operating_income is None or rate is None:
        return None
    return operating_income * (1.0 - rate)


def annual_rate_inputs(
    stmts: Statements, period_key: str, ttm: dict[str, float] | None
) -> tuple[float | None, float | None, bool]:
    """``(tax, pretax, rate_missing)`` on the basis of the flow the *ratios* use (F1): the TTM's own
    tax and pre-tax income on a 10-Q (with the "filed but not built" flag), the filing's own period
    values on a 10-K."""
    if ttm is None:
        return stmts.get("income_tax", period_key), stmts.get("pretax_income", period_key), False
    missing = bool(
        ttm.get("flag_income_tax_ttm_not_built") or ttm.get("flag_pretax_income_ttm_not_built")
    )
    return ttm.get("income_tax"), ttm.get("pretax_income"), missing


def outside_unit_interval(rate: float | None) -> bool:
    """F8 / R-3: the filing's own rate outside ``[0, 1]`` -- a raw value the metric stores as filed,
    flagged rather than clamped (the clamp had no source)."""
    return rate is not None and (rate < 0.0 or rate > 1.0)
