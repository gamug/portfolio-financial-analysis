"""Statement parsing and line-item resolution against real EDGAR captures."""

from __future__ import annotations

from typing import Any

import pytest

from fundamental_agent.statements import INSTANT, Period, Statements, _parse_period, iter_facts


def _tag(column: str) -> str | None:
    parsed = _parse_period(column)
    return parsed.tag if parsed else None


def _need(period: Period | None) -> Period:
    assert period is not None
    return period


def test_parses_duration_and_instant_columns() -> None:
    assert _tag("2023-09-30 (FY)") == "FY"
    assert _tag("2024-06-29 (Q3)") == "Q3"
    assert _tag("2024-06-29 (YTD)") == "YTD"
    assert _tag("2023-09-30") == INSTANT
    assert _parse_period("total assets") is None


def test_period_helpers(aapl_10k: Statements) -> None:
    fy = aapl_10k.fy_periods()
    assert [p.date for p in fy] == ["2021-09-25", "2022-09-24", "2023-09-30"]
    assert _need(aapl_10k.latest_fy()).date == "2023-09-30"
    assert _need(aapl_10k.prior_of(fy[-1])).date == "2022-09-24"
    assert aapl_10k.instant_periods()  # balance sheet columns are bare dates


def test_income_line_items_resolve(aapl_10k: Statements) -> None:
    key = "2023-09-30 (FY)"
    assert aapl_10k.get("revenue", key) == 383285000000.0
    assert aapl_10k.get("cogs", key) == 214137000000.0
    assert aapl_10k.get("gross_profit", key) == 169148000000.0
    assert aapl_10k.get("operating_income", key) == 114301000000.0
    assert aapl_10k.get("net_income", key) == 96995000000.0


def test_balance_sheet_resolves_via_instant_translation(aapl_10k: Statements) -> None:
    # asked with a duration key; balance-sheet items must map to the instant column.
    key = "2023-09-30 (FY)"
    assert aapl_10k.get("total_assets", key) == 352583000000.0
    assert aapl_10k.get("current_assets", key) == 143566000000.0
    assert aapl_10k.get("current_liabilities", key) == 145308000000.0
    assert aapl_10k.get("equity", key) == 62146000000.0
    # cash must be the balance-sheet stock, not a cash-flow reconciliation line.
    assert aapl_10k.get("cash", key) == 29965000000.0


def test_share_count_and_sbc_line_items(aapl_10k: Statements, jpm_10k: Statements) -> None:
    key = "2023-09-30 (FY)"
    # point-in-time common shares outstanding (balance sheet)
    assert aapl_10k.get("shares_outstanding", key) == 15550061000.0
    # diluted weighted-average, NOT the basic line that sits just above it
    assert aapl_10k.get("diluted_shares", key) == 15812547000.0
    assert aapl_10k.get("stock_based_compensation", key) == 10833000000.0

    jpm_key = _need(jpm_10k.latest_fy()).key
    # a bank's payload has no CommonStockSharesOutstanding / ShareBasedCompensation
    assert jpm_10k.get("shares_outstanding", jpm_key) is None
    assert jpm_10k.get("stock_based_compensation", jpm_key) is None
    assert jpm_10k.get("diluted_shares", jpm_key) == 2970000000.0


def test_get_all_returns_every_period_column_for_a_concept(
    aapl_10k: Statements, jpm_10k: Statements
) -> None:
    # .get() stops at one period; .get_all() is for a cross-filing scale-defect
    # check (fundamental_agent.db.detect_share_scale_factors) that needs every
    # period column this filing's own payload reports for the concept.
    assert aapl_10k.get_all("diluted_shares") == {
        "2023-09-30 (FY)": 15812547000.0,
        "2022-09-24 (FY)": 16325819000.0,
        "2021-09-25 (FY)": 16864919000.0,
    }
    # a registry item with no matching row anywhere in this payload returns {},
    # not an error (a bank's payload has no CommonStockSharesOutstanding).
    assert jpm_10k.get_all("shares_outstanding") == {}


def test_balance_sheet_items_are_never_negative(nvda_10k: Statements) -> None:
    key = _need(nvda_10k.latest_fy()).key
    for item in ("inventory", "receivables", "short_term_investments", "long_term_debt"):
        value = nvda_10k.get(item, key)
        assert value is not None
        assert value >= 0.0


def test_bank_has_no_operating_income_or_current_split(jpm_10k: Statements) -> None:
    key = _need(jpm_10k.latest_fy()).key
    assert jpm_10k.get("operating_income", key) is None
    assert jpm_10k.get("current_liabilities", key) is None
    # but a bank still reports revenue, net income, assets and equity.
    assert jpm_10k.get("revenue", key) == pytest.approx(128695000000.0)
    assert jpm_10k.get("equity", key) == pytest.approx(292332000000.0)


