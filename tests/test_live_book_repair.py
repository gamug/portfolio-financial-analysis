"""The live book keeps its history, cannot hold an inverted stint, and a backdated run's
writes can be undone (T-104).

Production's live book still carried a backdated 2026-06-30 selection run written over the
2026-09-22 book: the incumbents' weights overwritten in place, WFC's stint closed before it
opened, BF.B opened as if held since 2026-06-30. The fixtures below rebuild that shape.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from portfolio_common.db import Database

from cycle.cli import main as cycle_main
from cycle.repair import NotBackdated, apply_undo, plan_undo
from cycle.writers import sync_positions
from fundamental_agent import db as fa_db
from kg_schema import connect, ensure

LATER, EARLIER = "2026-09-22", "2026-06-30"


def _stints(conn: Database) -> list[tuple[int, str, str | None, float]]:
    return [
        (int(r["asset_id"]), str(r["valid_from"]), r["valid_to"], float(r["weight"]))
        for r in conn.execute(
            "SELECT asset_id, valid_from, valid_to, weight FROM portfolio_position ORDER BY id"
        )
    ]


def _live(conn: Database, day: str) -> dict[int, float]:
    return {
        int(r["asset_id"]): float(r["weight"])
        for r in conn.execute(
            "SELECT asset_id, weight FROM portfolio_position WHERE valid_from <= ? "
            "AND (valid_to IS NULL OR valid_to > ?)",
            (day, day),
        )
    }


@pytest.fixture
def book(memory_db: Database) -> Database:
    memory_db.execute(
        "INSERT INTO assets (id, ticker) VALUES (1, 'APA'), (2, 'WFC'), (3, 'BFB'), (4, 'XOM')"
    )
    memory_db.commit()
    return memory_db


# -- sync_positions keeps every weight on record -------------------------------------------------


def test_a_reweight_is_a_new_stint_and_the_old_weight_stays_on_record(book: Database) -> None:
    sync_positions(book, "2026-01-02", {1: 0.6, 2: 0.4}, {1: 10.0, 2: 20.0}, cycle_run_id=1)
    sync_positions(book, "2026-02-02", {1: 0.5, 2: 0.4}, {1: 11.0, 2: 21.0}, cycle_run_id=2)
    assert _stints(book) == [
        (1, "2026-01-02", "2026-02-02", 0.6),  # the old weight, closed where it changed
        (2, "2026-01-02", None, 0.4),  # unchanged: untouched
        (1, "2026-02-02", None, 0.5),
    ]
    carried = book.execute(
        "SELECT cost_basis FROM portfolio_position WHERE asset_id = 1 AND valid_to IS NULL"
    ).fetchone()[0]
    assert carried == 10.0  # the holding continues: its cost basis carries over


def test_a_same_date_rerun_updates_in_place(book: Database) -> None:
    sync_positions(book, "2026-01-02", {1: 0.6}, {}, cycle_run_id=1)
    sync_positions(book, "2026-01-02", {1: 0.7}, {}, cycle_run_id=1)
    assert _stints(book) == [(1, "2026-01-02", None, 0.7)]


def test_the_database_refuses_a_stint_that_ends_before_it_starts(book: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="valid_to before valid_from"):
        book.execute(
            "INSERT INTO portfolio_position (asset_id, valid_from, valid_to, weight) "
            "VALUES (1, ?, ?, 0.1)",
            (LATER, EARLIER),
        )
    book.execute(
        "INSERT INTO portfolio_position (asset_id, valid_from, weight) VALUES (1, ?, 0.1)",
        (LATER,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="valid_to before valid_from"):
        book.execute("UPDATE portfolio_position SET valid_to = ?", (EARLIER,))
    book.execute("UPDATE portfolio_position SET valid_to = valid_from")  # held for no day: fine


# -- undoing the backdated run -----------------------------------------------------------------


def _corrupted(conn: Database) -> None:
    """Production's shape, written the way the pre-T-104 code wrote it (triggers dropped):
    run 1 (LATER) picked APA 0.6 / WFC 0.4; run 2 (EARLIER, backdated) picked APA 0.5 /
    BFB 0.5 -- overwriting APA's weight in place, closing WFC at EARLIER, opening BFB."""
    conn.execute("DROP TRIGGER trg_pp_range_insert")
    conn.execute("DROP TRIGGER trg_pp_range_update")
    conn.execute(
        "INSERT INTO cycle_run (id, cycle_type, cycle_date, started_at, status) VALUES "
        "(1, 'SELECTION', ?, 't1', 'completed'), (2, 'SELECTION', ?, 't2', 'completed')",
        (LATER, EARLIER),
    )
    for run, aid, rank, weight in ((1, 1, 1, 0.6), (1, 2, 2, 0.4), (2, 1, 1, 0.5), (2, 3, 2, 0.5)):
        conn.execute(
            "INSERT INTO cycle_ranking (cycle_run_id, asset_id, rank, blended_score, selected, "
            "target_weight) VALUES (?, ?, ?, 50.0, 1, ?)",
            (run, aid, rank, weight),
        )
    conn.execute(
        "INSERT INTO portfolio_position (asset_id, valid_from, valid_to, weight, "
        "opened_by_cycle) VALUES (1, ?, NULL, 0.5, 1), (2, ?, ?, 0.4, 1), (3, ?, NULL, 0.5, 2)",
        (LATER, LATER, EARLIER, EARLIER),
    )
    conn.commit()
    ensure(conn)  # the triggers come back, as on any real database after T-104


