"""The one SQLite connection factory for the analysis-workstream databases.

Every package that opens ``KG_FINANCIAL_DB`` -- or a read-only companion like
``universe.db`` / the news ``urls.db`` -- goes through :func:`connect`. The pragma
policy that keeps concurrent writers well-behaved lives here instead of being
copied at each call site.

No WAL, ever: ``KG_FINANCIAL_DB`` may sit on a bind mount whose shared-memory
(``-shm``) support is unreliable, where ``journal_mode=WAL`` raises "disk I/O
error"; the default rollback journal works there and every writer is
single-writer anyway.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_CONNECT_TIMEOUT_S = 30.0
_BUSY_TIMEOUT_MS = 30_000


def connect(
    path: str | Path,
    *,
    read_only: bool = False,
    create_parents: bool = True,
) -> sqlite3.Connection:
    """Open *path* with the shared pragma policy.

    Always: rows come back as :class:`sqlite3.Row`.

    Read/write (default): ``PRAGMA foreign_keys = ON`` and
    ``PRAGMA busy_timeout = 30000`` -- neither is persistent, so both are
    reapplied on every connection; a connection that finds the file
    write-locked then retries internally for 30s instead of raising
    ``sqlite3.OperationalError: database is locked`` immediately. Parent
    directories of *path* are created unless *create_parents* is False.

    ``read_only=True``: opens ``file:{path}?mode=ro`` -- URI read-only, no
    pragmas beyond the row factory, no directory creation -- for the read-only
    companions (``universe.db``, the news ``urls.db``). *create_parents* is
    ignored.
    """
    if read_only:
        conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True, timeout=_CONNECT_TIMEOUT_S)
        conn.row_factory = sqlite3.Row
        return conn

    path = Path(path)
    if create_parents:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=_CONNECT_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    return conn


def connect_ro(path: str | Path) -> sqlite3.Connection:
    """Shorthand for ``connect(path, read_only=True)`` -- the read-only companions."""
    return connect(path, read_only=True)
