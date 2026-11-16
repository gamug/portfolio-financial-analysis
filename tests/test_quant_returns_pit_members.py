"""``build-returns --pit-members``: the panel keeps a leaver's history up to its exit.

Hermetic: a synthetic point-in-time universe and synthetic closes only.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import write_universe_db
from portfolio_common.db import Database

from kg_schema import connect
from pricing_agent.db import ensure_schema
from quant.config import QuantSettings
from quant.db import ensure_schema as ensure_quant_schema
from quant.returns import run_build_returns

_DAYS = ("2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05")
_LEAVER = 1
_CURRENT = 2
_FUTURE = 3
_PAST = 4


def _db(tmp_path: Path) -> tuple[Database, Path]:
    conn = connect(tmp_path / "financial.db")
    ensure_schema(conn)
    ensure_quant_schema(conn)
    conn.execute(
        "INSERT INTO assets (id, ticker) VALUES (?, 'LEAVER'), (?, 'CURRENT'), "
        "(?, 'FUTURE'), (?, 'PAST')",
        (_LEAVER, _CURRENT, _FUTURE, _PAST),
    )
    for asset_id in (_CURRENT, _FUTURE, _PAST):
        for day in _DAYS:
            conn.execute(
                "INSERT INTO price_daily (asset_id, date, close) VALUES (?, ?, ?)",
                (asset_id, day, 100.0 + asset_id),
            )
    # the leaver's price series stops at its last membership session
    for day in _DAYS[:3]:
        conn.execute(
            "INSERT INTO price_daily (asset_id, date, close) VALUES (?, ?, ?)",
            (_LEAVER, day, 100.0 + _LEAVER),
        )
    # a clean gateway backfill covering the window, as build-returns' T-086 guard requires
    conn.execute(
        "INSERT INTO quant_run (command, as_of, started_at, finished_at, status, "
        "engine_version, params_json) VALUES ('backfill-actions', ?, ?, ?, 'completed', "
        "'quant-v1', ?)",
        (
            _DAYS[-1],
            _DAYS[0] + "T00:00:00Z",
            _DAYS[0] + "T00:01:00Z",
            json.dumps(
                {
                    "source": "gateway",
                    "date_from": "2000-01-01",
                    "date_to": "2100-01-01",
                    "assets_seen": 4,
                    "assets_fetched": 4,
                    "assets_errored": 0,
                }
            ),
        ),
    )
    conn.execute(
        "INSERT INTO corporate_action (asset_id, action_type, ex_date, value, source, "
        "engine_version, ingested_at) VALUES (?, 'DIVIDEND', ?, 0.5, 'pricing-gateway', "
        "'corpact-v1', ?)",
        (_CURRENT, _DAYS[1], _DAYS[0] + "T00:00:00Z"),
    )
    conn.commit()
    udb = write_universe_db(
        tmp_path / "universe.db",
        [
            ("LEAVER", "2020-01-01", "2024-01-04"),  # leaves during the window
            ("CURRENT", "2020-01-01", None),  # member at the window's end
            ("FUTURE", "2024-02-01", None),  # joins after the window
            ("PAST", "2015-01-01", "2023-12-29"),  # left before the window
        ],
    )
    return conn, udb


def _settings(udb: Path) -> QuantSettings:
    return QuantSettings(db_path=Path(":memory:"), universe_db_path=udb, allow_dirty=True)


def _assets_with_returns(conn: Database) -> set[int]:
    return {int(row[0]) for row in conn.execute("SELECT DISTINCT asset_id FROM quant_return_daily")}


def test_the_default_keeps_the_members_as_of_the_window_end(tmp_path: Path) -> None:
    conn, udb = _db(tmp_path)
    run_build_returns(_settings(udb), date_from="2024-01-01", date_to="2024-01-05", conn=conn)
    assert _assets_with_returns(conn) == {_CURRENT}


def test_pit_members_keeps_a_leaver_up_to_its_exit_and_excludes_the_rest(tmp_path: Path) -> None:
    conn, udb = _db(tmp_path)
    report = run_build_returns(
        _settings(udb),
        date_from="2024-01-01",
        date_to="2024-01-05",
        conn=conn,
        pit_members=True,
    )
    assert report.assets == 2
    assert _assets_with_returns(conn) == {_LEAVER, _CURRENT}
    leaver_days = [
        str(row[0])
        for row in conn.execute(
            "SELECT obs_date FROM quant_return_daily WHERE asset_id = ? ORDER BY obs_date",
            (_LEAVER,),
        )
    ]
    assert leaver_days == list(_DAYS[:3])
    # the leaver's last stored close is its exit session: the panel stops there
    assert (
        conn.execute("SELECT MAX(date) FROM price_daily WHERE asset_id = ?", (_LEAVER,)).fetchone()[
            0
        ]
        == "2024-01-04"
    )
