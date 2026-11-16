"""ID-10 (T-149): a combined filing's statements use only the parent's consolidated facts.

Duke Energy's FY2024 10-K is one document for Duke Energy Corporation and its subsidiary
registrants (Duke Energy Carolinas, Progress Energy, Duke Energy Progress, Duke Energy Florida,
Duke Energy Ohio, Duke Energy Indiana, Piedmont). The gateway returns the subsidiaries' figures in
the same statements as rows carrying ``dei:LegalEntityAxis``; the consolidated entity has none
(A2 §5.3). The fixture is the real payload, captured 2026-10-11 through the gateway by CIK 1326160.

The expected figures are the filing's own consolidated statements, which the SEC's companyfacts
(non-dimensional facts only) repeats for accession 0001326160-25-000072: Total operating revenues
30,357M, Net income 4,614M (``ProfitLoss``; 4,524M attributable to Duke Energy Corporation, 4,402M
available to common stockholders), Total Assets 186,343M.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fundamental_agent.statements import REGISTRY, Statements, _parse_period, iter_facts

FIXTURE = (
    Path(__file__).parent / "fixtures" / "financials_DUK_10-K_2025_acc-0001326160-25-000072.json"
)
FY = "2024-12-31 (FY)"
BALANCE = "2024-12-31"
STATEMENTS = ("income_statement", "balance_sheet", "cash_flow")
LEGAL_ENTITY_AXIS = "dei:LegalEntityAxis"


@pytest.fixture(scope="module")
def payload() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())["data"]  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def duke(payload: dict[str, Any]) -> Statements:
    return Statements.from_payload(payload)


def _is_legal_entity(row: dict[str, Any]) -> bool:
    return bool(row.get("dimension")) and row.get("dimension_axis") == LEGAL_ENTITY_AXIS


def _values_by_date(payload: dict[str, Any], *, legal_entity: bool) -> dict[str, set[float]]:
    out: dict[str, set[float]] = {}
    for statement in STATEMENTS:
        for row in payload[statement]:
            if _is_legal_entity(row) is not legal_entity:
                continue
            for column, value in row.items():
                period = _parse_period(str(column))
                if period is not None and isinstance(value, (int, float)):
                    out.setdefault(period.date, set()).add(float(value))
    return out


def test_the_capture_is_a_combined_filing(payload: dict[str, Any]) -> None:
    """The fixture really carries the subsidiaries: hundreds of legal-entity rows, among them the
    seven registrants' own revenues."""
    rows = [r for s in STATEMENTS for r in payload[s] if _is_legal_entity(r)]
    assert len(rows) > 500
    labels = {r["label"] for r in rows}
    assert {"Duke Energy Carolinas", "Progress Energy", "Duke Energy Florida", "Piedmont"} <= labels


def test_the_parent_s_revenue_net_income_and_total_assets_are_the_consolidated_ones(
    duke: Statements,
) -> None:
    assert duke.get("revenue", FY) == 30_357_000_000.0
    # ID-03 (T-148): parent earnings, "Net Income Attributable to Duke Energy Corporation" -- not
    # ProfitLoss, 4,614M, which includes 90M of noncontrolling interests
    assert duke.get("net_income", FY) == 4_524_000_000.0
    assert duke.get("net_income_to_common", FY) == 4_402_000_000.0
    assert duke.get("total_assets", BALANCE) == 186_343_000_000.0


def test_no_subsidiary_figure_is_ever_the_value_of_an_item(
    payload: dict[str, Any], duke: Statements
) -> None:
    """Every item, every period: a resolved value that equals a legal-entity row's value must also
    be held by a consolidated row (a coincidence of two figures), never by the subsidiary alone."""
    subsidiary = _values_by_date(payload, legal_entity=True)
    consolidated = _values_by_date(payload, legal_entity=False)
    leaks = [
        (item, period.key, value)
        for item in REGISTRY
        for period in duke.periods
        if (value := duke.get(item, period.key)) is not None
        and value in subsidiary.get(period.date, set())
        and value not in consolidated.get(period.date, set())
    ]
    assert leaks == []


def test_the_subsidiaries_revenues_are_not_the_parent_s(duke: Statements) -> None:
    for subsidiary_revenue in (9_718_000_000.0, 13_633_000_000.0, 7_017_000_000.0, 6_595_000_000.0):
        assert duke.get("revenue", FY) != subsidiary_revenue


def test_stored_facts_come_only_from_rows_without_a_dimension(
    payload: dict[str, Any], duke: Statements
) -> None:
    facts = list(iter_facts(duke))
    assert facts
    consolidated = {
        (s, r["concept"], r["label"])
        for s in STATEMENTS
        for r in payload[s]
        if not r.get("dimension") and not r.get("abstract")
    }
    assert {(f["statement"], f["concept"], f["label"]) for f in facts} <= consolidated
    subsidiaries = {r["label"] for s in STATEMENTS for r in payload[s] if _is_legal_entity(r)}
    assert subsidiaries.isdisjoint({f["label"] for f in facts})
    # the subsidiaries' revenue concept is among the dimensional rows only
    revenue = [
        f for f in facts if f["concept"] == "us-gaap_RegulatedAndUnregulatedOperatingRevenue"
    ]
    assert {f["value"] for f in revenue if f["period_key"] == FY} == {30_357_000_000.0}


def test_without_the_consolidated_row_the_item_is_missing_not_a_subsidiary(
    payload: dict[str, Any],
) -> None:
    """The gateway's T-042 drops a total a filer tags only with a dimension (Duke's consolidated
    cost of sales). Remove the consolidated revenue total the same way: what is left is the
    subsidiaries' revenues, and none of them may stand in for the group's."""
    stripped = {
        s: [
            r
            for r in payload[s]
            if _is_legal_entity(r)
            or r.get("concept") != "us-gaap_RegulatedAndUnregulatedOperatingRevenue"
            or r.get("dimension")
        ]
        for s in STATEMENTS
    }
    stripped["corrections"] = []
    stripped["reconciliation_errors"] = []
    stripped["cover"] = payload["cover"]
    stmts = Statements.from_payload(stripped)
    assert stmts.get("revenue", FY) is None


def test_the_real_payload_has_no_consolidated_cost_of_sales(duke: Statements) -> None:
    """Duke's own case: ``CostOfGoodsAndServicesSold`` is filed per legal entity and segment only
    (the gateway reports it unvalued), so ``cogs`` is missing -- not Carolinas' 3,251M."""
    assert duke.get("cogs", FY) is None
