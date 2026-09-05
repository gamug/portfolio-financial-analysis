"""The one SQLite connection factory for the analysis-workstream databases.

Every package that opens ``KG_FINANCIAL_DB`` -- or a read-only companion like
``universe.db`` / the news ``urls.db`` -- goes through :func:`connect`. Thin
wrapper over :class:`portfolio_common.db.Database`: the pragma policy itself
(row factory, busy_timeout, WAL, foreign_keys, parent-directory creation) now
lives there; this module just pins the choices this domain makes.

No WAL, ever: ``KG_FINANCIAL_DB`` may sit on a bind mount whose shared-memory
(``-shm``) support is unreliable, where ``journal_mode=WAL`` raises "disk I/O
error"; the default rollback journal works there and every writer is
single-writer anyway. ``wal=False`` is passed explicitly below for that reason
-- never flip it for this domain.
"""

from __future__ import annotations

from pathlib import Path

from portfolio_common.db import Database


def connect(
    path: str | Path,
    *,
    read_only: bool = False,
    create_parents: bool = True,
) -> Database:
    """Open *path* with the shared pragma policy.

    Always: rows come back as :data:`portfolio_common.db.Row`;
    ``journal_mode`` is left at the engine's default rollback journal (see
    module docstring -- never WAL for this domain).

    Read/write (default): ``PRAGMA foreign_keys = ON`` and a busy_timeout are
    applied -- neither is persistent, so both are reapplied on every
    connection; a connection that finds the file write-locked then retries
    internally instead of raising ``portfolio_common.db.DatabaseError:
    database is locked`` immediately. Parent directories of *path* are created
    unless *create_parents* is False.

    ``read_only=True``: opens ``file:{path}?mode=ro`` -- URI read-only, no
    pragmas beyond the row factory, no directory creation (``Database.connect``
    already skips both when read-only) -- for the read-only companions
    (``universe.db``, the news ``urls.db``). *create_parents* is ignored.
    """
    return Database.connect(
        path,
        read_only=read_only,
        wal=False,
        foreign_keys=True,
        create_parents=create_parents,
    )


def connect_ro(path: str | Path) -> Database:
    """Shorthand for ``connect(path, read_only=True)`` -- the read-only companions."""
    return connect(path, read_only=True)
