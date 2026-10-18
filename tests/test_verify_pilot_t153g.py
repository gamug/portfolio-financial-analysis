"""verify_pilot's T-153g check (T-153 part 3d, D10): every recorded SPLIT row is in the tracked
event list.

PASS on a database whose SPLIT rows the list explains; FAIL naming the ticker and date on one that
holds a row the list does not.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from portfolio_common.db import Database
from verify_pilot import unexplained_split_row_problems

from kg_schema.db import connect
from pricing_agent.db import ensure_schema


def _db(tmp_path: Path) -> tuple[str, Database]:
    path = tmp_path / "financial.db"
    conn = connect(path)
    ensure_schema(conn)
    conn.execute("INSERT INTO assets (id, ticker, cik) VALUES (1, 'DD', '0001666700')")
    conn.commit()
    return str(path), conn


def _split(conn: Database, asset_id: int, ex_date: str, value: float) -> None:
    conn.execute(
        "INSERT INTO corporate_action (asset_id, action_type, ex_date, value, source, "
        "engine_version, ingested_at) VALUES (?, 'SPLIT', ?, ?, 'pricing-gateway', 'corpact-v1', "
        "'now')",
        (asset_id, ex_date, value),
    )
    conn.commit()


def test_only_explained_rows_pass(tmp_path: Path) -> None:
    path, conn = _db(tmp_path)
    _split(conn, 1, "2019-06-03", 0.4725)  # DD, the tracked reverse_split_spinoff row
    assert unexplained_split_row_problems(path) == []


def test_an_unexplained_row_fails_naming_the_ticker_and_date(tmp_path: Path) -> None:
    path, conn = _db(tmp_path)
    _split(conn, 1, "2019-06-03", 0.4725)
    _split(conn, 1, "2030-01-01", 1.5)  # not in the tracked list
    assert unexplained_split_row_problems(path) == ["DD 2030-01-01"]


def test_a_database_that_cannot_be_read_fails(tmp_path: Path) -> None:
    """Fail closed (fix round 2): a database the gate cannot read is a problem, not an all-clear."""
    broken = tmp_path / "broken.db"
    raw = sqlite3.connect(broken)
    raw.executescript(
        "CREATE TABLE corporate_action (id INTEGER PRIMARY KEY, asset_id INTEGER, action_type TEXT,"
        " ex_date TEXT, value REAL, engine_version TEXT);"
    )
    raw.commit()
    raw.close()
    problems = unexplained_split_row_problems(str(broken))
    assert len(problems) == 1 and "unreadable database" in problems[0]
