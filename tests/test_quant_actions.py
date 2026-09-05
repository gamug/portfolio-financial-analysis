"""Corporate-actions backfill: XBRL-derived fallback + gateway-probe failure."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from portfolio_common.db import Database

from quant.actions import (
    DERIVED_ENGINE_VERSION,
    backfill_corporate_actions,
    derive_corporate_actions_from_facts,
)
from quant.config import QuantSettings


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
