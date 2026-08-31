"""Turn a mutable ``assets`` universe into an append-only membership history.

``assets`` stays the write-once identity table. Each time a universe snapshot is
synced, :func:`reconcile` diffs the present members against the currently-open
``universe_membership`` rows: new tickers get an open row, vanished tickers get
``valid_to`` stamped. Rows are never deleted -- closing a stint is an ``UPDATE``.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def reconcile(  # noqa: PLR0913 - all keyword-only provenance fields, no sane grouping
    conn: sqlite3.Connection,
    universe: str,
    present_asset_ids: set[int],
    *,
    as_of: str,
    run_id: int | None = None,
    run_kind: str | None = None,
    source: str,
) -> tuple[int, int]:
    """Open memberships for newcomers, close them for the departed.

    Returns ``(opened, closed)`` counts. *as_of* is the effective date for both
    ``valid_from`` on new rows and ``valid_to`` on closed ones (ISO date string).
    """
    open_rows = conn.execute(
        "SELECT asset_id FROM universe_membership WHERE universe = ? AND valid_to IS NULL",
        (universe,),
    ).fetchall()
    open_ids = {int(r[0]) for r in open_rows}

    appeared = present_asset_ids - open_ids
    vanished = open_ids - present_asset_ids
    now = _now()

    for asset_id in sorted(appeared):
        conn.execute(
            """
            INSERT OR IGNORE INTO universe_membership
                (asset_id, universe, valid_from, valid_to, detected_at, run_id, run_kind, source)
            VALUES (?, ?, ?, NULL, ?, ?, ?, ?)
            """,
            (asset_id, universe, as_of, now, run_id, run_kind, source),
        )
    if vanished:
        conn.executemany(
            """
            UPDATE universe_membership SET valid_to = ?
            WHERE universe = ? AND asset_id = ? AND valid_to IS NULL
            """,
            [(as_of, universe, asset_id) for asset_id in sorted(vanished)],
        )
    conn.commit()
    return len(appeared), len(vanished)
