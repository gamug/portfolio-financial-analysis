"""The T-153c verify_pilot check: the ingestion scope and the point-in-time ranking.

Two directions: no ``cycle_ranking`` row for an asset that was not a member on its run's
``cycle_date`` (a leaver is never ranked outside its membership), and nothing of a leaver's
outside its D2 scope (no price or filing after its exit, no price before 2019-01-02, no filing
filed before 2022-01-01).
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

from conftest import cik_for, write_universe_db

_SPEC = importlib.util.spec_from_file_location(
    "verify_pilot", Path(__file__).resolve().parent.parent / "scripts" / "verify_pilot.py"
)
assert _SPEC and _SPEC.loader
verify = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_pilot"] = verify
_SPEC.loader.exec_module(verify)

_UNIVERSE = [("AAA", "2020-01-01", "2022-06-15"), ("BBB", "2020-01-01", None)]

_DDL = """
CREATE TABLE assets (id INTEGER PRIMARY KEY, ticker TEXT UNIQUE, cik TEXT);
CREATE TABLE price_daily (id INTEGER PRIMARY KEY, asset_id INTEGER, date TEXT, close REAL);
CREATE TABLE sec_filings (id INTEGER PRIMARY KEY, asset_id INTEGER, filing_date TEXT);
CREATE TABLE cycle_run (id INTEGER PRIMARY KEY, cycle_date TEXT);
CREATE TABLE cycle_ranking (id INTEGER PRIMARY KEY, cycle_run_id INTEGER, asset_id INTEGER);
"""


def _clean(tmp_path: Path) -> tuple[Path, Path]:
    """A leaver (AAA, to 2022-06-15) and a member (BBB), each within its D2 scope, one ranking."""
    udb = write_universe_db(tmp_path / "u.db", _UNIVERSE)
    fin = tmp_path / "f.db"
    conn = sqlite3.connect(fin)
    conn.executescript(_DDL)
    conn.executemany(
        "INSERT INTO assets (id, ticker, cik) VALUES (?, ?, ?)",
        [(1, "AAA", cik_for("AAA")), (2, "BBB", cik_for("BBB"))],
    )
    conn.execute("INSERT INTO price_daily (asset_id, date, close) VALUES (1, '2022-01-03', 1)")
    conn.execute("INSERT INTO price_daily (asset_id, date, close) VALUES (1, '2022-06-15', 1)")
    conn.execute("INSERT INTO sec_filings (asset_id, filing_date) VALUES (1, '2022-05-01')")
    conn.execute("INSERT INTO cycle_run (id, cycle_date) VALUES (1, '2022-03-01')")
    conn.execute("INSERT INTO cycle_ranking (cycle_run_id, asset_id) VALUES (1, 1), (1, 2)")
    conn.commit()
    conn.close()
    return fin, udb


def _add(fin: Path, *statements: str) -> None:
    conn = sqlite3.connect(fin)
    for statement in statements:
        conn.execute(statement)
    conn.commit()
    conn.close()


def test_the_check_passes_on_a_clean_scope(tmp_path: Path) -> None:
    fin, udb = _clean(tmp_path)
    assert verify.scope_problems(str(fin), str(udb)) == (1, 1, [])


def test_the_check_flags_a_ranking_outside_the_membership(tmp_path: Path) -> None:
    """AAA left 2022-06-15: a ranking row for it on a later date is not point-in-time."""
    fin, udb = _clean(tmp_path)
    _add(
        fin,
        "INSERT INTO cycle_run (id, cycle_date) VALUES (2, '2022-09-01')",
        "INSERT INTO cycle_ranking (cycle_run_id, asset_id) VALUES (2, 1)",
    )
    _, _, problems = verify.scope_problems(str(fin), str(udb))
    assert any("2022-09-01" in p and "not a member" in p for p in problems)


def test_the_check_flags_a_price_after_the_exit(tmp_path: Path) -> None:
    fin, udb = _clean(tmp_path)
    _add(fin, "INSERT INTO price_daily (asset_id, date, close) VALUES (1, '2022-07-01', 1)")
    _, _, problems = verify.scope_problems(str(fin), str(udb))
    assert any("AAA" in p and "after its exit" in p for p in problems)


def test_the_check_flags_a_filing_after_the_exit(tmp_path: Path) -> None:
    fin, udb = _clean(tmp_path)
    _add(fin, "INSERT INTO sec_filings (asset_id, filing_date) VALUES (1, '2022-09-01')")
    _, _, problems = verify.scope_problems(str(fin), str(udb))
    assert any("AAA" in p and "filings after its exit" in p for p in problems)


def test_the_check_flags_a_price_before_the_start(tmp_path: Path) -> None:
    fin, udb = _clean(tmp_path)
    _add(fin, "INSERT INTO price_daily (asset_id, date, close) VALUES (2, '2018-01-02', 1)")
    _, _, problems = verify.scope_problems(str(fin), str(udb))
    assert any("before 2019-01-02" in p for p in problems)
