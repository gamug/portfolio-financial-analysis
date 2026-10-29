"""Audit round #11 D-18 (reviewer B): inventory = 0 for the quick ratio only.

A company that carries no inventory has no inventory *line* at all, so today's ``quick_assets =
current_assets - inventory`` leaves the quick ratio empty. D-18 reads inventory as 0 for the quick
ratio alone when the balance sheet is classified (``AssetsCurrent`` / ``LiabilitiesCurrent``), the
filing shows no inventory concept or caption anywhere (any statement, any column, dimensional rows
and every filed fact included), and APP-00's type is not a financial one. Every other metric, the
``inventory`` input and the current and cash ratios stay exactly as they are.
"""

from __future__ import annotations

from typing import Any

import pytest

from fundamental_agent.metrics import efficiency, liquidity
from fundamental_agent.statements import Statements

G = "us-gaap_"
FY = "2025-12-31 (FY)"
COL = "2025-12-31"
PRIOR = "2024-12-31"


def _row(
    concept: str,
    label: str,
    column: str,
    val: float,
    sign: float = 1.0,
) -> dict[str, Any]:
    return {
        "concept": concept if "_" in concept else G + concept,
        "label": label,
        "abstract": False,
        "dimension": False,
        "preferred_sign": sign,
        column: val,
    }


def _fact(concept: str, value: float, end: str = COL, start: str | None = None) -> dict[str, Any]:
    return {
        "concept": "us-gaap:" + concept,
        "value": value,
        "unit": "USD",
        "period_start": start,
        "period_end": end,
    }


BASE_FACTS = [_fact("Revenues", 500.0, start="2025-01-01")]


def _stmts(
    income: list[dict[str, Any]] | None = None,
    balance: list[dict[str, Any]] | None = None,
    cash_flow: list[dict[str, Any]] | None = None,
    facts: list[dict[str, Any]] | None = None,
) -> Statements:
    return Statements.from_payload(
        {
            "income_statement": income or [_row("Revenues", "Revenue", FY, 500.0)],
            "balance_sheet": balance or [_row("Assets", "Total assets", COL, 1_000.0)],
            "cash_flow": cash_flow or [],
            "corrections": [],
            "filed_facts": BASE_FACTS if facts is None else facts,
        }
    )


def _classified() -> list[dict[str, Any]]:
    """A classified balance sheet with no inventory line: current assets 500, current liabilities 250."""
    return [
        _row("AssetsCurrent", "Total current assets", COL, 500.0),
        _row("CashAndCashEquivalentsAtCarryingValue", "Cash and cash equivalents", COL, 200.0),
        _row("LiabilitiesCurrent", "Total current liabilities", COL, 250.0),
    ]


def _operating_stmts(
    income: list[dict[str, Any]] | None = None,
    balance: list[dict[str, Any]] | None = None,
    cash_flow: list[dict[str, Any]] | None = None,
    facts: list[dict[str, Any]] | None = None,
) -> Statements:
    stmts = _stmts(
        income=income,
        balance=_classified() if balance is None else balance,
        cash_flow=cash_flow,
        facts=facts,
    )
    stmts.company_type = "operating"
    return stmts


def _ratios(stmts: Statements) -> dict[str, Any]:
    return {r.name: r for r in liquidity.compute(stmts, FY)}


def test_no_inventory_lines_reads_inventory_as_zero_for_the_quick_ratio_only() -> None:
    """D-18's rule: no inventory line, a classified balance sheet, an operating company -> CA / CL."""
    stmts = _operating_stmts()
    ratios = _ratios(stmts)
    quick = ratios["quick_ratio"]
    assert quick.value == pytest.approx(500.0 / 250.0)
    assert quick.inputs["flag_quick_ratio_no_inventory_lines"] == 1.0
    assert "inventory" not in quick.inputs
    assert stmts.get("inventory", FY) is None

    # The current and cash ratios and their inputs are exactly today's: an unknown type is today's
    # behaviour (no quick ratio), and the same current and cash values come out of it.
    control = _ratios(_stmts(balance=_classified()))
    assert ratios["current_ratio"].value == pytest.approx(control["current_ratio"].value)
    assert ratios["cash_ratio"].value == pytest.approx(control["cash_ratio"].value)
    assert control["quick_ratio"].value is None
    assert "flag_quick_ratio_no_inventory_lines" not in ratios["current_ratio"].inputs
    assert "flag_quick_ratio_no_inventory_lines" not in ratios["cash_ratio"].inputs


