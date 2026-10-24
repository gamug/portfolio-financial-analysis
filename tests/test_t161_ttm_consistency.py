"""T-161: the TTM consistency check's reading fix.

The YTD-method TTM ``FY(prior 10-K) - YTD(prior) + YTD(this year)`` mixes two meanings when a
filer tags its D&A differently on the 10-K and the 10-Qs. NDSN's FY2025 10-K files only the
intangible amortization under ``DepreciationDepletionAndAmortization`` (79,264,000, equal to
``AmortizationOfIntangibleAssets``) and files ``Depreciation`` (71,259,000) separately, while the
10-Qs file the full D&A (Q1 FY2026 36,585,000). The reading is the mirror of T-151's
``depreciation_only_not_da``: an aggregate equal to the filing's amortization alone, with
depreciation filed separately, is their sum -- and when the depreciation fact is ambiguous the
item is refused rather than stored mixed.
"""

from __future__ import annotations

from typing import Any

from conftest import filed_after
from portfolio_common.db import Database

from fundamental_agent import db
from fundamental_agent.db import YTDPair
from fundamental_agent.metrics.base import MetricResult
from fundamental_agent.statements import Statements

G = "us-gaap_"
FY = "2025-10-31 (FY)"
Q1 = "2026-01-31 (Q1)"
YTD = "2026-01-31 (YTD)"
PRIOR_YTD = "2025-01-31 (YTD)"
FY_START = "2024-11-01"
FY_END = "2025-10-31"


def _row(concept: str, label: str, value: float, column: str = FY) -> dict[str, Any]:
    return {
        "concept": G + concept,
        "label": label,
        "standard_concept": None,
        "abstract": False,
        "dimension": False,
        "preferred_sign": 1.0,
        column: value,
    }


def _fact(
    concept: str, value: float, *, start: str = FY_START, end: str = FY_END
) -> dict[str, Any]:
    return {
        "concept": f"us-gaap:{concept}",
        "value": value,
        "unit": "USD",
        "period_start": start,
        "period_end": end,
        "decimals": "-3",
    }


def _payload(
    *,
    cash_flow: list[dict[str, Any]] | None = None,
    filed_facts: list[dict[str, Any]] | None = None,
) -> Statements:
    return Statements.from_payload(
        {
            "income_statement": [],
            "balance_sheet": [],
            "cash_flow": cash_flow or [],
            "filed_facts": filed_facts or [],
            "filed_facts_error": None,
        }
    )


# -- the 10-K reading ---------------------------------------------------------------------------


def test_the_ndsn_10k_da_is_depreciation_plus_amortization() -> None:
    """NDSN FY2025: the aggregate is the amortization alone, ``Depreciation`` is filed apart.

    The cash-flow face carries ``AdjustmentForAmortization`` (79,264,000) and ``Depreciation``
    (71,259,000), so the resolver's statement tiers find no D&A aggregate; the filed facts carry
    ``DepreciationDepletionAndAmortization`` = ``AmortizationOfIntangibleAssets`` = 79,264,000.
    """
    stmts = _payload(
        cash_flow=[
            _row("AdjustmentForAmortization", "Amortization", 79_264_000.0),
            _row("Depreciation", "Depreciation", 71_259_000.0),
        ],
        filed_facts=[
            _fact("DepreciationDepletionAndAmortization", 79_264_000.0),
            _fact("AmortizationOfIntangibleAssets", 79_264_000.0),
            _fact("Depreciation", 71_259_000.0),
        ],
    )
    res = stmts.resolve("depreciation_amortization", FY)
    assert res.value == 150_523_000.0
    assert res.concept == f"{G}Depreciation + {G}AmortizationOfIntangibleAssets"
    assert res.how == "sourced_map"
    assert "da_amortization_only_sum" in res.flags
    assert "da_amortization_only_refused" not in res.flags


def test_the_10q_mirror_of_the_same_shape_is_the_sum_not_a_double_count() -> None:
    """A 10-Q whose aggregate is the amortization alone: 19,569,000 + 17,016,000 = 36,585,000.

    The aggregate and the ``AmortizationOfIntangibleAssets`` row carry the same value, so T-159a
    rule 4 would otherwise add them (39,138,000); T-161's mirror runs first and sums the two
    *different* parts."""
    stmts = _payload(
        cash_flow=[
            _row(
                "DepreciationDepletionAndAmortization", "Amortization of intangibles", 19_569_000.0
            ),
            _row("AmortizationOfIntangibleAssets", "Amortization of intangibles", 19_569_000.0),
            _row("Depreciation", "Depreciation", 17_016_000.0),
        ],
    )
    res = stmts.resolve("depreciation_amortization", FY)
    assert res.value == 36_585_000.0
    assert "da_amortization_only_sum" in res.flags


def test_an_ambiguous_depreciation_refuses_the_mixed_value() -> None:
    """Two different ``Depreciation`` facts for the period: no sourced reading, so the item is
    empty (never the amortization alone) and the refusal is flagged."""
    stmts = _payload(
        cash_flow=[
            _row("AdjustmentForAmortization", "Amortization", 79_264_000.0),
            _row("Depreciation", "Depreciation", 71_259_000.0),
        ],
        filed_facts=[
            _fact("DepreciationDepletionAndAmortization", 79_264_000.0),
            _fact("AmortizationOfIntangibleAssets", 79_264_000.0),
            _fact("Depreciation", 71_259_000.0),
            _fact("Depreciation", 70_000_000.0),
        ],
    )
    res = stmts.resolve("depreciation_amortization", FY)
    assert res.value is None
    assert "da_amortization_only_refused" in res.flags


