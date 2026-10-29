"""The driver end to end on a tiny fixture database (WP-B/WP-C/WP-E monkeypatched).

The real ``quant`` CLI, ``cycle.run_replay`` (WP-C) and ``cycle.backtest.stats`` (WP-E) are
built in parallel worktrees; this test pins the driver's own contract: the D1 schedule, the
pipeline calls with the exact §8 arguments, the engine run and the m018 persistence.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import _guard_double
import pytest

_guard_double.install_if_missing()

import _stats_double  # noqa: E402

import cycle.backtest  # noqa: E402
from cycle.backtest import driver  # noqa: E402
from cycle.backtest.driver import (  # noqa: E402
    BacktestError,
    load_params,
    refuse_production_db,
    run_backtest,
    sessions_between,
)
from fundamental_agent import db as fundamental_db  # noqa: E402
from kg_schema import connect, queries  # noqa: E402


@pytest.fixture(autouse=True)
def _install_stats_double(monkeypatch: pytest.MonkeyPatch) -> None:
    if importlib.util.find_spec("cycle.backtest.stats") is None:
        monkeypatch.setitem(sys.modules, "cycle.backtest.stats", _stats_double)
        monkeypatch.setattr(cycle.backtest, "stats", _stats_double, raising=False)


UNIVERSE_DDL = """
CREATE TABLE universe_membership (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    security TEXT NOT NULL,
    cik TEXT,
    gics_sector TEXT,
    gics_sub_industry TEXT,
    hq_location TEXT,
    date_added TEXT,
    founded TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    detected_at TEXT NOT NULL,
    run_id INTEGER,
    run_kind TEXT,
    source TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    """A migrated financial DB with 2 assets, returns and RF, plus a 1-member universe DB."""
    db_path = tmp_path / "financial.db"
    conn = connect(db_path)
    fundamental_db.ensure_schema(conn)
    conn.execute(
        "INSERT INTO assets (id, ticker, company_name, cik) VALUES "
        "(1, 'AAA', 'Alpha', '0000000001'), (2, 'BBB', 'Beta', '0000000002')"
    )
    sessions = sessions_between("2021-11-01", "2022-03-15")
    for session in sessions:
        for asset_id in (1, 2):
            log_return = -0.10 if (asset_id == 1 and session == "2022-01-06") else 0.001
            conn.execute(
                "INSERT INTO quant_return_daily (asset_id, obs_date, close_split_adj, "
                "adj_close, tr_index, tr_log_return, source, engine_version, computed_at) "
                "VALUES (?, ?, 100.0, 100.0, 100.0, ?, 'quant-tr-v1', 'qret-v2', ?)",
                (asset_id, session, log_return, _now()),
            )
        conn.execute(
            "INSERT INTO risk_free_rate (curve, rate_date, annualized_rate, source, "
            "engine_version, ingested_at) VALUES ('FF_RF_DAILY', ?, ?, 'french-ff3-daily', "
            "'rf-v2', ?)",
            (session, 0.0252, _now()),
        )
    conn.commit()
    conn.close()

    universe_path = tmp_path / "universe.db"
    universe = connect(universe_path)
    universe.create_schema(UNIVERSE_DDL)
    universe.execute(
        "INSERT INTO universe_membership (symbol, security, cik, valid_from, detected_at, "
        "source) VALUES ('AAA', 'Alpha', '0000000001', '2020-01-01', ?, 'wikipedia')",
        (_now(),),
    )
    universe.commit()
    universe.close()
    return db_path, universe_path


def _guard_block(code: str, layers: str, *, news: bool = False) -> list[str]:
    lines = [
        f"[guard.arms.{code}]",
        f"layers = {layers}",
        "J = 2",
        "gamma = 0.05",
        "delta = 0.03",
        "sigma_star = 10.0",
        "m = 2.0",
        "alpha = 0.5",
        "vol_window = 2",
        "periods_per_year = 252",
    ]
    if news:
        lines += [
            f"[guard.arms.{code}.news]",
            "k = 1",
            "tau = 0.5",
            "tau_exit = 0.4",
            "n_min = 2",
            "cooldown = 2",
        ]
    return lines


def _params_file(tmp_path: Path, *, with_news_arm: bool = False) -> Path:
    lines = [
        "[run]",
        "cost_bps = 10.0",
        "top_n = 2",
        "lookback = 756",
        "min_history = 740",
        "cov_estimator = 'ledoit_wolf_cc'",
        "returns_engine_version = 'qret-v2'",
        "rf_curve = 'FF_RF_DAILY'",
        "",
        "[guard]",
        'shadow_from = "TOPN_MV_GUARD_NO_NLP"',
        "",
    ]
    if with_news_arm:
        lines += _guard_block("TOPN_MV_GUARD", "[1, 2, 3, 4]", news=True)
        lines.append("")
    lines += _guard_block("TOPN_MV_GUARD_NO_NLP", "[1, 3, 4]")
    path = tmp_path / "params.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


def _fake_quant(conn: Any, calls: list[list[str]]) -> Any:
    def run(args: list[str], db: Path, universe: Path) -> dict[str, Any]:
        calls.append(list(args))
        if args[0] == "build-returns":
            return {}
        if args[0] == "build-risk-model":
            as_of = args[args.index("--analysis-date") + 1]
            cursor = conn.execute(
                "INSERT INTO quant_risk_model (as_of, model_version, lookback_days, "
                "min_history_days, n_assets, cov_estimator, ret_estimator, periods_per_year, "
                "panel_engine_version, panel_spec_json, computed_at) VALUES (?, 'rm-v2', 756, "
                "740, 2, 'ledoit_wolf_cc', 'equilibrium', 252, 'qret-v2', '{}', ?)",
                (as_of, _now()),
            )
            conn.commit()
            return {"model_id": int(cursor.lastrowid or 0)}
        if args[0] == "optimize":
            as_of = args[args.index("--analysis-date") + 1]
            kind = "min_var_topn" if "--cycle-run-id" in args else "min_var"
            model_id = None
            if "--risk-model-id" in args:
                model_id = int(args[args.index("--risk-model-id") + 1])
            if model_id is None:
                row = conn.execute(
                    "SELECT id FROM quant_risk_model WHERE as_of = ? ORDER BY id DESC",
                    (as_of,),
                ).fetchone()
                model_id = int(row["id"])
            cursor = conn.execute(
                "INSERT INTO quant_portfolio (model_id, as_of, kind, objective, solver, "
                "status, n_positions, engine_version, computed_at) VALUES (?, ?, ?, "
                "'min_var', 'CLARABEL', 'optimal', 2, 'opt-v2', ?)",
                (model_id, as_of, kind, _now()),
            )
            portfolio_id = int(cursor.lastrowid or 0)
            for asset_id in (1, 2):
                conn.execute(
                    "INSERT INTO quant_position (portfolio_id, asset_id, weight, valid_from) "
                    "VALUES (?, ?, 0.5, ?)",
                    (portfolio_id, asset_id, as_of),
                )
            conn.commit()
            return {"portfolio_id": portfolio_id, "model_id": model_id, "kind": kind}
        raise AssertionError(args)

    return run


def _fake_replay(conn: Any) -> Any:
    def run(  # noqa: PLR0913 - WP-C's §8.3 signature
        settings: Any,
        cycle_date: str,
        *,
        conn: Any,
        fundamental_hook: Any = None,
        risk_model_id: int | None = None,
        veto_cutoff_same_day: bool = False,
    ) -> Any:
        del settings, fundamental_hook, veto_cutoff_same_day
        row = conn.execute(
            "SELECT id FROM cycle_run WHERE cycle_type = 'REPLAY' AND cycle_date = ?",
            (cycle_date,),
        ).fetchone()
        if row is not None:
            return SimpleNamespace(cycle_run_id=int(row["id"]))
        cursor = conn.execute(
            "INSERT INTO cycle_run (cycle_type, cycle_date, started_at, status) "
            "VALUES ('REPLAY', ?, ?, 'completed')",
            (cycle_date, _now()),
        )
        run_id = int(cursor.lastrowid or 0)
        for rank, asset_id in enumerate((1, 2), start=1):
            conn.execute(
                "INSERT INTO cycle_ranking (cycle_run_id, asset_id, rank, blended_score, "
                "vetoed, veto_rules_json, selected) VALUES (?, ?, ?, ?, 0, '[]', 1)",
                (run_id, asset_id, rank, 100.0 - rank),
            )
        conn.commit()
        return SimpleNamespace(cycle_run_id=run_id, selected=2)

    return run


def test_the_driver_runs_the_pipeline_and_persists_every_m018_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, universe_path = _fixture(tmp_path)
    params_path = _params_file(tmp_path)
    conn = connect(db_path)
    monkeypatch.setitem(driver.WINDOWS, "PX", ("2022-01-03", "2022-03-15"))
    calls: list[list[str]] = []
    monkeypatch.setattr(driver, "run_quant", _fake_quant(conn, calls))
    monkeypatch.setattr(driver, "run_replay", _fake_replay(conn))
    monkeypatch.setattr(queries, "risk_model_asset_ids", lambda _c, _m: {1, 2}, raising=False)

    result = run_backtest(
        window_id="PX",
        db_path=db_path,
        universe_db_path=universe_path,
        params_path=params_path,
        warmup_steps=2,
    )
    assert result["status"] == "completed"
    assert result["formation_date"] == "2022-01-03"
    assert set(result["books"]) == {"MARKOWITZ_BASE", "TOPN_MV_NOGUARD", "TOPN_MV_GUARD_NO_NLP"}

    # The pipeline passed the §8 flags: --include-hard-vetoed on both, --cycle-run-id +
    # --risk-model-id on the Top-N book, one build-returns for the window.
    assert sum(1 for c in calls if c[0] == "build-returns") == 1
    optimizes = [c for c in calls if c[0] == "optimize"]
    assert len(optimizes) == 2 * 3  # 3 decision dates x (base, top-n)
    assert all("--include-hard-vetoed" in c for c in optimizes)
    assert sum(1 for c in optimizes if "--cycle-run-id" in c) == 3

    conn = connect(db_path)
    run = conn.execute(
        "SELECT * FROM backtest_run WHERE id = ?", (result["backtest_run_id"],)
    ).fetchone()
    assert run["status"] == "completed"
    assert run["formation_date"] == "2022-01-03"
    params = json.loads(str(run["params_json"]))
    assert params["params_sha256"] == load_params(params_path).sha256
    assert params["arms"] == [
        "MARKOWITZ_BASE",
        "TOPN_MV_NOGUARD",
        "TOPN_MV_GUARD_NO_NLP",
    ]
    assert params["rf_carried_sessions"] == 0

    assert conn.execute("SELECT COUNT(*) FROM strategy_book").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM book_rebalance").fetchone()[0] == 9
    assert conn.execute("SELECT COUNT(*) FROM book_weight_daily").fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM book_daily_performance").fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM backtest_summary").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM backtest_test").fetchone()[0] == 6

    statuses = {
        str(r["status"]) for r in conn.execute("SELECT status FROM guard_action").fetchall()
    }
    assert statuses == {"APPLIED", "ARM_DISABLED"}
    assert conn.execute("SELECT COUNT(*) FROM guard_alert").fetchone()[0] > 0

    cash = conn.execute(
        "SELECT COUNT(*) FROM book_weight_daily WHERE holding_key = 'CASH'"
    ).fetchone()[0]
    assert cash > 0
    # every weight row's final target sums to 1 with its cash row
    sums = conn.execute(
        "SELECT date, SUM(final_target_weight) AS total FROM book_weight_daily "
        "GROUP BY book_id, date HAVING ABS(SUM(final_target_weight) - 1.0) > 1e-6"
    ).fetchall()
    assert sums == []
    conn.close()


