"""Contract tests: the last stdout line of each quant writing command (BACKTEST_SPEC §8.2).

The driver parses these lines, so their exact shape matters: ``build-returns`` names the
engine version it wrote, ``build-risk-model`` names ``model_id``, plain ``optimize`` names a
``portfolio_ids`` map read by key (never by position), and ``optimize --cycle-run-id`` names
the single Top-30 ``portfolio_id``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cycle.backtest.driver import (
    BacktestError,
    _build_returns_version,
    _min_var_portfolio_id,
    _model_id,
    _topn_portfolio_id,
)
from quant.cli import _run_build_returns, _run_build_risk_model, _run_optimize
from quant.config import QuantSettings
from quant.persist import OptimizeRunResult, RiskModelResult, TopnBookResult


def _settings() -> QuantSettings:
    return QuantSettings(db_path=Path(":memory:"), objectives=["min_var"])


def _last_json(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    last = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(last)
    assert isinstance(payload, dict)
    return payload


def test_build_returns_ends_with_the_engine_version_and_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    report = SimpleNamespace(
        engine_version="qret-v3",
        rows_written=12,
        assets=3,
        assets_with_dividends=3,
        corpact_engine=None,
        dividends_guard_bypassed=None,
        dirty_tree_bypassed=None,
    )
    monkeypatch.setattr("quant.cli.run_build_returns", lambda *_a, **_k: report)
    args = argparse.Namespace(date_from="2019-01-02", allow_no_dividends=False)

    assert _run_build_returns(_settings(), args, "2022-10-12") == 0

    payload = _last_json(capsys)
    assert payload == {
        "engine_version": "qret-v3",
        "n_rows": 12,
        "date_from": "2019-01-02",
        "date_to": "2022-10-12",
    }
    _build_returns_version(payload, "qret-v3")
    with pytest.raises(BacktestError, match="refusing to simulate"):
        _build_returns_version(payload, "qret-v2")
    with pytest.raises(BacktestError, match="no engine_version"):
        _build_returns_version({"n_rows": 0}, "qret-v3")


def test_build_risk_model_ends_with_model_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake(*_a: object, **_k: object) -> RiskModelResult:
        return RiskModelResult(7, "2026-01-02", 5, "ledoit_wolf_cc", 0.1, 15, True, "abc12345")

    monkeypatch.setattr("quant.cli.run_build_risk_model", fake)
    assert _run_build_risk_model(_settings(), "2026-01-02", store_cov=True) == 0

    payload = _last_json(capsys)
    assert payload == {"model_id": 7}
    assert _model_id(payload) == 7


def test_plain_optimize_ends_with_a_portfolio_ids_map(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake(*_a: object, **_k: object) -> OptimizeRunResult:
        return OptimizeRunResult(7, "2026-01-02", {"min_var": 11, "tangency": 12}, 0, "abc12345")

    monkeypatch.setattr("quant.cli.run_optimize", fake)
    assert _run_optimize(_settings(), "2026-01-02") == 0

    payload = _last_json(capsys)
    assert payload == {
        "model_id": 7,
        "portfolio_ids": {"min_var": 11, "tangency": 12},
    }
    assert _min_var_portfolio_id(payload) == 11

    # By name, never "the first id": a map without min_var is refused even when non-empty.
    with pytest.raises(BacktestError, match=r"portfolio_ids\['min_var'\]"):
        _min_var_portfolio_id({"portfolio_ids": {"tangency": 12}})
    with pytest.raises(BacktestError, match=r"portfolio_ids\['min_var'\]"):
        _min_var_portfolio_id({"portfolio_ids": [11, 12]})


def test_optimize_cycle_run_ends_with_the_topn_portfolio_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake(*_a: object, **_k: object) -> TopnBookResult:
        return TopnBookResult(21, 7, "2026-01-02", 5, "min_var_topn", 30, "optimal", 1.0)

    monkeypatch.setattr("quant.cli.run_optimize_topn", fake)
    assert _run_optimize(_settings(), "2026-01-02", cycle_run_id=5, risk_model_id=7) == 0

    payload = _last_json(capsys)
    assert payload == {"portfolio_id": 21, "model_id": 7, "kind": "min_var_topn"}
    assert _topn_portfolio_id(payload) == 21
    with pytest.raises(BacktestError, match="no portfolio_id"):
        _topn_portfolio_id({"portfolio_ids": {"min_var": 21}})
