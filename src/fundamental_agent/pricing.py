"""Read the closing price near a filing's period-end from the shared database.

The pricing collector (:mod:`pricing_agent`) owns the ``price_daily`` table in the
same ``KG_FINANCIAL_DB``. This module only *reads* it -- there is no import of
``pricing_agent`` -- so the fundamental agent stays runnable whether or not the
pricing collector has ever populated that table.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from portfolio_common.db import Database, DatabaseError

# A period-end date can fall on a weekend or holiday; look back a few sessions for
# the last actual close, but not so far that a gap in coverage returns a stale price.
_MAX_LOOKBACK_DAYS = 7


@dataclass(frozen=True)
class ClosePrice:
    """A single daily close used to value a filing's period-end."""

    date: str  # ISO date of the close actually used
    close: float


def close_on_or_before(
    conn: Database,
    asset_id: int,
    period_end: str,
    *,
    max_lookback_days: int = _MAX_LOOKBACK_DAYS,
) -> ClosePrice | None:
    """Return the last ``price_daily`` close at or before *period_end*.

    ``None`` when there is no close within *max_lookback_days* of *period_end*, when
    the asset has no price rows, or when the ``price_daily`` table does not exist.
    """
    floor = _iso_days_before(period_end, max_lookback_days)
    try:
        row = conn.execute(
            """
            SELECT date, close
            FROM price_daily
            WHERE asset_id = ? AND date <= ? AND date >= ? AND close IS NOT NULL
            ORDER BY date DESC
            LIMIT 1
            """,
            (asset_id, period_end, floor),
        ).fetchone()
    except DatabaseError:
        return None  # price_daily not created -- pricing collector never ran
    if row is None:
        return None
    return ClosePrice(date=str(row[0]), close=float(row[1]))


def _iso_days_before(iso_date: str, days: int) -> str:
    """``iso_date`` shifted *days* earlier, staying in ``YYYY-MM-DD`` form."""
    year, month, day = (int(part) for part in iso_date[:10].split("-"))
    shifted = date(year, month, day).toordinal() - days
    return date.fromordinal(shifted).isoformat()
