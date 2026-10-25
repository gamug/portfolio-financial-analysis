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
- REIT capex under T-071 part 2b (D6): the FASB 2026 calculation tree makes
  PaymentsToAcquireRealEstate the parent of PaymentsToAcquireCommercialRealEstate and
  PaymentsForCapitalImprovements (a filed parent is used alone; the filed children are summed when it
  is absent, flagged reit_sibling_sum), and the REIT-only extension sum reads every captioned
  "capital expenditures" **company-extension** line (FRT), flagged reit_extension_sum.
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


def _row(concept: str, label: str, val: float | None = None, *, sign: float | None = None) -> dict:
    row: dict[str, Any] = {
        "concept": concept,
        "label": label,
        "abstract": False,
        "dimension": False,
        FY: val,
    }
    if sign is not None:
        row["preferred_sign"] = sign
    return row


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


def test_capex_filed_zero_resolves_as_zero() -> None:
    """MET-00 / R5: a filed 0 is a value, not a missing one.
    PaymentsToAcquireOilAndGasPropertyAndEquipment filed as 0.0 resolves to 0.0."""
    stmts = _cashflow(
        _row(
            "us-gaap_PaymentsToAcquireOilAndGasPropertyAndEquipment",
            "Oil and gas property and equipment",
            0.0,
        ),
    )
    res = stmts.resolve("capital_expenditure", FY)
    assert res.value == 0.0
    assert res.concept == "us-gaap_PaymentsToAcquireOilAndGasPropertyAndEquipment"
    assert "includes_acquisitions_contrary_to_a" in res.flags


def test_reit_capex_alone_zero_continues_to_next_tier() -> None:
    """G1: In a sum of components, a 0 line never forms the sum alone.
    REIT filing 0.0 for development alone continues to fallback tiers (empty)."""
    stmts = _cashflow(
        _row("us-gaap_PaymentsToDevelopRealEstateAssets", "Development", 0.0),
        reit=True,
    )
    res = stmts.resolve("capital_expenditure", FY)
    assert res.value is None


def test_reit_acquisitions_zero_and_improvements_added_without_overlap() -> None:
    """G1 (BXP case): REIT filing PaymentsToAcquireRealEstate 0.0 + PaymentsForCapitalImprovements
    171,424,000 -> 171,424,000, with no overlap flag."""
    stmts = _cashflow(
        _row("us-gaap_PaymentsToAcquireRealEstate", "Acquisitions", 0.0),
        _row(
            "us-gaap_PaymentsForCapitalImprovements",
            "Capital improvements",
            171_424_000.0,
        ),
        reit=True,
    )
    res = stmts.resolve("capital_expenditure", FY)
    assert res.value == 171_424_000.0
    assert res.concept == "us-gaap_PaymentsForCapitalImprovements"
    assert "reit_components" in res.flags
    assert "capital_improvements_overlap_not_added" not in res.flags


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


# -- T-071 part 2b (D6): the acquisition parent, the sibling sum and the extension sum -----------


def test_reit_parent_filed_is_used_alone_and_its_children_are_not_added() -> None:
    """The FASB 2026 calculation tree (``us-gaap-stm-scf-indir-cal-2026.xml``):
    ``PaymentsToAcquireRealEstate`` is the parent of ``PaymentsToAcquireCommercialRealEstate`` and
    ``PaymentsForCapitalImprovements``, so a filed parent is used **alone** -- APP-04b's own sentence
    (capital improvements are inside it) -- and the children are not added."""
    stmts = _cashflow(
        _row("us-gaap_PaymentsToAcquireRealEstate", "Acquisitions of real estate", 200_000_000.0),
        _row("us-gaap_PaymentsToAcquireCommercialRealEstate", "Net investment", 90_000_000.0),
        _row("us-gaap_PaymentsForCapitalImprovements", "Capital improvements", 30_000_000.0),
        reit=True,
    )
    res = stmts.resolve("capital_expenditure", FY)
    assert res.value == 200_000_000.0
    assert res.concept == "us-gaap_PaymentsToAcquireRealEstate"
    assert "capital_improvements_overlap_not_added" in res.flags
    assert "reit_sibling_sum" not in res.flags


def test_reit_parent_absent_sums_the_sibling_children() -> None:
    """Parent not filed: its children are siblings in the calculation sum, so their definitions are
    explicitly disjoint (MET-08 (b)) and both are added -- BXP/HST/O/WELL keep the capital
    improvements *and* gain the Commercial line."""
    stmts = _cashflow(
        _row("us-gaap_PaymentsToAcquireCommercialRealEstate", "Net investment", 90_000_000.0),
        _row("us-gaap_PaymentsForCapitalImprovements", "Capital improvements", 30_000_000.0),
        reit=True,
    )
    res = stmts.resolve("capital_expenditure", FY)
    assert res.value == 120_000_000.0
    assert res.how == "map"
    assert "reit_sibling_sum" in res.flags
    assert "capital_improvements_overlap_not_added" not in res.flags


