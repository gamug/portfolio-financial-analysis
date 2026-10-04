"""T-140 (a): a revenue total the gateway dropped is rebuilt from the statement's own later
"Total revenues and other" line (``docs/model_fixes.md``).

The gateway's T-042 rule (``no_filed_nondimensional_fact``) removes revenue a filer tags only with a
dimension, so APA's 2021Q1-2023Q3 statements arrive with every revenue line empty and only the
broader subtotal valued. The ``financials_APA_*`` fixtures are real gateway captures (statements
trimmed to the income statement, comparatives and the gateway's own ``corrections`` kept).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from fundamental_agent.statements import REGISTRY, Statements

FIXTURES = Path(__file__).parent / "fixtures"
_M = 1_000_000.0


def _payload(name: str) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads((FIXTURES / name).read_text())["data"])


def _apa(name: str) -> Statements:
    return Statements.from_payload(_payload(name))


FY2022 = "financials_APA_10-K_2023_acc-0001784031-23-000007.json"
FY2021 = "financials_APA_10-K_2022_acc-0001784031-22-000009.json"
Q1_2021 = "financials_APA_10-Q_2021_acc-0001673379-21-000012.json"
Q1_2022 = "financials_APA_10-Q_2022_acc-0001784031-22-000015.json"


# -- the real APA shapes ------------------------------------------------------------------------


def test_apa_fy2022_revenue_is_rebuilt_from_total_revenues_and_other() -> None:
    """The 10-K whose ``us-gaap_Revenues`` $11,075M the gateway dropped: "Total revenues and
    other" $12,132M less derivative loss (-114), divestiture gain (1,180), property-sale loss
    (-157) and other, net (148)."""
    stmts = _apa(FY2022)
    assert stmts.get("revenue", "2022-12-31 (FY)") == 11_075 * _M
    # the same filing's comparative years come out of the same rule
    assert stmts.get("revenue", "2021-12-31 (FY)") == 7_985 * _M
    assert stmts.get("revenue", "2020-12-31 (FY)") == 4_435 * _M


def test_apa_fy2021_revenue_is_the_10_ks_own_rows() -> None:
    """7,928 - (94 + 67 - 446 + 228) = 7,985: the figure the gateway reports as the original of the
    value it dropped from the FY2022 10-K, and $3M off what production stored (T-095's path)."""
    assert _apa(FY2021).get("revenue", "2021-12-31 (FY)") == 7_985 * _M


def test_a_surviving_zero_component_is_not_mistaken_for_the_total() -> None:
    """APA 2021Q1: the only revenue row the gateway kept is a $0 "production revenues" component.
    It sits at the head of the section, so it is never subtracted -- and it is a partial stream
    far below the rebuilt total, which wins."""
    stmts = _apa(Q1_2021)
    assert stmts._component_value(REGISTRY["revenue"], "2021-03-31 (Q1)") == 0.0
    assert stmts.get("revenue", "2021-03-31 (Q1)") == 1_871 * _M


def test_a_quarter_with_a_large_one_off_gain_is_rebuilt_only_when_the_gateway_agrees() -> None:
    """APA 2022Q1: a $1,176M divestiture gain is 31% of the $3,828M subtotal, above T-117's
    ceiling for a between row. The derivation (3,828 - (-62 + 1,176 + 45) = 2,669) is trusted
    because the gateway's own figure for the dropped total is 2,669 to the dollar; without that
    corroboration it is refused -- no value, never a guess."""
    payload = _payload(Q1_2022)
    stmts = Statements.from_payload(payload)
    spec = REGISTRY["revenue"]
    rebuild = stmts.rebuild_total(spec, "2022-03-31 (Q1)")
    assert rebuild is not None and rebuild.corroborated
    assert stmts.get("revenue", "2022-03-31 (Q1)") == 2_669 * _M

    uncorroborated = Statements.from_payload({**payload, "corrections": []})
    refused = uncorroborated.rebuild_total(spec, "2022-03-31 (Q1)")
    assert refused is not None and refused.value is None
    assert refused.refusal == "a row between is too large"
    assert uncorroborated.get("revenue", "2022-03-31 (Q1)") is None


def test_a_gateway_original_that_is_a_dimensional_slice_corroborates_nothing() -> None:
    """The gateway's ``original`` is not trusted by itself: for APA FY2021 it is $1,082M, the
    equity-method-investee slice (T-128's shape), and no rebuild equals that. Corroboration only
    ever *admits* an over-ceiling derivation that equals it; it never replaces one."""
    stmts = _apa(FY2021)
    spec = REGISTRY["revenue"]
    assert not stmts._gateway_original_is(spec, "2021-12-31 (FY)", 7_985 * _M)
    assert stmts._gateway_original_is(spec, "2021-12-31 (FY)", 1_082 * _M)
    assert stmts.get("revenue", "2021-12-31 (FY)") == 7_985 * _M  # not 1,082


@pytest.mark.parametrize("name", [FY2022, FY2021, Q1_2022])
def test_every_rebuilt_value_equals_the_gateways_original_or_the_original_is_a_slice(
    name: str,
) -> None:
    """The cross-check: wherever the gateway's ``corrections`` carry the dropped
    ``us-gaap_Revenues`` as ``original``, the rebuilt revenue equals it -- or the original is
    exactly one of the filing's dimensional ``us-gaap_Revenues`` rows (a slice the gateway promoted
    to a total), which is smaller than the total it stands in for."""
    payload = _payload(name)
    stmts = Statements.from_payload(payload)
    checked = 0
    for c in payload["corrections"]:
        if c["concept"] != "us-gaap_Revenues" or c["reason"] != "no_filed_nondimensional_fact":
            continue
        column, original = c["column"], c["original"]
        rebuilt = stmts.get("revenue", column)
        slices = [
            r[column]
            for r in payload["income_statement"]
            if r.get("dimension") and r["concept"] == "us-gaap_Revenues" and r.get(column)
        ]
        assert rebuilt == original or (original in slices and rebuilt is not None), (
            name,
            column,
            original,
            rebuilt,
        )
        checked += 1
    assert checked >= 1


# -- the rule, on small synthetic statements ----------------------------------------------------


def _row(concept: str, label: str, **columns: float | None) -> dict[str, Any]:
    return {"concept": concept, "label": label, "abstract": False, "dimension": False, **columns}


def _stmts(*rows: dict[str, Any], corrections: list[dict[str, Any]] | None = None) -> Statements:
    return Statements.from_payload(
        {
            "income_statement": list(rows),
            "balance_sheet": [],
            "cash_flow": [],
            "corrections": corrections or [],
        }
    )


FY = "2022-12-31 (FY)"
_DROPPED = _row("us-gaap_Revenues", "Total revenues", **{FY: None})


def test_the_rows_between_the_section_and_the_subtotal_are_subtracted() -> None:
    stmts = _stmts(
        _DROPPED,
        _row("x_Gain", "Gain on sale", **{FY: 100.0}),
        _row("x_Other", "Other, net", **{FY: -20.0}),
        _row("x_Total", "Total revenues and other", **{FY: 1_000.0}),
        _row("x_Opex", "Lease operating expenses", **{FY: 400.0}),  # after the subtotal: ignored
    )
    assert stmts.get("revenue", FY) == 920.0


def test_a_subtotal_with_nothing_between_is_the_revenue_itself() -> None:
    assert _stmts(_row("x_Total", "Total revenues", **{FY: 500.0})).get("revenue", FY) == 500.0


def test_it_refuses_when_a_row_between_is_too_large_to_trust() -> None:
    """A row above T-117's ceiling (25% of the subtotal) may be a revenue line rather than an
    adjustment: no value, not even the subtotal."""
    stmts = _stmts(
        _DROPPED,
        _row("x_Big", "Some large line", **{FY: 400.0}),
        _row("x_Total", "Total revenues and other", **{FY: 1_000.0}),
    )
    rebuild = stmts.rebuild_total(REGISTRY["revenue"], FY)
    assert rebuild is not None and rebuild.value is None and rebuild.between == (400.0,)
    assert stmts.get("revenue", FY) is None


def test_it_refuses_a_non_positive_result() -> None:
    stmts = _stmts(
        _row("x_A", "Gain", **{FY: 200.0}),
        _row("x_B", "Gain two", **{FY: 200.0}),
        _row("x_C", "Gain three", **{FY: 200.0}),
        _row("x_D", "Gain four", **{FY: 200.0}),
        _row("x_Total", "Total revenues and other", **{FY: 800.0}),
    )
    assert stmts.get("revenue", FY) is None


def test_a_cost_of_revenues_line_is_never_the_subtotal() -> None:
    """T-117's exclusion holds here too: "Total cost of revenues" reads as "total ... revenue"."""
    stmts = _stmts(
        _DROPPED,
        _row("us-gaap_CostOfRevenue", "Total cost of revenues", **{FY: 700.0}),
        _row("x_Gross", "Gross profit", **{FY: 300.0}),
    )
    assert stmts.rebuild_total(REGISTRY["revenue"], FY) is None
    assert stmts.get("revenue", FY) is None


def test_a_cost_line_before_a_real_subtotal_is_skipped_not_subtracted_from_it() -> None:
    stmts = _stmts(
        _row("us-gaap_CostOfRevenue", "Total cost of revenues", **{FY: 10.0}),
        _row("x_Total", "Total revenues and other", **{FY: 1_000.0}),
    )
    # the cost line is a between row (10 <= 25% of 1,000), never the anchor
    assert stmts.rebuild_total(REGISTRY["revenue"], FY).anchor_label == "Total revenues and other"  # type: ignore[union-attr]
    assert stmts.get("revenue", FY) == 990.0


def test_a_surviving_revenue_total_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rebuild never runs when a ``total_concepts`` row has a value."""
    stmts = _stmts(
        _row("us-gaap_Revenues", "Total revenues", **{FY: 800.0}),
        _row("x_Gain", "Gain", **{FY: 100.0}),
        _row("x_Total", "Total revenues and other", **{FY: 900.0}),
    )

    def boom(*_a: object) -> None:
        raise AssertionError("the rebuild must not run")

    monkeypatch.setattr(Statements, "_rebuild_missing_total", boom)
    assert stmts.get("revenue", FY) == 800.0


def test_a_total_that_is_present_but_rejected_is_not_a_dropped_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-095/T-128's shape: ``us-gaap_Revenues`` is *there*, as a promoted dimensional slice
    ($1,082M against a $3,064M component) and rejected as implausible. That is a mistagged value,
    not a dropped one, and Tier 2 answers as before -- the rebuild is for a column where no
    ``total_concepts`` row has a value at all."""
    slice_row = _row("us-gaap_Revenues", "Total revenues", **{FY: 1_082.0})
    slice_row.update(dimension=True, dimension_axis="us-gaap:EquityMethodInvestmentAxis")
    stmts = _stmts(
        _row("us-gaap_Revenues", "Total revenues", **{FY: 1_082.0}),
        slice_row,
        _row(
            "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax", "Sales", **{FY: 3_064.0}
        ),
        _row("x_Total", "Total revenues and other", **{FY: 3_100.0}),
    )

    def boom(*_a: object) -> None:
        raise AssertionError("the rebuild must not run")

    monkeypatch.setattr(Statements, "_rebuild_missing_total", boom)
    assert stmts.get("revenue", FY) == 3_064.0


def test_a_surviving_component_is_not_subtracted_and_a_larger_one_wins() -> None:
    """A real component the gateway kept is section, not adjustment. When it is *above* the rebuilt
    total the two contradict, and the component (the pre-T-140 answer) stands."""
    comp = _row(
        "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax", "Sales", **{FY: 900.0}
    )
    gain = _row("x_Gain", "Gain", **{FY: 100.0})
    total = _row("x_Total", "Total revenues and other", **{FY: 1_000.0})
    assert _stmts(comp, gain, total).get("revenue", FY) == 900.0  # 1,000 - 100, equal
    small = _row(
        "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax", "Sales", **{FY: 950.0}
    )
    assert _stmts(small, gain, total).get("revenue", FY) == 950.0  # component above 900: kept


def test_a_subtotal_above_a_revenue_component_is_not_a_later_total() -> None:
    """The PSX F2 shape: the custom total precedes the component, so it is not "after the section"."""
    stmts = _stmts(
        _row("psx_RevenuesAndOtherIncome", "Total Revenues and Other Income", **{FY: 114.0}),
        _row("us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax", "Sales", **{FY: 111.0}),
    )
    assert stmts.get("revenue", FY) == 111.0


def test_it_works_on_a_quarter_and_on_its_year_to_date_column() -> None:
    q, ytd = "2021-06-30 (Q2)", "2021-06-30 (YTD)"
    stmts = _stmts(
        _row("us-gaap_Revenues", "Total revenues", **{q: None, ytd: None}),
        _row("x_Deriv", "Derivative gains", **{q: -113.0, ytd: 45.0}),
        _row("x_Gain", "Gain on divestitures", **{q: 65.0, ytd: 67.0}),
        _row("x_Other", "Other, net", **{q: 74.0, ytd: 135.0}),
        _row("x_Total", "Total revenues and other", **{q: 1_782.0, ytd: 3_874.0}),
    )
    assert stmts.get("revenue", q) == 1_756.0
    assert stmts.get("revenue", ytd) == 3_627.0


def test_it_never_reads_a_filer_s_custom_concept_name() -> None:
    """Labels only: the same shape under another filer's concept names rebuilds the same way, and a
    row whose *concept* says revenue but whose label does not is no subtotal."""
    stmts = _stmts(
        _row("zzz_Whatever", "Total revenues and other", **{FY: 1_000.0}),
    )
    assert stmts.get("revenue", FY) == 1_000.0
    assert (
        _stmts(_row("apa_RevenuesAndOther", "Net operating income", **{FY: 1_000.0})).get(
            "revenue", FY
        )
        is None
    )
