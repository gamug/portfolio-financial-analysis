"""The NYSE trading calendar, and when a filing becomes usable (T-107).

EDGAR dates a submission accepted after the 17:30 ET cutoff -- and so every after-close earnings
10-Q -- with the same day, so a filing dated D is only safely known to the market from the next
session. :func:`available_from` is that session: the first NYSE trading day strictly after the
filing date. It is stored as ``available_at`` on ``sec_filings`` and copied onto the filing's
``fundamental_metrics`` and FUNDAMENTAL ``score_snapshot`` rows; every as-of reader keys on it.

The calendar is rule-based (weekends, the NYSE's regular holidays with their observance rules)
plus the unscheduled closures listed in :data:`SPECIAL_CLOSURES`. It is checked against every
session of the stored daily-price spine (2022-01-03 .. 2026-08-27, 1,167 sessions) and the
NYSE's published holiday lists (``tests/test_calendar.py``).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from functools import cache

# Unscheduled full-day closures since 2000 (national days of mourning, weather, 9/11).
SPECIAL_CLOSURES: frozenset[date] = frozenset(
    {
        date(2001, 9, 11),
        date(2001, 9, 12),
        date(2001, 9, 13),
        date(2001, 9, 14),
        date(2004, 6, 11),  # President Reagan
        date(2007, 1, 2),  # President Ford
        date(2012, 10, 29),  # Hurricane Sandy
        date(2012, 10, 30),
        date(2018, 12, 5),  # President G. H. W. Bush
        date(2025, 1, 9),  # President Carter
    }
)


_SATURDAY, _SUNDAY = 5, 6
_JUNETEENTH_FIRST_YEAR = 2022


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * ell) // 433
    month = (h + ell - 7 * m + 90) // 25
    return date(year, month, (h + ell - 7 * m + 33 * month + 19) % 32)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The *n*-th *weekday* (Mon = 0) of the month; ``n = -1`` is the last."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    """A Saturday holiday is observed on the Friday before, a Sunday one on the Monday after."""
    if day.weekday() == _SATURDAY:
        return day - timedelta(days=1)
    if day.weekday() == _SUNDAY:
        return day + timedelta(days=1)
    return day


@cache
def nyse_holidays(year: int) -> frozenset[date]:
    """The NYSE's full-day closures in *year* (weekends excluded)."""
    days = {
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _easter(year) - timedelta(days=2),  # Good Friday
        _nth_weekday(year, 5, 0, -1),  # Memorial Day
        _observed(date(year, 7, 4)),  # Independence Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),  # Christmas
    }
    # New Year's Day: a Saturday one is not moved back into December (NYSE Rule 7.2).
    new_year = date(year, 1, 1)
    if new_year.weekday() != _SATURDAY:
        days.add(_observed(new_year))
    if year >= _JUNETEENTH_FIRST_YEAR:
        days.add(_observed(date(year, 6, 19)))  # Juneteenth
    days |= {d for d in SPECIAL_CLOSURES if d.year == year}
    return frozenset(days)


def is_trading_day(day: date) -> bool:
    return day.weekday() < _SATURDAY and day not in nyse_holidays(day.year)


def next_trading_day(day: date) -> date:
    """The first NYSE trading day strictly after *day*."""
    nxt = day + timedelta(days=1)
    while not is_trading_day(nxt):
        nxt += timedelta(days=1)
    return nxt


def available_from(filing_date: str | None) -> str | None:
    """``available_at`` for a filing dated *filing_date* (ISO ``YYYY-MM-DD``): the next NYSE
    trading day. A filing with no date cannot be shown to be public: ``None``."""
    if not filing_date:
        return None
    return next_trading_day(date.fromisoformat(filing_date[:10])).isoformat()


# -- when a session's daily bar is final (T-131) ------------------------------------------

#: The regular close, 16:00 America/New_York. An early close (13:00) is treated as a full day:
#: refusing a little too long is the safe side of this guard.
_CLOSE_HOUR_ET = 16
#: A vendor's volume is still being revised just after the bell; this much later the bar is final.
SESSION_SETTLE_BUFFER = timedelta(hours=1)
_SUNDAY_WEEKDAY = 6
_MARCH, _NOVEMBER = 3, 11


def _is_dst(day: date) -> bool:
    """US daylight time: from the second Sunday of March to the first Sunday of November.
    The changeovers are Sunday 02:00 local, so a weekday's 16:00 close is never ambiguous."""
    start = _nth_weekday(day.year, _MARCH, _SUNDAY_WEEKDAY, 2)
    end = _nth_weekday(day.year, _NOVEMBER, _SUNDAY_WEEKDAY, 1)
    return start <= day < end


def session_final_at(day: date) -> datetime:
    """The UTC instant from which *day*'s daily bar is complete: the 16:00 ET close plus
    :data:`SESSION_SETTLE_BUFFER`. Hand-computed from the US DST rule rather than ``zoneinfo``
    so it needs no tz database in a minimal container."""
    utc_offset = 4 if _is_dst(day) else 5
    close = datetime(day.year, day.month, day.day, _CLOSE_HOUR_ET + utc_offset, tzinfo=UTC)
    return close + SESSION_SETTLE_BUFFER


def session_is_open_or_pending(day: date, now: datetime) -> bool:
    """True when *day* is a trading day whose bar is not yet final at *now* (an aware
    datetime): the session is in progress or has not started. A weekend or holiday has no
    session, so is never pending."""
    return is_trading_day(day) and now < session_final_at(day)


def last_final_session(now: datetime) -> date:
    """The latest NYSE trading day whose bar is final at *now*."""
    day = now.astimezone(UTC).date()
    while not is_trading_day(day) or now < session_final_at(day):
        day -= timedelta(days=1)
    return day
