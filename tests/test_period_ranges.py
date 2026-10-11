"""PER-08 (T-149): two period ends are one period when they round to the same month end and sit a
year (or a quarter) apart by the rule's day ranges -- not within a 20-day window.

The two pairings the audit found on production (``PER08.prior_gap_accepted_by_code_not_rule`` and
``PER08.fy_prior_not_a_year``) are rebuilt here with their real column dates: Cummins' Q3 2022 was
paired with the quarter ended 2021-10-03 (362 days earlier), and Expand Energy's FY2022 with FY2020
(730 days earlier, its FY2021 column being absent).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from portfolio_common.db import Database

from fundamental_agent import db, fiscal
from fundamental_agent.db import FilingKey, FilingMeta
from fundamental_agent.statements import Statements

D = date.fromisoformat


def _payload(*columns: str) -> dict[str, Any]:
    """A one-row income statement with the given period columns (a quarter is ``2022-09-30 (Q3)``)."""
    row: dict[str, Any] = {"concept": "us-gaap_Revenues", "label": "Revenues", "abstract": False}
    row.update({c: 100.0 for c in columns})
    return {"income_statement": [row], "balance_sheet": [], "cash_flow": []}


# -- the rounding -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("end", "rounded"),
    [
        ("2023-07-01", "2023-06-30"),  # Waters' second quarter, a Saturday
        ("2024-09-28", "2024-09-30"),  # Apple's fiscal year
        ("2023-01-01", "2022-12-31"),  # Johnson & Johnson's fiscal 2022
        ("2023-12-30", "2023-12-31"),
        ("2023-02-15", "2023-02-28"),  # nearer to the end of February than of January
        ("2024-02-29", "2024-02-29"),
    ],
)
def test_a_period_end_rounds_to_the_nearest_month_end(end: str, rounded: str) -> None:
    assert fiscal.month_end(D(end)) == D(rounded)


def test_the_rule_s_ranges_are_the_checklist_s() -> None:
    assert fiscal.YEAR_DAYS == (364, 371)
    assert fiscal.QUARTER_DAYS == (89, 98)
    assert fiscal.SIX_MONTH_DAYS == (181, 189)
    assert fiscal.NINE_MONTH_DAYS == (273, 280)


@pytest.mark.parametrize(
    ("earlier", "later"),
    [
        ("2022-12-31", "2023-12-31"),  # a calendar year
        ("2023-12-31", "2024-12-31"),  # 366 days
        ("2023-09-30", "2024-09-28"),  # Apple: 52 weeks, 364 days
        ("2022-10-01", "2023-09-30"),  # Waters: 364 days
        ("2021-12-26", "2022-12-31"),  # 370 days, a 53rd week
        ("2023-01-01", "2023-12-31"),  # J&J: 364 days
    ],
)
def test_a_year_apart_by_the_rule(earlier: str, later: str) -> None:
    assert fiscal.is_year_apart(D(earlier), D(later))


@pytest.mark.parametrize(
    ("earlier", "later"),
    [
        ("2021-10-03", "2022-09-30"),  # Cummins: 362 days -- the old window took it
        ("2020-12-31", "2022-12-31"),  # Expand Energy: two years
        ("2022-12-31", "2023-06-30"),  # half a year
        ("2022-06-30", "2023-12-31"),  # eighteen months
        ("2023-01-31", "2024-02-29"),  # 394 days
    ],
)
def test_not_a_year_apart(earlier: str, later: str) -> None:
    assert not fiscal.is_year_apart(D(earlier), D(later))


def test_a_quarter_apart() -> None:
    assert fiscal.is_quarter_apart(D("2023-09-30"), D("2023-12-31"))  # 92 days
    assert fiscal.is_quarter_apart(D("2023-09-30"), D("2024-01-01"))  # a 53-week year's Q4: 93
    assert not fiscal.is_quarter_apart(D("2023-06-30"), D("2023-12-31"))  # six months
    assert not fiscal.is_quarter_apart(D("2023-10-31"), D("2023-12-31"))  # two months


# -- the two audit cases, with their real columns ------------------------------------------------


def test_cummins_q3_2022_is_not_paired_with_the_quarter_ended_2021_10_03() -> None:
    """The payload carries '2021-10-03 (Q4)' beside '2022-09-30 (Q3)' -- a 52/53-week tag shift. The
    old window (365 +- 20 days) paired them at 362 days; the rule's [364, 371] does not."""
    stmts = Statements.from_payload(_payload("2022-09-30 (Q3)", "2021-10-03 (Q4)"))
    q3 = next(p for p in stmts.periods if p.date == "2022-09-30")
    assert stmts.prior_of(q3) is None


def test_expand_energy_fy2022_is_not_paired_with_fy2020() -> None:
    stmts = Statements.from_payload(_payload("2022-12-31 (FY)", "2020-12-31 (FY)"))
    assert stmts.prior_of(stmts.periods[-1]) is None


def test_a_prior_year_column_is_still_found_for_a_52_53_week_calendar() -> None:
    stmts = Statements.from_payload(
        _payload("2023-09-30 (FY)", "2022-10-01 (FY)", "2021-10-02 (FY)")
    )
    fy2023 = next(p for p in stmts.periods if p.date == "2023-09-30")
    prior = stmts.prior_of(fy2023)
    assert prior is not None and prior.date == "2022-10-01"


def test_a_quarter_pairs_with_the_neighbouring_tag_a_year_earlier() -> None:
    """T-140's Waters case, kept: 2023-09-30 (Q3) beside 2022-10-01 (Q4)."""
    stmts = Statements.from_payload(_payload("2023-09-30 (Q3)", "2022-10-01 (Q4)"))
    q3 = next(p for p in stmts.periods if p.date == "2023-09-30")
    prior = stmts.prior_of(q3)
    assert prior is not None and prior.date == "2022-10-01"


# -- the stored filings are found by the same rule ----------------------------------------------


def _10q(conn: Database, period: str, period_end: str) -> int:
    conn.execute("INSERT OR IGNORE INTO assets (id, ticker) VALUES (1, 'WAT')")
    return db.upsert_filing(
        conn,
        FilingKey(1, "10-Q", int(period_end[:4]), period),
        FilingMeta(filing_date=None, period_end=period_end),
    )


def test_a_stored_quarter_is_found_by_its_rounded_month_end(memory_db: Database) -> None:
    saturday = _10q(memory_db, "2023Q2", "2023-07-01")
    found = db._filing_near(memory_db, 1, "10-Q", D("2023-06-30"))
    assert found == (saturday, "2023-07-01")


def test_a_quarter_a_fortnight_into_the_next_month_is_not_the_quarter(memory_db: Database) -> None:
    """2023-07-16 is 16 days from 2023-06-30: inside the old 20-day window, rounding to July."""
    _10q(memory_db, "2023Q2", "2023-07-16")
    assert db._filing_near(memory_db, 1, "10-Q", D("2023-06-30")) is None
