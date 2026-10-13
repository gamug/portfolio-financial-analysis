"""Provenance on every metric (T-151 part 3, D14; fix round 1, G2).

Each stored metric's inputs carry one ``provenance`` block:

* the filing's accession (PER-01's availability is derived from it);
* for each line item the metric reads, its XBRL concept, how it was read (``concept`` / ``map`` /
  ``fallback`` / ..., T-148's :class:`~fundamental_agent.statements.Resolution`) and **the basis it
  was read on** (G2): ``period`` (the filing's own period column), ``ttm`` (the trailing twelve
  months of a 10-Q, with the method and the columns it used), ``prior_year`` or ``prior_quarter``
  (the capital at the prior fiscal year end or the prior year's same quarter, with the ``Capital``'s
  source). A metric that reads an item on two bases lists both;
* the rule IDs applied (the checklist rules and Annex B's ``MD-`` rows);
* the resolution flags;
* and, when the value is empty, a reason from a fixed list of codes (G4's precedence).

The declarations live here, one entry per metric name, so the metric modules keep their formulas and
the audit trail is added in one place (:func:`attach`). No schema change: the block goes inside the
existing ``inputs_json``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from fundamental_agent.metrics.base import MetricResult, TTMFlow
from fundamental_agent.statements import Capital, Resolution, Statements

# The fixed list of empty-reason codes (D14; G4's precedence is applied by :func:`_empty_reason`).
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

# The basis of each item a metric reads (G2): "period" (the filing's own period column), "ttm" (the
# trailing twelve months of a 10-Q), "prior_year" (the prior fiscal year end) or "prior_quarter"
# (the prior year's same quarter). "prior" means whichever of the last two the filing has (a 10-Q
# carries a prior-year quarter, a 10-K its own prior-year column).
ITEMS: dict[str, dict[str, str | tuple[str, ...]]] = {
    "gross_margin": {"revenue": "period", "cogs": "period", "gross_profit": "period"},
    "operating_margin": {"revenue": "period", "operating_income": "period"},
    "net_margin": {"revenue": "period", "net_income": "period"},
    "return_on_assets": {
        "operating_income": "ttm",
        "income_tax": "ttm",
        "pretax_income": "ttm",
        "total_assets": "prior_year",
    },
    "return_on_equity": {"net_income": "ttm", "equity": "prior_year"},
    "return_on_invested_capital": {
        "operating_income": "ttm",
        "income_tax": "ttm",
        "pretax_income": "ttm",
        "equity": "prior_year",
        "long_term_debt": "prior_year",
        "short_term_debt": "prior_year",
        "cash": "prior_year",
        "short_term_investments": "prior_year",
        "minority_interest": "prior_year",
        "redeemable_nci": "prior_year",
    },
    "return_on_invested_capital_effective_rate": {
        "operating_income": "ttm",
        "income_tax": "ttm",
        "pretax_income": "ttm",
        "equity": "prior_year",
        "long_term_debt": "prior_year",
        "short_term_debt": "prior_year",
        "cash": "prior_year",
        "short_term_investments": "prior_year",
        "minority_interest": "prior_year",
        "redeemable_nci": "prior_year",
    },
    "effective_tax_rate": {"income_tax": "period", "pretax_income": "period"},
    "nopat": {"operating_income": "period", "income_tax": "period", "pretax_income": "period"},
    "ebitda": {"operating_income": "period", "depreciation_amortization": "period"},
    "debt_to_equity": {"long_term_debt": "period", "short_term_debt": "period", "equity": "period"},
    "debt_to_assets": {
        "long_term_debt": "period",
        "short_term_debt": "period",
        "total_assets": "period",
    },
    "interest_coverage": {"operating_income": "period", "interest_expense": "period"},
    "net_debt_to_ebitda": {
        "long_term_debt": "period",
        "short_term_debt": "period",
        "cash": "period",
        "short_term_investments": "period",
        "operating_income": "ttm",
        "depreciation_amortization": "ttm",
    },
    "current_ratio": {"current_assets": "period", "current_liabilities": "period"},
    "quick_ratio": {
        "current_assets": "period",
        "current_liabilities": "period",
        "inventory": "period",
    },
    "cash_ratio": {
        "cash": "period",
        "short_term_investments": "period",
        "current_liabilities": "period",
    },
    "asset_turnover": {"revenue": "ttm", "total_assets": "period"},
    "inventory_turnover": {"cogs": "ttm", "inventory": "period"},
    "receivables_turnover": {"revenue": "ttm", "receivables": "period"},
    "revenue_growth": {"revenue": "period"},
    "operating_income_growth": {"operating_income": "period"},
    "net_income_growth": {"net_income": "period"},
    "free_cash_flow_growth": {"operating_cash_flow": "period", "capital_expenditure": "period"},
    "operating_cash_flow_margin": {"operating_cash_flow": "ttm", "revenue": "ttm"},
    "free_cash_flow_margin": {
        "operating_cash_flow": "ttm",
        "capital_expenditure": "ttm",
        "revenue": "ttm",
    },
    "free_cash_flow_conversion": {
        "operating_cash_flow": "ttm",
        "capital_expenditure": "ttm",
        "net_income": "ttm",
    },
    "capex_intensity": {"capital_expenditure": "ttm", "revenue": "ttm"},
    "revenue_cagr": {"revenue": "period"},
    "net_income_cagr": {"net_income": "period"},
    "operating_cash_flow_cagr": {"operating_cash_flow": "period"},
    "market_capitalization": {"shares_outstanding": "period", "diluted_shares": "period"},
    "enterprise_value": {
        "long_term_debt": "period",
        "short_term_debt": "period",
        "cash": "period",
        "short_term_investments": "period",
        "minority_interest": "period",
        "redeemable_nci": "period",
    },
    "free_cash_flow_yield": {"operating_cash_flow": "ttm", "capital_expenditure": "ttm"},
    "sbc_adjusted_fcf_yield": {
        "operating_cash_flow": "ttm",
        "capital_expenditure": "ttm",
        "stock_based_compensation": "ttm",
    },
    "enterprise_fcf_yield": {
        "operating_income": "ttm",
        "income_tax": "ttm",
        "pretax_income": "ttm",
        "capital_expenditure": "ttm",
        "depreciation_amortization": "ttm",
        "current_assets": ("period", "prior"),
        "current_liabilities": ("period", "prior"),
        "cash": ("period", "prior"),
        "short_term_investments": ("period", "prior"),
        "short_term_debt": ("period", "prior"),
    },
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

# The registry item -> the ``Capital`` field that resolved it (the prior-year bases).
_CAPITAL_FIELDS: dict[str, str] = {
    "total_assets": "total_assets",
    "equity": "equity",
    "cash": "cash",
    "short_term_investments": "short_term_investments",
    "minority_interest": "minority_interest",
    "redeemable_nci": "redeemable_nci",
    "equity_incl_nci": "equity_incl_nci",
    "current_assets": "current_assets",
    "current_liabilities": "current_liabilities",
    "long_term_debt": "debt",
    "short_term_debt": "short_term_debt",
}

# The flag -> the reason it names, in G4's precedence order.
_FLAG_REASONS: tuple[tuple[str, str], ...] = (
    ("negative_equity", NOT_APPLICABLE),
    ("growth_base_not_positive", GROWTH_BASE_NOT_POSITIVE),
    ("denominator_negative", DENOMINATOR_NOT_POSITIVE),
    ("invested_capital_not_positive", DENOMINATOR_NOT_POSITIVE),
    ("capture_failure", CAPTURE_FAILURE),
    ("refused", CAPTURE_FAILURE),
    ("unclassified_balance_sheet", CAPTURE_FAILURE),
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


def _flag_reason(flags: Any) -> str | None:
    for flag, reason in _FLAG_REASONS:
        if any(flag in raised for raised in flags):
            return reason
    return None


def _bases(entry: str | tuple[str, ...]) -> tuple[str, ...]:
    return (entry,) if isinstance(entry, str) else entry


def _empty_reason(  # noqa: C901, PLR0912 - G4's precedence, one linear pass
    stmts: Statements, period_key: str, result: MetricResult
) -> str:
    """G4's precedence, the first failing term in the formula's order:

    1. ``not_applicable`` (APP-07's negative equity) and the denominators/growth bases, from a flag
       naming the metric;
    2. ``missing_input``: a current-period numerator or denominator input that is not filed;
    3. ``capture_failure``: an input filed but refused or not formable (an unclassified balance
       sheet);
    4. ``prior_year_missing``: the prior column or the prior-year filing itself is absent;
    5. ``missing_input`` as the default."""
    items = ITEMS.get(result.name, {})
    # 1. a flag naming the metric (negative equity, a denominator, a growth base)
    for key in result.inputs:
        if key.startswith("flag_") and result.name in key:
            reason = _flag_reason([key])
            if reason in (NOT_APPLICABLE, DENOMINATOR_NOT_POSITIVE, GROWTH_BASE_NOT_POSITIVE):
                return reason
    # 2. a current-period numerator/denominator input that is not filed
    unclassified = any("unclassified_balance_sheet" in key for key in result.inputs)
    for item, entry in items.items():
        for basis in _bases(entry):
            if _prior_basis(stmts, basis) not in ("period", "ttm"):
                continue
            if unclassified and item in (
                "current_assets",
                "current_liabilities",
                "cash",
                "short_term_debt",
                "short_term_investments",
            ):
                continue
            resolution, _capital = _resolve(stmts, period_key, item, basis)
            if resolution.value is None and _flag_reason(resolution.flags) is None:
                return MISSING_INPUT
    # 3. an input filed but refused, or a prior column that is absent
    for item, entry in items.items():
        for basis in _bases(entry):
            resolution, capital = _resolve(stmts, period_key, item, basis)
            reason = _flag_reason(resolution.flags)
            if reason in (CAPTURE_FAILURE, PRIOR_YEAR_MISSING):
                return reason
            if capital is not None and capital.source is None:
                return PRIOR_YEAR_MISSING
    # 4. a metric-specific capture flag (an unclassified balance sheet)
    for key in result.inputs:
        if key.startswith("flag_") and _flag_reason([key]) == CAPTURE_FAILURE:
            return CAPTURE_FAILURE
    return MISSING_INPUT


def _prior_basis(stmts: Statements, basis: str) -> str:
    """``"prior"`` means the prior-year quarter when the filing has one (a 10-Q), else the prior
    fiscal year end."""
    if basis != "prior":
        return basis
    return "prior_quarter" if stmts.prior_quarter_capital is not None else "prior_year"


def _ttm_column(stmts: Statements, period_key: str) -> str:
    """The column a 10-Q's TTM is built from: its year-to-date column ending on the period end, else
    the period's own column."""
    end = period_key[:10]
    for period in stmts.periods:
        if period.tag == "YTD" and period.date == end:
            return str(period.key)
    return period_key


def _resolve(
    stmts: Statements, period_key: str, item: str, basis: str
) -> tuple[Resolution, Capital | None]:
    """The resolution of *item* on *basis*, and the ``Capital`` it came from when it is a prior one."""
    if basis == "ttm":
        return stmts.resolve(item, _ttm_column(stmts, period_key)), None
    if basis in ("prior_year", "prior_quarter"):
        capital = (
            stmts.prior_quarter_capital_at(period_key)
            if basis == "prior_quarter"
            else stmts.prior_year_capital(period_key)
        )
        field = _CAPITAL_FIELDS.get(item)
        if field is not None:
            return getattr(capital, field), capital
        return stmts.resolve(item, period_key), capital
    return stmts.resolve(item, period_key), None


def _flow_columns(flow: TTMFlow | None) -> dict[str, Any]:
    if flow is None:
        return {}
    return {
        "method": flow.method,
        "ytd": flow.ytd,
        "alt": flow.alt,
        "flow_flags": list(flow.flags),
    }


def _item_provenance(
    stmts: Statements,
    period_key: str,
    item: str,
    basis: str,
    ttm_flows: dict[str, TTMFlow] | None,
) -> dict[str, Any]:
    basis = _prior_basis(stmts, basis)
    resolution, capital = _resolve(stmts, period_key, item, basis)
    entry: dict[str, Any] = {
        "basis": basis,
        "concept": resolution.concept,
        "how": resolution.how,
        "flags": list(resolution.flags),
    }
    if basis == "ttm":
        entry.update(_flow_columns((ttm_flows or {}).get(item)))
    if capital is not None:
        entry["source"] = capital.source
    return entry


def build(
    stmts: Statements,
    period_key: str,
    result: MetricResult,
    ttm_flows: dict[str, TTMFlow] | None = None,
) -> dict[str, Any]:
    """The ``provenance`` block of one metric result (D14; G2's bases)."""
    items = ITEMS.get(result.name, {})
    return {
        "accession": stmts.accession,
        "period": period_key,
        "rule_ids": list(RULE_IDS.get(result.name, ())),
        "items": {
            item: [
                _item_provenance(stmts, period_key, item, basis, ttm_flows)
                for basis in ((bases,) if isinstance(bases, str) else bases)
            ]
            for item, bases in items.items()
        },
        "flags": _relevant_flags(result, tuple(items)),
        "empty_reason": None
        if result.value is not None
        else _empty_reason(stmts, period_key, result),
    }


def attach(
    results: list[tuple[str, MetricResult]],
    stmts: Statements,
    period_key: str,
    ttm_flows: dict[str, TTMFlow] | None = None,
) -> None:
    """Add the ``provenance`` block to every result's inputs (D14). Called once per filing, after
    the metric groups have run and before the rows are written.

    Each result gets its own copy of the inputs dict: a metric group shares one dict across its
    metrics, so writing the block in place would leave every row of the group carrying the last
    metric's provenance. ``MetricResult`` is frozen, so the list entry is replaced."""
    for index, (group, result) in enumerate(results):
        inputs = dict(result.inputs)
        inputs["provenance"] = build(stmts, period_key, result, ttm_flows)
        results[index] = (group, replace(result, inputs=inputs))


__all__ = ["EMPTY_REASONS", "ITEMS", "RULE_IDS", "attach", "build"]
