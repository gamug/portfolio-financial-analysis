"""F1 (docs/model_fixes.md): detecting an XBRL share-count scale/tagging defect
before it corrupts market cap, via two independent signals -- either sufficient
on its own: (1) already-ingested filing history for an overlapping period, and
(2) this filing's own net_income / as-filed diluted EPS (no history needed).

Fixtures reproduce the two real production patterns found in `data/financial.db`
(MCD: a concept divided by an exact 1e6, persisting through 3+ consecutive
filings until no filing history anywhere carries a clean overlapping period
anymore; WAT: the same concept multiplied by an exact 1e3 in the single
most-recent filing, whose specific reported quarter was never independently
reported by any other filing) without needing a captured EDGAR-gateway JSON --
both are pure XBRL-tagging artifacts, not something a real filing fixture would
add insight over. Signal (1) alone cannot detect either case in its *current*
state (verified directly against the live production DB) -- signal (2) is what
actually closes the gap; see docs/model_fixes.md's F1 entry for the full record.
"""

from __future__ import annotations

from typing import Any

from portfolio_common.db import Database

from fundamental_agent import db
from fundamental_agent.db import FilingKey, FilingMeta
from fundamental_agent.statements import Statements, iter_facts
from kg_schema.queries import UniverseMember

_DILUTED_CONCEPT = "us-gaap_WeightedAverageNumberOfDilutedSharesOutstanding"


def _company(symbol: str) -> UniverseMember:
    return UniverseMember(
        symbol=symbol,
        security=symbol,
        cik="0000000001",
        gics_sector="Consumer",
        gics_sub_industry="Sub",
        hq_location=None,
        date_added=None,
        founded=None,
        valid_from="2020-01-01",
        valid_to=None,
    )


def _diluted_shares_payload(period_values: dict[str, float]) -> dict[str, Any]:
    """A minimal income_statement-only payload carrying one
    WeightedAverageNumberOfDilutedSharesOutstanding row -- mirrors the real
    EDGAR-gateway shape captured in tests/fixtures/financials_*.json."""
    row: dict[str, Any] = {
        "concept": _DILUTED_CONCEPT,
        "label": "Diluted (in shares)",
        "standard_concept": "SharesFullyDilutedAverage",
        "abstract": False,
        "dimension": False,
    }
    row.update(period_values)
    return {"income_statement": [row], "balance_sheet": [], "cash_flow": []}


def _payload_with_eps(
    diluted_shares: dict[str, float], net_income: dict[str, float], eps_diluted: dict[str, float]
) -> dict[str, Any]:
    """Like _diluted_shares_payload, plus the two independently-disclosed GAAP
    facts (net income, as-filed diluted EPS) the EPS cross-check needs -- all
    three already flow through the real ingestion path's blanket fact
    extraction (iter_facts), just via different registry items."""
    rows = [
        {
            "concept": _DILUTED_CONCEPT,
            "label": "Diluted (in shares)",
            "standard_concept": "SharesFullyDilutedAverage",
            "abstract": False,
            "dimension": False,
            **diluted_shares,
        },
        {
            "concept": "us-gaap_NetIncomeLoss",
            "label": "Net income",
            "standard_concept": None,
            "abstract": False,
            "dimension": False,
            **net_income,
        },
        {
            "concept": "us-gaap_EarningsPerShareDiluted",
            "label": "Diluted (in dollars per share)",
            "standard_concept": None,
            "abstract": False,
            "dimension": False,
            **eps_diluted,
        },
    ]
    return {"income_statement": rows, "balance_sheet": [], "cash_flow": []}


