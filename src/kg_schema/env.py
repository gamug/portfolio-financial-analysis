"""Resolve the shared SQLite database path from the environment.

``KG_FINANCIAL_DB`` is the canonical name. ``KG_FINANTIAL_DB`` -- the original
misspelling -- is still honoured as a fallback so existing environments and
sibling repos keep working; new setups should use the corrected spelling.
"""

from __future__ import annotations

import os

DB_ENV_VAR = "KG_FINANCIAL_DB"
LEGACY_DB_ENV_VAR = "KG_FINANTIAL_DB"  # the historical misspelling, still read


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