def test_undo_restores_the_later_run_s_book(book: Database) -> None:
    _corrupted(book)
    assert _live(book, LATER) == {1: 0.5, 3: 0.5}  # the corruption, as production has it

    plan = plan_undo(book, 2)
    assert [(t, vf) for _s, t, vf in plan.void] == [("BFB", EARLIER)]
    assert [(t, vf) for _s, t, vf in plan.reopen] == [("WFC", LATER)]
    assert [(t, old, new) for _s, t, old, new in plan.reweight] == [("APA", 0.5, 0.6)]
    apply_undo(book, plan)

    assert _live(book, LATER) == {1: 0.6, 2: 0.4}
    assert _live(book, EARLIER) == {}  # BFB was never held
    assert book.execute("SELECT status FROM cycle_run WHERE id = 2").fetchone()[0] == "reverted"
    assert (
        book.execute(
            "SELECT COUNT(*) FROM portfolio_position WHERE valid_to < valid_from"
        ).fetchone()[0]
        == 0
    )
    assert plan_undo(book, 2).empty  # idempotent: nothing left to revert


def test_only_a_backdated_run_can_be_undone(book: Database) -> None:
    _corrupted(book)
    with pytest.raises(NotBackdated, match="not older"):
        plan_undo(book, 1)


def test_the_command_is_a_dry_run_unless_applied(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "fin.db"
    conn = connect(path)
    fa_db.ensure_schema(conn)
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'APA'), (2, 'WFC'), (3, 'BFB')")
    _corrupted(conn)
    conn.close()

    assert cycle_main(["undo-run", "--cycle-run", "2", "--db", str(path)]) == 0
    out = capsys.readouterr().out
    assert "would revert (dry run)" in out and "reopen  stint" in out and "void    stint" in out
    check = connect(path)
    assert _live(check, LATER) == {1: 0.5, 3: 0.5}  # untouched
    check.close()

    assert cycle_main(["undo-run", "--cycle-run", "2", "--db", str(path), "--apply"]) == 0
    assert "marked reverted" in capsys.readouterr().out
    check = connect(path)
    assert _live(check, LATER) == {1: 0.6, 2: 0.4}
    check.close()
    assert cycle_main(["undo-run", "--cycle-run", "1", "--db", str(path)]) == 1  # refused