def _seed_filing(
    conn: Database, asset_id: int, fiscal_period: str, period_values: dict[str, float]
) -> tuple[int, Statements]:
    """Upsert a filing and append its diluted-shares facts, the way
    pipeline._analyze_one does (append_financial_facts before analysis)."""
    year = int(fiscal_period[-4:])
    filing_id = db.upsert_filing(
        conn,
        FilingKey(asset_id, "10-K", year, fiscal_period),
        FilingMeta(filing_date=f"{year + 1}-03-01", period_end=f"{year}-12-31"),
    )
    stmts = Statements.from_payload(_diluted_shares_payload(period_values))
    db.append_financial_facts(conn, filing_id, iter_facts(stmts))
    return filing_id, stmts


def _seed_filing_with_eps(  # noqa: PLR0913 - keyword-only fixture-shape fields
    conn: Database,
    asset_id: int,
    fiscal_period: str,
    *,
    diluted_shares: dict[str, float],
    net_income: dict[str, float],
    eps_diluted: dict[str, float],
) -> tuple[int, Statements]:
    """Like _seed_filing, but also carrying net_income/eps_diluted facts (the
    two independently-disclosed GAAP figures the EPS cross-check needs)."""
    year = int(fiscal_period[-4:])
    filing_id = db.upsert_filing(
        conn,
        FilingKey(asset_id, "10-K", year, fiscal_period),
        FilingMeta(filing_date=f"{year + 1}-03-01", period_end=f"{year}-12-31"),
    )
    stmts = Statements.from_payload(_payload_with_eps(diluted_shares, net_income, eps_diluted))
    db.append_financial_facts(conn, filing_id, iter_facts(stmts))
    return filing_id, stmts


def test_detect_share_scale_factors_finds_divide_by_1e6_defect(memory_db: Database) -> None:
    """MCD shape: a filing reports the concept divided by an exact 1e6 relative
    to what an earlier filing correctly reported for the same period."""
    db.sync_universe(memory_db, [_company("MCD")])
    asset_id = db.load_universe(memory_db)[0]["id"]
    _seed_filing(memory_db, asset_id, "FY2022", {"2022-12-31 (FY)": 741_300_000.0})
    bad_filing_id, bad_stmts = _seed_filing(
        memory_db,
        asset_id,
        "FY2023",
        {"2022-12-31 (FY)": 741.3, "2023-12-31 (FY)": 732.3},  # restated + new, both /1e6
    )

    factors = db.detect_share_scale_factors(
        memory_db, asset_id, bad_stmts, "2023-12-31 (FY)", exclude_filing_id=bad_filing_id
    )

    assert factors == {"diluted_shares": 1_000_000.0}


def test_detect_share_scale_factors_finds_multiply_by_1e3_defect(memory_db: Database) -> None:
    """WAT shape: a single, most-recent filing reports the concept multiplied by
    an exact 1e3 relative to the immediately preceding filing's own value."""
    db.sync_universe(memory_db, [_company("WAT")])
    asset_id = db.load_universe(memory_db)[0]["id"]
    _seed_filing(memory_db, asset_id, "FY2024", {"2024-12-31 (FY)": 59_552_000.0})
    bad_filing_id, bad_stmts = _seed_filing(
        memory_db,
        asset_id,
        "FY2025",
        {"2024-12-31 (FY)": 59_552_000_000.0, "2025-12-31 (FY)": 59_706_000_000.0},
    )

    factors = db.detect_share_scale_factors(
        memory_db, asset_id, bad_stmts, "2025-12-31 (FY)", exclude_filing_id=bad_filing_id
    )

    # the stored (corrupted) value needs to be *multiplied* by 1e-3 to correct it
    assert factors == {"diluted_shares": 1e-3}


def test_detect_share_scale_factors_ignores_normal_yoy_drift(memory_db: Database) -> None:
    """Ordinary organic share-count change (a buyback, a few percent) must never
    be mistaken for a scale defect -- no factor is within 10% of a power of ten."""
    db.sync_universe(memory_db, [_company("AAA")])
    asset_id = db.load_universe(memory_db)[0]["id"]
    _seed_filing(memory_db, asset_id, "FY2022", {"2022-12-31 (FY)": 100_000_000.0})
    new_filing_id, new_stmts = _seed_filing(
        memory_db,
        asset_id,
        "FY2023",
        {"2022-12-31 (FY)": 102_000_000.0, "2023-12-31 (FY)": 104_000_000.0},  # ~2%/yr drift
    )

    factors = db.detect_share_scale_factors(
        memory_db, asset_id, new_stmts, "2023-12-31 (FY)", exclude_filing_id=new_filing_id
    )

    assert factors == {}