def test_reit_extension_sum_takes_every_captioned_extension_line() -> None:
    """FRT: "Capital expenditures - development and redevelopment" beside "Capital expenditures -
    other" are both filer extensions **presented as outflows**, so the REIT composite sums them with
    the concept-map components (the shared fallback's one-line rule refuses both)."""
    stmts = _cashflow(
        _row("us-gaap_PaymentsToAcquireRealEstate", "Acquisition of real estate", 85_397_000.0),
        _row(
            "frt_CapitalExpendituresDevelopmentRedevelopment",
            "Capital expenditures - development and redevelopment",
            -78_779_000.0,
            sign=-1.0,
        ),
        _row(
            "frt_CapitalExpendituresOther",
            "Capital expenditures - other",
            -56_241_000.0,
            sign=-1.0,
        ),
        reit=True,
    )
    res = stmts.resolve("capital_expenditure", FY)
    assert res.value == 220_417_000.0
    assert res.how == "map"
    for flag in ("reit_extension_sum", "caption_fallback", "extension_concept", "reit_components"):
        assert flag in res.flags


def test_reit_extension_sum_takes_only_extensions_and_only_payment_captions() -> None:
    """Only company extensions, only payment captions: a us-gaap line with the capex caption stays
    with the concept map (VTR's "Other capital expenditures"), and an extension captioned as proceeds
    is not a payment."""
    stmts = _cashflow(
        _row(
            "us-gaap_PaymentsToAcquirePropertyPlantAndEquipment",
            "Capital expenditures",
            10_000_000.0,
        ),
        _row(
            "us-gaap_PaymentsToAcquireOtherProductiveAssets",
            "Other capital expenditures",
            5_000_000.0,
        ),
        _row(
            "xyz_CapitalExpendituresOther", "Capital expenditures - other", -3_000_000.0, sign=-1.0
        ),
        _row(
            "xyz_CapitalExpendituresProceeds",
            "Capital expenditures, proceeds",
            -7_000_000.0,
            sign=-1.0,
        ),
        _row(
            "xyz_CapitalExpendituresGain",
            "Capital expenditures, gain on sale",
            -9_000_000.0,
            sign=-1.0,
        ),
        reit=True,
    )
    res = stmts.resolve("capital_expenditure", FY)
    # PP&E 10M + the extension 3M + the us-gaap "Other capital expenditures" 5M, a component by its
    # caption (decision D-2, pilot_a3/DECISIONS.md) -- taken by the concept path, not the extension
    # path; never the proceeds or gain lines
    assert res.value == 18_000_000.0
    assert "reit_extension_sum" in res.flags
    assert "reit_other_productive_captioned" in res.flags


def test_reit_extension_sum_refuses_a_non_outflow_and_a_mirror_line() -> None:
    """Ruling of 2026-10-15, (b) and (c): a line not presented as an outflow (CPT's accrual, +1) and
    a line whose concept name names an accrual / an unpaid / a period-end mirror (PSA's
    "…RemainingUnpaidAtPeriodEnd", whose *caption* is identical to its cash line) are both refused."""
    stmts = _cashflow(
        _row("us-gaap_PaymentsToAcquireRealEstate", "Acquisition of real estate", 85_397_000.0),
        _row(
            "cpt_AccrualAssociatedWithConstructionAndCapitalExpenditures",
            "Accrual associated with construction and capital expenditures",
            12_000_000.0,
            sign=1.0,  # a non-cash accrual, not an outflow
        ),
        _row(
            "psa_CapitalExpendituresForPropertyEnhancements",
            "Capital expenditures for property enhancements",
            -8_000_000.0,
            sign=-1.0,
        ),
        _row(
            "psa_CapitalExpendituresForPropertyEnhancementsRemainingUnpaidAtPeriodEnd",
            "Capital expenditures for property enhancements",  # the same caption as its cash line
            -8_000_000.0,
            sign=-1.0,
        ),
        reit=True,
    )
    res = stmts.resolve("capital_expenditure", FY)
    # only the PSA cash line is added: the accrual and the unpaid mirror are refused
    assert res.value == 93_397_000.0
    assert "psa_CapitalExpendituresForPropertyEnhancements" in (res.concept or "")
    assert "RemainingUnpaid" not in (res.concept or "")
    assert "cpt_" not in (res.concept or "")


def test_reit_extension_sum_is_not_added_when_a_taken_line_is_their_total() -> None:
    """Ruling of 2026-10-15, (d): a us-gaap line the composite already takes that equals the extension
    lines' sum (DQ-01's tolerance) IS their total -- the extensions are its parts, so they are not
    added and reit_extension_total_present is flagged instead."""
    stmts = _cashflow(
        _row("us-gaap_PaymentsToAcquireRealEstate", "Acquisition of real estate", 135_020_000.0),
        _row(
            "xyz_CapitalExpendituresDevelopment",
            "Capital expenditures - development",
            -78_779_000.0,
            sign=-1.0,
        ),
        _row(
            "xyz_CapitalExpendituresOther", "Capital expenditures - other", -56_241_000.0, sign=-1.0
        ),
        reit=True,
    )
    res = stmts.resolve("capital_expenditure", FY)
    assert res.value == 135_020_000.0  # the filer's own total, not the parts on top of it
    assert "reit_extension_total_present" in res.flags
    assert "reit_extension_sum" not in res.flags


