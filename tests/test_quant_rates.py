"""Risk-free rate: constant curve point + CSV loader."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from portfolio_common.db import Database

from quant.config import QuantSettings
from quant.rates import RiskFreeUnavailable, load_risk_free


def _conn() -> Database:
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    conn = Database(raw)
    conn.executescript(
        "CREATE TABLE risk_free_rate (id INTEGER PRIMARY KEY, curve TEXT NOT NULL, "
        "rate_date TEXT NOT NULL, annualized_rate REAL NOT NULL, source TEXT NOT NULL, "
        "engine_version TEXT NOT NULL, ingested_at TEXT NOT NULL, "
        "UNIQUE (curve, rate_date, engine_version))"
    )
    return conn


def test_constant_rate_is_persisted_and_compounded() -> None:
    conn = _conn()
    s = QuantSettings(db_path=Path(":memory:"), risk_free_rate=0.05, periods_per_year=252)
    rf = load_risk_free(s, as_of="2026-08-27", conn=conn)
    assert rf.curve == "CONST"
    assert rf.annualized_rate == 0.05
    assert rf.daily_rate == (1.05 ** (1 / 252)) - 1
    row = conn.execute("SELECT * FROM risk_free_rate").fetchone()
    assert (row["curve"], row["rate_date"], row["source"]) == ("CONST", "2026-08-27", "constant-v1")


def test_csv_source_picks_point_on_or_before_as_of(tmp_path: Path) -> None:
    csv_path = tmp_path / "rf.csv"
    csv_path.write_text("date,rate\n2026-01-01,0.04\n2026-06-01,0.045\n2027-01-01,0.05\n")
    conn = _conn()
    s = QuantSettings(
        db_path=Path(":memory:"), rf_source="csv", rf_csv_path=csv_path, periods_per_year=252
    )
    rf = load_risk_free(s, as_of="2026-08-27", conn=conn)
    assert rf.rate_date == "2026-06-01"
    assert rf.annualized_rate == 0.045
    assert conn.execute("SELECT COUNT(*) FROM risk_free_rate").fetchone()[0] == 3


def test_csv_source_refuses_an_as_of_before_the_series_starts(tmp_path: Path) -> None:
    """T-077 (audit Q3): the old ``or points`` fallback silently used the series' first -- future
    -- rate for an as-of before it, a lookahead into the risk-free rate."""
    csv_path = tmp_path / "rf.csv"
    csv_path.write_text("date,rate\n2026-01-01,0.04\n2026-06-01,0.045\n")
    conn = _conn()
    s = QuantSettings(db_path=Path(":memory:"), rf_source="csv", rf_csv_path=csv_path)
    with pytest.raises(
        RiskFreeUnavailable, match=r"starts 2026-01-01, after the as-of date 2025-12-31"
    ):
        load_risk_free(s, as_of="2025-12-31", conn=conn)
    assert (
        conn.execute("SELECT COUNT(*) FROM risk_free_rate").fetchone()[0] == 0
    )  # nothing persisted
    # the first date itself is fine, and so is any later one
    assert load_risk_free(s, as_of="2026-01-01", conn=conn).annualized_rate == 0.04
    empty = tmp_path / "empty.csv"
    empty.write_text("date,rate\n")
    with pytest.raises(RiskFreeUnavailable, match="no rows"):
        load_risk_free(s.model_copy(update={"rf_csv_path": empty}), as_of="2026-01-01", conn=conn)