def test_detect_share_scale_factors_empty_when_no_evidence_from_either_signal(
    memory_db: Database,
) -> None:
    """A company's first-ever filing, with no net_income/EPS facts either --
    neither signal has anything to compare against, so no correction is
    applied (a separate magnitude/plausibility backstop, PLAN.md's
    DQ_MCAP_SCALE, is the designated home for this residual case)."""
    db.sync_universe(memory_db, [_company("BBB")])
    asset_id = db.load_universe(memory_db)[0]["id"]
    filing_id, stmts = _seed_filing(memory_db, asset_id, "FY2023", {"2023-12-31 (FY)": 50_000.0})

    factors = db.detect_share_scale_factors(
        memory_db, asset_id, stmts, "2023-12-31 (FY)", exclude_filing_id=filing_id
    )

    assert factors == {}


def test_detect_share_scale_factors_via_eps_with_no_history_overlap(memory_db: Database) -> None:
    """The real MCD/WAT shape: the temporal-history signal alone finds nothing
    (no clean overlapping period exists anywhere -- verified directly against
    the live production DB, see docs/model_fixes.md, F1), but the same
    filing's own net_income / as-filed diluted EPS recovers the defect anyway,
    with no history needed at all."""
    db.sync_universe(memory_db, [_company("MCD")])
    asset_id = db.load_universe(memory_db)[0]["id"]
    # MCD's real FY2025 10-K: net_income $8,563M, EPS diluted $11.95, diluted
    # shares reported as 716.4 (should be 716,400,000 -- off by exactly 1e6).
    filing_id, stmts = _seed_filing_with_eps(
        memory_db,
        asset_id,
        "FY2025",
        diluted_shares={"2025-12-31 (FY)": 716.4},
        net_income={"2025-12-31 (FY)": 8_563_000_000.0},
        eps_diluted={"2025-12-31 (FY)": 11.95},
    )

    factors = db.detect_share_scale_factors(
        memory_db, asset_id, stmts, "2025-12-31 (FY)", exclude_filing_id=filing_id
    )

    assert factors == {"diluted_shares": 1_000_000.0}


def test_eps_signal_never_corroborates_shares_outstanding(memory_db: Database) -> None:
    """EPS is a weighted-average, duration-based measure -- it must never be
    used to correct the point-in-time, instant-dated shares_outstanding
    balance-sheet figure, only the also-duration-based diluted_shares."""
    db.sync_universe(memory_db, [_company("DDD")])
    asset_id = db.load_universe(memory_db)[0]["id"]
    payload = _payload_with_eps(
        diluted_shares={"2023-12-31 (FY)": 716.4},
        net_income={"2023-12-31 (FY)": 8_563_000_000.0},
        eps_diluted={"2023-12-31 (FY)": 11.95},
    )
    # add a shares_outstanding row with no corroborating history or EPS overlap
    payload["balance_sheet"] = [
        {
            "concept": "us-gaap_CommonStockSharesOutstanding",
            "label": "Common stock, shares outstanding",
            "standard_concept": "SharesYearEnd",
            "abstract": False,
            "dimension": False,
            "2023-12-31": 209.271,  # also off by 1e6, but on the wrong axis for EPS
        }
    ]
    filing_id = db.upsert_filing(
        memory_db,
        FilingKey(asset_id, "10-K", 2023, "FY2023"),
        FilingMeta(filing_date="2024-03-01", period_end="2023-12-31"),
    )
    stmts = Statements.from_payload(payload)
    db.append_financial_facts(memory_db, filing_id, iter_facts(stmts))

    factors = db.detect_share_scale_factors(
        memory_db, asset_id, stmts, "2023-12-31 (FY)", exclude_filing_id=filing_id
    )

    # diluted_shares is corrected via EPS; shares_outstanding is left untouched
    # (it wins the primary branch in _share_count, so this filing's market cap
    # would still need the temporal signal or a separate backstop to catch it)
    assert factors == {"diluted_shares": 1_000_000.0}


