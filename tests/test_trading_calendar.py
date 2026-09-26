"""The NYSE calendar behind ``available_at`` (T-107)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from kg_schema.trading_calendar import (
    available_from,
    is_trading_day,
    next_trading_day,
    nyse_holidays,
)

# The NYSE's published full-day closures (weekday closures only).
PUBLISHED = {
    2022: [
        "2022-01-17", "2022-02-21", "2022-04-15", "2022-05-30", "2022-06-20",
        "2022-07-04", "2022-09-05", "2022-11-24", "2022-12-26",
    ],  # New Year's Day 2022 fell on a Saturday and was not observed
    2023: [
        "2023-01-02", "2023-01-16", "2023-02-20", "2023-04-07", "2023-05-29",
        "2023-06-19", "2023-07-04", "2023-09-04", "2023-11-23", "2023-12-25",
    ],
    2024: [
        "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27",
        "2024-06-19", "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25",
    ],
    2025: [
        "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18",
        "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27",
        "2025-12-25",
    ],  # 01-09: national day of mourning for President Carter
    2026: [
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
        "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    ],
    2027: [
        "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
        "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
    ],
    2028: [
        "2028-01-17", "2028-02-21", "2028-04-14", "2028-05-29", "2028-06-19",
        "2028-07-04", "2028-09-04", "2028-11-23", "2028-12-25",
    ],
}  # fmt: skip


@pytest.mark.parametrize("year", sorted(PUBLISHED))
def test_the_holidays_match_the_nyse_s_published_lists(year: int) -> None:
    assert sorted(d.isoformat() for d in nyse_holidays(year)) == PUBLISHED[year]


def test_sessions_2022_to_2026_match_the_price_spine_s_count() -> None:
    """Production's daily-price spine holds 1,167 sessions from 2022-01-03 to 2026-08-27 --
    exactly the days this calendar calls trading days (checked date by date on 2026-09-26)."""
    day, end, n = date(2022, 1, 3), date(2026, 8, 27), 0
    while day <= end:
        n += is_trading_day(day)
        day += timedelta(days=1)
    assert n == 1167


@pytest.mark.parametrize(
    ("filed", "usable"),
    [
        ("2026-02-18", "2026-02-19"),  # Wednesday -> Thursday
        ("2026-02-20", "2026-02-23"),  # Friday -> Monday
        ("2026-02-21", "2026-02-23"),  # a Saturday date (rare on EDGAR) -> Monday
        ("2025-02-14", "2025-02-18"),  # Friday before Presidents' Day -> Tuesday
        ("2025-01-08", "2025-01-10"),  # the eve of the Carter closure
        ("2025-12-24", "2025-12-26"),  # Christmas Eve -> the day after Christmas
        ("2026-07-02", "2026-07-06"),  # Independence Day observed Friday 07-03
        ("2027-12-23", "2027-12-27"),  # Christmas observed Friday 12-24
        ("2001-09-10", "2001-09-17"),  # the longest gap since 2000
    ],
)
def test_a_filing_is_usable_from_the_next_trading_day(filed: str, usable: str) -> None:
    assert available_from(filed) == usable
    assert next_trading_day(date.fromisoformat(filed)).isoformat() == usable


def test_an_undated_filing_is_never_usable() -> None:
    assert available_from(None) is None
    assert available_from("") is None


def test_a_timestamp_is_read_by_its_date() -> None:
    assert available_from("2026-02-20T21:15:00") == "2026-02-23"
