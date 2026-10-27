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


def test_a_restored_row_keeps_magnitudes_in_every_column() -> None:
    # VLO 2022-Q1: no capex row in the payload; the first cell appends the row with the magnitude, the
    # prior-year cell of the same concept must not write the displayed (negative) sign into it
    cur, prior = "2022-03-31 (Q1)", "2021-03-31 (Q1)"
    cells = [
        {
            "statement": "cash_flow",
            "concept": concept,
            "column": column,
            "value": value,
            "displayed": -value,
        }
        for concept, by_column in (
            (G + "PaymentsToAcquirePropertyPlantAndEquipment", {cur: 384.0, prior: 339.0}),
            (G + "PaymentsForCapitalImprovements", {cur: 459.0, prior: 231.0}),
        )
        for column, value in by_column.items()
    ]
    stmts = Statements.from_payload(
        {
            "income_statement": [],
            "balance_sheet": [],
            "cash_flow": [
                {
                    **_row(G + "NetCashProvidedByUsedInOperatingActivities", "Operating", 588.0),
                    cur: 588.0,
                    prior: -52.0,
                }
            ],
            "_audit_face_cells": cells,
        }
    )
    assert stmts.resolve("capital_expenditure", cur).value == 384.0 + 459.0
    assert stmts.resolve("capital_expenditure", prior).value == 339.0 + 231.0


def _two_columns(debt_rows: list[dict[str, Any]], **payload: Any) -> Statements:
    prior = "2022-12-31"
    base = [
        {**_row(G + "Assets", "Assets", 100_000.0), prior: 90_000.0},
        {**_row(G + "StockholdersEquity", "Equity", 30_000.0), prior: 28_000.0},
        {**_row(G + "CashAndCashEquivalentsAtCarryingValue", "Cash", 2_000.0), prior: 1_500.0},
    ]
    return Statements.from_payload(
        {
            "income_statement": [],
            "balance_sheet": [*base, *debt_rows],
            "cash_flow": [],
            "_audit_own_date": PE,
            **payload,
        }
    )


def test_f_a17_a_comparative_read_another_way_takes_no_unverified_line() -> None:
    # REG FY2023's 2022 column: long-term debt reads as LongTermDebt there (blank face lines), which
    # already holds NotesPayable; the lines verified on the own column are not stacked on it
    rows = [
        {**_row(G + "LongTermDebt", "Total debt", None), "2022-12-31": 3_726.0},  # type: ignore[arg-type]
        {**_row(G + "NotesPayable", "Notes payable", 4_001.0), "2022-12-31": 3_726.0},
        {**_row(G + "UnsecuredDebt", "Line of credit", 152.0), "2022-12-31": None},
    ]
    stmts = _two_columns(
        rows,
        _audit_debt_add=[G + "NotesPayable", G + "UnsecuredDebt"],
        _audit_debt_add_by_date={
            "2022-12-31": {"lines": [], "sig": [G + "LongTermDebt", "concept"]}
        },
    )
    assert stmts.total_debt(PE).value == 4_001.0 + 152.0
    assert stmts.total_debt("2022-12-31").value == 3_726.0


def test_f_a17_a_comparative_takes_the_line_verified_for_its_own_filing() -> None:
    # EQT FY2024's 2023 column: the term loan is zero in the own column (not in its verified list) but
    # verified in the FY2023 10-K, whose long-term debt reads the same way
    rows = [
        {**_row(G + "LongTermDebtCurrent", "Current portion of debt", 321.0), "2022-12-31": 292.0},
        {**_row(G + "SeniorNotes", "Senior notes", 8_853.0), "2022-12-31": 4_200.0},
        {**_row(G + "LongTermLoansPayable", "Term loan", 0.0), "2022-12-31": 1_250.0},
    ]
    stmts = _two_columns(
        rows,
        _audit_debt_add=[G + "SeniorNotes"],
        _audit_debt_add_by_date={
            "2022-12-31": {
                "lines": [G + "SeniorNotes", G + "LongTermLoansPayable"],
                "sig": [None, None],
            }
        },
    )
    assert stmts.total_debt("2022-12-31").value == 292.0 + 4_200.0 + 1_250.0


def test_f_a18_a_comparative_matched_to_its_owner_takes_the_owner_reading() -> None:
    # MTD 2022-Q2's 2021 year-end column: the 10-K reads the short-term line from a filed fact this
    # 10-Q does not carry; the audit matched the 10-K's reading on this face, so the column takes it
    rows = [
        {**_row(G + "LongTermDebtNoncurrent", "Long-term debt", 1_821.0), "2022-12-31": 1_580.0},
        {**_row("mtd_ShortTermDebtAndCurrentMaturities", "Short-term", 106.0), "2022-12-31": 101.0},
    ]
    owner = {
        "value": 1_681.0,
        "concept": G + "LongTermDebtNoncurrent + " + G + "DebtCurrent",
        "flags": [],
    }
    stmts = _two_columns(rows, _audit_debt_by_column={"2022-12-31": owner})
    r = stmts.total_debt("2022-12-31")
    assert r.value == 1_681.0 and "audit_debt_owner_reading" in r.flags
    # the own column is never replaced
    assert "audit_debt_owner_reading" not in stmts.total_debt(PE).flags
