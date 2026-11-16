"""The T-153b verify_pilot check: the tracked ticker history against the universe file and the
assets (T-153 part 2)."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from dataclasses import replace
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
    """The universe file the CSV describes: one membership stint per tracked row.

    Not every tracked row is a membership stint. The declared PSKY exception (SA-2) is absent;
    and the post-exit renames (T-153 part 3a, D4) are members only under their **old** symbol,
    up to the index exit -- GAP and FBIN were never index members, and GPS/FBHS end at the
    exit, not at the symbol change."""
    out = []
    for r in read_history():
        if (r.cik, r.symbol) in verify._T153B_EXCEPTIONS or (r.cik, r.symbol) in _ABSENT:
            continue
        valid_to = _EXIT.get((r.cik, r.symbol), r.valid_to)
        out.append((r.symbol, r.cik, r.valid_from, valid_to))
    return out


#: The post-exit symbols the universe file does not carry, and the old symbols' index exit.
_ABSENT = {("0000039911", "GAP"), ("0001519751", "FBIN")}
_EXIT = {("0000039911", "GPS"): "2022-02-02", ("0001519751", "FBHS"): "2022-12-18"}


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
    assert rows == 21


def test_a_post_exit_rename_is_allowed(tmp_path: Path) -> None:
    """D4: GPS->GAP (2024-08-22) and FBHS->FBIN (2022-12-15) are past their index exits.

    The universe file shows GPS to 2022-02-02 and FBHS to 2022-12-18 and no GAP/FBIN at all, so
    neither the old row (extended to the symbol change) nor the new one is inside a stint -- the
    check allows both because the CIK has no open stint and the row starts on or after its last
    stint begins."""
    uni = _universe(tmp_path / "u.db", _file_rows())
    fin = _fin_db(tmp_path / "f.db", [])
    _, _, problems = verify.ticker_history_gaps(str(fin), str(uni))
    assert problems == []
    assert ("0000039911", "GAP") in {(r.cik, r.symbol) for r in read_history()}
    assert ("0001519751", "FBIN") in {(r.cik, r.symbol) for r in read_history()}


def test_a_post_exit_row_on_a_current_member_is_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row on a CIK that still has an open stint is never a post-exit rename (the PSKY shape).

    CIK 0002041610 is a member today (SKYD, open), so a row the file does not show is flagged --
    the general rule must not absorb PSKY's own declared exception."""
    uni = _universe(tmp_path / "u.db", _file_rows())
    fin = _fin_db(tmp_path / "f.db", [])
    extra = replace(
        read_history()[0],
        cik="0002041610",
        symbol="SKYQ",
        valid_from="2025-08-07",
        valid_to=None,
    )
    monkeypatch.setattr(verify, "read_history", lambda: [*read_history(), extra])
    _, _, problems = verify.ticker_history_gaps(str(fin), str(uni))
    assert any("SKYQ" in p and "not in the file" in p for p in problems)


def test_a_post_exit_row_before_the_last_stint_is_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leaver's row that starts before its last stint begins is not a post-exit rename."""
    uni = _universe(tmp_path / "u.db", _file_rows())
    fin = _fin_db(tmp_path / "f.db", [])
    early = replace(
        read_history()[0],
        cik="0000039911",
        symbol="GPSX",
        valid_from="1970-01-01",
        valid_to=None,
    )
    monkeypatch.setattr(verify, "read_history", lambda: [*read_history(), early])
    _, _, problems = verify.ticker_history_gaps(str(fin), str(uni))
    assert any("GPSX" in p and "not in the file" in p for p in problems)


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