def test_an_inventory_write_down_on_the_cash_flow_proves_inventory() -> None:
    """``us-gaap_InventoryWriteDown`` in the cash flow is an inventory concept: no quick ratio."""
    stmts = _operating_stmts(
        cash_flow=[_row("InventoryWriteDown", "Inventory write-down", FY, 12.0)]
    )
    assert stmts.no_inventory_lines() is False
    assert _ratios(stmts)["quick_ratio"].value is None


def test_an_inventory_filed_fact_of_another_period_proves_inventory() -> None:
    """A filed ``us-gaap:InventoryNet`` at another period end is still the filing's inventory."""
    stmts = _operating_stmts(facts=[*BASE_FACTS, _fact("InventoryNet", 40.0, end=PRIOR)])
    assert stmts.no_inventory_lines() is False
    assert _ratios(stmts)["quick_ratio"].value is None


def test_a_company_extension_naming_inventory_proves_inventory() -> None:
    """``abc_InventoriesNet`` -- any namespace whose local name contains ``inventor``."""
    stmts = _operating_stmts(
        balance=[*_classified(), _row("abc_InventoriesNet", "Inventories, net", COL, 40.0)]
    )
    assert stmts.no_inventory_lines() is False
    assert _ratios(stmts)["quick_ratio"].value is None


def test_a_row_captioned_inventories_proves_inventory_whatever_its_concept() -> None:
    """A row captioned "Inventories" under an unrelated extension concept is an inventory line."""
    stmts = _operating_stmts(
        balance=[*_classified(), _row("abc_CurrentAssetsOther", "Inventories", COL, 40.0)]
    )
    assert stmts.no_inventory_lines() is False
    assert _ratios(stmts)["quick_ratio"].value is None


@pytest.mark.parametrize(
    ("concept", "caption"),
    [
        ("MaterialsSuppliesAndOther", "Materials and supplies"),  # UNP, CSX, NSC
        ("Supplies", "Parts and supplies"),  # WM, UHS
        ("fdx_SparePartsSuppliesAndFuelLessAllowances", "Spare parts, supplies, and fuel"),  # FDX
    ],
)
def test_a_supplies_line_proves_inventory(concept: str, caption: str) -> None:
    """Audit round #11 (reviewer B): S-X 5-02 item 6's inventories include supplies -- the zero is not proven."""
    stmts = _operating_stmts(balance=[*_classified(), _row(concept, caption, COL, 40.0)])
    assert stmts.no_inventory_lines() is False
    assert _ratios(stmts)["quick_ratio"].value is None


def test_an_inventory_row_the_gateway_dropped_proves_inventory() -> None:
    """VST 2026-Q1: the gateway dropped ``InventoryNet`` (T-042) and kept its original in ``corrections``."""
    stmts = _operating_stmts()
    stmts.corrections.append(
        {
            "statement": "balance_sheet",
            "concept": "us-gaap_InventoryNet",
            "column": COL,
            "original": 1_030.0,
        }
    )
    assert stmts.no_inventory_lines() is False
    assert _ratios(stmts)["quick_ratio"].value is None


def test_a_dimensional_inventory_row_proves_inventory() -> None:
    """A dimensional row is not read by the metrics, but it still proves the filing has inventory."""
    dimensional = _row("InventoryNet", "Inventories", COL, 40.0)
    dimensional["dimension"] = True
    stmts = _operating_stmts(balance=[*_classified(), dimensional])
    assert stmts.get("inventory", FY) is None
    assert stmts.no_inventory_lines() is False
    assert _ratios(stmts)["quick_ratio"].value is None


@pytest.mark.parametrize(
    "company_type", ["article_9", "article_7", "other_financial", "multi_sector_holding"]
)
def test_a_financial_company_type_never_reads_inventory_as_zero(company_type: str) -> None:
    """A financial company's missing inventory line is not proof that it carries no inventory."""
    stmts = _operating_stmts()
    stmts.company_type = company_type
    assert stmts.no_inventory_lines() is True
    assert _ratios(stmts)["quick_ratio"].value is None