def test_quarterly_prior_period_detected_in_same_payload(msft_10q: Statements) -> None:
    quarters = msft_10q.quarter_periods()
    latest = quarters[-1]
    prior = msft_10q.prior_of(latest)
    assert prior is not None
    assert prior.tag == latest.tag
    assert prior.year == latest.year - 1


def test_iter_facts_yields_only_numeric_period_cells(aapl_10k: Statements) -> None:
    facts = list(iter_facts(aapl_10k))
    assert facts
    assert all(isinstance(f["value"], float) for f in facts)
    assert all(f["statement"] in aapl_10k.raw for f in facts)
    assert {f["statement"] for f in facts} == set(aapl_10k.raw)


def _income_row(concept: str, label: str, **periods: float) -> dict[str, Any]:
    """A minimal non-dimensional income-statement row -- mirrors the real
    EDGAR-gateway shape captured in tests/fixtures/financials_*.json."""
    row: dict[str, Any] = {
        "concept": concept,
        "label": label,
        "standard_concept": None,
        "abstract": False,
        "dimension": False,
    }
    row.update(periods)
    return row


def _revenue_payload(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"income_statement": list(rows), "balance_sheet": [], "cash_flow": []}


@pytest.mark.parametrize("total_first", [False, True])
def test_revenue_prefers_total_over_components_regardless_of_document_order(
    total_first: bool,
) -> None:
    """F2 (docs/model_fixes.md): a filer reporting a lease-income stream, a
    fee-income stream, and an explicit "Total revenues" (UDR's real shape)
    must resolve to the total, whichever order the rows appear in -- not
    whichever qualifying row the filer happened to list first."""
    key = "2023-12-31 (FY)"
    total_row = _income_row("us-gaap_Revenues", "Total revenues", **{key: 1_712_317_000.0})
    component_rows = [
        _income_row("us-gaap_OperatingLeaseLeaseIncome", "Rental income", **{key: 1_700_956_000.0}),
        _income_row(
            "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
            "Joint venture management and other fees",
            **{key: 11_361_000.0},
        ),
    ]
    rows = [total_row, *component_rows] if total_first else [*component_rows, total_row]
    stmts = Statements.from_payload(_revenue_payload(*rows))

    assert stmts.get("revenue", key) == 1_712_317_000.0


def test_revenue_sums_distinct_components_when_no_total_is_tagged() -> None:
    """F2: CPT's real shape -- a lease-income stream and a fee-income stream,
    but no separately tagged "Total revenues" row at all. The theoretically
    correct reconstruction is their sum (GAAP total revenue = sum of revenue
    streams), not just the first component encountered."""
    key = "2022-12-31 (FY)"
    stmts = Statements.from_payload(
        _revenue_payload(
            _income_row(
                "us-gaap_OperatingLeaseLeaseIncome", "Property revenues", **{key: 1_570_000_000.0}
            ),
            _income_row(
                "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
                "Fee and asset management",
                **{key: 13_000_000.0},
            ),
        )
    )

    assert stmts.get("revenue", key) == 1_583_000_000.0


def test_revenue_prefers_excluding_assessed_tax_over_the_synonym_variant() -> None:
    """F2 Sourcery follow-up (docs/model_fixes.md): ExcludingAssessedTax and
    IncludingAssessedTax are the SAME line reported two ways (net of vs.
    gross of pass-through sales/excise tax), never two additive amounts --
    verified live for BF.B/STZ/TAP/PM, all of whom tag both for every
    period with no separate total. Must resolve to the (correct,
    income-statement) excluding-tax figure alone, not their sum."""
    key = "2022-04-30 (FY)"
    stmts = Statements.from_payload(
        _revenue_payload(
            _income_row(
                "us-gaap_RevenueFromContractWithCustomerIncludingAssessedTax",
                "Sales",
                **{key: 5_081_000_000.0},
            ),
            _income_row(
                "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
                "Net sales",
                **{key: 3_933_000_000.0},
            ),
        )
    )

    assert stmts.get("revenue", key) == 3_933_000_000.0  # NOT 9_014_000_000.0


