"""``scripts/unexplained_split_rows.py`` (T-153 part 3d, D10): the gate over a database's SPLIT rows.

The script does not exist on ``master``, so this whole module fails to import there (the point of
the test). It runs the script on a synthetic database: one SPLIT row the tracked list explains
(DD 2019-06-03, the combined ``reverse_split_spinoff`` row) and one it does not.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import unexplained_split_rows as script


def _db(path: Path) -> Path:
    raw = sqlite3.connect(path)
    raw.executescript(
        "CREATE TABLE assets (id INTEGER PRIMARY KEY, ticker TEXT, cik TEXT);"
        "CREATE TABLE corporate_action (id INTEGER PRIMARY KEY, asset_id INTEGER, action_type TEXT,"
        " ex_date TEXT, value REAL, engine_version TEXT);"
        "INSERT INTO assets (id, ticker, cik) VALUES (1, 'DD', '0001666700');"
        "INSERT INTO corporate_action (asset_id, action_type, ex_date, value, engine_version)"
        " VALUES (1, 'SPLIT', '2019-06-03', 0.4725, 'corpact-v1');"
        "INSERT INTO corporate_action (asset_id, action_type, ex_date, value, engine_version)"
        " VALUES (1, 'SPLIT', '2030-01-01', 1.5, 'corpact-v1');"
    )
    raw.commit()
    raw.close()
    return path


def test_one_explained_row_and_one_unexplained(tmp_path: Path) -> None:
    db = _db(tmp_path / "synthetic.db")
    rows = script.unexplained_split_rows(db)
    assert [(r["ticker"], r["ex_date"]) for r in rows] == [("DD", "2030-01-01")]


def test_the_gate_exits_nonzero_when_a_row_is_unexplained(tmp_path: Path) -> None:
    db = _db(tmp_path / "synthetic.db")
    assert script.main(["--db", str(db)]) == 1


def test_a_database_without_the_table_has_nothing_to_explain(tmp_path: Path) -> None:
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    assert script.unexplained_split_rows(empty) == []
    assert script.main(["--db", str(empty)]) == 0
