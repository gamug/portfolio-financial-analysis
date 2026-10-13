"""NOPAT's hypothetical tax and the filing's own effective rate (MET-03, APP-04c, MD-30, L-05).

Two different rates (T-151 part 2, D3):

* :func:`nopat_tax_rate` -- what NOPAT is taxed at. The 21% federal marginal rate for every company
  except equity REITs; a REIT's own effective rate when its pre-tax income is positive, else 0 (the
  21% rate is never applied to a REIT, APP-04c). With operating income <= 0 the tax is 0, so NOPAT =
  operating income (MET-03's loss inference). Actual taxes paid are never used.
* :func:`filing_effective_tax_rate` -- the ``effective_tax_rate`` *metric*: the filing's own rate
  (income tax / pre-tax income, both in the filed sign), empty when pre-tax income is missing or
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


def filing_effective_tax_rate(stmts: Statements, period_key: str) -> float | None:
    """MD-30 / L-05: income tax (filed sign, ID-02b's map) / pre-tax income; empty when pre-tax
    income is missing or <= 0. Never the 21% default (that was defect N10)."""
    tax = stmts.get("income_tax", period_key)
    pretax = stmts.get("pretax_income", period_key)
    if tax is None or pretax is None or pretax <= 0.0:
        return None
    return tax / pretax


def clamped_filing_tax_rate(stmts: Statements, period_key: str) -> float:
    """The filing's own rate clamped to ``[0, _MAX_TAX_RATE]``, the statutory rate when unusable --
    the conservative input of the unlevered-FCF proxy, not the reported metric."""
    rate = filing_effective_tax_rate(stmts, period_key)
    if rate is None:
        return STATUTORY_TAX_RATE
    return min(max(rate, 0.0), _MAX_TAX_RATE)


def nopat_tax_rate(stmts: Statements, period_key: str) -> tuple[float, str]:
    """The rate NOPAT is taxed at (MET-03, APP-04c), with the reason it was chosen.

    Operating income <= 0 -> ``0.0`` (``operating_loss``: the tax is 0, NOPAT = operating income).
    An equity REIT -> its own effective rate when pre-tax income > 0 (``reit_effective``), else 0
    (``reit_no_rate``). Every other company -> the 21% federal marginal rate (``statutory_21``)."""
    operating = stmts.get("operating_income", period_key)
    if operating is not None and operating <= 0.0:
        return 0.0, "operating_loss"
    if stmts.reit:
        rate = filing_effective_tax_rate(stmts, period_key)
        if rate is None:
            return 0.0, "reit_no_rate"
        return rate, "reit_effective"
    return STATUTORY_TAX_RATE, "statutory_21"


def nopat(operating_income: float | None, rate: float) -> float | None:
    """NOPAT = operating income x (1 - rate) (MD-05)."""
    return None if operating_income is None else operating_income * (1.0 - rate)
