"""The ``analysis_date`` run seam: parse, default-to-today, and the argparse flag.

Every agent gains an optional ``--analysis-date YYYY-MM-DD`` that defaults to
today. This module is the single place the format and the default live, so the 11
package-local ``_now()`` helpers do not each grow a copy.
"""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime

ISO_FMT = "%Y-%m-%d"


def parse_analysis_date(value: str) -> str:
    """argparse ``type=`` for ``--analysis-date``. Validates and normalises to ISO."""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected an ISO date (YYYY-MM-DD), got {value!r}"
        ) from exc


def today() -> str:
    """Today's date as ISO ``YYYY-MM-DD``. The one wall-clock seam for run dates."""
    return datetime.now(tz=UTC).date().isoformat()


def resolve(value: str | None) -> str:
    """*value* if a caller supplied ``--analysis-date``, else :func:`today`."""
    return value or today()


def add_analysis_date_argument(parser: argparse.ArgumentParser) -> None:
    """Add ``--analysis-date`` to *parser* (``dest='analysis_date'``, default ``None``)."""
    parser.add_argument(
        "--analysis-date",
        dest="analysis_date",
        type=parse_analysis_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="as-of date for the run (universe + no-lookahead cutoff); default: today",
    )
