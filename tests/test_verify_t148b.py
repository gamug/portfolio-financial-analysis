"""Unit tests for ``scripts/verify_t148b.py`` helpers (T-148 part 2)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from fundamental_agent.statements import REGISTRY, Statements

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
