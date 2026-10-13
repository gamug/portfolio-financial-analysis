"""Provenance on every metric (T-151 part 3, D14).

Each stored metric's inputs carry one ``provenance`` block:

* the filing's accession (PER-01's availability is derived from it);
* for each line item the metric reads, its XBRL concept and how it was read (``concept`` / ``map`` /
  ``fallback`` / ..., T-148's :class:`~fundamental_agent.statements.Resolution`);
* the rule IDs applied (the checklist rules and Annex B's ``MD-`` rows);
* the resolution flags;
* and, when the value is empty, a reason from a fixed list of codes.

The declarations live here, one entry per metric name, so the metric modules keep their formulas and
the audit trail is added in one place (:func:`attach`). No schema change: the block goes inside the
existing ``inputs_json``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from fundamental_agent.metrics.base import MetricResult
from fundamental_agent.statements import Resolution, Statements

# The fixed list of empty-reason codes (D14).
MISSING_INPUT = "missing_input"
NOT_APPLICABLE = "not_applicable"
DENOMINATOR_NOT_POSITIVE = "denominator_not_positive"
GROWTH_BASE_NOT_POSITIVE = "growth_base_not_positive"
CAPTURE_FAILURE = "capture_failure"
PRIOR_YEAR_MISSING = "prior_year_missing"

EMPTY_REASONS = (
    MISSING_INPUT,
    NOT_APPLICABLE,
    DENOMINATOR_NOT_POSITIVE,
    GROWTH_BASE_NOT_POSITIVE,
    CAPTURE_FAILURE,
    PRIOR_YEAR_MISSING,
)

# metric name -> (registry items it reads, rule IDs applied). The items are the registry keys of
# ``statements.py``; ``total_debt`` / ``nci`` are resolved by their helpers, named here for the
# concept/how trail.
ITEMS: dict[str, tuple[str, ...]] = {
    "gross_margin": ("revenue", "cogs", "gross_profit"),
    "operating_margin": ("revenue", "operating_income"),
    "net_margin": ("revenue", "net_income"),
    "return_on_assets": ("operating_income", "total_assets", "income_tax", "pretax_income"),
    "return_on_equity": ("net_income", "equity"),
    "return_on_invested_capital": (
        "operating_income",
        "income_tax",
        "pretax_income",
        "equity",
        "long_term_debt",
        "short_term_debt",
        "cash",
        "short_term_investments",
        "minority_interest",
        "redeemable_nci",
    ),
    "return_on_invested_capital_effective_rate": (
        "operating_income",
        "income_tax",
        "pretax_income",
        "equity",
        "long_term_debt",
        "short_term_debt",
        "cash",
        "short_term_investments",
        "minority_interest",
        "redeemable_nci",
    ),
    "effective_tax_rate": ("income_tax", "pretax_income"),
    "nopat": ("operating_income", "income_tax", "pretax_income"),
    "ebitda": ("operating_income", "depreciation_amortization"),
    "debt_to_equity": ("long_term_debt", "short_term_debt", "equity"),
    "debt_to_assets": ("long_term_debt", "short_term_debt", "total_assets"),
    "interest_coverage": ("operating_income", "interest_expense"),
    "net_debt_to_ebitda": (
        "long_term_debt",
        "short_term_debt",
        "cash",
        "short_term_investments",
        "operating_income",
        "depreciation_amortization",
    ),
    "current_ratio": ("current_assets", "current_liabilities"),
    "quick_ratio": ("current_assets", "current_liabilities", "inventory"),
    "cash_ratio": ("cash", "short_term_investments", "current_liabilities"),
    "asset_turnover": ("revenue", "total_assets"),
    "inventory_turnover": ("cogs", "inventory"),
    "receivables_turnover": ("revenue", "receivables"),
    "revenue_growth": ("revenue",),
    "operating_income_growth": ("operating_income",),
    "net_income_growth": ("net_income",),
    "free_cash_flow_growth": ("operating_cash_flow", "capital_expenditure"),
    "operating_cash_flow_margin": ("operating_cash_flow", "revenue"),
    "free_cash_flow_margin": ("operating_cash_flow", "capital_expenditure", "revenue"),
    "free_cash_flow_conversion": ("operating_cash_flow", "capital_expenditure", "net_income"),
    "capex_intensity": ("capital_expenditure", "revenue"),
    "revenue_cagr": ("revenue",),
    "net_income_cagr": ("net_income",),
    "operating_cash_flow_cagr": ("operating_cash_flow",),
    "market_capitalization": ("shares_outstanding", "diluted_shares"),
    "enterprise_value": (
        "long_term_debt",
        "short_term_debt",
        "cash",
        "short_term_investments",
        "minority_interest",
        "redeemable_nci",
    ),
    "free_cash_flow_yield": ("operating_cash_flow", "capital_expenditure"),
    "sbc_adjusted_fcf_yield": (
        "operating_cash_flow",
        "capital_expenditure",
        "stock_based_compensation",
    ),
    "enterprise_fcf_yield": (
        "operating_income",
        "income_tax",
        "pretax_income",
        "capital_expenditure",
        "depreciation_amortization",
        "current_assets",
        "current_liabilities",
        "cash",
        "short_term_investments",
        "short_term_debt",
    ),
}

# metric name -> rule IDs applied (checklist rules and Annex B's MD- rows).
RULE_IDS: dict[str, tuple[str, ...]] = {
    "gross_margin": ("MD-06", "CON-11"),
    "operating_margin": ("MD-07",),
    "net_margin": ("MD-08", "ID-03"),
    "return_on_assets": ("MD-09", "MET-01", "MET-03", "APP-02"),
    "return_on_equity": ("MD-10", "MET-02", "APP-07"),
    "return_on_invested_capital": ("MD-11", "MD-02", "MET-02", "MET-03", "APP-07", "N15"),
    "return_on_invested_capital_effective_rate": ("MD-11", "MET-03", "APP-07", "N15"),
    "effective_tax_rate": ("MD-30", "L-05"),
    "nopat": ("MD-05", "MET-03", "APP-04c"),
    "ebitda": ("MD-01", "ID-02b"),
    "debt_to_equity": ("MD-15", "MET-07", "APP-07"),
    "debt_to_assets": ("MD-16", "MET-07"),
    "interest_coverage": ("MD-17", "MET-09"),
    "net_debt_to_ebitda": ("MD-18", "MD-03", "MD-01", "MET-09", "N9"),
    "current_ratio": ("MD-12", "APP-09"),
    "quick_ratio": ("MD-13", "APP-09"),
    "cash_ratio": ("MD-14", "APP-09"),
    "asset_turnover": ("MD-19",),
    "inventory_turnover": ("MD-20", "APP-01", "APP-03"),
    "receivables_turnover": ("MD-21",),
    "revenue_growth": ("MD-22", "MET-04", "PER-04", "PER-12"),
    "operating_income_growth": ("MD-23", "MET-04", "PER-04", "PER-12"),
    "net_income_growth": ("MD-24", "MET-04", "PER-04", "PER-14"),
    "free_cash_flow_growth": ("MD-25", "MET-04", "PER-04", "PER-12"),
    "operating_cash_flow_margin": ("MD-26", "CON-08"),
    "free_cash_flow_margin": ("MD-27", "MD-04"),
    "free_cash_flow_conversion": ("MD-28", "MD-04", "MET-09"),
    "capex_intensity": ("MD-29", "MET-08"),
    "revenue_cagr": ("MD-31", "PER-04", "MET-04", "PER-12"),
    "net_income_cagr": ("MD-32", "PER-04", "MET-04", "PER-12", "PER-14"),
    "operating_cash_flow_cagr": ("MD-33", "PER-04", "MET-04", "PER-12"),
    "market_capitalization": ("MD-34", "MKT-01", "MKT-02", "MKT-03", "MKT-04"),
    "enterprise_value": ("MD-35", "MET-00", "APP-02"),
    "free_cash_flow_yield": ("MD-36", "MD-04"),
    "sbc_adjusted_fcf_yield": ("MD-37",),
    "enterprise_fcf_yield": ("MD-38", "MD-05", "MD-01", "MET-08", "APP-02"),
}

# The empty-reason codes each flag maps to (D14's fixed list).
_FLAG_REASONS: tuple[tuple[str, str], ...] = (
    ("growth_base_not_positive", GROWTH_BASE_NOT_POSITIVE),
    ("denominator_negative", DENOMINATOR_NOT_POSITIVE),
    ("invested_capital_not_positive", DENOMINATOR_NOT_POSITIVE),
    ("negative_equity", NOT_APPLICABLE),
    ("capture_failure", CAPTURE_FAILURE),
    ("prior_year_bs_missing", PRIOR_YEAR_MISSING),
    ("prior_year_assets_missing", PRIOR_YEAR_MISSING),
    ("prior_year_equity_missing", PRIOR_YEAR_MISSING),
    ("prior_year_invested_capital_missing", PRIOR_YEAR_MISSING),
    ("prior_wc_missing", PRIOR_YEAR_MISSING),
)


def _relevant_flags(result: MetricResult, items: tuple[str, ...]) -> list[str]:
    """The ``flag_*`` keys of the metric's inputs that name the metric or one of its items -- the
    group shares one inputs dict, so its unrelated flags are left out."""
    out: list[str] = []
    for key in result.inputs:
        if not key.startswith("flag_"):
            continue
        rest = key[len("flag_") :]
        if result.name in rest or any(
            rest.startswith(item)
            or f"_{item}_" in rest
            or rest.startswith(f"prior_{item}")
            or rest.startswith(f"current_{item}")
            for item in items
        ):
            out.append(key)
    return sorted(out)


def _empty_reason(stmts: Statements, period_key: str, result: MetricResult) -> str:
    """The reason a metric is empty (D14's fixed codes): a flag naming the metric, else an input's
    own resolution flag, else ``missing_input``."""
    for key in result.inputs:
        if key.startswith("flag_") and result.name in key:
            for flag, reason in _FLAG_REASONS:
                if flag in key:
                    return reason
    for item in ITEMS.get(result.name, ()):
        resolution: Resolution = stmts.resolve(item, period_key)
        for flag, reason in _FLAG_REASONS:
            if any(flag in raised for raised in resolution.flags):
                return reason
    return MISSING_INPUT


def _item_provenance(stmts: Statements, period_key: str, item: str) -> dict[str, Any]:
    resolution: Resolution = stmts.resolve(item, period_key)
    return {
        "concept": resolution.concept,
        "how": resolution.how,
        "flags": list(resolution.flags),
    }


def build(stmts: Statements, period_key: str, result: MetricResult) -> dict[str, Any]:
    """The ``provenance`` block of one metric result (D14)."""
    items = ITEMS.get(result.name, ())
    return {
        "accession": stmts.accession,
        "period": period_key,
        "rule_ids": list(RULE_IDS.get(result.name, ())),
        "items": {item: _item_provenance(stmts, period_key, item) for item in items},
        "flags": _relevant_flags(result, items),
        "empty_reason": None
        if result.value is not None
        else _empty_reason(stmts, period_key, result),
    }


def attach(results: list[tuple[str, MetricResult]], stmts: Statements, period_key: str) -> None:
    """Add the ``provenance`` block to every result's inputs (D14). Called once per filing, after
    the metric groups have run and before the rows are written.

    Each result gets its own copy of the inputs dict: a metric group shares one dict across its
    metrics, so writing the block in place would leave every row of the group carrying the last
    metric's provenance. ``MetricResult`` is frozen, so the list entry is replaced."""
    for index, (group, result) in enumerate(results):
        inputs = dict(result.inputs)
        inputs["provenance"] = build(stmts, period_key, result)
        results[index] = (group, replace(result, inputs=inputs))
