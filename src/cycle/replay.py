"""``cycle backfill``'s simulated position book (T-115).

`portfolio_position` is the one production book every other repo reads as ground truth.
Before this, `backfill` called the live `run_selection`, which writes into it through
`writers.sync_positions` -- refused by T-097's out-of-order guard the moment a newer live
entry already exists, with no override, because there never should be one for the live book.
A historical replay needs its own book: same T-104 stint-immutability shape
(`portfolio_position_replay`), but never read by, or refused for conflicting with, the live
one.

Kept as its own literal-SQL module rather than a table-name-parameterized version of
`writers.sync_positions` / `writers.out_of_order_reason`: Code & Git #10 forbids building a
raw-SQL identifier from a variable -- even one this module fully controls -- since an
automated SAST scanner flags the pattern itself, not just genuinely-unsafe cases.
"""

from __future__ import annotations

from portfolio_common.db import Database

from cycle.writers import WEIGHT_EPS, OutOfOrderCycle


def out_of_order_replay_reason(conn: Database, cycle_date: str) -> str | None:
    """Same check as :func:`cycle.writers.out_of_order_reason`, against the replay book.

    ``cycle backfill --force`` resets the requested date range first
    (:func:`reset_replay_range`), so in the normal flow this only ever fires for a *new*,
    never-before-replayed date older than the replay book's current latest -- not the
    ordinary resume-then-extend case, which ``cycle_checkpoint`` already makes a no-op.
    """
    row = conn.execute("SELECT MAX(valid_from) AS latest FROM portfolio_position_replay").fetchone()
    latest = row["latest"] if row else None
    if latest is None or cycle_date >= str(latest):
        return None
    return (
        f"cycle_date {cycle_date} is older than the replay book's latest valid_from {latest}; "
        "pass --force to reset the replay book for this date range first"
    )


def sync_replay_positions(
    conn: Database,
    cycle_date: str,
    targets: dict[int, float],
    closes: dict[int, float | None],
    *,
    cycle_run_id: int,
) -> tuple[int, int]:
    """:func:`cycle.writers.sync_positions`'s exact open/close/reweight logic, against
    ``portfolio_position_replay`` instead of the live ``portfolio_position``."""
    open_rows = {
        int(r["asset_id"]): r
        for r in conn.execute(
            "SELECT id, asset_id, valid_from, weight, cost_basis FROM portfolio_position_replay "
            "WHERE valid_to IS NULL"
        )
    }
    now_target = set(targets)
    newer = sorted(
        str(r["valid_from"]) for r in open_rows.values() if str(r["valid_from"]) > cycle_date
    )
    if newer:
        raise OutOfOrderCycle(
            f"cycle_date {cycle_date} would end replay positions opened later ({newer[-1]}); "
            "pass --force to reset the replay book for this date range first"
        )
    opened = closed = 0
    for aid in set(open_rows) - now_target:
        conn.execute(
            "UPDATE portfolio_position_replay SET valid_to = ? WHERE id = ?",
            (cycle_date, int(open_rows[aid]["id"])),
        )
        closed += 1
    for aid in now_target - set(open_rows):
        conn.execute(
            """
            INSERT INTO portfolio_position_replay
                (asset_id, valid_from, valid_to, weight, cost_basis, opened_by_cycle, run_id)
            VALUES (?, ?, NULL, ?, ?, ?, ?)
            """,
            (aid, cycle_date, targets[aid], closes.get(aid), cycle_run_id, cycle_run_id),
        )
        opened += 1
    for aid in now_target & set(open_rows):
        row = open_rows[aid]
        if row["weight"] is not None and abs(float(row["weight"]) - targets[aid]) <= WEIGHT_EPS:
            continue
        if str(row["valid_from"]) == cycle_date:
            conn.execute(
                "UPDATE portfolio_position_replay SET weight = ? WHERE id = ?",
                (targets[aid], row["id"]),
            )
            continue
        conn.execute(
            "UPDATE portfolio_position_replay SET valid_to = ? WHERE id = ?",
            (cycle_date, row["id"]),
        )
        conn.execute(
            """
            INSERT INTO portfolio_position_replay
                (asset_id, valid_from, valid_to, weight, cost_basis, opened_by_cycle, run_id)
            VALUES (?, ?, NULL, ?, ?, ?, ?)
            """,
            (aid, cycle_date, targets[aid], row["cost_basis"], cycle_run_id, cycle_run_id),
        )
    conn.commit()
    return opened, closed


def reset_replay_range(conn: Database, date_from: str, date_to: str) -> None:
    """``cycle backfill --force``: wipe this date range's prior replay state so it can be
    fully recomputed, without touching anything outside it.

    A stint opened inside the range is deleted outright (the redo decides whether it ever
    existed); one closed inside the range is reopened (the redo decides when, if ever, it
    closes again); a stint wholly outside the range -- opened before *date_from* and never
    touched inside it -- is left alone. Also drops the range's ``cycle_run`` rows (cascading
    to their ``cycle_checkpoint``/``cycle_ranking`` rows), so every step in the range
    re-executes rather than being skipped as already ``done``.
    """
    conn.execute(
        "DELETE FROM portfolio_position_replay WHERE valid_from BETWEEN ? AND ?",
        (date_from, date_to),
    )
    conn.execute(
        "UPDATE portfolio_position_replay SET valid_to = NULL WHERE valid_to BETWEEN ? AND ?",
        (date_from, date_to),
    )
    conn.execute(
        "DELETE FROM cycle_run WHERE cycle_type = 'REPLAY' AND cycle_date BETWEEN ? AND ?",
        (date_from, date_to),
    )
    conn.commit()
