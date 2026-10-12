"""Unit tests for ``scripts/verify_t148b.py`` helpers (T-148 part 2)."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

from conftest import seed_filing

from fundamental_agent import db
from fundamental_agent.statements import REGISTRY, Statements, iter_facts
from kg_schema import connect
from pricing_agent import db as pricing_db

_SPEC = importlib.util.spec_from_file_location(
    "verify_t148b", Path(__file__).resolve().parent.parent / "scripts" / "verify_t148b.py"
)
assert _SPEC and _SPEC.loader
verify = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_t148b"] = verify
_SPEC.loader.exec_module(verify)


def test_d02_revenue_condition_takes_clean_revenue_streams() -> None:
    """CPT-like statement: only contract revenue and operating lease income, no extra revenue row."""
    payload = {
        "income_statement": [
            {
                "concept": "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
                "label": "Management fees",
                "2025-01-01_2025-12-31": 100.0,
            },
            {
                "concept": "us-gaap_OperatingLeaseLeaseIncome",
                "label": "Rental revenue",
                "2025-01-01_2025-12-31": 900.0,
            },
            {
                "concept": "us-gaap_OperatingExpenses",
                "label": "Total operating expenses",
                "2025-01-01_2025-12-31": 600.0,
            },
        ],
        "balance_sheet": [],
        "cash_flow": [],
    }
    stmts = Statements.from_payload(payload)
    taken, sum_val, used, blocking = verify.check_d02_revenue(
        stmts, "2025-01-01_2025-12-31", REGISTRY["revenue"]
    )
    assert taken is True
    assert sum_val == 1000.0
    assert len(used) == 2
    assert len(blocking) == 0


def test_d02_revenue_condition_refuses_when_extra_revenue_row_present() -> None:
    """Extra revenue stream that reads as revenue blocks the sum under D-02."""
    payload = {
        "income_statement": [
            {
                "concept": "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
                "label": "Management fees",
                "2025-01-01_2025-12-31": 100.0,
            },
            {
                "concept": "us-gaap_OperatingLeaseLeaseIncome",
                "label": "Rental revenue",
                "2025-01-01_2025-12-31": 900.0,
            },
            {
                "concept": "custom_OtherRevenues",
                "label": "Other revenues",
                "2025-01-01_2025-12-31": 50.0,
            },
        ],
        "balance_sheet": [],
        "cash_flow": [],
    }
    stmts = Statements.from_payload(payload)
    taken, sum_val, _used, blocking = verify.check_d02_revenue(
        stmts, "2025-01-01_2025-12-31", REGISTRY["revenue"]
    )
    assert taken is False
    assert sum_val == 1000.0
    assert len(blocking) == 1
    assert blocking[0]["concept"] == "custom_OtherRevenues"


def test_d02_revenue_condition_does_not_block_on_statement_total_matching_sum() -> None:
    """Row matching sum within 0.5% is the statement's own total and does not block."""
    payload = {
        "income_statement": [
            {
                "concept": "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
                "label": "Management fees",
                "2025-01-01_2025-12-31": 100.0,
            },
            {
                "concept": "us-gaap_OperatingLeaseLeaseIncome",
                "label": "Rental revenue",
                "2025-01-01_2025-12-31": 900.0,
            },
            {
                "concept": "custom_TotalRevenues",
                "label": "Total revenues",
                "2025-01-01_2025-12-31": 1000.0,
            },
        ],
        "balance_sheet": [],
        "cash_flow": [],
    }
    stmts = Statements.from_payload(payload)
    taken, sum_val, _used, blocking = verify.check_d02_revenue(
        stmts, "2025-01-01_2025-12-31", REGISTRY["revenue"]
    )
    assert taken is True
    assert sum_val == 1000.0
    assert len(blocking) == 0


def test_k1_allowed_caption_cleaning() -> None:
    """(k)(1) caption cleaning removes note references and parentheticals."""
    raw = "Cash and cash equivalents (Note 3)"
    cleaned = verify.NOTE_RE.sub("", raw)
    cleaned = verify.PAREN_RE.sub("", cleaned).strip().lower()
    assert cleaned == "cash and cash equivalents"
    assert cleaned in verify.ALLOWED_K1_CAPTIONS


def test_cmd_recompute_on_tiny_database(aapl_10k: Statements, tmp_path: Path) -> None:
    """Hermetic test for verify_t148b.py recompute on a tiny database."""
    path = tmp_path / "test_recompute.db"
    conn = connect(path)
    db.ensure_schema(conn)
    pricing_db.ensure_schema(conn)
    conn.execute("INSERT INTO sectors (id, name) VALUES (1, 'Information Technology')")
    conn.execute(
        "INSERT INTO assets (id, ticker, cik, sector_id) VALUES (1, 'AAPL', '0000320193', 1)"
    )
    period = aapl_10k.latest_fy()
    assert period is not None
    filing_id = seed_filing(conn, 1, period_end=period.date, filing_date="2023-11-03")
    db.append_financial_facts(
        conn, filing_id, iter_facts(aapl_10k), filing_version="test", event_time=period.date
    )
    conn.execute(
        "INSERT INTO filing_cover_shares (filing_id, value, as_of_date, event_time, ingested_at) VALUES (1, 15000000000.0, '2023-10-20', '2023-09-30', '2023-11-03')"
    )
    conn.execute("INSERT INTO price_daily (asset_id, date, close) VALUES (1, '2023-09-29', 170.0)")
    conn.commit()
    conn.close()

    verify.cmd_recompute(argparse.Namespace(db=str(path), limit=None))

    check_conn = connect(path)
    metrics_count = check_conn.execute(
        "SELECT COUNT(*) FROM fundamental_metrics WHERE engine_version = 'metrics-v6'"
    ).fetchone()[0]
    assert metrics_count > 0

    snapshot_count = check_conn.execute(
        "SELECT COUNT(*) FROM score_snapshot WHERE score_type = 'FUNDAMENTAL'"
    ).fetchone()[0]
    assert snapshot_count == 1

    # Verify idempotence / re-run works and deletes previous rows cleanly
    verify.cmd_recompute(argparse.Namespace(db=str(path), limit=None))
    metrics_count_after = check_conn.execute(
        "SELECT COUNT(*) FROM fundamental_metrics WHERE engine_version = 'metrics-v6'"
    ).fetchone()[0]
    assert metrics_count_after == metrics_count
    check_conn.close()