def test_an_unknown_company_type_never_reads_inventory_as_zero() -> None:
    """``company_type`` is None without a profile: an unknown type is not proof of a non-financial one."""
    stmts = _stmts(balance=_classified())
    assert stmts.company_type is None
    assert stmts.no_inventory_lines() is True
    assert _ratios(stmts)["quick_ratio"].value is None


def test_an_unclassified_balance_sheet_never_reads_inventory_as_zero() -> None:
    """No ``us-gaap_AssetsCurrent`` row: the balance sheet is unclassified (the inventory condition passes)."""
    stmts = _stmts(
        balance=[
            _row("Assets", "Total assets", COL, 1_000.0),
            _row("CashAndCashEquivalentsAtCarryingValue", "Cash and cash equivalents", COL, 200.0),
            _row("LiabilitiesCurrent", "Total current liabilities", COL, 250.0),
        ]
    )
    stmts.company_type = "operating"
    assert stmts.no_inventory_lines() is True
    ratios = _ratios(stmts)
    assert ratios["current_ratio"].value is None
    assert ratios["quick_ratio"].value is None


def test_inventory_present_keeps_the_quick_ratio_as_before() -> None:
    """With inventory filed the quick ratio is (CA - inventory) / CL and there is no flag."""
    stmts = _operating_stmts(
        balance=[*_classified(), _row("InventoryNet", "Inventories", COL, 100.0)]
    )
    ratios = _ratios(stmts)
    quick = ratios["quick_ratio"]
    assert quick.value == pytest.approx((500.0 - 100.0) / 250.0)
    assert quick.inputs["inventory"] == 100.0
    assert "flag_quick_ratio_no_inventory_lines" not in quick.inputs


def test_an_inventory_line_with_a_zero_value_is_still_an_inventory_line() -> None:
    """A zero is a filed value: the filing has an inventory line, so the rule does not apply."""
    stmts = _operating_stmts(
        balance=[*_classified(), _row("InventoryNet", "Inventories", COL, 0.0)]
    )
    assert stmts.no_inventory_lines() is False
    quick = _ratios(stmts)["quick_ratio"]
    assert quick.value == pytest.approx(500.0 / 250.0)
    assert quick.inputs["inventory"] == 0.0
    assert "flag_quick_ratio_no_inventory_lines" not in quick.inputs


def test_a_header_row_captioned_inventories_without_a_number_is_not_a_line() -> None:
    """A header/abstract row carries no number, so it does not prove an inventory line."""
    header = {
        "concept": G + "InventoryNet",
        "label": "Inventories",
        "abstract": True,
        "dimension": False,
        "preferred_sign": 1.0,
    }
    stmts = _operating_stmts(balance=[*_classified(), header])
    assert stmts.no_inventory_lines() is True
    assert _ratios(stmts)["quick_ratio"].value == pytest.approx(500.0 / 250.0)


def test_inventory_turnover_stays_empty_in_the_zero_inventory_case() -> None:
    """D-18 changes the quick ratio only: ``inventory_turnover`` still has no inventory to divide by."""
    stmts = _operating_stmts(
        income=[
            _row("Revenues", "Revenue", FY, 500.0),
            _row("CostOfRevenue", "Cost of revenue", FY, 300.0),
        ]
    )
    results = {r.name: r for r in efficiency.compute(stmts, FY)}
    assert results["inventory_turnover"].value is None
    assert "inventory" not in results["inventory_turnover"].inputs
    assert _ratios(stmts)["quick_ratio"].value == pytest.approx(500.0 / 250.0)


def test_without_the_filings_filed_facts_inventory_is_not_read_as_zero() -> None:
    """Review minor 6: with no filed facts the "no inventory fact" test proves nothing (L-21's rule for debt)."""
    stmts = _operating_stmts(facts=[])
    assert stmts.no_inventory_lines() is False
    assert _ratios(stmts)["quick_ratio"].value is None


def test_an_other_than_inventory_tax_element_is_not_inventory() -> None:
    """ADBE's 10-Ks file ``DeferredTaxAssetIntraEntityTransferAssetOtherThanInventory`` (ASU 2016-16): not inventory."""
    stmts = _operating_stmts(
        facts=[
            *BASE_FACTS,
            _fact("DeferredTaxAssetIntraEntityTransferAssetOtherThanInventory", 50.0),
        ]
    )
    assert stmts.no_inventory_lines() is True
