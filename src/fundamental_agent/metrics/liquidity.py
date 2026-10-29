"""Liquidity ratios: ability to cover current obligations."""

from __future__ import annotations

from fundamental_agent.metrics.base import MetricResult, flag_inputs, present, safe_div
from fundamental_agent.statements import G, Statements
from kg_schema.company_profile import FINANCIAL_TYPES

GROUP = "liquidity"

# D-18: the APP-00 types that never read inventory as 0 -- APP-00's financial firms and the
# multi-sector holdings: for them a missing inventory line is not proof that they carry none.
_NO_INVENTORY_ZERO_TYPES = FINANCIAL_TYPES | {"multi_sector_holding"}


def compute(
    stmts: Statements,
    period_key: str,
    prior_key: str | None = None,
    ttm: dict[str, float] | None = None,  # uniform ComputeFn signature, unused here
) -> list[MetricResult]:
    current_assets_resolution = stmts.resolve("current_assets", period_key)
    current_liabilities_resolution = stmts.resolve("current_liabilities", period_key)
    current_assets = current_assets_resolution.value
    current_liabilities = current_liabilities_resolution.value
    cash_resolution = stmts.resolve("cash", period_key)
    cash = cash_resolution.value
    short_term_investments = stmts.get("short_term_investments", period_key)
    inventory = stmts.get("inventory", period_key)

    quick_assets = None
    quick_inventory_zero = False
    if current_assets is not None and inventory is not None:
        quick_assets = current_assets - inventory
    elif (
        current_assets is not None
        and current_liabilities is not None
        and current_assets_resolution.concept == G + "AssetsCurrent"
        and current_liabilities_resolution.concept == G + "LiabilitiesCurrent"
        and stmts.company_type is not None
        and stmts.company_type not in _NO_INVENTORY_ZERO_TYPES
        and stmts.no_inventory_lines()
    ):
        # D-18: a classified balance sheet with no inventory line at all is a company without
        # inventory (S-X 5-02 item 6): the quick ratio alone reads inventory as 0.
        quick_assets = current_assets
        quick_inventory_zero = True
    cash_like = cash
    # N5 (ID-02a): the combined "cash, cash equivalents and short-term investments" line already
    # contains the short-term investments -- adding them again counted them twice
    cash_includes_investments = (
        cash_resolution.concept == G + "CashCashEquivalentsAndShortTermInvestments"
        and "cash_sti_tag_cash_caption" not in cash_resolution.flags
    )
    if cash is not None and short_term_investments is not None and not cash_includes_investments:
        cash_like = cash + short_term_investments

    inputs = present(
        current_assets=current_assets,
        current_liabilities=current_liabilities,
        cash=cash,
        short_term_investments=short_term_investments,
        inventory=inventory,
    )
    inputs.update(flag_inputs("cash", cash_resolution.flags))
    if cash_includes_investments:
        inputs["flag_cash_includes_short_term_investments"] = 1.0
    quick_inputs = dict(inputs)
    if quick_inventory_zero:
        quick_inputs["flag_quick_ratio_no_inventory_lines"] = 1.0
    return [
        MetricResult(
            "current_ratio",
            safe_div(current_assets, current_liabilities),
            "x",
            inputs,
        ),
        MetricResult("quick_ratio", safe_div(quick_assets, current_liabilities), "x", quick_inputs),
        MetricResult("cash_ratio", safe_div(cash_like, current_liabilities), "x", inputs),
    ]
