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


# -- T-103: history is point in time -------------------------------------------------------------


def _mcd_run(conn: Database) -> tuple[int, int, Statements]:
    """MCD's real shape (docs/model_fixes.md, T-103): FY2022 reported in whole shares; FY2023
    and FY2024 both report diluted shares in millions, and FY2024 -- filed a year *later* --
    restates FY2023 with the same defect. Returns (asset, FY2023 filing, its statements)."""
    db.sync_universe(conn, [_company("MCD")])
    asset_id = int(db.load_universe(conn)[0]["id"])
    _seed_filing(
        conn,
        asset_id,
        "FY2022",
        {"2021-12-31 (FY)": 751_800_000.0, "2022-12-31 (FY)": 741_300_000.0},
    )
    fy2023, stmts = _seed_filing_with_eps(
        conn,
        asset_id,
        "FY2023",
        diluted_shares={"2022-12-31 (FY)": 741.3, "2023-12-31 (FY)": 732.3},
        net_income={"2022-12-31 (FY)": 6_177_400_000.0, "2023-12-31 (FY)": 8_468_800_000.0},
        eps_diluted={"2022-12-31 (FY)": 8.33, "2023-12-31 (FY)": 11.56},
    )
    _seed_filing(  # filed 2025-03-01: after FY2023's 2024-03-01
        conn, asset_id, "FY2024", {"2023-12-31 (FY)": 732.3, "2024-12-31 (FY)": 721.9}
    )
    return asset_id, fy2023, stmts


def test_a_later_filing_s_mis_scaled_restatement_does_not_veto_the_correction(
    memory_db: Database,
) -> None:
    """Before T-103 the FY2024 filing's 732.3 for FY2023 was read as first-hand proof that
    FY2023 was clean, overriding both the FY2022 history and the EPS signal (1e6)."""
    asset_id, fy2023, stmts = _mcd_run(memory_db)
    factors = db.detect_share_scale_factors(
        memory_db, asset_id, stmts, "2023-12-31 (FY)", exclude_filing_id=fy2023
    )
    assert factors == {"diluted_shares": 1_000_000.0}


def test_overlapping_history_only_reads_filings_filed_before_this_one(
    memory_db: Database,
) -> None:
    asset_id, fy2023, _ = _mcd_run(memory_db)
    history = db.overlapping_history(
        memory_db,
        asset_id,
        (_DILUTED_CONCEPT,),
        ["2021-12-31 (FY)", "2022-12-31 (FY)", "2023-12-31 (FY)"],
        exclude_filing_id=fy2023,
    )
    assert history == {"2021-12-31 (FY)": 751_800_000.0, "2022-12-31 (FY)": 741_300_000.0}


def test_an_undated_filing_keeps_the_undated_behaviour_and_undated_history_is_skipped(
    memory_db: Database,
) -> None:
    asset_id, fy2023, _ = _mcd_run(memory_db)
    keys = ["2023-12-31 (FY)"]
    memory_db.execute(
        "UPDATE sec_filings SET filing_date = NULL, available_at = NULL WHERE id = ?", (fy2023,)
    )
    # the current filing has no date: every other filing counts, as before T-103
    assert db.overlapping_history(
        memory_db, asset_id, (_DILUTED_CONCEPT,), keys, exclude_filing_id=fy2023
    ) == {"2023-12-31 (FY)": 732.3}
    # a dated current filing never counts an undated one: it cannot be proven earlier
    memory_db.execute(
        "UPDATE sec_filings SET filing_date = '2024-03-01', available_at = '2024-03-04' "
        "WHERE id = ?",
        (fy2023,),
    )
    memory_db.execute(
        "UPDATE sec_filings SET filing_date = NULL, available_at = NULL "
        "WHERE fiscal_period = 'FY2022'"
    )
    assert (
        db.overlapping_history(
            memory_db, asset_id, (_DILUTED_CONCEPT,), ["2022-12-31 (FY)"], exclude_filing_id=fy2023
        )
        == {}
    )


# -- T-103: the EPS signal's guards and the cross-item check -------------------------------------


def _row(concept: str, **periods: float) -> dict[str, Any]:
    return {"concept": concept, "label": concept, "abstract": False, "dimension": False, **periods}


def _one_filing(  # noqa: PLR0913 - one filing's share facts, all keyword-only
    conn: Database,
    ticker: str,
    fiscal_year: int,
    *,
    diluted: float | None = None,
    outstanding: float | None = None,
    net_income: float | None = None,
    to_common: float | None = None,
    eps: float | None = None,
) -> tuple[int, int, Statements]:
    """A 10-K whose own period is FY*fiscal_year*, carrying only the facts given."""
    db.sync_universe(conn, [_company(ticker)])
    asset_id = int(conn.execute("SELECT id FROM assets WHERE ticker = ?", (ticker,)).fetchone()[0])
    key, instant = f"{fiscal_year}-12-31 (FY)", f"{fiscal_year}-12-31"
    income = [
        _row(concept, **{key: value})
        for concept, value in (
            (_DILUTED_CONCEPT, diluted),
            ("us-gaap_NetIncomeLoss", net_income),
            ("us-gaap_NetIncomeLossAvailableToCommonStockholdersDiluted", to_common),
            ("us-gaap_EarningsPerShareDiluted", eps),
        )
        if value is not None
    ]
    balance = (
        [_row("us-gaap_CommonStockSharesOutstanding", **{instant: outstanding})]
        if outstanding is not None
        else []
    )
    filing_id = db.upsert_filing(
        conn,
        FilingKey(asset_id, "10-K", fiscal_year, f"FY{fiscal_year}"),
        FilingMeta(filing_date=f"{fiscal_year + 1}-03-01", period_end=instant),
    )
    stmts = Statements.from_payload(
        {"income_statement": income, "balance_sheet": balance, "cash_flow": []}
    )
    db.append_financial_facts(conn, filing_id, iter_facts(stmts))
    return asset_id, filing_id, stmts


