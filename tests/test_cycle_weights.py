"""T-141: the equal composite weights, their renormalization, and the resume guard on them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from portfolio_common.db import Database

from cycle import orchestrator
from cycle.config import _DEFAULT_WEIGHTS, CycleSettings
from cycle.orchestrator import _blended
from cycle.state import ScoreWeightsMismatch

DAY = "2026-06-30"
OLD_WEIGHTS = {"FUNDAMENTAL": 0.4, "VALORIZATION": 0.3, "TECHNICAL": 0.2, "SEMANTIC": 0.1}


def _settings(**kw: object) -> CycleSettings:
    return CycleSettings(db_path=Path(":memory:"), top_n=3, **kw)  # type: ignore[arg-type]


def test_the_default_weights_are_three_components_at_one_third_each() -> None:
    weights = CycleSettings(db_path=Path(":memory:")).score_weights
    assert set(weights) == {"FUNDAMENTAL", "VALORIZATION", "TECHNICAL"}
    assert "SEMANTIC" not in weights
    assert all(w == pytest.approx(1 / 3) for w in weights.values())
    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights == _DEFAULT_WEIGHTS


def test_the_settings_get_their_own_copy_of_the_defaults() -> None:
    CycleSettings(db_path=Path(":memory:")).score_weights["FUNDAMENTAL"] = 0.9
    assert _DEFAULT_WEIGHTS["FUNDAMENTAL"] == pytest.approx(1 / 3)


def test_a_missing_component_is_renormalized_over_the_two_present() -> None:
    per_type: dict[str, dict[int, float | None]] = {
        "FUNDAMENTAL": {1: 60.0, 2: 60.0},
        "VALORIZATION": {1: 40.0, 2: None},
        "TECHNICAL": {1: 50.0, 2: 80.0},
        "SEMANTIC": {1: 99.0},  # stored rows of an unweighted type are not blended
    }
    full, parts = _blended(per_type, _DEFAULT_WEIGHTS, 1)
    assert full == pytest.approx((60 + 40 + 50) / 3)
    assert parts == {"FUNDAMENTAL": 60.0, "VALORIZATION": 40.0, "TECHNICAL": 50.0}
    partial, parts = _blended(per_type, _DEFAULT_WEIGHTS, 2)
    assert partial == pytest.approx((60 + 80) / 2)  # 1/2 each, not 1/3 each over a lost third
    assert parts["VALORIZATION"] is None


def test_an_asset_missing_a_component_is_blended_at_one_half_each_and_the_view_agrees(
    cycle_seed: Database,
) -> None:
    conn = cycle_seed
    # EEE has no price observations, so it has no TECHNICAL score
    conn.execute("DELETE FROM price_observation WHERE asset_id = 5")
    conn.commit()
    orchestrator.run_monitoring(_settings(), DAY, conn=conn)

    row = conn.execute(
        "SELECT r.asset_id, r.blended_score, r.components_json FROM cycle_ranking r "
        "JOIN assets a ON a.id = r.asset_id WHERE a.ticker = 'EEE'"
    ).fetchone()
    components = json.loads(row["components_json"])
    assert components["TECHNICAL"] is None
    assert components["FUNDAMENTAL"] is not None and components["VALORIZATION"] is not None
    assert row["blended_score"] == pytest.approx(
        (components["FUNDAMENTAL"] + components["VALORIZATION"]) / 2
    )  # no soft veto in this cohort, so no penalty

    effective = {
        r["score_type"]: r["effective_weight"]
        for r in conn.execute(
            "SELECT score_type, effective_weight FROM v_cycle_ranking_component WHERE asset_id = ?",
            (row["asset_id"],),
        )
    }
    assert effective == {"FUNDAMENTAL": pytest.approx(0.5), "VALORIZATION": pytest.approx(0.5)}
    assert sum(effective.values()) == pytest.approx(1.0)


def test_a_full_asset_has_three_effective_weights_summing_to_one_and_three_weight_rows(
    cycle_seed: Database,
) -> None:
    conn = cycle_seed
    orchestrator.run_monitoring(_settings(), DAY, conn=conn)
    rows = conn.execute(
        "SELECT score_type, weight FROM v_weight_component WHERE cycle_type = 'MONITORING'"
    ).fetchall()
    assert {r["score_type"] for r in rows} == {"FUNDAMENTAL", "VALORIZATION", "TECHNICAL"}
    assert all(r["weight"] == pytest.approx(1 / 3) for r in rows)
    sums = [
        r[0]
        for r in conn.execute(
            "SELECT SUM(effective_weight) FROM v_cycle_ranking_component GROUP BY asset_id"
        )
    ]
    assert len(sums) == 5 and all(s == pytest.approx(1.0) for s in sums)


def test_resuming_under_other_score_weights_is_refused(cycle_seed: Database) -> None:
    conn = cycle_seed
    orchestrator.run_monitoring(_settings(), DAY, conn=conn)
    before = conn.execute("SELECT params_json FROM cycle_run").fetchone()["params_json"]
    ranking = [tuple(r) for r in conn.execute("SELECT * FROM cycle_ranking ORDER BY id")]

    with pytest.raises(ScoreWeightsMismatch, match="mix two blends") as exc:
        orchestrator.run_monitoring(_settings(score_weights=OLD_WEIGHTS), DAY, conn=conn)
    assert "SEMANTIC" in str(exc.value)  # the message names what changed
    with pytest.raises(ScoreWeightsMismatch):
        orchestrator.run_monitoring(
            _settings(score_weights={"FUNDAMENTAL": 0.5, "VALORIZATION": 0.25, "TECHNICAL": 0.25}),
            DAY,
            conn=conn,
        )

    # nothing was touched, and the run was not flipped to running/failed
    assert conn.execute("SELECT params_json FROM cycle_run").fetchone()["params_json"] == before
    assert conn.execute("SELECT status FROM cycle_run").fetchone()["status"] == "completed"
    assert [tuple(r) for r in conn.execute("SELECT * FROM cycle_ranking ORDER BY id")] == ranking
    # the same weights resume fine
    again = orchestrator.run_monitoring(_settings(), DAY, conn=conn)
    assert "rank" in again.steps_skipped


def test_a_selection_run_recorded_under_the_old_weights_refuses_the_new_defaults(
    cycle_seed: Database,
) -> None:
    conn = cycle_seed
    orchestrator.run_selection(_settings(score_weights=OLD_WEIGHTS), DAY, conn=conn)
    with pytest.raises(ScoreWeightsMismatch, match="SEMANTIC"):
        orchestrator.run_selection(_settings(), DAY, conn=conn)


def test_a_run_that_recorded_no_weights_is_left_alone(cycle_seed: Database) -> None:
    conn = cycle_seed
    orchestrator.run_monitoring(_settings(), DAY, conn=conn)
    params = json.loads(conn.execute("SELECT params_json FROM cycle_run").fetchone()["params_json"])
    del params["score_weights"]
    conn.execute("UPDATE cycle_run SET params_json = ?", (json.dumps(params),))
    conn.commit()
    orchestrator.run_monitoring(_settings(score_weights=OLD_WEIGHTS), DAY, conn=conn)

    conn.execute("UPDATE cycle_run SET params_json = NULL")
    conn.commit()
    orchestrator.run_monitoring(_settings(score_weights=OLD_WEIGHTS), DAY, conn=conn)