def test_revenue_sum_ignores_a_label_only_match_from_a_custom_total_concept() -> None:
    """F2 Sourcery follow-up: a filer's own custom-taxonomy "Total ..."
    extension concept (PSX's `psx_RevenuesAndOtherIncome`, real shape) is
    not in `total_concepts` and must NOT be pulled into the component sum
    just because its label contains "total revenue" -- that would double
    the real component instead of summing genuinely distinct streams."""
    key = "2021-12-31 (FY)"
    stmts = Statements.from_payload(
        _revenue_payload(
            _income_row(
                "psx_RevenuesAndOtherIncome",
                "Total Revenues and Other Income",
                **{key: 114_852_000_000.0},
            ),
            _income_row(
                "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
                "Sales and other operating revenues",
                **{key: 111_476_000_000.0},
            ),
        )
    )

    assert stmts.get("revenue", key) == 111_476_000_000.0  # NOT the sum


def test_revenue_rejects_a_total_far_smaller_than_a_named_component() -> None:
    """T-095 (docs/model_fixes.md): APA FY2021's real shape -- its
    `us-gaap_Revenues` "Total revenues" tag is mistagged on one small
    dimensional slice ($1,082M, matching only the "Equity Method Investment,
    Nonconsolidated Investee" breakdown row) rather than the consolidated
    aggregate; the real total sits on
    `us-gaap_RevenueFromContractWithCustomerIncludingAssessedTax` ($7,988M,
    corroborated by `apa_RevenuesAndOther`'s $7,928M "Total revenues and
    other", not modeled here). A `total_concepts` match this far below a
    named `concepts` candidate must be rejected, not trusted outright."""
    key = "2021-12-31 (FY)"
    stmts = Statements.from_payload(
        _revenue_payload(
            _income_row("us-gaap_Revenues", "Total revenues", **{key: 1_082_000_000.0}),
            _income_row(
                "us-gaap_RevenueFromContractWithCustomerIncludingAssessedTax",
                "Revenue from contract with customer, including assessed tax",
                **{key: 7_988_000_000.0},
            ),
        )
    )

    assert stmts.get("revenue", key) == 7_988_000_000.0  # NOT the mistagged 1_082_000_000.0


def test_revenue_total_concepts_with_no_component_to_compare_is_trusted() -> None:
    """A filer with no `concepts` rows tagged at all -- a bank's
    `RevenuesNetOfInterestExpense` total, JPM's real shape (also covered end
    to end by the `jpm_10k` fixture) -- has nothing to sanity-check the total
    against, so T-095's plausibility floor must not invent a rejection."""
    key = "2023-12-31 (FY)"
    stmts = Statements.from_payload(
        _revenue_payload(
            _income_row(
                "us-gaap_RevenuesNetOfInterestExpense", "Total net revenue", **{key: 5_000_000.0}
            ),
        )
    )

    assert stmts.get("revenue", key) == 5_000_000.0


@pytest.mark.parametrize(
    ("ratio", "expect_total"),
    [(0.5, True), (0.49, False)],
    ids=["at_floor_trusted", "just_below_floor_rejected"],
)
def test_revenue_total_concepts_plausibility_floor_is_pinned(
    ratio: float, expect_total: bool
) -> None:
    """T-095's plausibility floor (`Statements._TOTAL_PLAUSIBILITY_FLOOR`) is
    an exact, documented fraction of the largest named component -- not an
    arbitrary cutoff picked to fit APA alone. Pin the boundary explicitly so
    a future change to the constant is a deliberate edit, not a silent
    mutation."""
    key = "2023-12-31 (FY)"
    largest_component = 1_000_000_000.0
    total = largest_component * ratio
    stmts = Statements.from_payload(
        _revenue_payload(
            _income_row("us-gaap_Revenues", "Total revenues", **{key: total}),
            _income_row(
                "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
                "Net revenues",
                **{key: largest_component},
            ),
        )
    )

    assert stmts.get("revenue", key) == (total if expect_total else largest_component)


def test_non_revenue_multi_concept_item_keeps_first_match_only() -> None:
    """`cogs` has the same two-distinct-concepts shape `revenue` had pre-F2
    but has NOT opted into `sum_components` -- must still return exactly the
    first document-order match, not their sum. Guards against a future
    change that flips the default, or copies revenue's registry shape onto
    another item without the same total/component analysis."""
    key = "2023-12-31 (FY)"
    stmts = Statements.from_payload(
        {
            "income_statement": [
                _income_row("us-gaap_CostOfGoodsSold", "Cost of goods sold", **{key: 100.0}),
                _income_row("us-gaap_CostOfServices", "Cost of services", **{key: 50.0}),
            ],
            "balance_sheet": [],
            "cash_flow": [],
        }
    )

    assert stmts.get("cogs", key) == 100.0  # first match only, NOT summed to 150.0