def _factors(conn: Database, asset_id: int, filing_id: int, stmts: Statements, year: int) -> Any:
    return db.detect_share_scale_factors(
        conn, asset_id, stmts, f"{year}-12-31 (FY)", exclude_filing_id=filing_id
    )


def test_eps_is_net_income_available_to_common_over_shares(memory_db: Database) -> None:
    """ALL 2023Q3's shape: preferred dividends ($36M) sit between net income and the -$41M
    available to common that EPS (-$0.16) divides. Plain net income / EPS gave ALL 25M shares
    against 261.8M -- a false x0.1; available to common gives 256M, which agrees."""
    args = _one_filing(
        memory_db, "ALL", 2023, diluted=261_800_000.0, net_income=-4e6, to_common=-41e6, eps=-0.16
    )
    assert _factors(memory_db, *args, 2023) == {}


def test_a_cent_sized_eps_is_not_used(memory_db: Database) -> None:
    """MCHP FY2025's shape: -$0.5M / -$0.01 = 50M against 537.3M looks like x0.1, but the
    EPS is rounded to the cent -- its own error is far beyond the tolerance."""
    args = _one_filing(memory_db, "MCHP", 2025, diluted=537_300_000.0, net_income=-5e5, eps=-0.01)
    assert _factors(memory_db, *args, 2025) == {}


def test_an_eps_that_is_itself_mis_scaled_is_not_used(memory_db: Database) -> None:
    """HAL 2022Q3's shape: EPS $0.60 tagged as 600,000, so net income / EPS = 907 shares."""
    args = _one_filing(memory_db, "HAL", 2022, diluted=910_000_000.0, net_income=544e6, eps=6e5)
    assert _factors(memory_db, *args, 2022) == {}


def test_outstanding_is_corrected_against_an_eps_confirmed_diluted_count(
    memory_db: Database,
) -> None:
    """RTX FY2021's shape: outstanding tagged in thousands (1,708,065), diluted 1,508.5M
    confirmed by EPS -- the old code only caught it by reading a *later* filing."""
    args = _one_filing(
        memory_db,
        "RTX",
        2021,
        diluted=1_508_500_000.0,
        outstanding=1_708_065.0,
        net_income=3_864_000_000.0,
        eps=2.56,
    )
    assert _factors(memory_db, *args, 2021) == {"shares_outstanding": 1_000.0}


def test_a_clean_outstanding_is_not_corrected_from_a_mis_scaled_earlier_filing(
    memory_db: Database,
) -> None:
    """RTX FY2022's shape: the only earlier report of 2021-12-31 is FY2021's defective
    1,708,065, which alone would say x0.001 -- the EPS-confirmed diluted count says the
    current 1,711M is right."""
    _one_filing(memory_db, "RTX", 2021, outstanding=1_708_065.0)
    asset_id, filing_id, _ = _one_filing(
        memory_db,
        "RTX",
        2022,
        diluted=1_485_900_000.0,
        net_income=5_197_000_000.0,
        eps=3.50,
    )
    payload = {
        "income_statement": [
            _row(_DILUTED_CONCEPT, **{"2022-12-31 (FY)": 1_485_900_000.0}),
            _row("us-gaap_NetIncomeLoss", **{"2022-12-31 (FY)": 5_197_000_000.0}),
            _row("us-gaap_EarningsPerShareDiluted", **{"2022-12-31 (FY)": 3.50}),
        ],
        "balance_sheet": [
            _row(
                "us-gaap_CommonStockSharesOutstanding",
                **{"2022-12-31": 1_710_960_000.0, "2021-12-31": 1_708_065_000.0},
            )
        ],
        "cash_flow": [],
    }
    stmts = Statements.from_payload(payload)
    db.append_financial_facts(memory_db, filing_id, iter_facts(stmts))
    assert _factors(memory_db, asset_id, filing_id, stmts, 2022) == {}


def test_a_different_share_concept_is_not_snapped_to_another_decade(memory_db: Database) -> None:
    """NVR's shape: 20.6M shares *issued* against ~3.5M diluted is a 5.9x gap -- another
    concept, not a scale defect -- so the cross-item check has no opinion."""
    args = _one_filing(
        memory_db,
        "NVR",
        2022,
        diluted=3_508_524.0,
        outstanding=20_555_330.0,
        net_income=1_725_600_000.0,
        eps=491.82,
    )
    assert _factors(memory_db, *args, 2022) == {}


def test_magnitude_offset() -> None:
    assert db._magnitude_offset(1_708_065.0, 1_508_500_000.0) == 1_000.0
    assert db._magnitude_offset(1_710_960_000.0, 1_485_900_000.0) == 1.0
    assert db._magnitude_offset(20_555_330.0, 3_508_524.0) is None  # 5.9x: between decades
    assert db._magnitude_offset(None, 1.0) is None
    assert db._magnitude_offset(0.0, 1.0) is None