def test_a_genuine_aggregate_that_is_not_the_amortization_stays() -> None:
    """A real aggregate D&A (150,523,000) is not the amortization (79,264,000): the reading is
    unchanged and the new flags do not fire."""
    stmts = _payload(
        cash_flow=[
            _row(
                "DepreciationDepletionAndAmortization",
                "Depreciation and amortization",
                150_523_000.0,
            ),
            _row("Depreciation", "Depreciation", 71_259_000.0),
        ],
    )
    res = stmts.resolve("depreciation_amortization", FY)
    assert res.value == 150_523_000.0
    assert "da_amortization_only_sum" not in res.flags
    assert "da_amortization_only_refused" not in res.flags


def test_an_aggregate_equal_to_adjustment_for_amortization_is_kept() -> None:
    """INTU 2025Q1's shape: the 10-Q's DDA (165,000,000) equals ``AdjustmentForAmortization``,
    with ``Depreciation`` 44,000,000 separate. ``AdjustmentForAmortization`` is not the
    amortization term (it also carries financing-cost and debt-discount amortization), so the
    165,000,000 is kept — never 209,000,000."""
    stmts = _payload(
        cash_flow=[
            _row(
                "DepreciationDepletionAndAmortization",
                "Amortization of acquired intangible assets",
                165_000_000.0,
            ),
            _row(
                "AdjustmentForAmortization",
                "Amortization of acquired intangible assets",
                165_000_000.0,
            ),
            _row("Depreciation", "Depreciation", 44_000_000.0),
        ],
    )
    res = stmts.resolve("depreciation_amortization", FY)
    assert res.value == 165_000_000.0
    assert res.concept == f"{G}DepreciationDepletionAndAmortization"
    assert "da_amortization_only_sum" not in res.flags
    assert "da_amortization_only_refused" not in res.flags


def test_an_aggregate_that_already_equals_the_sum_is_kept_not_doubled() -> None:
    """DDA 150,523,000 = Depreciation 71,259,000 + AmortizationOfIntangibleAssets 79,264,000:
    the aggregate already is the full D&A, so T-159a's rule 4 must not add the amortization a
    second time (229,787,000)."""
    stmts = _payload(
        cash_flow=[
            _row(
                "DepreciationDepletionAndAmortization",
                "Depreciation and amortization",
                150_523_000.0,
            ),
            _row("AmortizationOfIntangibleAssets", "Amortization of intangibles", 79_264_000.0),
            _row("Depreciation", "Depreciation", 71_259_000.0),
        ],
    )
    res = stmts.resolve("depreciation_amortization", FY)
    assert res.value == 150_523_000.0
    assert "da_amortization_only_sum" not in res.flags
    assert "da_plus_intangibles_row" not in res.flags


# -- the TTM through it --------------------------------------------------------------------------


def _asset(conn: Database) -> int:
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'NDSN')")
    return 1


def _record(
    conn: Database, form: str, period: str, period_end: str, inputs: dict[str, Any]
) -> None:
    fid = db.upsert_filing(
        conn,
        db.FilingKey(1, form, int(period_end[:4]), period),
        db.FilingMeta(filing_date=filed_after(period_end), period_end=period_end),
    )
    db.record_metrics(
        conn,
        fid,
        [("leverage", MetricResult("net_debt_to_ebitda", None, "x", dict(inputs)))],
    )


def test_the_ttm_through_the_corrected_10k_is_150_077_000(memory_db: Database) -> None:
    """NDSN Q1 FY2026: 150,523,000 - 37,031,000 + 36,585,000 = 150,077,000 (stored 78,818,000
    before the reading fix)."""
    _asset(memory_db)
    _record(
        memory_db,
        "10-K",
        "FY2025",
        "2025-10-31",
        {"depreciation_amortization": 150_523_000.0},
    )
    flows = db.ttm_detail(
        memory_db,
        1,
        period_end="2026-01-31",
        current={},
        ytd={"depreciation_amortization": YTDPair(36_585_000.0, 37_031_000.0, "2025-01-31")},
    )
    flow = flows["depreciation_amortization"]
    assert flow.method == "ytd"
    assert flow.value == 150_077_000.0
    assert flow.terms == (
        ("prior_fiscal_year", 150_523_000.0),
        ("prior_ytd", 37_031_000.0),
        ("ytd", 36_585_000.0),
    )


def test_the_prior_10k_refusal_reaches_the_ttm_flow(memory_db: Database) -> None:
    """A prior 10-K whose D&A was refused for the amortization-only shape: the 10-Q's D&A flow
    carries the refusal flag, and with no four-quarter sum it is refused (never the mixed value)."""
    _asset(memory_db)
    _record(
        memory_db,
        "10-K",
        "FY2025",
        "2025-10-31",
        {"flag_depreciation_amortization_da_amortization_only_refused": 1.0},
    )
    flows = db.ttm_detail(
        memory_db,
        1,
        period_end="2026-01-31",
        current={},
        ytd={"depreciation_amortization": YTDPair(36_585_000.0, 37_031_000.0, "2025-01-31")},
    )
    flow = flows["depreciation_amortization"]
    assert flow.method == "refused"
    assert flow.value == 0.0
    assert flow.flags == ("da_amortization_only_refused",)
