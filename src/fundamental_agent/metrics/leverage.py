"""Leverage ratios: how much debt sits in the capital structure."""

from __future__ import annotations

from fundamental_agent.metrics import capital
from fundamental_agent.metrics.base import MetricResult, flag_inputs, present, safe_div
from fundamental_agent.statements import Statements

GROUP = "leverage"


def _net_debt_to_ebitda(
    net_debt: float | None, ebitda: float | None, inputs: dict[str, float]
) -> float | None:
    """MET-09 / N9 (D5): a denominator <= 0 inverts the ordering. EBITDA = 0 -> not defined (empty,
    no flag); EBITDA < 0 -> empty, flagged ``denominator_negative``; net cash (net debt < 0) with a
    positive EBITDA is a valid negative value, flagged ``net_cash`` -- never an error."""
    if net_debt is None or ebitda is None or ebitda == 0.0:
        return None
    if ebitda < 0.0:
        inputs["flag_net_debt_to_ebitda_denominator_negative"] = 1.0
        return None
    if net_debt < 0.0:
        inputs["flag_net_debt_to_ebitda_net_cash"] = 1.0
    return net_debt / ebitda


def _debt_to_equity(
    debt: float | None, equity: float | None, inputs: dict[str, float]
) -> float | None:
    """APP-07 / D6: D/E is not meaningful on negative book equity -- NA, never a veto."""
    if equity is None or equity == 0.0:
        return None
    if equity < 0.0:
        inputs["flag_debt_to_equity_negative_equity"] = 1.0
        return None
    return safe_div(debt, equity)


def _da_flags(
    stmts: Statements, period_key: str, ttm: dict[str, float] | None, flags: set[str]
) -> set[str]:
    """The D&A refusal flags: ``depreciation_only_not_da`` from any column of the filing, and from
    the TTM flow itself (C1, T-151 part 1)."""
    if ttm is None:
        return flags
    for p in stmts.periods:
        is_other_flow = p.tag == "YTD" or (p.is_quarter and p.key != period_key)
        if (
            is_other_flow
            and "depreciation_only_not_da"
            in stmts.resolve("depreciation_amortization", p.key).flags
        ):
            flags.add("depreciation_only_not_da")
            break
    if ttm.get("flag_depreciation_amortization_depreciation_only_not_da"):
        flags.add("depreciation_only_not_da")
    return flags


def compute(
    stmts: Statements,
    period_key: str,
    prior_key: str | None = None,
    ttm: dict[str, float] | None = None,
) -> list[MetricResult]:
    """Leverage ratios. Net debt / EBITDA divides a stock by a flow, so on a 10-Q the EBITDA
    is trailing-twelve-months (*ttm*, T-105) -- a single quarter made it ~4x too high."""
    cap = stmts.capital_at(period_key)
    debt = cap.debt
    operating = stmts.get("operating_income", period_key)
    interest_resolution = stmts.resolve("interest_expense", period_key)
    interest = interest_resolution.value
    da_resolution = stmts.resolve("depreciation_amortization", period_key)
    dep_amort = da_resolution.value
    da_flags = _da_flags(stmts, period_key, ttm, set(da_resolution.flags))

    # MD-03 / D9: net debt = debt - cash - short-term investments (short-term investments when filed
    # apart from cash, D6). MET-00: a missing debt or cash leaves it empty, never 0.
    net_debt = capital.net_debt(cap)

    # MD-01: EBITDA = operating income + D&A (never `Depreciation` alone, ID-02b).
    if ttm is not None:
        op_annual, da_annual = ttm.get("operating_income"), ttm.get("depreciation_amortization")
    else:
        op_annual, da_annual = operating, dep_amort
    ebitda = op_annual + da_annual if op_annual is not None and da_annual is not None else None

    inputs = present(
        total_debt=debt.value,
        equity=cap.equity.value,
        total_assets=cap.total_assets.value,
        cash=cap.cash.value,
        short_term_investments=capital.net_short_term_investments(cap).value,
        operating_income=operating,
        interest_expense=interest,
        depreciation_amortization=dep_amort,
        ebitda_ttm=ebitda if ttm is not None else None,
        ebitda=ebitda,
    )
    inputs.update(flag_inputs("total_debt", debt.flags))
    inputs.update(flag_inputs("interest_expense", interest_resolution.flags))
    inputs.update(flag_inputs("depreciation_amortization", da_flags))
    inputs.update(capital.capital_flags(cap, "current"))
    # MET-09: interest coverage is empty when interest expense is not positive. Zero is not
    # defined; a negative filed value (a net interest income, a wrong sign the resolver did not
    # reject) is flagged, never turned positive with abs() (ID-11).
    if interest is not None and interest < 0.0:
        inputs["flag_interest_expense_negative"] = 1.0
    coverage = safe_div(operating, interest) if interest is not None and interest > 0.0 else None

    return [
        MetricResult(
            "debt_to_equity", _debt_to_equity(debt.value, cap.equity.value, inputs), "x", inputs
        ),
        MetricResult(
            "debt_to_assets", safe_div(debt.value, cap.total_assets.value), "ratio", inputs
        ),
        MetricResult("interest_coverage", coverage, "x", inputs),
        MetricResult(
            "net_debt_to_ebitda", _net_debt_to_ebitda(net_debt.value, ebitda, inputs), "x", inputs
        ),
        MetricResult("ebitda", ebitda, "usd", inputs),
    ]
