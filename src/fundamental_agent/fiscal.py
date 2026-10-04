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