def test_direct_target_evidence_overrides_a_different_defective_period(
    memory_db: Database,
) -> None:
    """A filing can restate one historical period incorrectly while its own
    target period is fine (or vice versa) -- direct evidence *at the target
    period itself* must decide, never a factor borrowed from a different
    period in the same filing (the bug a code-review bot caught: the original
    implementation would have blanket-applied 2020's 1e6 defect to a
    perfectly clean 2021 value just because they shared a filing)."""
    db.sync_universe(memory_db, [_company("FFF")])
    asset_id = db.load_universe(memory_db)[0]["id"]
    _seed_filing(
        memory_db,
        asset_id,
        "FY2021",
        {"2020-12-31 (FY)": 400_000_000.0, "2021-12-31 (FY)": 410_000_000.0},
    )
    bad_filing_id, bad_stmts = _seed_filing(
        memory_db,
        asset_id,
        "FY2022",
        {"2020-12-31 (FY)": 400.0, "2021-12-31 (FY)": 410_000_000.0},  # 2020 defective, 2021 fine
    )

    # Asking about the genuinely defective period still corrects it...
    defective = db.detect_share_scale_factors(
        memory_db, asset_id, bad_stmts, "2020-12-31 (FY)", exclude_filing_id=bad_filing_id
    )
    assert defective == {"diluted_shares": 1_000_000.0}

    # ...but asking about the clean period in the SAME filing must not borrow
    # that factor just because another period in the filing needed it.
    clean = db.detect_share_scale_factors(
        memory_db, asset_id, bad_stmts, "2021-12-31 (FY)", exclude_filing_id=bad_filing_id
    )
    assert clean == {}


def test_detect_share_scale_factors_catches_a_non_thousands_power_of_ten(
    memory_db: Database,
) -> None:
    """The candidate factor list must cover every power of ten (10x, 100x,
    ...), not just the 1e3/1e6/1e9 grouping MCD/WAT happened to show -- a
    code-review bot caught this gap in the original candidate list."""
    db.sync_universe(memory_db, [_company("GGG")])
    asset_id = db.load_universe(memory_db)[0]["id"]
    _seed_filing(memory_db, asset_id, "FY2022", {"2022-12-31 (FY)": 50_000_000.0})
    bad_filing_id, bad_stmts = _seed_filing(
        memory_db,
        asset_id,
        "FY2023",
        {"2022-12-31 (FY)": 500_000.0, "2023-12-31 (FY)": 510_000.0},  # both off by exactly 1e2
    )

    factors = db.detect_share_scale_factors(
        memory_db, asset_id, bad_stmts, "2023-12-31 (FY)", exclude_filing_id=bad_filing_id
    )

    assert factors == {"diluted_shares": 100.0}


def test_overlapping_history_excludes_the_filing_itself(memory_db: Database) -> None:
    """The current filing's own just-appended facts must not become their own
    anchor -- otherwise every filing would trivially "match" itself."""
    db.sync_universe(memory_db, [_company("CCC")])
    asset_id = db.load_universe(memory_db)[0]["id"]
    filing_id, _ = _seed_filing(memory_db, asset_id, "FY2023", {"2023-12-31 (FY)": 741.3})

    history = db.overlapping_history(
        memory_db,
        asset_id,
        (_DILUTED_CONCEPT,),
        ["2023-12-31 (FY)"],
        exclude_filing_id=filing_id,
    )

    assert history == {}
