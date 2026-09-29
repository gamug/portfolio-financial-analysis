"""Delete a stale ``live_book`` snapshot and its dependent rows (T-121).

``quant evaluate`` snapshots the live book into ``quant_portfolio`` (``kind='live_book'``,
``persist.py::_snapshot_live_book``) each time it runs, so it can grade the optimized books
against it later. If the live ``portfolio_position`` book was ever corrupted and later repaired
(T-104), a snapshot taken before the repair is a permanent, stale copy of the wrong book --
nothing re-derives it, and any ``evaluate``/dry run that reads it (as T-108's pre-correction PR
body did, against ``quant_portfolio`` id 4) produces meaningless active-return figures.

:func:`plan_void` works out, read-only, what deleting portfolio *P* means: its own row, its
``quant_position`` stints, its ``quant_benchmark_performance`` rows, and any
``quant_frontier_point`` rows (never populated for a ``live_book`` in practice, but read so the
plan is never silently incomplete). :func:`apply_void` deletes the ``quant_portfolio`` row in
one transaction; ``quant_position``/``quant_benchmark_performance`` cascade
(``ON DELETE CASCADE``) and ``quant_frontier_point`` is deleted explicitly first (its FK carries
no cascade action, since a real optimized book's frontier points must never vanish as a side
effect of deleting an unrelated row).

Scoped to ``kind='live_book'`` only -- refuses any other kind (``min_var``, ``tangency``,
``frontier_k``, ...), which every other book downstream (``evaluate``, the API's
``v_quant_vs_live``, reports) actually depends on; this tool exists to clean up an erroneous
*snapshot*, never to delete an optimized book.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from portfolio_common.db import Database


class PortfolioNotFound(RuntimeError):
    """No ``quant_portfolio`` row with that id."""


class NotALiveBookSnapshot(RuntimeError):
    """The row is a real optimized book, not a ``live_book`` snapshot -- refused."""


@dataclass
class VoidPlan:
    portfolio_id: int
    as_of: str
    status: str
    # (ticker, weight, valid_from, valid_to)
    positions: list[tuple[str, float, str, str | None]] = field(default_factory=list)
    performance_rows: int = 0
    frontier_points: int = 0


def plan_void(conn: Database, portfolio_id: int) -> VoidPlan:
    row = conn.execute(
        "SELECT id, as_of, kind, status FROM quant_portfolio WHERE id = ?", (portfolio_id,)
    ).fetchone()
    if row is None:
        raise PortfolioNotFound(f"quant_portfolio {portfolio_id} does not exist")
    if row["kind"] != "live_book":
        raise NotALiveBookSnapshot(
            f"quant_portfolio {portfolio_id} is kind={row['kind']!r}, not 'live_book' -- "
            "this tool only voids a stale live-book snapshot, never an optimized book"
        )
    positions = [
        (str(r["ticker"]), float(r["weight"]), str(r["valid_from"]), r["valid_to"])
        for r in conn.execute(
            "SELECT a.ticker, p.weight, p.valid_from, p.valid_to FROM quant_position p "
            "JOIN assets a ON a.id = p.asset_id WHERE p.portfolio_id = ? ORDER BY a.ticker",
            (portfolio_id,),
        )
    ]
    performance_rows = int(
        conn.execute(
            "SELECT COUNT(*) FROM quant_benchmark_performance WHERE portfolio_id = ?",
            (portfolio_id,),
        ).fetchone()[0]
    )
    frontier_points = int(
        conn.execute(
            "SELECT COUNT(*) FROM quant_frontier_point WHERE portfolio_id = ?", (portfolio_id,)
        ).fetchone()[0]
    )
    return VoidPlan(
        portfolio_id=int(row["id"]),
        as_of=str(row["as_of"]),
        status=str(row["status"]),
        positions=positions,
        performance_rows=performance_rows,
        frontier_points=frontier_points,
    )


def apply_void(conn: Database, plan: VoidPlan) -> None:
    """Delete the plan's ``quant_portfolio`` row and everything it plans to take with it."""
    with conn.transaction():
        conn.execute(
            "DELETE FROM quant_frontier_point WHERE portfolio_id = ?", (plan.portfolio_id,)
        )
        conn.execute("DELETE FROM quant_portfolio WHERE id = ?", (plan.portfolio_id,))
