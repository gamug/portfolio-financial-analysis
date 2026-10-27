"""Audit (reviewer B): the sector review's verified debt lines added to the total debt (MET-07)."""

from __future__ import annotations

from typing import Any

from fundamental_agent.statements import Statements

G = "us-gaap_"
PE = "2024-12-31"


def _row(concept: str, label: str, value: float) -> dict[str, Any]:
    return {
        "concept": concept,
        "label": label,
        "standard_concept": None,
        "abstract": False,
        "dimension": False,
        PE: value,
    }


def _stmts(debt_rows: list[dict[str, Any]], add: list[str]) -> Statements:
    balance = [
        _row(G + "Assets", "Assets", 100_000.0),
        _row(G + "StockholdersEquity", "Equity", 30_000.0),
        _row(G + "CashAndCashEquivalentsAtCarryingValue", "Cash", 2_000.0),
        *debt_rows,
    ]
    return Statements.from_payload(
        {
            "income_statement": [],
            "balance_sheet": balance,
            "cash_flow": [],
            "_audit_debt_add": add,
        }
    )


def test_the_override_keeps_the_short_term_line_the_reading_refused() -> None:
    # DE FY2024: "Short-term borrowings" (DebtCurrent) resolves, the long-term line is an extension
    # the registry does not read -> the reading refuses (short-term only); the verified long-term
    # line completes it, it does not replace it
    stmts = _stmts(
        [
            _row(G + "DebtCurrent", "Short-term borrowings", 13_533.0),
            _row(G + "SecuredDebt", "Short-term securitization borrowings", 8_431.0),
            _row("de_LongTermDebtAndFinanceLeasesNoncurrent", "Long-term borrowings", 43_229.0),
        ],
        ["de_LongTermDebtAndFinanceLeasesNoncurrent", G + "SecuredDebt"],
    )
    r = stmts.total_debt(PE)
    assert r.value == 13_533.0 + 8_431.0 + 43_229.0
    assert r.concept is not None and r.concept.startswith(G + "DebtCurrent")
    assert "audit_debt_lines_added" in r.flags
    assert "capture_failure" not in r.flags and "short_term_debt_only" not in r.flags


def test_a_combined_debt_and_lease_line_is_flagged() -> None:
    # TSLA: the filer's only debt lines are its own "debt and finance leases" elements (MET-07)
    stmts = _stmts(
        [
            _row("tsla_LongTermDebtAndFinanceLeasesCurrent", "Current portion", 1_589.0),
            _row("tsla_LongTermDebtAndFinanceLeasesNoncurrent", "Debt and finance leases", 5_245.0),
        ],
        ["tsla_LongTermDebtAndFinanceLeasesCurrent", "tsla_LongTermDebtAndFinanceLeasesNoncurrent"],
    )
    r = stmts.total_debt(PE)
    assert r.value == 1_589.0 + 5_245.0
    assert "includes_finance_leases" in r.flags


def test_a_debt_override_without_a_lease_line_is_not_flagged_as_leases() -> None:
    # AES: the non-recourse non-current line, labelled "... related to variable interest entities"
    stmts = _stmts(
        [
            _row(G + "LongTermDebtNoncurrent", "Recourse debt", 3_729.0),
            _row("aes_NonRecourseDebtNonCurrent", "Non-recourse debt", 13_603.0),
        ],
        ["aes_NonRecourseDebtNonCurrent"],
    )
    r = stmts.total_debt(PE)
    assert r.value == 3_729.0 + 13_603.0
    assert "includes_finance_leases" not in r.flags


def test_a_face_cell_the_gateway_emptied_is_restored_from_the_instance() -> None:
    # AES 2025-Q3: the gateway emptied ShortTermBorrowings and LongTermDebt (dimensional only, T-042); the
    # face's recourse + non-recourse members restore them (F-A15)
    debt = [
        _row(G + "ShortTermBorrowings", "Recourse debt", 0.0),
        _row(G + "LongTermDebt", "Non-recourse debt", 0.0),
    ]
    for r in debt:
        r[PE] = None
    stmts = Statements.from_payload(
        {
            "income_statement": [],
            "balance_sheet": [
                _row(G + "Assets", "Assets", 100_000.0),
                _row(G + "StockholdersEquity", "Equity", 30_000.0),
                _row(G + "CashAndCashEquivalentsAtCarryingValue", "Cash", 2_000.0),
                *debt,
            ],
            "cash_flow": [],
            "_audit_face_cells": [
                {
                    "statement": "balance_sheet",
                    "concept": G + "ShortTermBorrowings",
                    "column": PE,
                    "value": 4_386.0,
                },
                {
                    "statement": "balance_sheet",
                    "concept": G + "LongTermDebt",
                    "column": PE,
                    "value": 26_463.0,
                },
                # a valued cell is never overwritten
                {"statement": "balance_sheet", "concept": G + "Assets", "column": PE, "value": 1.0},
            ],
        }
    )
    assert stmts.total_debt(PE).value == 4_386.0 + 26_463.0
    assert stmts.resolve("total_assets", PE).value == 100_000.0
