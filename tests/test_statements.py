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


def _slice_row(
    concept: str, label: str, axis: str, member: str, **periods: float
) -> dict[str, Any]:
    """A dimensional income-statement row -- the gateway's real shape for a breakdown slice
    (``dimension=true`` plus ``dimension_axis``/``dimension_member``, see the AAPL/NVDA fixtures)."""
    row = _income_row(concept, label, **periods)
    row.update(dimension=True, dimension_axis=axis, dimension_member=member)
    return row


_EQUITY_METHOD_AXIS = "us-gaap:EquityMethodInvestmentNonconsolidatedInvesteeAxis"


def _revenue_payload(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"income_statement": list(rows), "balance_sheet": [], "cash_flow": []}


def test_from_payload_reads_the_gateways_own_corrections_list() -> None:
    """T-118: `get_financials` (PR #44, upstream) adds a top-level `data["corrections"]`
    naming every value it derived rather than returned as filed. A payload predating
    that gateway version (no `corrections` key at all) parses with an empty list, not
    an error -- the shape iter_facts below always sees, whichever gateway version
    answered."""
    key = "2023-12-31 (FY)"
    corrections = [
        {
            "statement": "income_statement",
            "concept": "us-gaap_Revenues",
            "column": key,
            "original": 16_558_000_000.0,
            "corrected": 8_279_000_000.0,
            "rule": "T-118",
        }
    ]
    payload = _revenue_payload(_income_row("us-gaap_Revenues", "Total revenues", **{key: 8.279e9}))
    payload["corrections"] = corrections

    assert Statements.from_payload(payload).corrections == corrections
    assert Statements.from_payload(_revenue_payload()).corrections == []


def test_iter_facts_marks_only_the_fact_the_gateway_corrected() -> None:
    """T-118 (PR #98 review): a corrected value must be persisted distinguishably from a
    filed one -- `iter_facts` tags the matching `(statement, concept, column)` cell with
    the correction's `rule` id and leaves every other fact (including the same concept's
    other periods, and every other concept) `None`, meaning filed as-is."""
    key, other_key = "2023-12-31 (FY)", "2022-12-31 (FY)"
    payload = _revenue_payload(
        _income_row("us-gaap_Revenues", "Total revenues", **{key: 8.279e9, other_key: 11.075e9}),
        _income_row("us-gaap_CostOfRevenue", "Cost of revenue", **{key: 3.0e9}),
    )
    payload["corrections"] = [
        {
            "statement": "income_statement",
            "concept": "us-gaap_Revenues",
            "column": key,
            "original": 16_558_000_000.0,
            "corrected": 8_279_000_000.0,
            "rule": "T-118",
        }
    ]
    stmts = Statements.from_payload(payload)

    facts = {(f["concept"], f["period_key"]): f["correction_rule"] for f in iter_facts(stmts)}

    assert facts[("us-gaap_Revenues", key)] == "T-118"
    assert facts[("us-gaap_Revenues", other_key)] is None
    assert facts[("us-gaap_CostOfRevenue", key)] is None


