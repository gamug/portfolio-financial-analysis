"""Resolve the shared SQLite database paths from the environment.

``KG_FINANCIAL_DB`` is the canonical name for the analysis DB. ``KG_FINANTIAL_DB``
-- the original misspelling -- is still honoured as a fallback so existing
environments and sibling repos keep working; new setups should use the corrected
spelling.

``KG_UNIVERSE_DB`` points at the point-in-time S&P 500 membership database
(``universe.db``). Unlike the analysis DB it has a packaged default, so
:func:`universe_database_path` always returns a usable path.
"""

from __future__ import annotations

import os

DB_ENV_VAR = "KG_FINANCIAL_DB"
LEGACY_DB_ENV_VAR = "KG_FINANTIAL_DB"  # the historical misspelling, still read

UNIVERSE_DB_ENV_VAR = "KG_UNIVERSE_DB"
DEFAULT_UNIVERSE_DB = "/workspaces/thesis/data/universe.db"


def database_path(explicit: str | None = None) -> str | None:
    """Return the shared DB path.

    *explicit* wins if given; otherwise the first of ``KG_FINANCIAL_DB`` then the
    legacy misspelled variable that is set to a non-empty value. ``None`` when
    nothing is configured.
    """
    if explicit:
        return explicit
    for var in (DB_ENV_VAR, LEGACY_DB_ENV_VAR):
        value = os.environ.get(var)
        if value:
            return value
    return None


def universe_database_path(explicit: str | None = None) -> str:
    """Return the point-in-time universe DB path.

    *explicit* wins if given; otherwise ``KG_UNIVERSE_DB`` when set to a non-empty
    value; otherwise the packaged default :data:`DEFAULT_UNIVERSE_DB`. Always
    returns a usable path -- never ``None``.
    """
    if explicit:
        return explicit
    return os.environ.get(UNIVERSE_DB_ENV_VAR) or DEFAULT_UNIVERSE_DB
