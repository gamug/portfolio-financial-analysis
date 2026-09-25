"""``v_quant_vs_live`` keeps live-only positions (T-042).

The view was one ``LEFT JOIN`` anchored on the optimized books, so a name held in the live
``portfolio_position`` book but in no optimized book vanished from it entirely -- not
null-weighted, gone (production 2026-09-22: APA, one of ten live names). It now adds one
``kind = 'LIVE_ONLY'`` row per such name and as-of date.
"""

from __future__ import annotations

from typing import Any

import pytest
from portfolio_common.db import Database

from quant.db import PortfolioRow, insert_portfolio, sync_positions

AS_OF = "2026-01-02"


def _book(conn: Database, kind: str, weights: dict[int, float], as_of: str = AS_OF) -> int:
    pid = insert_portfolio(
        conn,
        PortfolioRow(
            as_of=as_of,
            kind=kind,
            objective=kind,
            solver="CLARABEL",
            status="optimal",
            expected_return=0.05,
            expected_vol=0.1,
            sharpe=0.5,
            rf_annual=0.04,
            n_positions=len(weights),
            engine_version="opt-v1+abcd1234",
        ),
    )
    sync_positions(conn, pid, as_of, weights)
    return pid


def _live(
    conn: Database, asset_id: int, weight: float, valid_from: str, valid_to: Any = None
) -> None:
    conn.execute(
        "INSERT INTO portfolio_position (asset_id, valid_from, valid_to, weight) VALUES (?, ?, ?, ?)",
        (asset_id, valid_from, valid_to, weight),
    )


@pytest.fixture
def books(memory_quant_db: Database) -> Database:
    """Books min_var {1, 2} and tangency {2, 3} on AS_OF, plus a live_book snapshot holding
    4 (a reference book, not a benchmark). Live: 1 and 4 open at AS_OF, 5 closed the day
    before, 6 opening later."""
    conn = memory_quant_db
    conn.execute(
        "INSERT INTO assets (id, ticker, company_name) VALUES (1, 'AA', 'A'), (2, 'BB', 'B'), "
        "(3, 'CC', 'C'), (4, 'DD', 'D'), (5, 'EE', 'E'), (6, 'FF', 'F')"
    )
    _book(conn, "min_var", {1: 0.6, 2: 0.4})
    _book(conn, "tangency", {2: 0.7, 3: 0.3})
    _book(conn, "live_book", {1: 0.5, 4: 0.3})
    _live(conn, 1, 0.5, "2025-12-01")
    _live(conn, 4, 0.3, "2025-12-01")
    _live(conn, 5, 0.2, "2025-12-01", "2026-01-01")
    _live(conn, 6, 0.1, "2026-02-01")
    conn.commit()
    return conn


def _rows(conn: Database) -> list[tuple[Any, ...]]:
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT as_of, kind, ticker, benchmark_weight, live_weight, active_weight "
            "FROM v_quant_vs_live ORDER BY kind, ticker"
        )
    ]


def test_a_live_name_no_benchmark_holds_gets_a_live_only_row(books: Database) -> None:
    live_only = [r for r in _rows(books) if r[1] == "LIVE_ONLY"]
    assert live_only == [(AS_OF, "LIVE_ONLY", "DD", None, 0.3, -0.3)]


def test_the_benchmark_rows_are_unchanged(books: Database) -> None:
    bench = [r for r in _rows(books) if r[1] != "LIVE_ONLY"]
    assert bench == [
        (AS_OF, "min_var", "AA", 0.6, 0.5, pytest.approx(0.1)),
        (AS_OF, "min_var", "BB", 0.4, None, 0.4),
        (AS_OF, "tangency", "BB", 0.7, None, 0.7),
        (AS_OF, "tangency", "CC", 0.3, None, 0.3),
    ]


def test_every_live_position_open_at_the_date_appears(books: Database) -> None:
    """The invariant the rewrite exists for; positions closed before, or opened after, the
    as-of date are not part of that date's live book."""
    shown = {
        r["ticker"]
        for r in books.execute(
            "SELECT DISTINCT ticker FROM v_quant_vs_live WHERE live_weight IS NOT NULL"
        )
    }
    assert shown == {"AA", "DD"}


def test_no_live_only_rows_for_a_date_without_optimized_books(books: Database) -> None:
    """On 2026-03-02 names 4 and 6 are live, but only a live_book snapshot exists: there is
    no benchmark to compare against, so that date contributes no rows."""
    _book(books, "live_book", {4: 1.0}, as_of="2026-03-02")
    books.commit()
    assert {r[0] for r in _rows(books)} == {AS_OF}
