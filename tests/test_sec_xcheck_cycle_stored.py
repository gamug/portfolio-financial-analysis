"""``cycle_measures``, ``extra_measures``, ``marketcap_measures`` and ``stored_measures`` (T-147): the in-memory replay of
the cycle's gates and rules, DQ-01's identity tolerance, the approximate market cap and the stored-metric checks."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from portfolio_common.db import Database
from sec_xcheck import cycle_measures as cm
from sec_xcheck import extra_measures as em
from sec_xcheck import marketcap_measures as mc
from sec_xcheck import stored_measures as sm
from sec_xcheck.common import SecClient
from sec_xcheck_support import metric, rec

from cycle.scores.normalize import _winsorize

G = "us-gaap_"


# -- cycle replay ---------------------------------------------------------------------------------
def test_quarter_ends_are_inclusive_calendar_quarter_ends() -> None:
    assert cm.quarter_ends("2022-03-31", "2022-12-31") == [
        "2022-03-31",
        "2022-06-30",
        "2022-09-30",
        "2022-12-31",
    ]
    assert cm.quarter_ends("2022-04-01", "2022-09-29") == ["2022-06-30"]


def test_the_latest_annual_filing_is_chosen_by_availability_not_by_period_end() -> None:
    old = rec(cik="1", period_end="2022-12-31", available_at="2023-02-15")
    new = rec(cik="1", period_end="2023-12-31", available_at="2024-02-15")
    quarterly = rec(cik="1", form="10-Q", period_end="2024-03-31", available_at="2024-05-01")
    assert (
        cm.latest_annual([old, new, quarterly], "2024-01-31")["1"] is old
    )  # the FY2023 10-K is not public yet
    assert (
        cm.latest_annual([old, new, quarterly], "2024-06-30")["1"] is new
    )  # a 10-Q is never the annual basis
    assert cm.latest_annual([old, new], "2023-01-01") == {}


def _bank_metrics() -> dict[str, dict[str, object]]:
    return {
        "debt_to_equity": metric(0.5, "leverage", equity=100.0, total_debt=50.0),
        "free_cash_flow_margin": metric(-0.2, "cashflow", revenue=100.0, net_income=10.0),
        "net_margin": metric(
            0.1, "profitability", revenue=100.0, net_income=10.0, equity=100.0, total_assets=1000.0
        ),
    }


def test_the_replay_vetoes_a_negative_ttm_free_cash_flow_margin_even_for_a_financial_firm() -> None:
    bank = rec(ticker="BNK", cik="9", sector="Financials", metrics=_bank_metrics())
    out = cm.replay_cycle({"9": bank}, "2026-09-22")
    assert [(rule, sev) for _c, rule, sev, _e in out["hits"]] == [("NEGATIVE_FCF", "HARD")]


def test_a_missing_revenue_hard_vetoes_through_data_quality() -> None:
    """DQ_REVENUE_POS: net income reported and revenue missing quarantines the revenue ratios and vetoes the company --
    how ten Article 9 filers are excluded by accident."""
    no_revenue = rec(
        cik="9",
        metrics={
            "net_margin": metric(
                None, "profitability", net_income=10.0, equity=50.0, total_assets=500.0
            )
        },
    )
    hits = cm.replay_cycle({"9": no_revenue}, "2026-09-22")["hits"]
    assert [(r, s) for _c, r, s, _e in hits] == [("DATA_QUALITY", "HARD")]
    assert "DQ_REVENUE_POS" in hits[0][3]["gates"]


def test_negative_equity_quarantines_the_ratios_so_the_leverage_rules_never_see_them() -> None:
    """D-04: the negative-equity branch of LEVERAGE_EXTREME reads a debt/equity that the gate has already nulled."""
    r = rec(
        cik="9",
        items={"equity": -10.0},
        metrics={
            "debt_to_equity": metric(-3.0, "leverage", equity=-10.0, total_debt=30.0, cash=0.0),
            "net_debt_to_ebitda": metric(
                8.0,
                "leverage",
                total_debt=30.0,
                cash=0.0,
                operating_income=3.0,
                depreciation_amortization=1.0,
            ),
            "interest_coverage": metric(1.0, "leverage"),
        },
    )
    out = cm.replay_cycle({"9": r}, "2026-09-22")
    assert out["negative_equity"] == {"9"}
    assert not [h for h in out["hits"] if h[1] == "LEVERAGE_EXTREME"]


def test_distress_conditions_use_the_gates_formulas_and_drop_without_condition_2() -> None:
    burning = rec(
        items={"equity": -5.0},
        metrics={
            "net_debt_to_ebitda": metric(
                -399.0,
                "leverage",
                total_debt=100.0,
                cash=10.0,
                operating_income=-20.0,
                depreciation_amortization=9.0,
            )
        },
    )
    c = cm.distress_conditions(burning)
    assert c["c2_ebitda_nonpositive_with_net_debt"] and not c["c1_ndte_above_5"]
    table = cm.negative_equity_table({"1": burning})
    assert table[0]["flagged_with_c2"] and not table[0]["flagged_without_c2"]
    healthy = rec(
        items={"equity": -5.0},
        metrics={
            "net_debt_to_ebitda": metric(
                2.0,
                "leverage",
                total_debt=20.0,
                cash=0.0,
                operating_income=9.0,
                depreciation_amortization=1.0,
            ),
            "interest_coverage": metric(6.0, "leverage"),
        },
    )
    assert not cm.negative_equity_table({"1": healthy})[0]["flagged_with_c2"]


# -- DQ-01 identities --------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("diff", "base", "expected"),
    [
        (500_000.0, 1e12, "pass"),
        (5_000_000.0, 1e12, "warn"),
        (6e9, 1e12, "block"),
        (2e6, 1e8, "block"),
    ],
)
def test_severity_follows_dq01(diff: float, base: float, expected: str) -> None:
    assert em.severity(diff, base) == expected


def test_a_balance_sheet_that_does_not_balance_is_a_block() -> None:
    assert (
        em.identity_result(
            {G + "Assets": 1000e6, G + "LiabilitiesAndStockholdersEquity": 1000e6}, "CON-01"
        )
        == "pass"
    )
    assert (
        em.identity_result(
            {G + "Assets": 1000e6, G + "LiabilitiesAndStockholdersEquity": 900e6}, "CON-01"
        )
        == "block"
    )
    assert (
        em.identity_result({G + "Assets": 1000e6}, "CON-01") is None
    )  # an operand not filed: not tested


def test_con19_accepts_the_displayed_sign_of_the_nci_line() -> None:
    base = {G + "ProfitLoss": 110e6, G + "NetIncomeLoss": 100e6}
    assert (
        em.identity_result(
            {**base, G + "NetIncomeLossAttributableToNoncontrollingInterest": 10e6}, "CON-19"
        )
        == "pass"
    )
    assert (
        em.identity_result(
            {**base, G + "NetIncomeLossAttributableToNoncontrollingInterest": -10e6}, "CON-19"
        )
        == "pass"
    )


def test_con08_reads_total_equals_continuing_when_no_discontinued_line_is_filed() -> None:
    ocf, cont = (
        G + "NetCashProvidedByUsedInOperatingActivities",
        G + "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    )
    assert em.identity_result({ocf: 500e6, cont: 500e6}, "CON-08") == "pass"
    assert em.identity_result({ocf: 500e6, cont: 400e6}, "CON-08") == "block"


def test_winsorizing_at_two_percent_and_a_half_percent_trim_one_value_per_tail_below_100_assets() -> (
    None
):
    """N17: ``k = max(1, int(n x frac))`` is 1 for both fractions at n = 20 (5% per tail), so they are identical there."""
    twenty = [float(i) for i in range(20)] + [1000.0]
    assert _winsorize(twenty, 0.02) == _winsorize(twenty, 0.005)
    assert em.winsor_effect(twenty) == (0, 0.0)
    assert (
        sorted(set(_winsorize(twenty, 0.02)))[-1] == 19.0
    )  # one value per tail, whatever the fraction
    large = [float(i) for i in range(1000)]
    assert _winsorize(large, 0.02) != _winsorize(large, 0.005)  # 20 values per tail against 5


# -- market cap ----------------------------------------------------------------------------------------
def _facts(tmp_path: Path, rows: list[tuple[str, str, float]]) -> SecClient:
    path = tmp_path / "companyfacts" / "CIK0000000001.json"
    path.parent.mkdir(parents=True)
    units = [{"end": e, "val": v, "filed": f, "accn": "x"} for f, e, v in rows]
    path.write_text(
        json.dumps(
            {"facts": {"dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": units}}}}}
        )
    )
    return SecClient(tmp_path, offline=True)


def test_cover_shares_take_the_latest_count_filed_on_or_before_the_date(tmp_path: Path) -> None:
    client = _facts(
        tmp_path,
        [
            ("2026-04-29", "2026-04-17", 100.0),
            ("2026-07-28", "2026-07-17", 90.0),
            ("2026-10-05", "2026-09-30", 80.0),
        ],
    )
    assert mc.cover_shares(client, "1", "2026-09-22") == (
        90.0,
        "2026-07-17",
    )  # the October count was not yet public
    assert mc.cover_shares(client, "1", "2026-01-01") is None


def test_a_stale_cover_count_is_not_used(tmp_path: Path) -> None:
    client = _facts(tmp_path, [("2025-01-10", "2025-01-05", 100.0)])
    assert mc.cover_shares(client, "1", "2026-09-22") is None


def test_the_financial_share_is_the_cap_of_financial_types_over_the_covered_total() -> None:
    caps = {
        "1": {"cap": 300.0, "tickers": ["JPM"]},
        "2": {"cap": 700.0, "tickers": ["XOM"]},
        "3": {"cap": None, "tickers": ["BRK.B"], "reason": "x"},
    }
    types = {
        "1": {"type": "article_9"},
        "2": {"type": "operating"},
        "3": {"type": "multi_sector_holding"},
    }
    found = {f.key: f for f in mc.share_by_type(caps, types, "2026-09-22")}
    assert found["D07.financial_firms_cap_share"].count == 3000  # 30.00% in basis points
    assert found["D07.cap_coverage"].count == 2 and found["D07.cap_coverage"].total == 3


# -- stored metrics ---------------------------------------------------------------------------------------
@pytest.fixture
def stored_db(memory_db: Database) -> Database:
    memory_db.execute("INSERT INTO sectors (id, name) VALUES (1, 'Consumer Discretionary')")
    memory_db.execute(
        "INSERT INTO assets (id, ticker, cik, sector_id) VALUES (1, 'MCD', '63908', 1)"
    )
    fid = memory_db.execute(
        "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, period_end, filing_date, available_at, "
        "accession_number, retrieved_at) VALUES (1, '10-Q', 2023, '2023Q2', '2023-06-30', '2023-08-02', '2023-08-03', 'acc-1', 'now') RETURNING id"
    ).fetchone()[0]
    inputs = {
        "shares": 7.1e8,
        "share_price": 280.0,
        "market_capitalization": 1.99e11,
        "shares_are_diluted_average": 1.0,
        "shares_scale_correction_factor": 1000000.0,
        "price_date_offset_days": 0.0,
    }
    for name, value, payload in (
        ("market_capitalization", 1.99e11, inputs),
        ("debt_to_assets", 0.7, {"total_assets": 56e9}),
    ):
        memory_db.execute(
            "INSERT INTO fundamental_metrics (filing_id, metric_group, metric_name, value, inputs_json, computed_at, "
            "engine_version, event_time, available_at) VALUES (?, 'g', ?, ?, ?, 'now', 'metrics-v2', '2023-06-30', '2023-08-03')",
            (fid, name, value, json.dumps(payload)),
        )
    return memory_db


def test_the_diluted_average_fallback_and_the_corrector_are_counted(stored_db: Database) -> None:
    found = {f.key: f for f in sm.m_valuation(stored_db, "t")}
    assert found["MKT04.diluted_average_fallback"].count == 1
    assert found["MKT04.shares_not_from_cover_page"].count == 1
    assert found["ID18.scale_corrector_applied"].count == 1


def test_a03_catches_the_uncorrected_share_scale_error(stored_db: Database) -> None:
    """MCD's 10^6 error: the uncorrected market cap is far above 100x total assets, so Annex A-03 flags it with no corrector."""
    found = {f.key: f for f in sm.m_valuation(stored_db, "t")}
    assert found["ID18.a03_would_catch_without_corrector"].count == 1


def test_availability_dates_are_checked_and_the_database_refuses_a_wrong_one(
    stored_db: Database,
) -> None:
    found = {f.key: f for f in sm.m_availability(stored_db, "t")}
    assert found["PER01.metrics_available_at_null"].count == 0
    assert found["PER01.metrics_available_at_not_after_period_end"].count == 0
    # PER-01 is also enforced in the schema: a ratio cannot carry a date other than its filing's
    with pytest.raises(sqlite3.IntegrityError, match="available_at must be its filing"):
        stored_db.execute("UPDATE fundamental_metrics SET available_at = '2023-06-30'")
