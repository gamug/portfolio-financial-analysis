"""End-to-end: seed -> returns -> risk model -> optimize -> persisted benchmark books."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path

import pytest

from kg_schema import version
from quant.actions import backfill_corporate_actions
from quant.config import QuantSettings
from quant.persist import run_build_risk_model, run_optimize
from quant.returns import run_build_returns


def _settings(**over: object) -> QuantSettings:
    base: dict[str, object] = {
        "db_path": Path(":memory:"),
        "lookback_days": 200,
        "min_history_days": 140,
        "liquidity_min_dollar_volume": 0.0,
        "max_name_weight": None,
        "max_sector_weight": None,
        "objectives": ["min_var", "tangency", "target_vol", "frontier"],
        "frontier_k": 5,
    }
    base.update(over)
    return QuantSettings(**base)  # type: ignore[arg-type]


@pytest.fixture
def seeded(
    memory_quant_db: sqlite3.Connection, quant_seed: Callable[..., sqlite3.Connection]
) -> sqlite3.Connection:
    conn = quant_seed(memory_quant_db, n_assets=6, n_days=280, with_dividends=True)
    s = QuantSettings(db_path=Path(":memory:"))
    backfill_corporate_actions(
        s, date_from="2000-01-01", date_to="2100-01-01", source="derive", conn=conn
    )
    run_build_returns(s, date_from="2000-01-01", date_to="2100-01-01", conn=conn)
    return conn


def test_optimize_persists_one_book_per_objective(seeded: sqlite3.Connection) -> None:
    as_of = seeded.execute("SELECT MAX(obs_date) FROM quant_return_daily").fetchone()[0]
    run_build_risk_model(_settings(), as_of=as_of, conn=seeded)
    res = run_optimize(_settings(), as_of=as_of, conn=seeded)

    assert set(res.books) == {"min_var", "tangency", "target_vol"}
    assert res.frontier_points == 5

    kinds = {r[0] for r in seeded.execute("SELECT kind FROM quant_portfolio")}
    assert {"min_var", "tangency", "target_vol"} <= kinds

    for pid in res.books.values():
        w = [
            float(r[0])
            for r in seeded.execute(
                "SELECT weight FROM quant_position WHERE portfolio_id = ? AND valid_to IS NULL",
                (pid,),
            )
        ]
        assert w, "book has no open positions"
        assert sum(w) == pytest.approx(1.0, abs=1e-4)
        assert min(w) >= -1e-8

    # frontier points: monotone non-decreasing vol, weights_json parses
    pts = seeded.execute(
        "SELECT k, expected_vol, weights_json FROM v_quant_frontier_point ORDER BY k"
    ).fetchall()
    assert len(pts) == 5
    vols = [p["expected_vol"] for p in pts if p["expected_vol"] > 0]
    assert all(b >= a - 1e-9 for a, b in pairwise(vols))
    assert all(isinstance(json.loads(p["weights_json"]), dict) for p in pts)

    # v_quant_portfolio surfaces the books; target_vol carries its scalar
    tv = seeded.execute(
        "SELECT target_param FROM v_quant_portfolio WHERE kind = 'target_vol'"
    ).fetchone()[0]
    assert tv is not None and tv > 0

    # schema_version untouched throughout
    assert version.current_version(seeded) == 0


def test_optimize_respects_caps(seeded: sqlite3.Connection) -> None:
    as_of = seeded.execute("SELECT MAX(obs_date) FROM quant_return_daily").fetchone()[0]
    s = _settings(max_name_weight=0.30, max_sector_weight=0.60, objectives=["min_var"])
    run_build_risk_model(s, as_of=as_of, conn=seeded)
    res = run_optimize(s, as_of=as_of, conn=seeded)

    pid = res.books["min_var"]
    rows = seeded.execute(
        "SELECT p.asset_id, p.weight, a.sector_id FROM quant_position p "
        "JOIN assets a ON a.id = p.asset_id WHERE p.portfolio_id = ? AND p.valid_to IS NULL",
        (pid,),
    ).fetchall()
    assert max(r["weight"] for r in rows) <= 0.30 + 1e-6
    by_sector: dict[int, float] = {}
    for r in rows:
        by_sector[r["sector_id"]] = by_sector.get(r["sector_id"], 0.0) + r["weight"]
    assert max(by_sector.values()) <= 0.60 + 1e-6


def test_optimize_auto_builds_risk_model_when_missing(seeded: sqlite3.Connection) -> None:
    as_of = seeded.execute("SELECT MAX(obs_date) FROM quant_return_daily").fetchone()[0]
    # no explicit run_build_risk_model call
    res = run_optimize(_settings(objectives=["min_var"]), as_of=as_of, conn=seeded)
    assert res.model_id > 0
    assert seeded.execute("SELECT COUNT(*) FROM quant_risk_model").fetchone()[0] == 1
