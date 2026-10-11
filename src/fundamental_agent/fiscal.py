"""A 10-Q's fiscal quarter, derived from the company's own fiscal year end (T-140).

The gateway tags each column with a quarter, but the tag is a function of the period end's
*calendar month* relative to the fiscal year end, so a 52/53-week filer whose quarter closes a
few days after a month boundary is tagged one ahead: Waters' fiscal Q2 ends 2023-07-01 and
arrives as ``(Q3)``, beside its real Q3 (2023-09-30), and its Q3 of 2021-10-02 as ``(Q4)``. A key
built on that tag collided (the second filing was dropped as "already done") or named a quarter a
10-Q cannot be.

Distance in days from the last fiscal year end is the same fact without the rounding: a quarter
closes about 91, 182 or 273 days after it on any calendar, 52/53-week included, and the
distance is taken modulo a year so a fiscal year end from a neighbouring year anchors it too.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable
from datetime import date, timedelta

_YEAR_DAYS = 365.25
_QUARTER_DAYS = _YEAR_DAYS / 4
_LAST_10Q_QUARTER = 3  # the fourth is the fiscal year itself, on a 10-K
# How far a quarter end may sit from a whole number of quarters after the fiscal year end: a
# 52/53-week calendar moves it by under a week, a 4-4-5 one by about three days; a fiscal year
# end from a neighbouring year adds a day or two of drift. Beyond this the date is not a quarter
# end of that fiscal year at all (a changed fiscal year, a wrong anchor) and no label is guessed.
_TOLERANCE_DAYS = 30
# A period that ends in the first week of January closed the *year before* as far as a label goes:
# a 52/53-week fiscal year can end on 2023-01-01 (Johnson & Johnson's fiscal 2022) and the next on
# 2023-12-31, and a first quarter on 2023-01-01 and the next year's on 2023-12-31 (Starbucks).
# Both pairs shared a calendar-year label (``FY2023``, ``2023Q1``): one of each was dropped.
_LABEL_YEAR_SHIFT = timedelta(days=7)
# The window, in days before a 10-Q's period end, in which its balance sheet's comparative column
# can be the last fiscal year end: not the prior-year same-quarter instant (365 days back), and
# not the period end itself.
_FYE_WINDOW_DAYS = (30, 330)


def label_year(period_end: date) -> int:
    """The year a period's label carries: the calendar year of its end, counting a period that
    ends in the first week of January as the previous year's (:data:`_LABEL_YEAR_SHIFT`). The
    same for every other end date, so no label of a calendar filer moves."""
    return (period_end - _LABEL_YEAR_SHIFT).year


def quarter_number(period_end: date, fiscal_year_end: date) -> int | None:
    """The fiscal quarter (1-3) a 10-Q's *period_end* closes, or ``None`` when it is not within
    :data:`_TOLERANCE_DAYS` of a quarter boundary of the year *fiscal_year_end* closes. Quarter 4
    is the fiscal year itself, reported on a 10-K -- never a 10-Q."""
    since = (period_end - fiscal_year_end).days % _YEAR_DAYS
    quarters = round(since / _QUARTER_DAYS)
    if abs(since - quarters * _QUARTER_DAYS) > _TOLERANCE_DAYS:
        return None
    return quarters if 1 <= quarters <= _LAST_10Q_QUARTER else None


def quarter_label(period_end: date, fiscal_year_end: date) -> str | None:
    """``"<year>Q<fiscal quarter>"`` -- the existing label convention (:func:`label_year` of the
    period end), with the quarter read from the fiscal calendar instead of the gateway's tag. A
    calendar-quarter filer keeps every label it has; ``None`` when :func:`quarter_number` finds
    no quarter."""
    quarter = quarter_number(period_end, fiscal_year_end)
    return None if quarter is None else f"{label_year(period_end)}Q{quarter}"


def payload_fiscal_year_end(instants: Iterable[str], period_end: date) -> date | None:
    """The last fiscal year end as a 10-Q's own balance sheet carries it -- the comparative
    column beside the current one (Regulation S-X, rule 10-01). The earliest instant inside
    :data:`_FYE_WINDOW_DAYS` of *period_end*, so a prior-year column or a stray later instant is
    never taken for it. ``None`` when the payload carries none."""
    low, high = _FYE_WINDOW_DAYS
    found = [
        d for d in (date.fromisoformat(i) for i in instants) if low <= (period_end - d).days <= high
    ]
    return min(found) if found else None


# -- PER-08: when two period ends are the same period (T-149) ---------------------------------
#
# "Two periods match when their end dates round to the same month end, and their length is within
# [364, 371] days for a year, [89, 98] for a quarter, [181, 189] for six months and [273, 280] for
# nine months" (checklist v1.3, PER-08). The ranges are arithmetic -- 52 or 53 weeks, 13 or 14 weeks
# -- not a source; the rounding is the SEC's ("ddate: the end date for the data value, rounded to
# the nearest month end", Financial Statement Data Sets, A2 §5.3). A filing's payload gives a
# column's end date, not its start, so a length is read as the distance between two consecutive
# period ends (a period starts the day after the one before it ends).

YEAR_DAYS = (364, 371)
QUARTER_DAYS = (89, 98)
SIX_MONTH_DAYS = (181, 189)
NINE_MONTH_DAYS = (273, 280)


def month_end(day: date) -> date:
    """The month end nearest to *day* (a tie goes to the earlier one): 2023-07-01 -> 2023-06-30,
    2024-09-28 -> 2024-09-30, 2023-02-15 -> 2023-02-28."""
    previous = day.replace(day=1) - timedelta(days=1)
    this = day.replace(day=calendar.monthrange(day.year, day.month)[1])
    return previous if (day - previous) <= (this - day) else this


def same_period_end(a: date, b: date) -> bool:
    """Whether two period end dates round to the same month end."""
    return month_end(a) == month_end(b)


def within(days: int, bounds: tuple[int, int]) -> bool:
    low, high = bounds
    return low <= days <= high


def one_year_before(day: date) -> date:
    """*day* a calendar year earlier (29 February becomes 28 February)."""
    if day.month == 2 and day.day == 29:  # noqa: PLR2004 - the leap day
        return date(day.year - 1, 2, 28)
    return day.replace(year=day.year - 1)


def is_year_apart(earlier: date, later: date) -> bool:
    """Whether *earlier* is the period end one year before *later*: the distance is within
    :data:`YEAR_DAYS` and *earlier* rounds to the same month end as *later* a year earlier."""
    return within((later - earlier).days, YEAR_DAYS) and same_period_end(
        earlier, one_year_before(later)
    )


def is_quarter_apart(earlier: date, later: date) -> bool:
    """Whether *earlier* is the period end one quarter before *later*: the distance is within
    :data:`QUARTER_DAYS` and *earlier* rounds to the same month end as *later* three months
    earlier."""
    return within((later - earlier).days, QUARTER_DAYS) and same_period_end(
        earlier, months_before(later, 3)
    )


def months_before(day: date, months: int) -> date:
    """*day* moved back *months* calendar months; a month end stays a month end (2023-08-31 - 3
    -> 2023-05-31, 2024-02-29 - 3 -> 2023-11-30)."""
    year, month0 = divmod(day.year * 12 + (day.month - 1) - months, 12)
    month = month0 + 1
    last = calendar.monthrange(year, month)[1]
    was_month_end = day.day == calendar.monthrange(day.year, day.month)[1]
    return date(year, month, last if was_month_end else min(day.day, last))
