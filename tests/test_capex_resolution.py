"""Tests for T-148 (f): capex under MET-08 and APP-04b.

Rules tested:
- Non-REIT Tier 1: first filed of PaymentsToAcquirePropertyPlantAndEquipment and PaymentsForCapitalImprovements.
- Non-REIT Tier 2 & 3: PP&E-family lines (Machinery, Furniture, Other PP&E) and Oil & Gas lines (E&D, Equipment).
  - DOW: Machinery (2,479M) + Gas field developments (157M) -> 2,636M flagged ppe_family_plus_og_added.
  - EOG: O&G E&D (6,115M) + Other PP&E (479M) -> 6,594M flagged ppe_family_plus_og_added.
  - KKR: Furniture (100M) beside zero O&G line (0.0) -> furniture line with ppe_family_line.
- Tier 4: PaymentsToAcquireOilAndGasPropertyAndEquipment flagged includes_acquisitions_contrary_to_a.
  Mineral interests (PaymentsToAcquireOilAndGasProperty) alone is not capex -> empty.
- Tier 5: Productive assets fallback (PaymentsToAcquireProductiveAssets, PaymentsToAcquireOtherProductiveAssets).
- Tier 6: Net element last resort (PaymentsForProceedsFromProductiveAssets).
- REIT capex under APP-04b:
  - Acquisitions + developments summed with reit_components and fcf_non_standard_reit.
  - Acquisitions + improvements raises capital_improvements_overlap_not_added and improvements omitted.
  - Every REIT capex carries fcf_non_standard_reit.
- Capex flag inputs reach both cash-flow and valuation metrics.
"""

from __future__ import annotations

from typing import Any

from fundamental_agent.metrics import cashflow, valuation
from fundamental_agent.pricing import ClosePrice
from fundamental_agent.statements import Statements

FY = "2025-12-31 (FY)"


def _cashflow(*rows: dict[str, Any], reit: bool = False) -> Statements:
    return Statements.from_payload(
        {
            "income_statement": [],
            "balance_sheet": [],
            "cash_flow": list(rows),
            "corrections": [],
        },
        reit=reit,
    )


def _row(concept: str, label: str, val: float | None = None) -> dict[str, Any]:
    return {
        "concept": concept,
        "label": label,
        "abstract": False,
        "dimension": False,
        FY: val,
    }


def test_dow_capex_sums_machinery_and_gas_field_developments() -> None:
    """DOW FY2025 (acc 0001751788-26-000018):
    PaymentsToAcquireMachineryAndEquipment 2,479M +
    PaymentsToExploreAndDevelopOilAndGasProperties 157M = 2,636M
    flagged ppe_family_plus_og_added."""
    stmts = _cashflow(
        _row(
            "us-gaap_PaymentsToAcquireMachineryAndEquipment",
            "Capital expenditures",
            2_479_000_000.0,
        ),
        _row(
            "us-gaap_PaymentsToExploreAndDevelopOilAndGasProperties",
            "Investment in gas field developments",
            157_000_000.0,
        ),
    )
    res = stmts.resolve("capital_expenditure", FY)
    assert res.value == 2_636_000_000.0
    assert res.how == "map"
    assert "ppe_family_plus_og_added" in res.flags


def test_eog_capex_sums_oil_and_gas_and_other_ppe() -> None:
    """EOG: PaymentsToExploreAndDevelopOilAndGasProperties 6,115M +
    PaymentsToAcquireOtherPropertyPlantAndEquipment 479M = 6,594M
    flagged ppe_family_plus_og_added."""
    stmts = _cashflow(
        _row(
            "us-gaap_PaymentsToExploreAndDevelopOilAndGasProperties",
            "Additions to oil and gas properties",
            6_115_000_000.0,
        ),
        _row(
            "us-gaap_PaymentsToAcquireOtherPropertyPlantAndEquipment",
            "Other property, plant and equipment",
            479_000_000.0,
        ),
    )
    res = stmts.resolve("capital_expenditure", FY)
    assert res.value == 6_594_000_000.0
    assert res.how == "map"
    assert "ppe_family_plus_og_added" in res.flags


def test_kkr_capex_takes_furniture_beside_zero_oil_and_gas_line() -> None:
    """KKR FY2021: zero PaymentsToExploreAndDevelopOilAndGasProperties beside
    PaymentsToAcquireFurnitureAndFixtures 100M -> resolves furniture line with ppe_family_line."""
    stmts = _cashflow(
        _row("us-gaap_PaymentsToExploreAndDevelopOilAndGasProperties", "Oil and gas", 0.0),
        _row(
            "us-gaap_PaymentsToAcquireFurnitureAndFixtures",
            "Furniture and fixtures",
            100_000_000.0,
        ),
    )
    res = stmts.resolve("capital_expenditure", FY)
    assert res.value == 100_000_000.0
    assert res.concept == "us-gaap_PaymentsToAcquireFurnitureAndFixtures"
    assert "ppe_family_line" in res.flags


