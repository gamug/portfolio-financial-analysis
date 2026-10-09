"""Metric-definition rules (MET-00 to MET-09, APP-07) at level (b), over the replayed metric groups.

Each predicate reads ``Rec.f["metrics"]`` (:func:`sec_xcheck.features.replay_metrics`): the values and inputs the metric
modules compute for the filing's own period. 10-Q TTM substitutions are not replayed (the conditions below depend on
line items only).
"""

from __future__ import annotations

from collections.abc import Callable

from sec_xcheck.findings import Finding
from sec_xcheck.records import Rec, count

CMD = "uv run python scripts/audit_sec_checklist.py measure --only metrics"
DEFAULT_TAX_RATE = 0.21


def metric(r: Rec, name: str) -> float | None:
    m = r.f["metrics"].get(name)
    return None if m is None else m["value"]


def inputs(r: Rec, name: str) -> dict[str, float]:
    m = r.f["metrics"].get(name)
    return {} if m is None else m["inputs"]


def has(r: Rec, name: str) -> bool:
    return metric(r, name) is not None


# ---- MET-03 / L-05 / N10: the effective tax rate when pre-tax income is not positive
def tax_rate_defaulted(r: Rec) -> bool:
    pretax = r.value("pretax_income")
    return (pretax is None or pretax <= 0.0) and metric(r, "effective_tax_rate") == DEFAULT_TAX_RATE


def tax_rate_defaulted_on_loss(r: Rec) -> bool:
    pretax = r.value("pretax_income")
    return (
        pretax is not None and pretax <= 0.0 and metric(r, "effective_tax_rate") == DEFAULT_TAX_RATE
    )


def nopat_taxed_on_loss(r: Rec) -> bool:
    """NOPAT on an operating loss is reduced by a tax (MET-03: with operating income <= 0 the tax is 0)."""
    op, nopat = r.value("operating_income"), metric(r, "nopat")
    return op is not None and op < 0.0 and nopat is not None and nopat > op


def nopat_on_filed_rate(r: Rec) -> bool:
    """NOPAT uses the filing's own effective rate where MET-03 says the 21% federal rate."""
    pretax, tax = r.value("pretax_income"), r.value("income_tax")
    return (
        pretax is not None
        and pretax > 0.0
        and tax is not None
        and metric(r, "effective_tax_rate") != DEFAULT_TAX_RATE
    )


def tax_rate_clamped(r: Rec) -> bool:
    pretax, tax = r.value("pretax_income"), r.value("income_tax")
    if pretax is None or pretax <= 0.0 or tax is None:
        return False
    raw = tax / pretax
    return raw < 0.0 or raw > 0.5  # noqa: PLR2004


# ---- MET-04: growth from a base <= 0
GROWTH_BASES = {
    "revenue_growth": "revenue_prior",
    "operating_income_growth": "operating_income_prior",
    "net_income_growth": "net_income_prior",
    "free_cash_flow_growth": "free_cash_flow_prior",
}


def growth_on_nonpositive_base(name: str) -> Callable[[Rec], bool]:
    base = GROWTH_BASES[name]

    def pred(r: Rec) -> bool:
        b = inputs(r, name).get(base)
        return b is not None and b <= 0.0 and has(r, name)

    return pred


# ---- MET-09 / N9: other ratios with a denominator <= 0
def ebitda(r: Rec) -> float | None:
    i = inputs(r, "net_debt_to_ebitda")
    op, da = i.get("operating_income"), i.get("depreciation_amortization")
    return None if op is None or da is None else op + da


def nd_ebitda_on_nonpositive_ebitda(r: Rec) -> bool:
    e = ebitda(r)
    return e is not None and e <= 0.0 and has(r, "net_debt_to_ebitda")


def net_cash_positive_ebitda(r: Rec) -> bool:
    """The N9 case: net cash with positive EBITDA gives a negative ratio that T-116 treats as unresolved."""
    e, v = ebitda(r), metric(r, "net_debt_to_ebitda")
    return e is not None and e > 0.0 and v is not None and v < 0.0


def fcf_conversion_on_nonpositive_income(r: Rec) -> bool:
    ni = inputs(r, "free_cash_flow_conversion").get("net_income")
    return ni is not None and ni <= 0.0 and has(r, "free_cash_flow_conversion")


