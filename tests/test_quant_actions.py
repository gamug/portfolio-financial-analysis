"""Corporate-actions backfill: XBRL-derived fallback + gateway-probe failure."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from portfolio_common.db import Database

from quant.actions import (
    DERIVED_10Q_ENGINE_VERSION,
    DERIVED_ENGINE_VERSION,
    backfill_corporate_actions,
    derive_corporate_actions_from_facts,
    derive_quarterly_dividends_from_10q_ytd,
)
from quant.config import QuantSettings
from quant.db import load_actions


def _settings() -> QuantSettings:
    return QuantSettings(db_path=Path(":memory:"))


def test_derive_uses_aggregate_payments_when_no_per_share_fact(
    memory_quant_db: Database,
) -> None:
    conn = memory_quant_db
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'XYZ')")
    fid = conn.execute(
        "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, period_end, "
        "retrieved_at) VALUES (1, '10-K', 2024, 'FY', '2024-10-31', '2024-11-01T00:00:00Z') "
        "RETURNING id"
    ).fetchone()["id"]
    # cash-flow line is a negative outflow; shares is a weighted-average duration fact
    conn.execute(
        "INSERT INTO financial_facts (filing_id, statement, concept, period_key, value, event_time) "
        "VALUES (?, 'cash_flow', 'us-gaap_PaymentsOfDividendsCommonStock', '2024-10-31 (FY)', "
        "-250000000.0, '2024-10-31')",
        (fid,),
    )
    conn.execute(
        "INSERT INTO financial_facts (filing_id, statement, concept, period_key, value, event_time) "
        "VALUES (?, 'income_statement', "
        "'us-gaap_WeightedAverageNumberOfDilutedSharesOutstanding', '2024-10-31 (FY)', "
        "500000000.0, '2024-10-31')",
        (fid,),
    )
    conn.commit()

    rows = derive_corporate_actions_from_facts(conn, 1)
    assert len(rows) == 4
    assert sum(r.value for r in rows) == pytest.approx(0.50, abs=1e-6)  # 250M / 500M


def test_derive_spreads_fy_dps_into_four_quarters(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=2, n_days=260, with_dividends=True)
    # asset 1 seeded with DPS = 1.20 + 0.1*1 = 1.30
    rows = derive_corporate_actions_from_facts(conn, 1)
    assert len(rows) == 4
    assert all(r.action_type == "DIVIDEND" and r.frequency == "quarterly" for r in rows)
    assert sum(r.value for r in rows) == pytest.approx(1.30, abs=1e-6)
    assert rows == sorted(rows, key=lambda r: r.ex_date)  # ascending ex-dates


def test_backfill_derive_writes_rows_and_completes_run(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=3, n_days=260)
    report = backfill_corporate_actions(
        _settings(), date_from="2024-01-01", date_to="2024-12-31", source="derive", conn=conn
    )
    assert report.source == "derive"
    assert report.engine_version == DERIVED_ENGINE_VERSION
    assert report.dividends == 12  # 3 assets * 4 quarters
    n = conn.execute(
        "SELECT COUNT(*) FROM corporate_action WHERE engine_version = ?", (DERIVED_ENGINE_VERSION,)
    ).fetchone()[0]
    assert n == 12
    assert conn.execute("SELECT status FROM quant_run ORDER BY id DESC LIMIT 1").fetchone()[0] == (
        "completed"
    )

    # re-run is a no-op (INSERT OR IGNORE on the versioned key)
    again = backfill_corporate_actions(
        _settings(), date_from="2024-01-01", date_to="2024-12-31", source="derive", conn=conn
    )
    assert again.inserted == 0


def test_gateway_probe_failure_falls_back_to_derive(
    memory_quant_db: Database,
    quant_seed: Callable[..., Database],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=2, n_days=260)
    monkeypatch.setattr("quant.pricing_client.QuantPricingClient.probe", lambda self, t: False)
    report = backfill_corporate_actions(
        _settings(), date_from="2024-01-01", date_to="2024-12-31", source="gateway", conn=conn
    )
    assert report.gateway_probe_failed is True
    assert report.source == "derive"
    assert report.engine_version == DERIVED_ENGINE_VERSION
    assert report.inserted == 8  # 2 assets * 4 quarters


def _seed_10q(  # noqa: PLR0913 - keyword-only fixture-shape fields
    conn: Database,
    asset_id: int,
    fiscal_period: str,
    period_end: str,
    concept: str,
    *,
    tag: str,
    value: float,
) -> None:
    """A single 10-Q filing carrying one dividend-related concept fact,
    mirroring the real EDGAR-gateway shape (Q3, docs/model_fixes.md)."""
    year = int(fiscal_period[:4])
    fid = conn.execute(
        "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, period_end, "
        "retrieved_at) VALUES (?, '10-Q', ?, ?, ?, ?) RETURNING id",
        (asset_id, year, fiscal_period, period_end, period_end + "T00:00:00Z"),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO financial_facts (filing_id, statement, concept, period_key, value, "
        "event_time) VALUES (?, 'income_statement', ?, ?, ?, ?)",
        (fid, concept, f"{period_end} ({tag})", value, period_end),
    )
    conn.commit()


def test_derive_from_10q_ytd_differences_a_flat_quarterly_dividend(
    memory_quant_db: Database,
) -> None:
    """Q3 (docs/model_fixes.md): a filer tagging dividends-per-share
    cumulatively (YTD) across three 10-Qs of the same fiscal year -- each
    quarter's dividend is the difference from the prior quarter's YTD."""
    conn = memory_quant_db
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'XOM')")
    concept = "us-gaap_CommonStockDividendsPerShareDeclared"
    _seed_10q(conn, 1, "2024Q1", "2024-03-31", concept, tag="YTD", value=0.30)
    _seed_10q(conn, 1, "2024Q2", "2024-06-30", concept, tag="YTD", value=0.60)
    _seed_10q(conn, 1, "2024Q3", "2024-09-30", concept, tag="YTD", value=0.90)

    rows = derive_quarterly_dividends_from_10q_ytd(conn, 1)

    assert [r.ex_date for r in rows] == ["2024-03-31", "2024-06-30", "2024-09-30"]
    assert [r.value for r in rows] == pytest.approx([0.30, 0.30, 0.30])
    assert all(r.frequency == "quarterly" for r in rows)


def test_derive_from_10q_prefers_a_discrete_quarter_tag_over_ytd(
    memory_quant_db: Database,
) -> None:
    """A filer tagging the discrete quarter directly needs no differencing."""
    conn = memory_quant_db
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'PG')")
    concept = "us-gaap_CommonStockDividendsPerShareDeclared"
    _seed_10q(conn, 1, "2024Q2", "2024-06-30", concept, tag="Q2", value=0.45)

    rows = derive_quarterly_dividends_from_10q_ytd(conn, 1)

    assert len(rows) == 1
    assert rows[0].value == pytest.approx(0.45)
    assert rows[0].ex_date == "2024-06-30"


def test_derive_from_10q_ytd_falls_back_to_aggregate_paid_over_shares(
    memory_quant_db: Database,
) -> None:
    """No per-share fact at all -- aggregate dividends paid / a YTD share
    count, same fallback shape as the FY-level derivation."""
    conn = memory_quant_db
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'T')")
    fid = conn.execute(
        "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, period_end, "
        "retrieved_at) VALUES (1, '10-Q', 2024, '2024Q1', '2024-03-31', "
        "'2024-04-01T00:00:00Z') RETURNING id"
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO financial_facts (filing_id, statement, concept, period_key, value, "
        "event_time) VALUES (?, 'cash_flow', 'us-gaap_PaymentsOfDividendsCommonStock', "
        "'2024-03-31 (YTD)', -100000000.0, '2024-03-31')",
        (fid,),
    )
    conn.execute(
        "INSERT INTO financial_facts (filing_id, statement, concept, period_key, value, "
        "event_time) VALUES (?, 'income_statement', "
        "'us-gaap_WeightedAverageNumberOfDilutedSharesOutstanding', '2024-03-31 (YTD)', "
        "500000000.0, '2024-03-31')",
        (fid,),
    )
    conn.commit()

    rows = derive_quarterly_dividends_from_10q_ytd(conn, 1)

    assert len(rows) == 1
    assert rows[0].value == pytest.approx(0.20)  # 100M / 500M


def test_load_actions_prefers_a_single_engine_never_blends_two(
    memory_quant_db: Database,
) -> None:
    """Q3 (docs/model_fixes.md): corpact-v0-approx and corpact-v1-derived
    write different synthetic ex-dates for the same asset -- load_actions
    must pick the higher-priority engine outright, not merge both sets of
    ex-dates (which would double-count the dividend)."""
    conn = memory_quant_db
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'NEE')")
    conn.execute(
        "INSERT INTO corporate_action (asset_id, action_type, ex_date, value, source, "
        "engine_version, ingested_at) VALUES (1, 'DIVIDEND', '2024-01-15', 0.25, 'test', "
        "'corpact-v0-approx', '2024-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO corporate_action (asset_id, action_type, ex_date, value, source, "
        "engine_version, ingested_at) VALUES (1, 'DIVIDEND', '2024-03-31', 0.30, 'test', "
        "'corpact-v1-derived', '2024-02-01T00:00:00Z')"
    )
    conn.commit()

    result = load_actions(conn, 1, "DIVIDEND", start="2024-01-01", end="2024-12-31")

    assert result == {"2024-03-31": 0.30}  # v1-derived only, v0-approx's ex-date excluded


def test_backfill_derive_writes_both_engines_for_a_10q_only_asset(
    memory_quant_db: Database,
) -> None:
    """Q3 acceptance criterion: an asset with only 10-Q dividend facts (no
    10-K DPS at all -- the XOM/PG/T/NEE shape) gets usable, non-zero
    dividends from the derive path via load_actions."""
    conn = memory_quant_db
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'NEE')")
    conn.execute(
        "INSERT INTO universe_membership (asset_id, universe, valid_from, detected_at, "
        "source) VALUES (1, 'SP500', '2024-01-01', '2024-01-01T00:00:00Z', 'test')"
    )
    concept = "us-gaap_CommonStockDividendsPerShareDeclared"
    _seed_10q(conn, 1, "2024Q1", "2024-03-31", concept, tag="Q1", value=0.40)

    report = backfill_corporate_actions(
        _settings(), date_from="2024-01-01", date_to="2024-12-31", source="derive", conn=conn
    )

    assert report.dividends == 1
    n = conn.execute(
        "SELECT COUNT(*) FROM corporate_action WHERE engine_version = ?",
        (DERIVED_10Q_ENGINE_VERSION,),
    ).fetchone()[0]
    assert n == 1
    result = load_actions(conn, 1, "DIVIDEND", start="2024-01-01", end="2024-12-31")
    assert result == pytest.approx({"2024-03-31": 0.40})