def test_iter_facts_keys_corrections_by_statement_too() -> None:
    """T-118 (PR #98 review, item 2): once T-042 started reconciling all three statements,
    the same `(concept, column)` can appear on more than one -- e.g.
    `us-gaap_NetIncomeLoss` on the income statement and again as a cash-flow
    reconciliation line -- and be corrected on only one of them. Keying the lookup by
    `(concept, column)` alone would tag both facts identically; keying by
    `(statement, concept, column)` tags only the one the gateway actually named."""
    key = "2023-12-31 (FY)"
    concept = "us-gaap_NetIncomeLoss"
    payload = {
        "income_statement": [_income_row(concept, "Net income", **{key: 500_000_000.0})],
        "balance_sheet": [],
        "cash_flow": [_income_row(concept, "Net income", **{key: 500_000_000.0})],
        "corrections": [
            {
                "statement": "cash_flow",
                "concept": concept,
                "column": key,
                "original": 500_000_000.0,
                "corrected": 480_000_000.0,
                "rule": "T-042",
            }
        ],
    }
    stmts = Statements.from_payload(payload)

    facts = {
        (f["statement"], f["concept"], f["period_key"]): f["correction_rule"]
        for f in iter_facts(stmts)
    }

    assert facts[("income_statement", concept, key)] is None
    assert facts[("cash_flow", concept, key)] == "T-042"


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
    other", not modeled here). T-128: the filing's `us-gaap:Revenues` exists only on
    the equity-method-investee dimension (verified in the 10-K's XBRL instance); the
    gateway's non-dimensional row duplicates it exactly. A `total_concepts` match that
    duplicates a dimensional slice and is below a named `concepts` candidate must be
    rejected, not trusted outright."""
    key = "2021-12-31 (FY)"
    stmts = Statements.from_payload(
        _revenue_payload(
            _income_row("us-gaap_Revenues", "Total revenues", **{key: 1_082_000_000.0}),
            _slice_row(
                "us-gaap_Revenues",
                "Total revenues",
                _EQUITY_METHOD_AXIS,
                "us-gaap_EquityMethodInvestmentNonconsolidatedInvesteeOrGroupOfInvesteesMember",
                **{key: 1_082_000_000.0},
            ),
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


def _gas_producer_payload(key: str, total: float, *extra: dict[str, Any]) -> dict[str, Any]:
    """A gas producer whose reported total nets hedging losses below its gross sales (EQT's
    real shape): gross sales component 6,804M, total operating revenues 3,064M."""
    return _revenue_payload(
        _income_row("us-gaap_Revenues", "Total operating revenues", **{key: total}),
        _income_row(
            "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
            "Sales of natural gas, natural gas liquids and oil",
            **{key: 6_804_020_000.0},
        ),
        *extra,
    )


@pytest.mark.parametrize(
    ("total", "label"),
    [(3_064_663_000.0, "EQT FY2021, ratio 0.45"), (5_273_309_000.0 * 0.35, "ratio 0.35")],
    ids=["eqt_fy2021", "deeper_hedging_loss"],
)
def test_a_total_netting_hedges_below_half_a_component_is_trusted(total: float, label: str) -> None:
    """T-128: T-095's 50% floor rejected EQT FY2021/FY2024/FY2025 and EXE FY2022/FY2024 --
    gas producers whose top line legitimately nets derivative losses below half their gross
    sales. Their totals are not a dimensional slice (checked in each 10-K's XBRL instance),
    so the structural check leaves them alone however small the ratio."""
    key = "2021-12-31 (FY)"
    stmts = Statements.from_payload(_gas_producer_payload(key, total))

    assert stmts.get("revenue", key) == total, label


def test_a_total_that_duplicates_an_oil_and_gas_slice_next_to_a_larger_component_is_rejected() -> (
    None
):
    """The ratio is irrelevant in the other direction too: APA's duplicate sits at 0.14 here, but
    the same defect at 0.9 of the component is still a promoted slice, and is rejected."""
    key = "2021-12-31 (FY)"
    total = 6_800_000_000.0
    stmts = Statements.from_payload(
        _gas_producer_payload(
            key,
            total,
            _slice_row(
                "us-gaap_Revenues", "Total", _EQUITY_METHOD_AXIS, "inv_Member", **{key: total}
            ),
        )
    )

    assert stmts.get("revenue", key) == 6_804_020_000.0


def test_a_total_equal_to_a_whole_entity_segment_is_trusted() -> None:
    """A single-segment filer's segment revenue *is* its total, so a dimensional twin on the
    segment/consolidation axes is no evidence of a promoted slice -- even beside a larger gross
    component (hedging again)."""
    key = "2021-12-31 (FY)"
    total = 3_064_663_000.0
    stmts = Statements.from_payload(
        _gas_producer_payload(
            key,
            total,
            _slice_row(
                "us-gaap_Revenues",
                "Operating segments",
                "srt:ConsolidationItemsAxis",
                "us-gaap_OperatingSegmentsMember",
                **{key: total},
            ),
        )
    )

    assert stmts.get("revenue", key) == total


def test_a_dimensional_twin_is_ignored_when_the_total_is_not_below_a_component() -> None:
    """A real aggregate is never smaller than one of its components, so a twin on a
    non-segment axis (a single-product filer) cannot make a total at least as large as every
    component implausible."""
    key = "2021-12-31 (FY)"
    stmts = Statements.from_payload(
        _revenue_payload(
            _income_row("us-gaap_Revenues", "Total revenues", **{key: 1_000_000_000.0}),
            _income_row(
                "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
                "Net revenues",
                **{key: 900_000_000.0},
            ),
            _slice_row(
                "us-gaap_Revenues",
                "Total revenues",
                _EQUITY_METHOD_AXIS,
                "inv_Member",
                **{key: 1_000_000_000.0},
            ),
        )
    )

    assert stmts.get("revenue", key) == 1_000_000_000.0


def test_a_dimensional_row_of_another_period_or_value_is_not_a_twin() -> None:
    """The twin must match in the same column and exactly -- a slice that merely sits close to
    the total, or in another fiscal year, proves nothing."""
    key = "2021-12-31 (FY)"
    stmts = Statements.from_payload(
        _gas_producer_payload(
            key,
            3_064_663_000.0,
            _slice_row(
                "us-gaap_Revenues",
                "Total",
                _EQUITY_METHOD_AXIS,
                "inv_Member",
                **{key: 3_064_000_000.0, "2020-12-31 (FY)": 3_064_663_000.0},
            ),
        )
    )

    assert stmts.get("revenue", key) == 3_064_663_000.0


def _apa_fy2023_rows(key: str) -> list[dict[str, Any]]:
    """APA's real FY2023 10-K income-statement shape (`financial_facts`, T-117):
    ``us-gaap_Revenues`` "Total revenues" is a gateway-mislabeled breakdown figure, roughly
    double the statement's own later "Total revenues and other" subtotal, less four small
    adjustment lines between the two (derivative gains/losses, divestiture gains, losses on
    previously sold properties, other, net)."""
    return [
        _income_row("us-gaap_Revenues", "Total revenues", **{key: 16_558_000_000.0}),
        _income_row(
            "us-gaap_GainLossOnDerivativeInstrumentsNetPretax",
            "Derivative instrument gains (losses), net",
            **{key: 99_000_000.0},
        ),
        _income_row(
            "us-gaap_GainLossOnSaleOfBusiness",
            "Gain on divestitures, net",
            **{key: 8_000_000.0},
        ),
        _income_row(
            "apa_LossOnPreviouslySoldProperties",
            "Losses on previously sold Gulf of Mexico properties",
            **{key: -212_000_000.0},
        ),
        _income_row("apa_OtherSalesRevenueLossesNet", "Other, net", **{key: 18_000_000.0}),
        _income_row("apa_RevenuesAndOther", "Total revenues and other", **{key: 8_192_000_000.0}),
        _income_row(
            "us-gaap_OperatingLeaseExpense", "Lease operating expenses", **{key: 1_436_000_000.0}
        ),
    ]


def test_revenue_rejects_a_total_far_larger_than_the_statement_s_own_later_total() -> None:
    """T-117 (docs/model_fixes.md): APA's FY2023-2025 shape -- the mirror image of T-095's
    too-small case. `us-gaap_Revenues` is not merely untrustworthy, it resolves to exactly
    the derived "Total revenues" the task's acceptance criterion names ($8,279M), not the
    mislabeled $16,558M nor the broader "Total revenues and other" ($8,192M, which still
    includes non-operating adjustment items)."""
    key = "2023-12-31 (FY)"
    stmts = Statements.from_payload(_revenue_payload(*_apa_fy2023_rows(key)))

    assert stmts.get("revenue", key) == 8_279_000_000.0


def test_revenue_total_label_correction_leaves_a_genuinely_larger_total_alone() -> None:
    """A later, larger "and other" total (APA's own FY2022, not a defect: "Total revenues
    and other" $12,132M >= "Total revenues" $11,075M, since "and other" only adds to
    revenue) must not be treated as a contradiction -- the Tier 1 total is trusted as-is."""
    key = "2022-12-31 (FY)"
    stmts = Statements.from_payload(
        _revenue_payload(
            _income_row("us-gaap_Revenues", "Total revenues", **{key: 11_075_000_000.0}),
            _income_row(
                "apa_RevenuesAndOther", "Total revenues and other", **{key: 12_132_000_000.0}
            ),
        )
    )

    assert stmts.get("revenue", key) == 11_075_000_000.0


def test_revenue_total_label_correction_ignores_cost_of_revenue_lines() -> None:
    """T-117 full-universe validation (5,076 stored filings): "Total cost of revenues" --
    a near-universal COGS-line label (ADBE, STE, TER, TSLA, URI, XYZ all use this exact
    phrase) -- must never be mistaken for a later revenue total merely because its label
    contains both "total" and "revenue"; every one of those was a false positive before
    the cost-word exclusion, none a real defect."""
    key = "2021-12-31 (FY)"
    stmts = Statements.from_payload(
        _revenue_payload(
            _income_row("us-gaap_Revenues", "Total revenues", **{key: 3_702_881_000.0}),
            _income_row(
                "us-gaap_CostOfGoodsAndServicesSold",
                "Total cost of revenues (exclusive of acquired intangible assets "
                "amortization shown separately below)",
                **{key: 1_496_225_000.0},
            ),
        )
    )

    assert stmts.get("revenue", key) == 3_702_881_000.0


def test_revenue_total_label_correction_refuses_to_guess_when_between_rows_are_too_large() -> None:
    """When a later, smaller "total"-labeled row is found but the rows between it and the
    Tier 1 match are not small adjustment items -- too large relative to the later total to
    trust as a clean subtraction -- the correction must refuse to guess: no returned value,
    not the untrustworthy Tier 1 total either."""
    key = "2023-12-31 (FY)"
    stmts = Statements.from_payload(
        _revenue_payload(
            _income_row("us-gaap_Revenues", "Total revenues", **{key: 16_558_000_000.0}),
            _income_row(
                "us-gaap_SomeHugeUnrelatedAdjustment",
                "Some huge unrelated adjustment",
                **{key: 6_000_000_000.0},  # far more than 25% of the later total below
            ),
            _income_row(
                "apa_RevenuesAndOther", "Total revenues and other", **{key: 8_192_000_000.0}
            ),
        )
    )

    assert stmts.get("revenue", key) is None


def test_net_income_resolves_the_available_to_common_variant_when_no_plain_tag_exists() -> None:
    """T-096 (docs/model_fixes.md): WAT's real shape -- 14 of 18 `metrics-v2` filings in
    the 20-ticker sample tag no `us-gaap_NetIncomeLoss`/`us-gaap_ProfitLoss` row at all,
    only `us-gaap_NetIncomeLossAvailableToCommonStockholdersBasic` (WAT's Oct-2022 10-Q,
    real value). Left unresolved, `net_income` silently comes back `None` -- and, chained
    through F4's `ttm_flows` (`db.ttm_flows`/`_recorded_flow`), poisons every later
    quarter's trailing-twelve-month window that needs this quarter's own value."""
    key = "2022-10-01 (Q4)"
    stmts = Statements.from_payload(
        _revenue_payload(
            _income_row(
                "us-gaap_NetIncomeLossAvailableToCommonStockholdersBasic",
                "Net income",
                **{key: 155_998_000.0},
            ),
        )
    )

    assert stmts.get("net_income", key) == 155_998_000.0


def test_net_income_still_prefers_the_plain_tag_when_both_are_present() -> None:
    """The `AvailableToCommonStockholdersBasic` fallback must not disturb the ordinary
    case (`us-gaap_NetIncomeLoss` present) -- a defensive regression guard; live
    verification (WAT) found the two never co-occur non-dimensionally in the same
    filing, but this pins the intended behavior if they ever did."""
    key = "2023-12-31 (FY)"
    stmts = Statements.from_payload(
        _revenue_payload(
            _income_row("us-gaap_NetIncomeLoss", "Net income", **{key: 100.0}),
            _income_row(
                "us-gaap_NetIncomeLossAvailableToCommonStockholdersBasic",
                "Net income available to common stockholders",
                **{key: 80.0},
            ),
        )
    )

    assert stmts.get("net_income", key) == 100.0


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


# -- T-102: a utility's total operating revenue --------------------------------------------------


def test_a_utility_total_is_its_revenue_when_it_is_the_only_revenue_tag(
    nee_10k: Statements,
) -> None:
    """NEE tags its income-statement revenue only as
    `us-gaap_RegulatedAndUnregulatedOperatingRevenue` ("OPERATING REVENUES"); before T-102 it
    resolved to None in every filing, and every revenue-denominated ratio with it."""
    assert nee_10k.get("revenue", "2025-12-31 (FY)") == 27_412_000_000.0
    assert nee_10k.get("revenue", "2024-12-31 (FY)") is not None


def test_a_utility_total_wins_over_its_regulated_and_unregulated_lines(
    xel_10k: Statements,
) -> None:
    """XEL reports electric (a company-specific tag), gas and other lines plus the total; the
    total is the revenue -- the lines are its breakdown, not additional streams."""
    key = "2025-12-31 (FY)"
    assert xel_10k.get("revenue", key) == 14_669_000_000.0  # = 12,160 + 2,452 + 57 (millions)


def test_the_utility_total_is_still_checked_against_a_named_component() -> None:
    """The new total concept goes through the duplicate-slice check (T-095/T-128) like the others."""
    key = "2025-12-31 (FY)"
    stmts = Statements.from_payload(
        _revenue_payload(
            _income_row(
                "us-gaap_RegulatedAndUnregulatedOperatingRevenue",
                "Operating revenues",
                **{key: 100.0},
            ),
            _slice_row(
                "us-gaap_RegulatedAndUnregulatedOperatingRevenue",
                "Subsidiary",
                _EQUITY_METHOD_AXIS,
                "inv_Member",
                **{key: 100.0},
            ),
            _income_row(
                "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenue from contracts with customers",
                **{key: 1_000.0},
            ),
        )
    )
    assert stmts.get("revenue", key) == 1_000.0
