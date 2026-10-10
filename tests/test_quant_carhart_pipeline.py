"""``build-risk-model`` / ``optimize --mu carhart`` end to end (T-077), on a seeded database and
the French-factor fixture: the model stores the estimator and its regression record, degrades when
the factor data does not cover the date, refuses a tampered file, and ``optimize`` refuses a model
that has no carhart rows."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from french_support import FF3
from portfolio_common.db import Database

from quant import cli
from quant.config import QuantSettings
from quant.db import load_expected_returns
from quant.factors import FactorIntegrityError
from quant.persist import MuUnavailable, run_build_risk_model, run_optimize
from quant.returns import run_build_returns


def _settings(factor_dir: Path | None, **over: object) -> QuantSettings:
    return QuantSettings(
        db_path=Path(":memory:"),
        lookback_days=220,
        min_history_days=150,
        liquidity_min_dollar_volume=0.0,
        factors_dir=factor_dir,
        carhart_min_obs=100,
        **over,  # type: ignore[arg-type]
    )


def _seeded(
    memory_quant_db: Database,
    quant_seed: Callable[..., Database],
    factor_dir: Path,
    **over: object,
) -> tuple[Database, QuantSettings, str]:
    """Seed first: ``quant_seed`` points KG_UNIVERSE_DB at its own universe, which the settings read."""
    conn = quant_seed(memory_quant_db, n_assets=6, n_days=300, with_dividends=False)
    s = _settings(factor_dir, **over)
    run_build_returns(
        s, date_from="2000-01-01", date_to="2100-01-01", conn=conn, allow_no_dividends=True
    )
    return conn, s, conn.execute("SELECT MAX(obs_date) FROM quant_return_daily").fetchone()[0]


def test_build_risk_model_stores_carhart_and_records_the_regression(
    memory_quant_db: Database, quant_seed: Callable[..., Database], french_factor_dir: Path
) -> None:
    conn, s, as_of = _seeded(memory_quant_db, quant_seed, french_factor_dir)
    res = run_build_risk_model(s, as_of=as_of, conn=conn)

    per_model = dict(
        conn.execute("SELECT mu_model, COUNT(*) FROM quant_expected_return GROUP BY mu_model")
    )
    assert per_model == {m: 6 for m in ("hist_mean", "james_stein", "equilibrium", "carhart")}
    mu = load_expected_returns(conn, res.model_id, "carhart")
    assert all(np.isfinite(v) for v in mu.values())

    row = conn.execute("SELECT model_version, manifest_json FROM quant_risk_model").fetchone()
    assert row["model_version"].startswith("rm-v2+")
    rec = json.loads(row["manifest_json"])["carhart"]
    assert rec["factor_library_version"] == "202608 CRSP"
    assert rec["factor_file_last_date"] == "2024-12-31"
    # the seeded panel runs to early 2025, past the fixture's last day: the overlap is regressed
    # and the gap is on the record
    assert rec["panel_gap_after_factor_end"] > 0
    assert rec["regression_date_end"] <= "2024-12-31"
    assert rec["regression_n_dates"] + rec["panel_gap_after_factor_end"] <= rec["panel_n_dates"]
    assert set(rec["lambda_bar"]) == {"MKT", "SMB", "HML", "MOM"}
    assert set(rec["beta_bar"]) == set(rec["beta_prior_var"]) == set(rec["shrinkage_weight"])
    assert rec["flagged_asset_ids"] == []
    assert res.carhart == rec
    run = json.loads(
        conn.execute(
            "SELECT params_json FROM quant_run WHERE command = 'build-risk-model'"
        ).fetchone()[0]
    )
    assert run["carhart"]["regression_n_dates"] == rec["regression_n_dates"]

    # no schema change: the same two views expose the model, the manifest carrying the record
    view = conn.execute("SELECT manifest_json FROM v_quant_risk_model").fetchone()
    assert json.loads(view["manifest_json"])["carhart"] == rec


def test_carhart_is_unavailable_when_the_factor_data_does_not_cover_the_date(
    memory_quant_db: Database, quant_seed: Callable[..., Database], french_factor_dir: Path
) -> None:
    conn, s, as_of = _seeded(memory_quant_db, quant_seed, french_factor_dir)
    s = s.model_copy(update={"carhart_min_obs": 504})
    res = run_build_risk_model(s, as_of=as_of, conn=conn)  # builds the other three anyway

    per_model = dict(
        conn.execute("SELECT mu_model, COUNT(*) FROM quant_expected_return GROUP BY mu_model")
    )
    assert per_model == {m: 6 for m in ("hist_mean", "james_stein", "equilibrium")}
    assert "need 504" in res.carhart["unavailable"]
    stored = json.loads(conn.execute("SELECT manifest_json FROM quant_risk_model").fetchone()[0])
    assert stored["carhart"] == res.carhart

    with pytest.raises(MuUnavailable, match=r"no carhart expected returns: only \d+ of the panel"):
        run_optimize(s.model_copy(update={"ret_estimator": "carhart"}), as_of=as_of, conn=conn)
    failed = conn.execute(
        "SELECT status, error FROM quant_run WHERE command = 'optimize'"
    ).fetchone()
    assert failed["status"] == "failed" and "carhart" in failed["error"]
    assert conn.execute("SELECT COUNT(*) FROM quant_portfolio").fetchone()[0] == 0


def test_optimize_mu_carhart_refuses_an_rm_v1_model(
    memory_quant_db: Database, quant_seed: Callable[..., Database], french_factor_dir: Path
) -> None:
    """An rm-v1 model was built before T-077: it has no carhart rows and no carhart manifest."""
    conn, s, as_of = _seeded(
        memory_quant_db, quant_seed, french_factor_dir, risk_model_version="rm-v1"
    )
    res = run_build_risk_model(s, as_of=as_of, conn=conn)
    conn.execute("DELETE FROM quant_expected_return WHERE mu_model = 'carhart'")
    stored = json.loads(conn.execute("SELECT manifest_json FROM quant_risk_model").fetchone()[0])
    stored.pop("carhart")
    conn.execute(
        "UPDATE quant_risk_model SET manifest_json = ? WHERE id = ?",
        (json.dumps(stored), res.model_id),
    )
    conn.commit()

    with pytest.raises(MuUnavailable, match=r"rm-v1\+\w+ @ .*built before T-077.*rm-v2"):
        run_optimize(s.model_copy(update={"ret_estimator": "carhart"}), as_of=as_of, conn=conn)
    # the model still serves the other estimators
    run_optimize(s.model_copy(update={"objectives": ["min_var"]}), as_of=as_of, conn=conn)


def test_a_tampered_factor_file_fails_the_build_and_is_refused_by_the_cli(
    memory_quant_db: Database,
    quant_seed: Callable[..., Database],
    french_factor_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    conn, s, as_of = _seeded(memory_quant_db, quant_seed, french_factor_dir)
    path = french_factor_dir / FF3
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(FactorIntegrityError, match="does not match the manifest"):
        run_build_risk_model(s, as_of=as_of, conn=conn)
    assert conn.execute("SELECT COUNT(*) FROM quant_risk_model").fetchone()[0] == 0
    status = conn.execute("SELECT status FROM quant_run WHERE command = 'build-risk-model'")
    assert status.fetchone()[0] == "failed"
    capsys.readouterr()


def test_optimize_mu_carhart_writes_its_own_books_beside_equilibrium(
    memory_quant_db: Database, quant_seed: Callable[..., Database], french_factor_dir: Path
) -> None:
    conn, s, as_of = _seeded(
        memory_quant_db, quant_seed, french_factor_dir, objectives=["min_var", "tangency"]
    )
    eq = run_optimize(s, as_of=as_of, conn=conn)
    ca = run_optimize(s.model_copy(update={"ret_estimator": "carhart"}), as_of=as_of, conn=conn)
    assert eq.model_id == ca.model_id  # one risk model, two estimators

    rows = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, kind, engine_version, expected_return, manifest_json, params_json "
            "FROM quant_portfolio"
        )
    }
    assert len(rows) == 4  # nothing overwritten
    for pid in eq.books.values():
        assert "+mu-" not in rows[pid]["engine_version"]  # the default keeps its key
        assert "ret_estimator" not in json.loads(rows[pid]["manifest_json"])
    for pid in ca.books.values():
        assert rows[pid]["engine_version"].endswith("+mu-carhart")
        assert json.loads(rows[pid]["manifest_json"])["ret_estimator"] == "carhart"
        assert json.loads(rows[pid]["params_json"])["ret_estimator"] == "carhart"

    # min_var never touches mu: the same book under either estimator; tangency moves
    def weights(pid: int) -> dict[int, float]:
        return {
            int(r["asset_id"]): float(r["weight"])
            for r in conn.execute(
                "SELECT asset_id, weight FROM quant_position WHERE portfolio_id=?", (pid,)
            )
        }

    assert weights(eq.books["min_var"]) == pytest.approx(weights(ca.books["min_var"]), abs=1e-6)
    assert weights(eq.books["tangency"]) != pytest.approx(weights(ca.books["tangency"]), abs=1e-3)
    assert rows[eq.books["tangency"]]["expected_return"] != pytest.approx(
        rows[ca.books["tangency"]]["expected_return"], abs=1e-6
    )


def test_cli_accepts_mu_carhart_and_validates_the_turnover_cap(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = cli.build_parser().parse_args(["optimize", "--mu", "carhart", "--turnover-cap", "0.5"])
    assert (args.ret_estimator, args.turnover_cap) == ("carhart", 0.5)
    assert cli.build_parser().parse_args(["optimize", "--turnover-cap", "2"]).turnover_cap == 2.0
    for bad in ("0", "-0.1", "2.5", "x"):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["optimize", "--turnover-cap", bad])
    assert "turnover cap must be in (0, 2]" in capsys.readouterr().err
    ev = cli.build_parser().parse_args(["evaluate", "--turnover-cost-bps", "15"])
    assert ev.turnover_cost_bps == 15.0