# ---- APP-07 / N15: negative equity and invested capital
def roe_on_negative_equity(r: Rec) -> bool:
    eq = r.value("equity")
    return eq is not None and eq < 0.0 and has(r, "return_on_equity")


def de_on_negative_equity(r: Rec) -> bool:
    eq = r.value("equity")
    return eq is not None and eq < 0.0 and has(r, "debt_to_equity")


def roic_on_nonpositive_capital(r: Rec) -> bool:
    i = inputs(r, "return_on_invested_capital")
    eq, debt, cash = i.get("equity"), i.get("total_debt"), i.get("cash")
    if eq is None and debt is None:
        return False
    capital = (eq or 0.0) + (debt or 0.0) - (cash or 0.0)
    return capital <= 0.0 and has(r, "return_on_invested_capital")


def negative_equity(r: Rec) -> bool:
    eq = r.value("equity")
    return eq is not None and eq < 0.0


def m_metrics(recs: list[Rec]) -> list[Finding]:
    out = [
        count(
            "N10.tax_rate_21_on_nonpositive_pretax",
            ["MET-03", "L-05"],
            "effective_tax_rate shows 21% when pre-tax income is missing or <= 0 (L-05: empty)",
            recs,
            tax_rate_defaulted,
            command=CMD,
        ),
        count(
            "N10.tax_rate_21_on_loss",
            ["MET-03", "L-05"],
            "effective_tax_rate shows 21% on a pre-tax loss (pre-tax income <= 0)",
            recs,
            tax_rate_defaulted_on_loss,
            command=CMD,
        ),
        count(
            "MET03.nopat_taxed_on_operating_loss",
            ["MET-03"],
            "NOPAT on an operating loss is reduced by a tax (should equal operating income)",
            recs,
            nopat_taxed_on_loss,
            population=lambda r: (
                r.value("operating_income") is not None and r.value("operating_income") < 0
            ),
            command=CMD,
        ),
        count(
            "MET03.nopat_on_filed_rate",
            ["MET-03"],
            "NOPAT uses the filing's own effective rate, not the 21% federal rate",
            recs,
            nopat_on_filed_rate,
            command=CMD,
        ),
        count(
            "MET03.tax_rate_clamped",
            ["MET-03"],
            "Effective tax rate clamped into [0, 50%] from a raw rate outside it",
            recs,
            tax_rate_clamped,
            command=CMD,
        ),
        count(
            "N9.net_cash_positive_ebitda",
            ["MET-09"],
            "Net cash with positive EBITDA (negative net debt/EBITDA that T-116 treats as unresolved)",
            recs,
            net_cash_positive_ebitda,
            command=CMD,
        ),
        count(
            "MET09.net_debt_ebitda_on_nonpositive_ebitda",
            ["MET-09"],
            "net_debt_to_ebitda stored with EBITDA <= 0, unflagged",
            recs,
            nd_ebitda_on_nonpositive_ebitda,
            command=CMD,
        ),
        count(
            "MET09.fcf_conversion_on_nonpositive_income",
            ["MET-09"],
            "free_cash_flow_conversion kept with net income <= 0",
            recs,
            fcf_conversion_on_nonpositive_income,
            command=CMD,
        ),
        count(
            "N15.roe_on_negative_equity",
            ["APP-07"],
            "return_on_equity computed on negative equity (the gate quarantines it downstream)",
            recs,
            roe_on_negative_equity,
            command=CMD,
        ),
        count(
            "APP07.debt_to_equity_on_negative_equity",
            ["APP-07"],
            "debt_to_equity computed on negative equity",
            recs,
            de_on_negative_equity,
            command=CMD,
        ),
        count(
            "N15.roic_on_nonpositive_capital",
            ["APP-07"],
            "return_on_invested_capital computed with invested capital <= 0",
            recs,
            roic_on_nonpositive_capital,
            command=CMD,
        ),
        count(
            "APP07.negative_equity_filings",
            ["APP-07"],
            "Filings with negative book equity",
            recs,
            negative_equity,
            population=lambda r: r.value("equity") is not None,
            command=CMD,
        ),
    ]
    for name in GROWTH_BASES:
        out.append(
            count(
                f"MET04.{name}_base_nonpositive",
                ["MET-04"],
                f"{name} computed from a base <= 0 (divides by |prior|)",
                recs,
                growth_on_nonpositive_base(name),
                command=CMD,
            )
        )
    return out