def test_non_reit_caption_fallback_still_refuses_two_matching_lines() -> None:
    """The one-line rule is unchanged for every other filer: the REIT extension sum is REIT-only."""
    stmts = _cashflow(
        _row(
            "xyz_CapitalExpendituresDevelopment",
            "Capital expenditures - development",
            -8_000_000.0,
            sign=-1.0,
        ),
        _row(
            "xyz_CapitalExpendituresOther", "Capital expenditures - other", -5_000_000.0, sign=-1.0
        ),
    )
    res = stmts.resolve("capital_expenditure", FY)
    assert res.value is None


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


def test_ppe_and_oil_and_gas_property_and_equipment_flags_acquisitions_contrary_to_a() -> None:
    """MET-08 (b)(4): When PaymentsToAcquireOilAndGasPropertyAndEquipment is added to a
    PP&E-family line, raise both ppe_family_plus_og_added and includes_acquisitions_contrary_to_a."""
    stmts = _cashflow(
        _row(
            "us-gaap_PaymentsToAcquireMachineryAndEquipment",
            "Machinery",
            500_000_000.0,
        ),
        _row(
            "us-gaap_PaymentsToAcquireOilAndGasPropertyAndEquipment",
            "Oil and gas property and equipment",
            300_000_000.0,
        ),
    )
    res = stmts.resolve("capital_expenditure", FY)
    assert res.value == 800_000_000.0
    assert res.how == "map"
    assert "ppe_family_plus_og_added" in res.flags
    assert "includes_acquisitions_contrary_to_a" in res.flags


def test_reit_components_follow_the_fasb_tree_children_and_tenant_improvements() -> None:
    """Audit F-A10: WELL's acquisitions are ``PaymentsToAcquireOtherRealEstate`` (a child of
    ``PaymentsToAcquireRealEstate``), its development ``PaymentsForConstructionInProcess``; BXP's
    tenant improvements are their own branch. All are APP-04b components and are summed."""
    stmts = _cashflow(
        _row("us-gaap_PaymentsForCapitalImprovements", "Capital improvements", 1_050_000_000.0),
        _row("us-gaap_PaymentsForConstructionInProcess", "Construction in progress", 437_000_000.0),
        _row("us-gaap_PaymentsToAcquireOtherRealEstate", "Acquisitions", 13_914_000_000.0),
        _row("us-gaap_PaymentsForTenantImprovements", "Tenant improvements", 258_000_000.0),
        reit=True,
    )
    res = stmts.resolve("capital_expenditure", FY)
    assert res.value == 15_659_000_000.0
    assert "reit_sibling_sum" in res.flags


def test_reit_parent_smaller_than_its_children_is_a_separate_line() -> None:
    """Decision D-3: BXP 2024's 35 M acquisitions (the parent) cannot contain its 190 M capital
    improvements (the child) -- both are added; a parent at least as large stays alone."""
    small = _cashflow(
        _row("us-gaap_PaymentsToAcquireRealEstate", "Acquisitions of real estate", 35_000_000.0),
        _row("us-gaap_PaymentsForCapitalImprovements", "Building improvements", 190_000_000.0),
        reit=True,
    ).resolve("capital_expenditure", FY)
    assert small.value == 225_000_000.0
    assert "reit_parent_smaller_than_children" in small.flags
    large = _cashflow(
        _row("us-gaap_PaymentsToAcquireRealEstate", "Acquisitions of real estate", 500_000_000.0),
        _row("us-gaap_PaymentsForCapitalImprovements", "Building improvements", 190_000_000.0),
        reit=True,
    ).resolve("capital_expenditure", FY)
    assert large.value == 500_000_000.0
    assert "capital_improvements_overlap_not_added" in large.flags


def test_reit_other_productive_assets_only_when_captioned_as_capital_spending() -> None:
    """Decision D-2: BXP's "Construction in progress" filed as ``PaymentsToAcquireOtherProductiveAssets``
    is a component; the same tag captioned as something else is not."""
    def capex(label: str) -> float | None:
        return _cashflow(
            _row("us-gaap_PaymentsForCapitalImprovements", "Building improvements", 190_000_000.0),
            _row("us-gaap_PaymentsToAcquireOtherProductiveAssets", label, 651_000_000.0),
            reit=True,
        ).resolve("capital_expenditure", FY).value

    assert capex("Construction in progress") == 841_000_000.0
    assert capex("Purchases of intangible assets") == 190_000_000.0
