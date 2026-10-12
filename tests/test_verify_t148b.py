"""Unit tests for ``scripts/verify_t148b.py`` helpers (T-148 part 2)."""

from __future__ import annotations

import argparse
import importlib.util
import json
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
        "SELECT COUNT(*) FROM fundamental_metrics WHERE engine_version = ?",
        (db.METRICS_ENGINE_VERSION,),
    ).fetchone()[0]
    assert metrics_count > 0

    snapshot_count = check_conn.execute(
        "SELECT COUNT(*) FROM score_snapshot WHERE score_type = 'FUNDAMENTAL'"
    ).fetchone()[0]
    assert snapshot_count == 1

    # Verify idempotence / re-run works and deletes previous rows cleanly
    verify.cmd_recompute(argparse.Namespace(db=str(path), limit=None))
    metrics_count_after = check_conn.execute(
        "SELECT COUNT(*) FROM fundamental_metrics WHERE engine_version = ?",
        (db.METRICS_ENGINE_VERSION,),
    ).fetchone()[0]
    assert metrics_count_after == metrics_count
    check_conn.close()


def test_recompute_reads_quarter_column_on_10q(tmp_path: Path) -> None:
    """R1: on a 10-Q filing where instant, quarter and YTD share the period_end date,
    recompute must target the quarter column and resolve its flow items."""
    path = tmp_path / "test_10q_recompute.db"
    conn = connect(path)
    db.ensure_schema(conn)
    pricing_db.ensure_schema(conn)
    conn.execute("INSERT INTO sectors (id, name) VALUES (1, 'Information Technology')")
    conn.execute(
        "INSERT INTO assets (id, ticker, cik, sector_id) VALUES (1, 'TEST', '0001234567', 1)"
    )
    period_end = "2024-06-30"
    filing_id = seed_filing(
        conn, 1, form="10-Q", fiscal_period="Q2", period_end=period_end, filing_date="2024-07-20"
    )

    payload = {
        "balance_sheet": [
            {
                "concept": "us-gaap_Assets",
                "label": "Total assets",
                period_end: 1_000_000_000.0,
            }
        ],
        "income_statement": [
            {
                "concept": "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
                "label": "Revenue",
                f"{period_end} (Q2)": 250_000_000.0,
                f"{period_end} (YTD)": 480_000_000.0,
            },
            {
                "concept": "us-gaap_NetIncomeLoss",
                "label": "Net income",
                f"{period_end} (Q2)": 50_000_000.0,
                f"{period_end} (YTD)": 95_000_000.0,
            },
        ],
        "cash_flow": [],
    }
    stmts = Statements.from_payload(payload)
    db.append_financial_facts(
        conn, filing_id, iter_facts(stmts), filing_version="test", event_time=period_end
    )
    conn.execute(
        "INSERT INTO filing_cover_shares (filing_id, value, as_of_date, event_time, ingested_at) "
        "VALUES (1, 100000000.0, '2024-07-15', '2024-06-30', '2024-07-20')"
    )
    conn.execute("INSERT INTO price_daily (asset_id, date, close) VALUES (1, '2024-06-28', 50.0)")
    conn.commit()
    conn.close()

    verify.cmd_recompute(argparse.Namespace(db=str(path), limit=None))

    check_conn = connect(path)
    row = check_conn.execute(
        "SELECT value, inputs_json FROM fundamental_metrics WHERE filing_id = ? AND metric_name = 'net_margin'",
        (filing_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == 0.2  # 50M / 250M

    inputs = json.loads(row[1])
    assert inputs["revenue"] == 250_000_000.0
    assert inputs["net_income"] == 50_000_000.0
    check_conn.close()
