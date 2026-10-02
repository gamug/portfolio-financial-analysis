"""The risk model's market portfolio never silently drops an asset (T-132(d)).

``_expected_returns`` used to do ``caps_by_id.get(a, 0.0)``: an asset with no cap got weight 0 in
the equilibrium market portfolio (XOM, and PG about 40% of the weight in risk model 3), and a panel
with no caps at all fell back to equal weights. Now the build refuses, or -- with
``--allow-missing-caps`` -- records exactly who was weighted 0 and why, on the run.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from portfolio_common.db import Database

from cycle import data as cycle_data
from kg_schema.market_cap import market_caps_as_of
from quant.cli import _run_build_risk_model, _settings, build_parser
from quant.config import QuantSettings
from quant.persist import MissingMarketCaps, RiskModelResult, run_build_risk_model
from quant.returns import run_build_returns


def _config(**over: object) -> QuantSettings:
    base: dict[str, object] = {
        "db_path": Path(":memory:"),
        "lookback_days": 200,
        "min_history_days": 140,
        "liquidity_min_dollar_volume": 0.0,
        "max_name_weight": None,
        "max_sector_weight": None,
        "objectives": ["min_var"],
        "frontier_k": 4,
    }
    base.update(over)
    return QuantSettings(**base)  # type: ignore[arg-type]


@pytest.fixture
def seeded(memory_quant_db: Database, quant_seed: Callable[..., Database]) -> Database:
    conn = quant_seed(memory_quant_db, n_assets=6, n_days=280, with_dividends=True)
    run_build_returns(
        QuantSettings(db_path=Path(":memory:")),
        date_from="2000-01-01",
        date_to="2100-01-01",
        conn=conn,
    )
    return conn


def _as_of(conn: Database) -> str:
    return str(conn.execute("SELECT MAX(obs_date) FROM quant_return_daily").fetchone()[0])


def _drop_counts(conn: Database, asset_id: int) -> None:
    conn.execute(
        "DELETE FROM filing_cover_shares WHERE filing_id IN "
        "(SELECT id FROM sec_filings WHERE asset_id = ?)",
        (asset_id,),
    )
    conn.commit()


def _equilibrium(conn: Database, model_id: int) -> dict[int, float]:
    return {
        int(r["asset_id"]): float(r["mu"])
        for r in conn.execute(
            "SELECT asset_id, mu FROM quant_expected_return WHERE model_id = ? "
            "AND mu_model = 'equilibrium'",
            (model_id,),
        )
    }


def _run_params(conn: Database, command: str = "build-risk-model") -> dict[str, object]:
    row = conn.execute(
        "SELECT params_json FROM quant_run WHERE command = ? ORDER BY id DESC LIMIT 1", (command,)
    ).fetchone()
    return dict(json.loads(row["params_json"]))


# -- coverage is recorded ----------------------------------------------------------------------


def test_the_run_records_cap_coverage_and_age(seeded: Database) -> None:
    res = run_build_risk_model(_config(), as_of=_as_of(seeded), conn=seeded)

    cov = res.market_cap_coverage
    assert (cov["n_assets"], cov["n_with_cap"], cov["n_missing"]) == (6, 6, 0)
    assert cov["missing"] == {}
    assert 0 <= cov["share_age_days_max"] <= cov["max_share_age_days"] == 200
    assert cov["share_age_days_median"] is not None

    # on the run (T-132(d)), beside the settings it was started with, and on the model itself
    assert _run_params(seeded)["market_caps"] == cov
    assert "build_params" not in _run_params(seeded) and "lookback_days" in _run_params(seeded)
    model_params = json.loads(
        seeded.execute(
            "SELECT params_json FROM quant_risk_model WHERE id = ?", (res.model_id,)
        ).fetchone()["params_json"]
    )
    assert model_params["market_caps"] == cov


def test_the_equilibrium_follows_the_caps(seeded: Database) -> None:
    """The seeded counts are 100k x the asset's index, so the larger names must move the market
    portfolio: the equilibrium prior differs from one with every cap equal."""
    unequal = run_build_risk_model(_config(), as_of=_as_of(seeded), conn=seeded)
    seeded.execute("UPDATE filing_cover_shares SET value = 100000")
    seeded.commit()
    equal = run_build_risk_model(
        _config(risk_model_version="rm-equal"), as_of=_as_of(seeded), conn=seeded
    )
    assert _equilibrium(seeded, unequal.model_id) != _equilibrium(seeded, equal.model_id)


# -- refusing ----------------------------------------------------------------------------------


def test_a_panel_asset_without_a_cap_refuses_the_model(seeded: Database) -> None:
    _drop_counts(seeded, 3)
    with pytest.raises(MissingMarketCaps, match=r"1 of 6.*\b3 \(no_cover_shares\)"):
        run_build_risk_model(_config(), as_of=_as_of(seeded), conn=seeded)
    status = seeded.execute(
        "SELECT status, error FROM quant_run WHERE command = 'build-risk-model'"
    ).fetchone()
    assert status["status"] == "failed" and "allow-missing-caps" in status["error"]
    assert seeded.execute("SELECT COUNT(*) FROM quant_risk_model").fetchone()[0] == 0


def test_a_stale_count_counts_as_missing(seeded: Database) -> None:
    """The seeded filings are quarterly; with a 1-day limit none is fresh enough."""
    with pytest.raises(MissingMarketCaps, match="no panel asset has a market cap"):
        run_build_risk_model(
            _config(market_cap_max_share_age_days=1), as_of=_as_of(seeded), conn=seeded
        )


def test_no_price_near_the_date_counts_as_missing(seeded: Database) -> None:
    as_of = _as_of(seeded)
    seeded.execute(
        "DELETE FROM price_daily WHERE asset_id = 2 AND date > date(?, '-30 days')", (as_of,)
    )
    seeded.commit()
    with pytest.raises(MissingMarketCaps, match=r"\b2 \(no_recent_price\)"):
        run_build_risk_model(_config(), as_of=as_of, conn=seeded)


def test_a_panel_with_no_caps_at_all_always_refuses(seeded: Database) -> None:
    """Even with the override: there is no market portfolio, and equal weights would pass for one
    (the old fallback)."""
    seeded.execute("DELETE FROM filing_cover_shares")
    seeded.commit()
    with pytest.raises(MissingMarketCaps, match="no panel asset has a market cap"):
        run_build_risk_model(_config(allow_missing_caps=True), as_of=_as_of(seeded), conn=seeded)


# -- the override ------------------------------------------------------------------------------


def test_allowing_missing_caps_weights_them_zero_and_names_them(seeded: Database) -> None:
    as_of = _as_of(seeded)
    full = run_build_risk_model(_config(risk_model_version="rm-full"), as_of=as_of, conn=seeded)
    _drop_counts(seeded, 3)
    res = run_build_risk_model(_config(allow_missing_caps=True), as_of=as_of, conn=seeded)

    cov = res.market_cap_coverage
    assert (cov["n_with_cap"], cov["n_missing"]) == (5, 1)
    assert cov["missing"] == {"3": "no_cover_shares"}  # named, with the reason
    params = _run_params(seeded)
    assert cov == params["market_caps"]  # the same facts, on the run
    assert params["allow_missing_caps"] is True
    # and it really did change the market portfolio -- the model is not a silent copy of the full one
    assert _equilibrium(seeded, res.model_id) != _equilibrium(seeded, full.model_id)


# -- the CLI -----------------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["build-risk-model", "optimize"])
def test_the_flag_reaches_the_settings(command: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KG_FINANCIAL_DB", ":memory:")  # QuantSettings.load() needs the path
    parser = build_parser()
    assert parser.parse_args([command]).allow_missing_caps is False
    args = parser.parse_args([command, "--allow-missing-caps"])
    assert _settings(args).allow_missing_caps is True
    assert _settings(parser.parse_args([command])).allow_missing_caps is False
    age = parser.parse_args([command, "--market-cap-max-share-age-days", "90"])
    assert _settings(age).market_cap_max_share_age_days == 90
    assert _settings(parser.parse_args([command])).market_cap_max_share_age_days == 200


def test_the_cli_reports_a_refusal_and_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise MissingMarketCaps("1 of 6 panel assets have no market cap")

    monkeypatch.setattr("quant.cli.run_build_risk_model", refuse)
    assert _run_build_risk_model(_config(), "2026-01-02", store_cov=True) == 1
    assert "no market cap" in capsys.readouterr().err


def test_the_cli_prints_the_coverage_and_warns_about_a_missing_cap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cov = {
        "n_assets": 6,
        "n_with_cap": 5,
        "n_missing": 1,
        "missing": {"3": "no_cover_shares"},
        "share_age_days_median": 41.0,
        "share_age_days_max": 88,
    }

    def built(*_a: object, **_k: object) -> RiskModelResult:
        return RiskModelResult(
            7, "2026-01-02", 6, "ledoit_wolf_cc", 0.1, 10, True, "abc12345", market_cap_coverage=cov
        )

    monkeypatch.setattr("quant.cli.run_build_risk_model", built)
    assert _run_build_risk_model(_config(), "2026-01-02", store_cov=True) == 0
    captured = capsys.readouterr()
    assert "market caps: 5/6 assets" in captured.out and "max 88d" in captured.out
    assert "weighted 1 asset(s) 0" in captured.err and "3" in captured.err


# -- cycle reads the same function --------------------------------------------------------------


def test_cycle_and_quant_read_the_same_caps(seeded: Database) -> None:
    as_of = _as_of(seeded)
    _drop_counts(seeded, 3)
    ids = [1, 2, 3, 4, 5, 6]
    cycle = cycle_data.market_cap_estimates(seeded, as_of, ids)
    shared = market_caps_as_of(seeded, ids, as_of=as_of)
    assert cycle == {a: shared.get(a) for a in ids}
    assert cycle[3] is None and all(cycle[a] for a in (1, 2, 4, 5, 6))  # missing => None, not 0
