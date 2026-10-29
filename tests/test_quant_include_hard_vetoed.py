"""``--include-hard-vetoed`` widens the universe gate, and the choice is recorded (WP-B).

BACKTEST_SPEC §8.2: ``build-risk-model --include-hard-vetoed`` and ``optimize
--include-hard-vetoed`` set ``exclude_hard_vetoed = False`` and record it on the run's
``params_json`` and manifest. The flag also changes the manifest **tag**, so a model built over
the names under a T-1 HARD veto neither reuses nor overwrites the narrow model stored at the same
as-of. ``min_coverage`` (0.98, the 740-session floor) moved from a function default into
``QuantSettings`` and every manifest.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from portfolio_common.db import Database

from kg_schema.versions import MetricVersions
from quant.cli import _run_build_risk_model, _run_optimize, _settings, build_parser
from quant.config import DEFAULT_MIN_COVERAGE, QuantSettings
from quant.manifest import QuantManifest, resolve_quant_manifest
from quant.persist import OptimizeRunResult, RiskModelResult, run_build_risk_model, run_optimize
from quant.returns import run_build_returns

_PRE_CHANGE_TAG = "1d7256fb"  # tag of {consumer, metrics {valuation: metrics-v1}, returns qret-v2}


def _domestic(**over: object) -> QuantSettings:
    base: dict[str, object] = {
        "db_path": Path(":memory:"),
        "lookback_days": 200,
        "min_history_days": 140,
        "liquidity_min_dollar_volume": 0.0,
        "max_name_weight": None,
        "max_sector_weight": None,
        "objectives": ["min_var"],
    }
    base.update(over)
    return QuantSettings(**base)  # type: ignore[arg-type]


def _as_of(conn: Database) -> str:
    return str(conn.execute("SELECT MAX(obs_date) FROM quant_return_daily").fetchone()[0])


@pytest.fixture
def seeded(memory_quant_db: Database, quant_seed: Callable[..., Database]) -> Database:
    conn = quant_seed(memory_quant_db, n_assets=5, n_days=280, with_dividends=True)
    run_build_returns(
        QuantSettings(db_path=Path(":memory:")),
        date_from="2000-01-01",
        date_to="2100-01-01",
        conn=conn,
    )
    # asset 4 is under a HARD veto from 2024-06-01 on (T-1: raised_on <= as_of - 1)
    conn.execute(
        "INSERT INTO rule_catalog (rule_id, description, severity, created_at) "
        "VALUES ('R1', 'x', 'HARD', '2024-01-01')"
    )
    conn.execute(
        "INSERT INTO veto (asset_id, rule_id, severity, raised_on, last_seen_on, detected_at) "
        "VALUES (4, 'R1', 'HARD', '2024-06-01', '2024-06-01', '2024-06-01T00:00:00Z')"
    )
    conn.commit()
    return conn


# -- the flag ---------------------------------------------------------------------------------


def test_the_flag_exists_on_both_commands() -> None:
    parser = build_parser()
    for command in ("build-risk-model", "optimize"):
        args = parser.parse_args([command, "--include-hard-vetoed"])
        assert args.include_hard_vetoed is True
        assert parser.parse_args([command]).include_hard_vetoed is False
        assert _settings(args).exclude_hard_vetoed is False
        assert _settings(parser.parse_args([command])).exclude_hard_vetoed is True


# -- the manifest -----------------------------------------------------------------------------


def test_the_default_tag_is_still_the_pre_change_one() -> None:
    """No stored model or book may change key: a default run's manifest inputs are untouched."""
    default = QuantManifest(
        metrics=MetricVersions({"valuation": "metrics-v1"}), return_engine_version="qret-v2"
    )
    assert default.tag == _PRE_CHANGE_TAG
    assert "exclude_hard_vetoed" not in default.inputs
    assert "min_coverage" not in default.inputs


def test_a_widened_manifest_has_its_own_tag_and_records_the_choice() -> None:
    narrow = QuantManifest(
        metrics=MetricVersions({"valuation": "metrics-v1"}), return_engine_version="qret-v2"
    )
    wide = QuantManifest(
        metrics=MetricVersions({"valuation": "metrics-v1"}),
        return_engine_version="qret-v2",
        exclude_hard_vetoed=False,
    )
    assert wide.tag != narrow.tag
    assert wide.inputs["exclude_hard_vetoed"] is False
    assert wide.record()["exclude_hard_vetoed"] is False
    assert "exclude_hard_vetoed" not in narrow.record()


