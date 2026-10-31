"""L-21 (audit 2026-10-28, user-approved): the zero debt the filing itself proves, when ID-02b's first tests refuse it.

Each case is one the whole-denominator simulation (`audit/checks/zero_debt_sim.py`, 481 empty-debt filings) met:
lease or facility interest with no borrowing (PLTR, VRTX), a debt line shown at 0 after a repayment (TER, FFIV,
WSM), "liabilities other than long-term debt" (ISRG), a cash caption naming restricted cash (CPRT), and the refusals
that must stand: a debt balance among the filing's facts, a debt amount the gateway dropped (AIG), a debt balance at
another date with no zero shown (PAYC FY2023), no facts at all.
"""

from __future__ import annotations

from typing import Any

from fundamental_agent.statements import Statements

G = "us-gaap_"
FY = "2025-12-31 (FY)"
COL = "2025-12-31"
PRIOR = "2024-12-31"


def _bs(concept: str, label: str, val: float | None, prior: float | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "concept": G + concept,
        "label": label,
        "abstract": False,
        "dimension": False,
        COL: val,
    }
    if prior is not None:
        row[PRIOR] = prior
    return row


def _is(concept: str, label: str, val: float) -> dict[str, Any]:
    return {"concept": G + concept, "label": label, "abstract": False, "dimension": False, FY: val}


def _fact(concept: str, value: float, end: str = COL, start: str | None = None) -> dict[str, Any]:
    return {
        "concept": "us-gaap:" + concept,
        "value": value,
        "unit": "USD",
        "period_start": start,
        "period_end": end,
    }


BASE_FACTS = [_fact("Revenues", 500.0, start="2025-01-01")]


def _stmts(
    *extra_bs: dict[str, Any],
    interest: bool = False,
    cash: dict[str, Any] | None = None,
    facts: list[dict[str, Any]] | None = None,
    corrections: list[dict[str, Any]] | None = None,
) -> Statements:
    balance = [
        cash or _bs("CashAndCashEquivalentsAtCarryingValue", "Cash and cash equivalents", 100.0),
        _bs("AccountsPayableCurrent", "Accounts payable", 50.0),
        *extra_bs,
        _bs("Assets", "Total assets", 1_000.0),
        _bs("StockholdersEquity", "Total stockholders' equity", 600.0),
    ]
    income = [_is("Revenues", "Revenue", 500.0)]
    if interest:
        income.append(_is("InterestExpense", "Interest expense", 2.0))
    return Statements.from_payload(
        {
            "income_statement": income,
            "balance_sheet": balance,
            "cash_flow": [],
            "corrections": corrections or [],
            "filed_facts": BASE_FACTS if facts is None else facts,
        }
    )


def test_lease_interest_without_any_debt_balance_is_a_verified_zero() -> None:
    """PLTR / VRTX: an interest-expense line (leases, an undrawn facility), no debt line, no debt balance in the
    filing's facts -> 0, flagged ``debt_absent_verified`` beside ``no_debt_lines``."""
    total = _stmts(interest=True).total_debt(FY)
    assert total.value == 0.0
    assert total.flags == ("no_debt_lines", "debt_absent_verified")


def test_a_debt_balance_among_the_filing_facts_refuses_the_zero() -> None:
    """The face shows no debt line but the filing's own facts carry a debt balance at the date: missing, not 0."""
    total = _stmts(interest=True, facts=[*BASE_FACTS, _fact("LongTermDebt", 250.0)]).total_debt(FY)
    assert (
        total.value is None
        and "capture_failure" in total.flags
        and "debt_absent_verified" not in total.flags
    )


def test_interest_with_a_debt_balance_at_another_date_and_no_zero_shown_stays_refused() -> None:
    """PAYC FY2023: interest expense, a debt balance at the prior date, nothing on this year's face proving 0."""
    total = _stmts(
        interest=True, facts=[*BASE_FACTS, _fact("LineOfCredit", 30.0, end=PRIOR)]
    ).total_debt(FY)
    assert total.value is None and total.flags == ("capture_failure",)


def test_a_debt_line_shown_at_zero_after_a_repayment_is_a_verified_zero() -> None:
    """TER 2026-Q1: "Short-term debt" 200 last year, 0 this year, interest expense on the income statement."""
    total = _stmts(
        _bs("ShortTermBorrowings", "Short-term debt", 0.0, prior=200.0),
        interest=True,
        facts=[*BASE_FACTS, _fact("ShortTermBorrowings", 200.0, end=PRIOR)],
    ).total_debt(FY)
    assert total.value == 0.0
    assert "debt_absent_verified" in total.flags and "short_term_debt_only" not in total.flags


def test_a_short_term_line_at_zero_alone_is_not_short_term_debt_only() -> None:
    """FFIV FY2023: "Current portion of long-term debt" 0.0 and no long-term line -> 0, not a refusal."""
    total = _stmts(
        _bs("LongTermDebtCurrent", "Current portion of long-term debt", 0.0, prior=349.8)
    ).total_debt(FY)
    assert total.value == 0.0 and "short_term_debt_only" not in total.flags


def test_a_debt_amount_the_gateway_dropped_refuses_the_zero_even_without_its_row() -> None:
    """AIG: the whole ``LongTermDebt`` row dropped (T-042), only the ``corrections`` entry left."""
    dropped = [
        {
            "statement": "balance_sheet",
            "concept": G + "LongTermDebt",
            "column": COL,
            "original": 29_938.0,
        }
    ]
    total = _stmts(corrections=dropped).total_debt(FY)
    assert total.value is None and total.flags == ("capture_failure",)


def test_liabilities_other_than_long_term_debt_is_not_a_debt_line() -> None:
    """ISRG: ``LiabilitiesOtherThanLongtermDebtNoncurrent`` ("Other long-term liabilities") is not debt -- the
    plain ID-02b zero."""
    total = _stmts(
        _bs("LiabilitiesOtherThanLongtermDebtNoncurrent", "Other long-term liabilities", 510.8)
    ).total_debt(FY)
    assert total.value == 0.0 and total.flags == ("no_debt_lines",)


def test_a_cash_caption_naming_restricted_cash_still_shows_a_whole_balance_sheet() -> None:
    """CPRT: cash is refused as *cash* (L-5) but the line is on the face -> the zero is verified."""
    restricted = _bs(
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "Cash, cash equivalents, and restricted cash",
        3_698.1,
    )
    stmts = _stmts(cash=restricted)
    assert stmts.resolve("cash", FY).value is None
    total = stmts.total_debt(FY)
    assert total.value == 0.0 and total.flags == ("no_debt_lines", "debt_absent_verified")


def test_without_the_filing_facts_the_zero_cannot_be_verified() -> None:
    """No facts to test (the gateway's read failed): the old guard stands."""
    total = _stmts(interest=True, facts=[]).total_debt(FY)
    assert total.value is None and total.flags == ("capture_failure",)
