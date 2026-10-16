"""The T-153e verify_pilot check: a stint's cycle date must be priced (T-153 part 3c).

The check calls ``cycle.data.unpriced_assets`` -- the session rule itself -- so a stint that opens
on a name with D-1's close but not session(D)'s fails it.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import cycle.data as cycle_data

_SPEC = importlib.util.spec_from_file_location(
    "verify_pilot", Path(__file__).resolve().parent.parent / "scripts" / "verify_pilot.py"
)
assert _SPEC and _SPEC.loader
verify = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_pilot"] = verify
_SPEC.loader.exec_module(verify)

_DDL = """
CREATE TABLE assets (id INTEGER PRIMARY KEY, ticker TEXT UNIQUE, cik TEXT, sector_id INTEGER);
CREATE TABLE price_observation (
    id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL, obs_date TEXT NOT NULL, close REAL,
    obs_time TEXT, event_time TEXT, computed_at TEXT, engine_version TEXT NOT NULL);
CREATE TABLE portfolio_position (
    id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT,
    weight REAL, cost_basis REAL);
CREATE TABLE portfolio_position_replay (
    id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT,
    weight REAL, cost_basis REAL);
CREATE TABLE cycle_ranking (
    id INTEGER PRIMARY KEY, cycle_run_id INTEGER NOT NULL, asset_id INTEGER NOT NULL, rank INTEGER,
    blended_score REAL, components_json TEXT, vetoed INTEGER NOT NULL DEFAULT 0,
    veto_rules_json TEXT, selected INTEGER NOT NULL DEFAULT 0, target_weight REAL);
CREATE TABLE veto (id INTEGER PRIMARY KEY, asset_id INTEGER, rule_id TEXT, severity TEXT,
    raised_on TEXT, cleared_on TEXT, last_seen_on TEXT);
"""


def _db(
    tmp_path: Path,
    *,
    close_on_session: bool = True,
    veto_marker: str | None = None,
    selected_marker: str | None = None,
) -> str:
    path = tmp_path / "t153e.db"
    conn = sqlite3.connect(path)
    conn.executescript(_DDL)
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA'), (2, 'BBB')")
    # 2026-06-30 is a Tuesday: session(D); 2026-06-29 is D-1
    if close_on_session:
        conn.execute(
            "INSERT INTO price_observation (asset_id, obs_date, close, engine_version) "
            "VALUES (1, '2026-06-30', 10.0, 'priceobs-v2'), (2, '2026-06-30', 20.0, 'priceobs-v2')"
        )
    else:
        conn.execute(
            "INSERT INTO price_observation (asset_id, obs_date, close, engine_version) "
            "VALUES (1, '2026-06-29', 10.0, 'priceobs-v2'), (2, '2026-06-30', 20.0, 'priceobs-v2')"
        )
    conn.execute("INSERT INTO portfolio_position (asset_id, valid_from) VALUES (1, '2026-06-30')")
    conn.execute(
        "INSERT INTO portfolio_position_replay (asset_id, valid_from) VALUES (2, '2026-06-30')"
    )
    if veto_marker:
        conn.execute(
            "INSERT INTO veto (asset_id, rule_id, severity, raised_on) VALUES (1, ?, 'SOFT', "
            "'2026-06-30')",
            (veto_marker,),
        )
    if selected_marker:
        conn.execute(
            "INSERT INTO cycle_ranking (cycle_run_id, asset_id, rank, blended_score, "
            "veto_rules_json, selected) VALUES (1, 1, 1, 50.0, ?, 1)",
            (f'["{selected_marker}"]',),
        )
    conn.commit()
    conn.close()
    return str(path)


def test_a_priced_stint_passes(tmp_path: Path) -> None:
    stints, marked, problems = verify.unpriced_position_problems(_db(tmp_path))
    assert (stints, marked, problems) == (2, 0, [])


def test_a_stint_opening_on_d_minus_one_but_not_the_session_fails(tmp_path: Path) -> None:
    """The reviewer's case: D-1's close is not a close on session(D)."""
    stints, _marked, problems = verify.unpriced_position_problems(
        _db(tmp_path, close_on_session=False)
    )
    assert stints == 2
    assert problems == ["AAA opens a stint on 2026-06-30 unpriced"]


def test_a_veto_row_named_a_marker_fails(tmp_path: Path) -> None:
    _stints, _marked, problems = verify.unpriced_position_problems(
        _db(tmp_path, veto_marker="UNPRICED")
    )
    assert problems == ["a veto row is named UNPRICED: a data gap is never a veto"]


def test_a_selected_row_carrying_a_marker_fails(tmp_path: Path) -> None:
    _stints, marked, problems = verify.unpriced_position_problems(
        _db(tmp_path, selected_marker="UNSCORED")
    )
    assert marked == 1
    assert problems == ["AAA is selected with the UNSCORED marker"]


def test_the_check_calls_the_rules_own_function() -> None:
    """A second query or a 7-day window here would drift from the cycle's own rule."""
    assert verify.unpriced_assets is cycle_data.unpriced_assets
