"""A book is written once per key, NULL ``frontier_k`` included (T-101).

``quant_portfolio`` is keyed ``(as_of, kind, frontier_k, engine_version)``, but ``frontier_k``
is NULL for every non-frontier book and SQL NULLs never compare equal, so the table's own
``UNIQUE`` / ``ON CONFLICT`` never matched: every identical ``optimize`` or ``evaluate`` re-run
inserted another copy of each book, and the read-back returned the *oldest* copy, so positions
kept landing on it while the new rows had none. ``insert_portfolio`` now matches the key with
``frontier_k IS ?``, and migration m007 merges existing duplicates and adds a NULL-safe index.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from portfolio_common.db import Database

from kg_schema import connect, ensure, queries
from kg_schema.migrations import MIGRATIONS, _m007_quant_portfolio_null_safe_key
from quant.config import QuantSettings
from quant.db import PortfolioRow, insert_portfolio, sync_positions
from quant.persist import run_optimize
from quant.returns import run_build_returns


def _row(**over: object) -> PortfolioRow:
    base: dict[str, object] = {
        "as_of": "2026-01-02",
        "kind": "min_var",
        "objective": "min_var",
        "solver": "CLARABEL",
        "status": "optimal",
        "expected_return": 0.05,
        "expected_vol": 0.12,
        "sharpe": 0.4,
        "rf_annual": 0.045,
        "n_positions": 2,
        "engine_version": "opt-v1+abcd1234",
    }
    base.update(over)
    return PortfolioRow(**base)  # type: ignore[arg-type]


def _count(conn: Database, table: str = "quant_portfolio") -> int:
    sql = {
        "quant_portfolio": "SELECT COUNT(*) FROM quant_portfolio",
        "quant_position": "SELECT COUNT(*) FROM quant_position",
        "quant_benchmark_performance": "SELECT COUNT(*) FROM quant_benchmark_performance",
    }[table]
    return int(conn.execute(sql).fetchone()[0])


# -- insert_portfolio -------------------------------------------------------------------------


def test_a_non_frontier_book_is_updated_in_place(memory_quant_db: Database) -> None:
    conn = memory_quant_db
    first = insert_portfolio(conn, _row(quant_run_id=1, sharpe=0.4))
    second = insert_portfolio(conn, _row(quant_run_id=2, sharpe=0.9))
    assert second == first
    assert _count(conn) == 1
    stored = conn.execute("SELECT quant_run_id, sharpe FROM quant_portfolio").fetchone()
    assert (stored["quant_run_id"], stored["sharpe"]) == (2, 0.9)


def test_the_live_book_snapshot_is_updated_in_place(memory_quant_db: Database) -> None:
    conn = memory_quant_db
    live = {"kind": "live_book", "objective": "live", "engine_version": "opt-v1"}
    a = insert_portfolio(conn, _row(**live))
    b = insert_portfolio(conn, _row(**live))
    assert a == b
    assert _count(conn) == 1


def test_frontier_books_stay_one_per_k(memory_quant_db: Database) -> None:
    conn = memory_quant_db
    ids = {insert_portfolio(conn, _row(kind="frontier_k", frontier_k=k)) for k in (1, 2, 1, 2)}
    assert len(ids) == 2
    assert _count(conn) == 2


def test_different_keys_are_different_books(memory_quant_db: Database) -> None:
    conn = memory_quant_db
    ids = {
        insert_portfolio(conn, _row()),
        insert_portfolio(conn, _row(kind="tangency")),
        insert_portfolio(conn, _row(as_of="2026-01-03")),
        insert_portfolio(conn, _row(engine_version="opt-v1+ffff0000")),
    }
    assert len(ids) == 4


def test_rerunning_optimize_refreshes_its_books(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=6, n_days=280, with_dividends=True)
    run_build_returns(
        QuantSettings(db_path=Path(":memory:")),
        date_from="2000-01-01",
        date_to="2100-01-01",
        conn=conn,
    )
    as_of = str(conn.execute("SELECT MAX(obs_date) FROM quant_return_daily").fetchone()[0])
    settings = QuantSettings(
        db_path=Path(":memory:"),
        lookback_days=200,
        min_history_days=140,
        liquidity_min_dollar_volume=0.0,
        max_name_weight=None,
        max_sector_weight=None,
        objectives=["min_var", "tangency"],
    )
    one = run_optimize(settings, as_of=as_of, conn=conn)
    two = run_optimize(settings, as_of=as_of, conn=conn)
    assert two.books == one.books  # same ids
    assert _count(conn) == 2  # was 4 before T-101
    run_ids = {
        int(r["quant_run_id"]) for r in conn.execute("SELECT quant_run_id FROM quant_portfolio")
    }
    latest = int(
        conn.execute("SELECT MAX(id) FROM quant_run WHERE command = 'optimize'").fetchone()[0]
    )
    assert run_ids == {latest}  # the metadata is the second run's
    for pid in two.books.values():  # the returned ids carry the positions
        n = conn.execute(
            "SELECT COUNT(*) FROM quant_position WHERE portfolio_id = ?", (pid,)
        ).fetchone()[0]
        assert n > 0


# -- migration m007 ---------------------------------------------------------------------------


@pytest.fixture
def duplicated(memory_quant_db: Database) -> Database:
    """The pre-T-101 state after one re-run: book 1 (oldest) holds the positions but stale
    metadata; book 2 (newest) holds the latest metadata and no positions; both have forward
    performance rows; a frontier point references book 2. Plus an unrelated single book 3."""
    conn = memory_quant_db
    conn.execute(
        "INSERT INTO assets (id, ticker, company_name) VALUES (1, 'AA', 'A'), (2, 'BB', 'B')"
    )
    conn.execute(
        "INSERT INTO quant_risk_model (id, as_of, model_version, lookback_days, "
        "min_history_days, n_assets, cov_estimator, ret_estimator, periods_per_year, "
        "panel_engine_version, panel_spec_json, computed_at) VALUES (1, '2026-01-02', "
        "'rm-v1+abcd1234', 756, 504, 2, 'ledoit_wolf_cc', 'equilibrium', 252, 'qret-v2', '{}', "
        "'2026-01-02T00:00:00Z')"
    )
    for pid, run, sharpe, kind in (
        (1, 10, 0.4, "min_var"),
        (2, 11, 0.9, "min_var"),
        (3, 11, 0.7, "tangency"),
    ):
        conn.execute(
            "INSERT INTO quant_portfolio (id, quant_run_id, model_id, as_of, kind, objective, "
            "solver, status, sharpe, n_positions, engine_version, computed_at) VALUES "
            "(?, ?, 1, '2026-01-02', ?, ?, 'CLARABEL', 'optimal', ?, 2, 'opt-v1+abcd1234', ?)",
            (pid, run, kind, kind, sharpe, f"2026-01-0{pid}T00:00:00Z"),
        )
    sync_positions(conn, 1, "2026-01-02", {1: 0.6, 2: 0.4})
    sync_positions(conn, 3, "2026-01-02", {1: 0.5, 2: 0.5})
    for pid in (1, 2, 3):
        conn.execute(
            "INSERT INTO quant_benchmark_performance (portfolio_id, date, realized_return, "
            "cumulative_return, engine_version, computed_at) VALUES "
            "(?, '2026-01-05', 0.01, 0.01, 'perf-v1', '2026-01-05T00:00:00Z')",
            (pid,),
        )
    conn.execute(
        "INSERT INTO quant_frontier_point (model_id, k, target_return, expected_return, "
        "expected_vol, status, weights_json, portfolio_id) VALUES "
        "(1, 1, 0.05, 0.05, 0.1, 'optimal', '{}', 2)"
    )
    conn.commit()
    return conn


def test_m007_merges_duplicates_into_the_book_with_the_positions(duplicated: Database) -> None:
    conn = duplicated
    _m007_quant_portfolio_null_safe_key(conn)
    books = conn.execute(
        "SELECT id, quant_run_id, sharpe FROM quant_portfolio ORDER BY id"
    ).fetchall()
    assert [(b["id"], b["quant_run_id"], b["sharpe"]) for b in books] == [
        (1, 11, 0.9),  # kept id 1, refreshed with book 2's metadata
        (3, 11, 0.7),  # untouched
    ]
    assert (
        conn.execute("SELECT COUNT(*) FROM quant_position WHERE portfolio_id = 1").fetchone()[0]
        == 2
    )
    perf = {int(r[0]) for r in conn.execute("SELECT portfolio_id FROM quant_benchmark_performance")}
    assert perf == {1, 3}  # book 2's derived rows went with it
    assert conn.execute("SELECT portfolio_id FROM quant_frontier_point").fetchone()[0] == 1


def test_m007_then_refuses_a_duplicate_book(duplicated: Database) -> None:
    conn = duplicated
    _m007_quant_portfolio_null_safe_key(conn)
    with pytest.raises(sqlite3.IntegrityError, match="ux_quant_portfolio_book"):
        conn.execute(
            "INSERT INTO quant_portfolio (as_of, kind, objective, solver, status, n_positions, "
            "engine_version, computed_at) VALUES ('2026-01-02', 'min_var', 'min_var', 'x', "
            "'optimal', 0, 'opt-v1+abcd1234', 'now')"
        )
    # a frontier book with a real k is still its own key
    conn.execute(
        "INSERT INTO quant_portfolio (as_of, kind, frontier_k, objective, solver, status, "
        "n_positions, engine_version, computed_at) VALUES ('2026-01-02', 'frontier_k', 1, "
        "'frontier', 'x', 'optimal', 0, 'opt-v1+abcd1234', 'now')"
    )


def test_m007_is_a_noop_without_duplicates_and_idempotent(memory_quant_db: Database) -> None:
    conn = memory_quant_db
    insert_portfolio(conn, _row())
    _m007_quant_portfolio_null_safe_key(conn)
    _m007_quant_portfolio_null_safe_key(conn)
    assert _count(conn) == 1


def test_m007_skips_a_database_without_quant_tables() -> None:
    conn = connect(":memory:")
    _m007_quant_portfolio_null_safe_key(conn)  # no quant_portfolio: nothing to do


def test_m007_is_registered_and_advances_the_floor(memory_quant_db: Database) -> None:
    assert (7, _m007_quant_portfolio_null_safe_key) in {(v, fn) for v, _d, fn in MIGRATIONS}
    ensure(memory_quant_db, run_migrations=True)
    assert queries.current_version(memory_quant_db) >= 7
    names = {
        r[0]
        for r in memory_quant_db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'quant_portfolio'"
        )
    }
    assert "ux_quant_portfolio_book" in names
