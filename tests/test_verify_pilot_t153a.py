"""The T-153a verify_pilot check: ``members_asof`` against the closed-interval universe read."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import write_universe_db

_SPEC = importlib.util.spec_from_file_location(
    "verify_pilot", Path(__file__).resolve().parent.parent / "scripts" / "verify_pilot.py"
)
assert _SPEC and _SPEC.loader
verify = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_pilot"] = verify
_SPEC.loader.exec_module(verify)


def _fin_db(path: Path, cycle_dates: list[str]) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cycle_run (id INTEGER PRIMARY KEY, cycle_date TEXT NOT NULL)")
    conn.executemany("INSERT INTO cycle_run (cycle_date) VALUES (?)", [(d,) for d in cycle_dates])
    conn.commit()
    conn.close()
    return path


def test_the_check_agrees_on_a_leave_date_and_the_day_after(tmp_path: Path) -> None:
    uni = write_universe_db(tmp_path / "u.db", [("LEFT", "2015-01-01", "2021-03-01")])
    fin = _fin_db(tmp_path / "f.db", ["2021-03-01", "2021-03-02"])
    n, mismatches = verify.members_asof_mismatches(str(fin), str(uni))
    assert mismatches == []
    assert n == 2  # 2021-03-01 (cycle + valid_to) and 2021-03-02 (cycle + valid_to + 1)


def test_the_check_finds_a_one_day_stint(tmp_path: Path) -> None:
    uni = write_universe_db(tmp_path / "u.db", [("PHIN", "2023-07-05", "2023-07-05")])
    fin = _fin_db(tmp_path / "f.db", ["2023-07-05"])
    n, mismatches = verify.members_asof_mismatches(str(fin), str(uni))
    assert mismatches == []
    assert n == 2  # 2023-07-05 and 2023-07-06


def test_the_check_catches_the_open_interval_predicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uni = write_universe_db(tmp_path / "u.db", [("LEFT", "2015-01-01", "2021-03-01")])
    fin = _fin_db(tmp_path / "f.db", ["2021-03-01"])
    real = verify.members_asof

    def as_open(db: Any, as_of: str, **kwargs: Any) -> list[Any]:
        return [m for m in real(db, as_of, **kwargs) if m.valid_to is None or m.valid_to > as_of]

    monkeypatch.setattr(verify, "members_asof", as_open)
    _, mismatches = verify.members_asof_mismatches(str(fin), str(uni))
    assert len(mismatches) == 1
    assert mismatches[0].startswith("2021-03-01")
