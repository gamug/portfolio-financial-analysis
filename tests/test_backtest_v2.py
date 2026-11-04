"""V2_PLAN §2 hand-computed known answers.

Each expected number is written out by hand from the plan's arithmetic (core weights, the drift
formula, the drawdown scaler, Faber's 210-session mean, the bootstrap's add-one p), independently
of the code under test. Synthetic series only: no real data, no network, no LLM.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from test_backtest_known_answers import _by_date, _guard, _run, _sessions

from cycle.backtest import core, driver, engine, stats

BASE = engine.ARM_MARKOWITZ_BASE
BASE_GUARD = engine.ARM_MARKOWITZ_BASE_GUARD
NOGUARD = engine.ARM_TOPN_NOGUARD
GUARD = engine.ARM_TOPN_GUARD_NO_NLP
CORE_ID = 100


def _close_series(count: int = 210, value: float = 100.0) -> dict[str, float]:
    """*count* synthetic sessions with a constant close ``d0000``.. (zero-padded, sortable)."""
    return {f"d{index:04d}": value for index in range(count)}


# -- (a) the core ----------------------------------------------------------------------------


def test_the_core_target_is_c_in_spy_plus_one_minus_c_times_the_satellite() -> None:
    satellite = {1: 0.6, 2: 0.4}
    target = core.core_target(0.5, satellite, CORE_ID)
    assert target == pytest.approx({CORE_ID: 0.5, 1: 0.3, 2: 0.2}, abs=1e-15)
    # c = 0 is the v1 target, unchanged
    assert core.core_target(0.0, satellite, CORE_ID) == satellite
    with pytest.raises(ValueError):
        core.core_target(1.0, satellite, CORE_ID)
    with pytest.raises(ValueError):
        core.core_target(-0.1, satellite, CORE_ID)


def test_the_core_drifts_between_decisions_hand_computed() -> None:
    sessions = _sessions(4)
    returns = {
        sessions[1]: {CORE_ID: 0.0, 1: 0.0},
        sessions[2]: {CORE_ID: 0.02, 1: 0.0},
        sessions[3]: {CORE_ID: 0.0, 1: 0.0},
    }
    target = {CORE_ID: 0.5, 1: 0.5}
    targets = {BASE: {sessions[0]: target}, NOGUARD: {sessions[0]: target}}
    result = _run(
        sessions,
        returns,
        targets,
        [engine.ArmSpec(BASE, BASE), engine.ArmSpec(NOGUARD, NOGUARD)],
        formation=sessions[1],
        decision_dates=[sessions[0]],
    )
    # the formation close executes the target exactly
    assert _by_date(result, NOGUARD)[sessions[1]].final_target == pytest.approx(target, abs=1e-15)
    # the formation close is the first close at risk, so its own return is not earned; the next
    # session (SPY +2 %, satellite 0 % -> book +1 %) drifts the weights by (1 + r_i) / 1.01
    drifted = _by_date(result, NOGUARD)[sessions[3]].drifted
    assert drifted[CORE_ID] == pytest.approx(0.5 * 1.02 / 1.01, abs=1e-15)
    assert drifted[1] == pytest.approx(0.5 / 1.01, abs=1e-15)
    assert sum(drifted.values()) == pytest.approx(1.0, abs=1e-15)


def test_whole_book_scaling_and_the_cash_residual_hand_computed() -> None:
    sessions = _sessions(4)
    returns = {
        sessions[1]: {CORE_ID: 0.0, 1: 0.0},
        sessions[2]: {CORE_ID: -0.10, 1: -0.10},
        sessions[3]: {CORE_ID: 0.0, 1: 0.0},
    }
    target = {CORE_ID: 0.6, 1: 0.4}
    arms = [
        engine.ArmSpec(BASE, BASE),
        engine.ArmSpec(
            BASE_GUARD,
            BASE,
            guard=_guard(layers={4}, m=1.0 / (1.0 - 0.75), alpha=0.75, vol_window=2),
        ),
    ]
    result = _run(
        sessions,
        returns,
        {BASE: {sessions[0]: target}},
        arms,
        formation=sessions[1],
        decision_dates=[sessions[0]],
        cost_bps=0.0,
    )
    # after the -10 % session NAV = 0.9, peak = 1.0, m = 1/(1-alpha) = 4:
    # s = 4 (0.9 - 0.75) / 0.9 = 2/3 on the whole book.
    guard = _by_date(result, BASE_GUARD)[sessions[2]]
    assert guard.final_target[CORE_ID] == pytest.approx(0.6 * 2.0 / 3.0, abs=1e-12)
    assert guard.final_target[1] == pytest.approx(0.4 * 2.0 / 3.0, abs=1e-12)
    assert guard.cash_weight == pytest.approx(1.0 / 3.0, abs=1e-12)
    # the unguarded twin keeps the full book, and the scale is on the whole book
    base = _by_date(result, BASE)[sessions[2]]
    assert base.final_target == pytest.approx(target, abs=1e-12)
    assert base.cash_weight == pytest.approx(0.0, abs=1e-12)


# -- (c) the trend filter --------------------------------------------------------------------


def test_the_trend_filter_sma_rule_and_dates_hand_computed() -> None:
    close = _close_series(209)
    # 209 sessions: not evaluable -> the filter stays off and says so
    assert core.trend_trigger(close, "d0208") is None
    close["d0209"] = 99.0
    # mean of the last 210 closes = (209 * 100 + 99) / 210 = 99.995238; 99 is below it
    assert core.trend_trigger(close, "d0209") is True
    close["d0209"] = 101.0
    assert core.trend_trigger(close, "d0209") is False
    # exactly at the mean is not "below"
    close["d0209"] = 100.0
    assert core.trend_trigger(close, "d0209") is False
    # only sessions <= the decision enter the mean
    close["d0210"] = 1.0
    assert core.trend_trigger(close, "d0209") is False
    with pytest.raises(core.CoreSeriesError):
        core.trend_trigger(close, "d9999")
    states = core.trend_states(_close_series(3), ["d0000", "d0001"])
    assert states == [
        {"date": "d0000", "state": "not_evaluable"},
        {"date": "d0001", "state": "not_evaluable"},
    ]


def test_the_filter_and_the_core_never_touch_the_base_book() -> None:
    base = {1: 0.5, 2: 0.5}
    satellite = {1: 0.6, 2: 0.4}
    close = _close_series()
    close["d0209"] = 99.0  # the filter triggers at d0209
    config = core.config_targets(
        base,
        satellite,
        core_weight=0.5,
        core_asset_id=CORE_ID,
        trend_filter=core.FILTER_ON,
        close=close,
        decision="d0209",
    )
    assert config.base == base  # never core, never cash
    assert config.topn == {}  # 100% cash
    assert config.trend_state == "cash"
    risk_on = core.config_targets(
        base,
        satellite,
        core_weight=0.5,
        core_asset_id=CORE_ID,
        trend_filter=core.FILTER_ON,
        close=close,
        decision="d0208",
    )
    assert risk_on.base == base
    assert risk_on.topn == pytest.approx({CORE_ID: 0.5, 1: 0.3, 2: 0.2}, abs=1e-15)
    assert risk_on.trend_state == "not_evaluable"


def test_a_filtered_decision_sends_both_topn_arms_to_cash() -> None:
    sessions = _sessions(4)
    returns = {session: {1: 0.01} for session in sessions[1:]}
    targets = {
        BASE: {sessions[0]: {1: 1.0}, sessions[2]: {1: 1.0}},
        NOGUARD: {sessions[0]: {1: 1.0}, sessions[2]: {}},
    }
    arms = [
        engine.ArmSpec(BASE, BASE),
        engine.ArmSpec(NOGUARD, NOGUARD),
        engine.ArmSpec(GUARD, NOGUARD, guard=_guard(layers={4}, m=2.0, alpha=0.75, vol_window=2)),
    ]
    result = _run(
        sessions,
        returns,
        targets,
        arms,
        formation=sessions[1],
        decision_dates=[sessions[0], sessions[2]],
    )
    for code in (NOGUARD, GUARD):
        after = _by_date(result, code)[sessions[3]]
        assert after.final_target == {}
        assert after.cash_weight == pytest.approx(1.0, abs=1e-12)
    # MARKOWITZ_BASE is not filtered
    assert _by_date(result, BASE)[sessions[3]].final_target == pytest.approx({1: 1.0}, abs=1e-12)


# -- (d) the base GUARD arm ------------------------------------------------------------------


def test_the_base_guard_arm_is_the_same_overlay_on_the_base_book() -> None:
    sessions = _sessions(5)
    returns = {session: {1: 0.001, 2: 0.001} for session in sessions[1:]}
    targets = {BASE: {sessions[0]: {1: 0.5, 2: 0.5}}}
    arms = [
        engine.ArmSpec(BASE, BASE),
        engine.ArmSpec(
            BASE_GUARD, BASE, guard=_guard(layers={1}, gamma=1.0, delta=0.0, vol_window=2)
        ),
    ]
    result = _run(
        sessions, returns, targets, arms, formation=sessions[1], decision_dates=[sessions[0]]
    )
    for session in sessions[1:]:
        assert _by_date(result, BASE_GUARD)[session].final_target == pytest.approx(
            _by_date(result, BASE)[session].final_target, abs=1e-12
        )
    params = driver.load_params("src/cycle/backtest/params.frozen.toml")
    pairs = driver._test_pairs({BASE, NOGUARD, GUARD, BASE_GUARD})
    assert (BASE_GUARD, BASE) in pairs
    assert (GUARD, BASE) in pairs
    specs = driver._arm_specs([BASE, NOGUARD, GUARD, BASE_GUARD], params, GUARD)
    spec = next(s for s in specs if s.code == BASE_GUARD)
    assert spec.target_source == BASE
    assert spec.guard is params.guard[GUARD]
    assert driver.resolve_arms([BASE, NOGUARD, BASE_GUARD], params) == [BASE, NOGUARD, BASE_GUARD]


# -- (e) the bootstrap -----------------------------------------------------------------------


def test_the_bootstrap_mdd_p_value_is_add_one_and_two_sided_hand_computed() -> None:
    losing = [-0.10] * 10
    flat = [0.0] * 10
    observed = 1.0 - 0.9**10
    diff, p_value = stats.bootstrap_mdd_diff(losing, flat, block=2, n_boot=9)
    assert diff == pytest.approx(observed, abs=1e-15)
    # every replicate has the same MDD as the observed path, so nothing reaches the gap:
    # p = (1 + 0) / (1 + 9) = 0.1
    assert p_value == pytest.approx(0.1, abs=0.0)
    # identical arms: no difference, p = 1 exactly
    assert stats.bootstrap_mdd_diff(losing, losing, block=2, n_boot=9) == (0.0, 1.0)
    with pytest.raises(ValueError):
        stats.bootstrap_mdd_diff(losing, flat, block=11, n_boot=9)
    with pytest.raises(ValueError):
        stats.bootstrap_mdd_diff(losing, flat[:-1], block=2, n_boot=9)


# -- the verified SPY file -------------------------------------------------------------------


def test_the_core_loader_refuses_a_file_that_does_not_match_its_manifest(
    tmp_path: Path,
) -> None:
    series = {
        "2024-01-02": {"close": 100.0, "div": 0.0, "tr": None},
        "2024-01-03": {"close": 101.0, "div": 0.0, "tr": 0.01},
    }
    path = tmp_path / "spy_tr.json"
    path.write_text(json.dumps(series))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"spy_tr_sha256": digest}))
    loaded = core.load_core_series(path)
    assert loaded.sha256 == digest
    assert loaded.close["2024-01-03"] == 101.0
    assert loaded.total_return["2024-01-03"] == pytest.approx(0.01)
    manifest.write_text(json.dumps({"spy_tr_sha256": "0" * 64}))
    with pytest.raises(core.CoreSeriesError):
        core.load_core_series(path)
    manifest.unlink()
    with pytest.raises(core.CoreSeriesError):
        core.load_core_series(path)