def test_mineral_interests_alone_is_not_capex() -> None:
    """MET-08 (a): PaymentsToAcquireOilAndGasProperty (mineral interests) is not capex.
    When filed alone -> empty."""
    stmts = _cashflow(
        _row("us-gaap_PaymentsToAcquireOilAndGasProperty", "Mineral interests", 500_000_000.0),
    )
    res = stmts.resolve("capital_expenditure", FY)
    assert res.value is None


def test_tier4_og_property_and_equipment_flagged() -> None:
    """Tier 4: PaymentsToAcquireOilAndGasPropertyAndEquipment when no finer line
    raises includes_acquisitions_contrary_to_a."""
    stmts = _cashflow(
        _row(
            "us-gaap_PaymentsToAcquireOilAndGasPropertyAndEquipment",
            "Oil and gas property and equipment",
            800_000_000.0,
        ),
    )
    res = stmts.resolve("capital_expenditure", FY)
    assert res.value == 800_000_000.0
    assert "includes_acquisitions_contrary_to_a" in res.flags


def test_reit_acquisitions_and_development() -> None:
    """APP-04b: Equity REIT sums real estate acquisitions and developments.
    Carries reit_components and fcf_non_standard_reit."""
    stmts = _cashflow(
        _row("us-gaap_PaymentsToAcquireRealEstate", "Acquisitions of real estate", 200_000_000.0),
        _row(
            "us-gaap_PaymentsToDevelopRealEstateAssets", "Development of real estate", 50_000_000.0
        ),
        reit=True,
    )
    res = stmts.resolve("capital_expenditure", FY)
    assert res.value == 250_000_000.0
    assert res.how == "map"
    assert "reit_components" in res.flags
    assert "fcf_non_standard_reit" in res.flags


def test_reit_improvements_and_acquisitions_overlap_flagged() -> None:
    """APP-04b: When a REIT files both an acquisition line and PaymentsForCapitalImprovements,
    improvements are omitted and capital_improvements_overlap_not_added is flagged."""
    stmts = _cashflow(
        _row("us-gaap_PaymentsToAcquireRealEstate", "Acquisitions of real estate", 200_000_000.0),
        _row("us-gaap_PaymentsForCapitalImprovements", "Capital improvements", 30_000_000.0),
        reit=True,
    )
    res = stmts.resolve("capital_expenditure", FY)
    assert res.value == 200_000_000.0
    assert "reit_components" in res.flags
    assert "capital_improvements_overlap_not_added" in res.flags
    assert "fcf_non_standard_reit" in res.flags


def test_reit_improvements_without_acquisitions_added() -> None:
    """APP-04b: When a REIT files PaymentsForCapitalImprovements without an acquisition line,
    improvements are added to other components."""
    stmts = _cashflow(
        _row("us-gaap_PaymentsToAcquirePropertyPlantAndEquipment", "PP&E additions", 20_000_000.0),
        _row("us-gaap_PaymentsForCapitalImprovements", "Capital improvements", 30_000_000.0),
        reit=True,
    )
    res = stmts.resolve("capital_expenditure", FY)
    assert res.value == 50_000_000.0
    assert "reit_components" in res.flags
    assert "capital_improvements_overlap_not_added" not in res.flags
    assert "fcf_non_standard_reit" in res.flags


def test_capex_flags_propagate_to_cashflow_and_valuation_inputs() -> None:
    """Capex flags propagate to cashflow inputs and valuation inputs."""
    stmts = _cashflow(
        _row(
            "us-gaap_PaymentsToAcquireMachineryAndEquipment",
            "Capital expenditures",
            2_479_000_000.0,
        ),
        _row(
            "us-gaap_PaymentsToExploreAndDevelopOilAndGasProperties",
            "Investment in gas field developments",
            157_000_000.0,
        ),
    )
    # Cashflow
    cf_results = cashflow.compute(stmts, FY)
    assert cf_results[0].inputs.get("flag_capital_expenditure_ppe_family_plus_og_added") == 1.0

    # Valuation
    price = ClosePrice("2025-12-31", 50.0)
    val_results = valuation.compute(stmts, FY, price)
    assert val_results[0].inputs.get("flag_capital_expenditure_ppe_family_plus_og_added") == 1.0
