"""The T-153b verify_pilot check: the tracked ticker history against the universe file and the
assets (T-153 part 2)."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from kg_schema.ticker_history import read_history

_SPEC = importlib.util.spec_from_file_location(
    "verify_pilot", Path(__file__).resolve().parent.parent / "scripts" / "verify_pilot.py"
)
assert _SPEC and _SPEC.loader
verify = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_pilot"] = verify
_SPEC.loader.exec_module(verify)

_UNIVERSE_DDL = """
CREATE TABLE universe_membership (
    symbol TEXT NOT NULL, security TEXT NOT NULL, gics_sector TEXT, gics_sub_industry TEXT,
    hq_location TEXT, date_added TEXT, cik TEXT, founded TEXT,
    valid_from TEXT NOT NULL, valid_to TEXT, source TEXT NOT NULL);
"""


def _universe(path: Path, rows: list[tuple[str, str, str, str | None]]) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(_UNIVERSE_DDL)
    conn.executemany(
        "INSERT INTO universe_membership (symbol, security, cik, valid_from, valid_to, source) "
        "VALUES (?, ?, ?, ?, ?, 'test')",
        [(symbol, f"{symbol} Inc.", cik, vf, vt) for symbol, cik, vf, vt in rows],
    )
    conn.commit()
    conn.close()
    return path


def _file_rows() -> list[tuple[str, str, str, str | None]]:
    """The universe file the CSV describes: one stint per tracked row, except the declared
    PSKY exception (the file does not show it)."""
    return [
        (r.symbol, r.cik, r.valid_from, r.valid_to)
        for r in read_history()
        if (r.cik, r.symbol) not in verify._T153B_EXCEPTIONS
    ]


def _fin_db(path: Path, assets: list[tuple[str, str]]) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, ticker TEXT UNIQUE, cik TEXT)")
    conn.executemany("INSERT INTO assets (ticker, cik) VALUES (?, ?)", assets)
    conn.commit()
    conn.close()
    return path


def test_the_check_agrees_on_the_tracked_history(tmp_path: Path) -> None:
    uni = _universe(tmp_path / "u.db", _file_rows())
    fin = _fin_db(tmp_path / "f.db", [("META", "0001326801")])
    pairs, rows, problems = verify.ticker_history_gaps(str(fin), str(uni))
    assert problems == []
    assert pairs == 8  # the six renames plus DISCA->WBD and DISCK->WBD
    assert rows == 17


def test_the_check_names_the_psky_exception() -> None:
    """PSKY is the one tracked row the universe file does not show (SA-2): it is not flagged."""
    assert ("0002041610", "PSKY") in verify._T153B_EXCEPTIONS
    assert ("0002041610", "SKYD") not in verify._T153B_EXCEPTIONS


def test_the_check_flags_a_rename_with_no_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uni = _universe(tmp_path / "u.db", _file_rows())
    fin = _fin_db(tmp_path / "f.db", [])
    without_meta = [r for r in read_history() if r.symbol != "META"]
    monkeypatch.setattr(verify, "read_history", lambda: without_meta)
    _, _, problems = verify.ticker_history_gaps(str(fin), str(uni))
    assert any("FB->META" in p for p in problems)


def test_the_check_flags_an_old_symbol_asset(tmp_path: Path) -> None:
    """An FB asset beside META is exactly the split the task prevents: FB is a non-canonical
    symbol of that CIK's line."""
    uni = _universe(tmp_path / "u.db", _file_rows())
    fin = _fin_db(tmp_path / "f.db", [("META", "0001326801"), ("FB", "0001326801")])
    _, _, problems = verify.ticker_history_gaps(str(fin), str(uni))
    assert any("FB" in p and "old symbol" in p for p in problems)