def test_arm_three_is_not_run_when_layer_two_has_no_news_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, universe_path = _fixture(tmp_path)
    params_path = _params_file(tmp_path, with_news_arm=True)
    conn = connect(db_path)
    monkeypatch.setitem(driver.WINDOWS, "PX", ("2022-01-03", "2022-03-15"))
    monkeypatch.setattr(driver, "run_quant", _fake_quant(conn, []))
    monkeypatch.setattr(driver, "run_replay", _fake_replay(conn))
    monkeypatch.setattr(queries, "risk_model_asset_ids", lambda _c, _m: {1, 2}, raising=False)

    result = run_backtest(
        window_id="PX",
        db_path=db_path,
        universe_db_path=universe_path,
        params_path=params_path,
        warmup_steps=1,
    )
    assert "TOPN_MV_GUARD" not in result["books"]
    conn = connect(db_path)
    params = json.loads(
        str(
            conn.execute(
                "SELECT params_json FROM backtest_run WHERE id = ?",
                (result["backtest_run_id"],),
            ).fetchone()["params_json"]
        )
    )
    assert any("layer 2 off" in note for note in params["notes"])
    conn.close()


def test_the_driver_refuses_a_production_database() -> None:
    with pytest.raises(BacktestError, match="read-only"):
        refuse_production_db(Path("/workspaces/thesis/data/financial.db"))


def test_a_missing_database_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BacktestError, match="does not exist"):
        run_backtest(
            window_id="P1",
            db_path=tmp_path / "missing.db",
            universe_db_path=tmp_path / "universe.db",
            params_path=tmp_path / "params.toml",
        )


def test_the_example_params_file_is_valid() -> None:
    path = Path(driver.__file__).parent / "params.example.toml"
    params = load_params(path)
    assert params.top_n == 30
    assert params.cost_bps == 10.0
    assert set(params.guard) == {"TOPN_MV_GUARD", "TOPN_MV_GUARD_NO_NLP"}
    assert params.guard["TOPN_MV_GUARD"].news is not None
    assert 2 in params.guard["TOPN_MV_GUARD"].layers
    assert 2 not in params.guard["TOPN_MV_GUARD_NO_NLP"].layers
