"""verify_pilot's T-153f check (T-153 part 3d): the import's rows never leave through a view,
no line mixes vendors, and the run record equals the database.

Each test builds the state the check must catch on a scratch database, so the check's own
detection is exercised -- not only its happy path.
"""

from __future__ import annotations

import json
from pathlib import Path

from portfolio_common.db import Database
from verify_pilot import tiingo_import_problems

from kg_schema.db import connect
from pricing_agent.db import ensure_schema


def _db(tmp_path: Path) -> tuple[str, Database]:
    path = tmp_path / "financial.db"
    conn = connect(path)
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO assets (id, ticker, cik) VALUES (1, 'K', '0000055067'), "
        "(2, 'AAPL', '0000320193')"
    )
    conn.commit()
    return str(path), conn


def _import_run(conn: Database, *, kind: str = "tiingo-import", price_rows: int = 2) -> None:
    conn.execute(
        "INSERT INTO pricing_run (started_at, status, params_json) VALUES ('now', 'completed', ?)",
        (
            json.dumps(
                {
                    "kind": kind,
                    "file_sha256": "a" * 64,
                    "price_rows": price_rows,
                    "dividend_rows": 0,
                    "split_rows": 0,
                }
            ),
        ),
    )
    conn.commit()


def _tiingo_bar(conn: Database, asset_id: int = 1, day: str = "2026-09-01") -> None:
    conn.execute(
        "INSERT INTO price_daily (asset_id, date, close, source) VALUES (?, ?, 100.0, 'tiingo')",
        (asset_id, day),
    )
    conn.commit()


def test_a_clean_import_passes(tmp_path: Path) -> None:
    path, conn = _db(tmp_path)
    _tiingo_bar(conn)
    _tiingo_bar(conn, 1, "2026-09-02")
    _import_run(conn, price_rows=2)
    conn.execute(
        "INSERT INTO price_daily (asset_id, date, close, source) "
        "VALUES (2, '2026-09-01', 200.0, 'yfinance')"
    )
    conn.commit()
    rows, reason, problems = tiingo_import_problems(path)
    assert (rows, reason, problems) == (1, None, [])


def test_a_database_with_no_imported_row_is_not_judged(tmp_path: Path) -> None:
    path, conn = _db(tmp_path)
    conn.execute(
        "INSERT INTO price_daily (asset_id, date, close, source) "
        "VALUES (2, '2026-09-01', 200.0, 'yfinance')"
    )
    conn.commit()
    rows, reason, problems = tiingo_import_problems(path)
    assert (rows, problems) == (0, [])
    assert reason is not None and "no Tiingo-sourced price row" in reason


def test_a_price_view_that_stopped_filtering_fails(tmp_path: Path) -> None:
    """The regression the check exists for: the filter is part of the read contract."""
    path, conn = _db(tmp_path)
    _tiingo_bar(conn)
    conn.execute(
        "INSERT INTO price_observation (asset_id, obs_date, close, source, event_time, "
        "computed_at, engine_version) VALUES (1, '2026-09-01', 100.0, 'tiingo', '2026-09-01', "
        "'now', 'priceobs-v2')"
    )
    _import_run(conn, price_rows=1)
    conn.execute("DROP VIEW v_price_observation")
    conn.execute(
        "CREATE VIEW v_price_observation AS SELECT p.id, a.ticker, p.asset_id, p.obs_date, p.close, "
        "p.source FROM price_observation p JOIN assets a ON a.id = p.asset_id"
    )
    conn.commit()
    _rows, _reason, problems = tiingo_import_problems(path)
    assert any("v_price_observation returns 1" in problem for problem in problems), problems


def test_a_cost_basis_that_stopped_being_nulled_fails(tmp_path: Path) -> None:
    path, conn = _db(tmp_path)
    _tiingo_bar(conn)
    conn.execute(
        "INSERT INTO portfolio_position (asset_id, valid_from, weight, cost_basis) "
        "VALUES (1, '2026-09-01', 0.05, 100.0)"
    )
    _import_run(conn, price_rows=1)
    conn.execute("DROP VIEW v_portfolio_position")
    conn.execute(
        "CREATE VIEW v_portfolio_position AS SELECT p.id, a.ticker, p.asset_id, p.valid_from, "
        "p.valid_to, p.weight, p.cost_basis, p.opened_by_cycle, p.run_id FROM portfolio_position p "
        "JOIN assets a ON a.id = p.asset_id"
    )
    conn.commit()
    _rows, _reason, problems = tiingo_import_problems(path)
    assert any("v_portfolio_position returns 1" in problem for problem in problems), problems


def test_a_line_that_mixes_two_vendors_fails(tmp_path: Path) -> None:
    path, conn = _db(tmp_path)
    _tiingo_bar(conn)
    conn.execute(
        "INSERT INTO price_daily (asset_id, date, close, source) "
        "VALUES (1, '2026-09-02', 101.0, 'yfinance')"
    )
    _import_run(conn, price_rows=1)
    conn.commit()
    _rows, _reason, problems = tiingo_import_problems(path)
    assert any(
        "mix the gateway's and the import's price_daily" in problem for problem in problems
    ), problems


def test_imported_rows_with_no_run_record_fail(tmp_path: Path) -> None:
    path, conn = _db(tmp_path)
    _tiingo_bar(conn)
    _rows, _reason, problems = tiingo_import_problems(path)
    assert any("no import run records them" in problem for problem in problems), problems


def test_a_run_record_that_disagrees_with_the_database_fails(tmp_path: Path) -> None:
    """D7's rule 4: the record's counts are the database's counts."""
    path, conn = _db(tmp_path)
    _tiingo_bar(conn)
    _import_run(conn, price_rows=99)
    _rows, _reason, problems = tiingo_import_problems(path)
    assert any(
        "the import runs account for 99 price_rows, the database holds 1" in p for p in problems
    ), problems


def test_a_re_run_that_wrote_nothing_does_not_break_the_record_check(tmp_path: Path) -> None:
    """An idempotent re-run records zero rows; the counts are summed over the runs, so the
    database still agrees with what the imports account for."""
    path, conn = _db(tmp_path)
    _tiingo_bar(conn)
    _import_run(conn, price_rows=1)
    _import_run(conn, price_rows=0)  # the second run wrote nothing
    _rows, reason, problems = tiingo_import_problems(path)
    assert (reason, problems) == (None, [])


def test_a_removal_that_deleted_every_row_is_reported_not_judged(tmp_path: Path) -> None:
    path, conn = _db(tmp_path)
    _import_run(conn, price_rows=1)
    _import_run(conn, kind="tiingo-removal", price_rows=1)  # nothing left in the database
    _rows, reason, problems = tiingo_import_problems(path)
    assert problems == []
    assert reason is not None and "no Tiingo-sourced price row" in reason
