"""Undo a backdated ``cycle select`` run's writes to the live book (T-104).

A selection run dated *earlier* than the live book's latest date closes the book's newer
stints at its own, earlier date (a stint that ends before it starts), opens its own picks as
if held since that date, and -- before T-104 -- overwrote the surviving stints' weights in
place. The out-of-order guard (T-097) now refuses such a run, and ``sync_positions`` no longer
overwrites weights, but a book corrupted before either landed stays corrupted until repaired.

:func:`plan_undo` works out, read-only, what reverting run *R* (dated *D*) means:

- **void** every stint *R* opened at *D*: ``valid_to = valid_from`` -- held for no day at all,
  kept on record (never deleted) and attributable to *R* through ``opened_by_cycle``;
- **reopen** every stint *R* closed that had started after *D* (``valid_from > D``,
  ``valid_to = D``): ``valid_to = NULL``;
- **reweight** every stint left open to the target its own opening run chose
  (``cycle_ranking.target_weight``), undoing *R*'s in-place overwrite.

:func:`apply_undo` executes the plan in one transaction and marks *R* ``reverted``. Refused
unless *R* really was backdated: its date must be older than a stint another run opened.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from portfolio_common.db import Database

from cycle.writers import WEIGHT_EPS


class NotBackdated(RuntimeError):
    """The run was not dated before the live book it wrote into, so there is nothing to undo."""


@dataclass
class UndoPlan:
    cycle_run_id: int
    cycle_date: str
    # (stint id, ticker, valid_from)
    void: list[tuple[int, str, str]] = field(default_factory=list)
    reopen: list[tuple[int, str, str]] = field(default_factory=list)
    # (stint id, ticker, current weight, restored weight)
    reweight: list[tuple[int, str, float | None, float]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.void or self.reopen or self.reweight)


def plan_undo(conn: Database, cycle_run_id: int) -> UndoPlan:
    run = conn.execute(
        "SELECT id, cycle_type, cycle_date, status FROM cycle_run WHERE id = ?", (cycle_run_id,)
    ).fetchone()
    if run is None or run["cycle_type"] != "SELECTION":
        raise NotBackdated(f"cycle_run {cycle_run_id} is not a SELECTION run")
    day = str(run["cycle_date"])
    latest_other = conn.execute(
        "SELECT MAX(valid_from) FROM portfolio_position "
        "WHERE opened_by_cycle IS NULL OR opened_by_cycle != ?",
        (cycle_run_id,),
    ).fetchone()[0]
    if latest_other is None or day >= str(latest_other):
        raise NotBackdated(
            f"cycle_run {cycle_run_id} ({day}) is not older than the rest of the live book "
            f"(latest valid_from {latest_other}); nothing to undo"
        )
    plan = UndoPlan(cycle_run_id, day)
    for r in conn.execute(
        "SELECT p.id, a.ticker, p.valid_from FROM portfolio_position p "
        "JOIN assets a ON a.id = p.asset_id "
        "WHERE p.opened_by_cycle = ? AND p.valid_from = ? "
        "AND (p.valid_to IS NULL OR p.valid_to != p.valid_from) ORDER BY a.ticker",
        (cycle_run_id, day),
    ):
        plan.void.append((int(r["id"]), str(r["ticker"]), str(r["valid_from"])))
    for r in conn.execute(
        "SELECT p.id, a.ticker, p.valid_from FROM portfolio_position p "
        "JOIN assets a ON a.id = p.asset_id "
        "WHERE p.valid_to = ? AND p.valid_from > ? ORDER BY a.ticker",
        (day, day),
    ):
        plan.reopen.append((int(r["id"]), str(r["ticker"]), str(r["valid_from"])))
    voided = {v[0] for v in plan.void}
    reopened = {r[0] for r in plan.reopen}
    for r in conn.execute(
        "SELECT p.id, a.ticker, p.weight, p.valid_to, cr.target_weight "
        "FROM portfolio_position p JOIN assets a ON a.id = p.asset_id "
        "JOIN cycle_ranking cr ON cr.cycle_run_id = p.opened_by_cycle "
        "AND cr.asset_id = p.asset_id "
        "WHERE p.opened_by_cycle != ? AND cr.target_weight IS NOT NULL ORDER BY a.ticker",
        (cycle_run_id,),
    ):
        sid = int(r["id"])
        still_open = r["valid_to"] is None or sid in reopened
        if sid in voided or not still_open:
            continue
        target = float(r["target_weight"])
        if r["weight"] is None or abs(float(r["weight"]) - target) > WEIGHT_EPS:
            plan.reweight.append((sid, str(r["ticker"]), r["weight"], target))
    return plan


def apply_undo(conn: Database, plan: UndoPlan) -> None:
    """Execute *plan* in one transaction and mark its run ``reverted``."""
    with conn.transaction():
        for sid, _t, _vf in plan.void:
            conn.execute("UPDATE portfolio_position SET valid_to = valid_from WHERE id = ?", (sid,))
        for sid, _t, _vf in plan.reopen:
            conn.execute("UPDATE portfolio_position SET valid_to = NULL WHERE id = ?", (sid,))
        for sid, _t, _w, target in plan.reweight:
            conn.execute("UPDATE portfolio_position SET weight = ? WHERE id = ?", (target, sid))
        conn.execute("UPDATE cycle_run SET status = 'reverted' WHERE id = ?", (plan.cycle_run_id,))
