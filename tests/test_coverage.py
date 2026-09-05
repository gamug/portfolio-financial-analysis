"""Point-in-time core-data coverage report + `coverage` command."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

import pytest
from conftest import _QUANT_EXTRA_DDL
from portfolio_common.db import Database

from kg_schema.cli import run_coverage
from kg_schema.queries import check_coverage, connect_ro, persist_coverage
from pricing_agent import db as pdb
from quant import db as qdb

_UDB = Callable[..., Path]


def _ensure_fin(conn: Database) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    pdb.ensure_schema(conn)  # assets, sectors, price_daily, price_window + kg_schema
    conn.executescript(_QUANT_EXTRA_DDL)  # sec_filings, financial_facts
    qdb.ensure_schema(conn)  # quant_run, quant_return_daily + re-runs kg_schema.ensure


def _fin_db() -> Database:
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    conn = Database(raw)
    _ensure_fin(conn)
    return conn


def _seed_member(
    conn: Database,
    ticker: str,
    *,
    fundamental: bool = False,
    price: bool = False,
    obs: int = 0,
) -> int:
    aid = int(
        conn.execute("INSERT INTO assets (ticker) VALUES (?) RETURNING id", (ticker,)).fetchone()[
            "id"
        ]
    )
    if fundamental:
        conn.execute(
            "INSERT INTO score_snapshot (asset_id, score_type, raw_value, normalized_score, "
            "event_time, computed_at, model, run_kind) VALUES (?, 'FUNDAMENTAL', 1, 1, "
            "'2024-01-01', '2024-02-01T00:00:00Z', 'seed', 'analysis')",
            (aid,),
        )
    if price:
        conn.execute(
            "INSERT INTO price_daily (asset_id, date, close) VALUES (?, '2024-01-02', 100.0)",
            (aid,),
        )
    d0 = date(2022, 1, 3)
    for i in range(obs):
        od = (d0 + timedelta(days=i)).isoformat()
        conn.execute(
            "INSERT INTO price_observation (asset_id, obs_date, close, event_time, computed_at, "
            "engine_version) VALUES (?, ?, 100.0, ?, '2024-02-01T00:00:00Z', 'priceobs-v1')",
            (aid, od, od),
        )
    conn.commit()
    return int(aid)


def test_check_coverage_flags_missing_core_data(universe_db: _UDB) -> None:
    conn = _fin_db()
    _seed_member(conn, "FULL", fundamental=True, price=True, obs=5)
    _seed_member(conn, "NOPRICE", fundamental=True, price=False, obs=0)
    # "GHOST" is in the universe as of the date but never got an assets row.
    udb = universe_db([(s, "2020-01-01", None) for s in ("FULL", "NOPRICE", "GHOST")])

    u = connect_ro(udb)
    try:
        report = check_coverage(conn, u, "2024-06-30", min_observation_days=3)
    finally:
        u.close()

    by_sym = {r.symbol: r for r in report.rows}
    assert by_sym["FULL"].covered
    assert not by_sym["NOPRICE"].covered
    assert set(by_sym["NOPRICE"].missing_required) == {"pricing", "observations"}
    assert by_sym["GHOST"].missing_required == ["assets", "fundamental", "pricing", "observations"]
    assert report.covered == 1
    assert report.total == 3
    assert report.missing_for("pricing") == ["GHOST", "NOPRICE"]


def test_persist_coverage_upserts(universe_db: _UDB) -> None:
    conn = _fin_db()
    _seed_member(conn, "A", fundamental=True, price=True, obs=5)
    udb = universe_db([("A", "2020-01-01", None), ("B", "2020-01-01", None)])
    u = connect_ro(udb)
    try:
        report = check_coverage(conn, u, "2024-06-30", min_observation_days=3)
    finally:
        u.close()

    assert persist_coverage(conn, report) == 2
    assert persist_coverage(conn, report) == 2  # idempotent upsert
    rows = conn.execute(
        "SELECT symbol, covered, missing_json FROM universe_coverage ORDER BY symbol"
    ).fetchall()
    assert [r["symbol"] for r in rows] == ["A", "B"]
    assert rows[0]["covered"] == 1
    assert rows[1]["covered"] == 0
    assert "pricing" in json.loads(rows[1]["missing_json"])


def test_run_coverage_command_warn_vs_strict(
    universe_db: _UDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fin = tmp_path / "fin.db"
    raw = sqlite3.connect(fin)
    raw.row_factory = sqlite3.Row
    conn = Database(raw)
    _ensure_fin(conn)
    _seed_member(conn, "A", fundamental=True, price=True, obs=600)
    conn.close()

    udb = universe_db([("A", "2020-01-01", None), ("B", "2020-01-01", None)])
    monkeypatch.setenv("KG_FINANCIAL_DB", str(fin))
    monkeypatch.setenv("KG_UNIVERSE_DB", str(udb))

    assert run_coverage(None, analysis_date="2024-06-30") == 0  # warn: never fails
    assert run_coverage(None, analysis_date="2024-06-30", strict=True) == 1  # 50% < 100%
    assert run_coverage(None, analysis_date="2024-06-30", strict=True, min_fraction=0.5) == 0