def test_min_coverage_is_recorded_and_only_off_default_moves_the_tag() -> None:
    base = QuantManifest(
        metrics=MetricVersions({"valuation": "metrics-v1"}), return_engine_version="qret-v2"
    )
    assert base.min_coverage == DEFAULT_MIN_COVERAGE == 0.98
    assert base.record()["min_coverage"] == 0.98
    other = QuantManifest(
        metrics=MetricVersions({"valuation": "metrics-v1"}),
        return_engine_version="qret-v2",
        min_coverage=0.90,
    )
    assert other.tag != base.tag
    assert other.inputs["min_coverage"] == 0.90


def test_resolve_manifest_carries_the_settings_choice(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=3, n_days=200, with_dividends=False)
    narrow = resolve_quant_manifest(conn, _domestic())
    wide = resolve_quant_manifest(conn, _domestic(exclude_hard_vetoed=False, min_coverage=0.5))
    assert narrow.tag != wide.tag
    assert wide.record()["exclude_hard_vetoed"] is False
    assert wide.record()["min_coverage"] == 0.5
    assert "exclude_hard_vetoed" not in narrow.record()


# -- the gate the model is built from ---------------------------------------------------------


def test_the_widened_model_covers_a_hard_vetoed_name(seeded: Database) -> None:
    as_of = _as_of(seeded)
    narrow = run_build_risk_model(_domestic(), as_of=as_of, conn=seeded)
    assert (
        4
        not in json.loads(
            seeded.execute(
                "SELECT panel_spec_json FROM quant_risk_model WHERE id = ?", (narrow.model_id,)
            ).fetchone()[0]
        )["asset_ids"]
    )

    wide = run_build_risk_model(_domestic(exclude_hard_vetoed=False), as_of=as_of, conn=seeded)
    assert wide.model_id != narrow.model_id  # a parallel model, not an overwrite
    panel = json.loads(
        seeded.execute(
            "SELECT panel_spec_json FROM quant_risk_model WHERE id = ?", (wide.model_id,)
        ).fetchone()[0]
    )["asset_ids"]
    assert 4 in panel

    stored = seeded.execute(
        "SELECT params_json, manifest_json FROM quant_risk_model WHERE id = ?", (wide.model_id,)
    ).fetchone()
    assert json.loads(stored["params_json"])["exclude_hard_vetoed"] is False
    assert json.loads(stored["manifest_json"])["exclude_hard_vetoed"] is False


def test_a_default_optimize_does_not_reuse_the_widened_model(seeded: Database) -> None:
    as_of = _as_of(seeded)
    wide = run_build_risk_model(_domestic(exclude_hard_vetoed=False), as_of=as_of, conn=seeded)
    narrow_opt = run_optimize(_domestic(), as_of=as_of, conn=seeded)
    assert narrow_opt.model_id != wide.model_id
    assert seeded.execute("SELECT COUNT(*) FROM quant_risk_model").fetchone()[0] == 2

    wide_opt = run_optimize(_domestic(exclude_hard_vetoed=False), as_of=as_of, conn=seeded)
    assert wide_opt.model_id == wide.model_id  # reused by its own tagged version
    params = json.loads(
        seeded.execute(
            "SELECT params_json FROM quant_portfolio WHERE id = ?",
            (wide_opt.books["min_var"],),
        ).fetchone()[0]
    )
    assert params  # the book exists under the widened manifest


# -- the final JSON line (BACKTEST_SPEC §8.2) -------------------------------------------------


def test_build_risk_model_ends_with_a_json_model_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake(*_a: object, **_k: object) -> RiskModelResult:
        return RiskModelResult(7, "2026-01-02", 5, "ledoit_wolf_cc", 0.1, 15, True, "abc12345")

    monkeypatch.setattr("quant.cli.run_build_risk_model", fake)
    assert _run_build_risk_model(_domestic(), "2026-01-02", store_cov=True) == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(last) == {"model_id": 7}


def test_optimize_ends_with_a_json_portfolio_ids_map(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake(*_a: object, **_k: object) -> OptimizeRunResult:
        return OptimizeRunResult(7, "2026-01-02", {"min_var": 11, "tangency": 12}, 0, "abc12345")

    monkeypatch.setattr("quant.cli.run_optimize", fake)
    assert _run_optimize(_domestic(), "2026-01-02") == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(last) == {"model_id": 7, "portfolio_ids": {"min_var": 11, "tangency": 12}}
