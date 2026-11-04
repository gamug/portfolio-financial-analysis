"""Round-4 Basing Points stops and stop distances, hand-computed from the plan's rules."""

from __future__ import annotations

import pytest

from cycle.backtest import driver
from cycle.backtest.driver import unadjusted_price
from cycle.guard import GuardInput, GuardParams, evaluate, initial_state
from cycle.guard.basing import (
    Bar,
    BasingPointState,
    advance,
    distance_cell,
    establish,
    price_class,
    sensitivity_class,
    stop_distance,
)


def _machine(**over: object) -> BasingPointState:
    base: dict[str, object] = {
        "entry_session": "d010",
        "running_high": 101.0,
        "running_high_session": "d011",
        "candidate_low": None,
        "candidate_high": None,
        "candidate_session": None,
        "away_days": 0,
        "last_bp_session": "d010",
        "last_bp_low": None,
        "stop": 90.0,
        "peak_high": 101.0,
        "peak_recognized": True,
        "peak_used": False,
        "out": False,
        "fallback": False,
    }
    base.update(over)
    return BasingPointState(**base)  # type: ignore[arg-type]


def test_three_non_consecutive_away_days_establish_a_basing_point() -> None:
    state = _machine(candidate_low=96.0, candidate_high=100.0, candidate_session="d012")
    # away at d013, d015, d017; d014 and d016 are not away days.
    for session, bar in (
        ("d013", Bar(high=102.0, low=101.0, close=101.5)),
        ("d014", Bar(high=101.0, low=98.0, close=99.5)),
        ("d015", Bar(high=103.0, low=102.0, close=102.5)),
        ("d016", Bar(high=100.0, low=99.0, close=99.5)),
        ("d017", Bar(high=104.0, low=103.0, close=103.5)),
    ):
        state, events = advance(state, session, bar, distance=0.05, rule="bp3")
    assert [event.kind for event in events] == ["bp3"]
    assert events[0].value == pytest.approx(96.0)
    assert state.stop == pytest.approx(96.0 * 0.95)
    assert state.candidate_low is None and state.away_days == 0


def test_a_strictly_lower_low_resets_the_candidate() -> None:
    state = _machine(candidate_low=96.0, candidate_high=100.0, candidate_session="d012")
    state, _ = advance(
        state, "d013", Bar(high=102.0, low=101.0, close=101.5), distance=0.05, rule="bp3"
    )
    assert state.away_days == 1
    state, events = advance(
        state, "d014", Bar(high=99.0, low=95.0, close=97.0), distance=0.05, rule="bp3"
    )
    assert events == [] and state.candidate_low == 95.0 and state.away_days == 0
    for session, bar in (
        ("d015", Bar(high=101.0, low=100.0, close=100.5)),
        ("d016", Bar(high=102.0, low=101.0, close=101.5)),
        ("d017", Bar(high=103.0, low=102.0, close=102.5)),
    ):
        state, events = advance(state, session, bar, distance=0.05, rule="bp3")
    assert [event.value for event in events] == [95.0]


def test_equal_lows_keep_the_earlier_session() -> None:
    state = _machine(candidate_low=None, candidate_high=None, candidate_session=None)
    state, _ = advance(
        state, "d012", Bar(high=99.0, low=96.0, close=97.0), distance=0.05, rule="bp3"
    )
    assert state.candidate_session == "d012"
    state, _ = advance(
        state, "d013", Bar(high=98.0, low=96.0, close=97.0), distance=0.05, rule="bp3"
    )
    assert state.candidate_session == "d012"


def test_the_stop_only_moves_up() -> None:
    state = _machine(candidate_low=96.0, candidate_high=100.0, candidate_session="d012")
    for session, bar in (
        ("d013", Bar(high=102.0, low=101.0, close=101.5)),
        ("d015", Bar(high=103.0, low=102.0, close=102.5)),
        ("d017", Bar(high=104.0, low=103.0, close=103.5)),
    ):
        state, _ = advance(state, session, bar, distance=0.05, rule="bp3")
    assert state.stop == pytest.approx(91.2)
    # a later Basing Point with a much lower low would set 76; the stop stays at 91.2
    later = _machine(
        running_high=104.0,
        running_high_session="d017",
        candidate_low=80.0,
        candidate_high=85.0,
        candidate_session="d020",
        last_bp_session="d017",
        last_bp_low=96.0,
        stop=91.2,
        peak_used=True,
    )
    for session, bar in (
        ("d021", Bar(high=90.0, low=86.0, close=88.0)),
        ("d022", Bar(high=91.0, low=87.0, close=89.0)),
        ("d023", Bar(high=92.0, low=88.0, close=90.0)),
    ):
        later, _ = advance(later, session, bar, distance=0.05, rule="bp3")
    assert later.stop == pytest.approx(91.2)


def test_rule_four_fires_once_per_peak_and_only_under_bp4() -> None:
    state = _machine(
        running_high=100.0,
        running_high_session="d011",
        peak_high=100.0,
        peak_recognized=True,
        peak_used=False,
        stop=90.0,
    )
    # d012 makes a new high but its close (102) is below 1.03 x the peak (103): no fire
    state, events = advance(
        state, "d012", Bar(high=103.0, low=95.0, close=102.0), distance=0.05, rule="bp4"
    )
    assert events == []
    assert state.running_high == 103.0 and state.peak_high == 100.0
    # d013 makes a new high whose close (104) is above 1.03 x the recognized peak: fire
    state, events = advance(
        state, "d013", Bar(high=105.0, low=96.0, close=104.0), distance=0.05, rule="bp4"
    )
    assert [event.kind for event in events] == ["bp4"]
    assert state.peak_used is True
    # d014 also makes a new high above 1.03 x the old peak; the same peak cannot fire twice
    state, events = advance(
        state, "d014", Bar(high=106.0, low=97.0, close=105.0), distance=0.05, rule="bp4"
    )
    assert events == []
    # d015 does not make a new high, so the 106 peak is recognized (unused); d016 fires
    state, events = advance(
        state, "d015", Bar(high=106.0, low=98.0, close=102.0), distance=0.05, rule="bp4"
    )
    assert state.peak_high == 106.0 and state.peak_used is False and events == []
    state, events = advance(
        state, "d016", Bar(high=110.0, low=99.0, close=109.5), distance=0.05, rule="bp4"
    )
    assert [event.kind for event in events] == ["bp4"]
    # bp3 never runs rule 4 on the same bars
    bp3 = _machine(
        running_high=100.0,
        running_high_session="d011",
        peak_high=100.0,
        peak_recognized=True,
        peak_used=False,
        stop=90.0,
    )
    for session, bar in (
        ("d012", Bar(high=103.0, low=95.0, close=102.0)),
        ("d013", Bar(high=105.0, low=96.0, close=104.0)),
    ):
        bp3, events = advance(bp3, session, bar, distance=0.05, rule="bp3")
        assert not any(event.kind == "bp4" for event in events)


def test_a_new_high_below_the_rule_four_threshold_does_not_fire() -> None:
    state = _machine(
        running_high=100.0,
        running_high_session="d011",
        peak_high=100.0,
        peak_recognized=True,
        peak_used=False,
        stop=90.0,
    )
    state, events = advance(
        state, "d012", Bar(high=103.0, low=95.0, close=102.0), distance=0.05, rule="bp4"
    )
    assert events == []
    assert state.running_high == 103.0
    assert state.peak_used is False


def _with_basing_point() -> list[Bar]:
    history = [Bar(high=100.0, low=95.0, close=97.5) for _ in range(56)]
    history += [
        Bar(high=99.0, low=90.0, close=92.0),  # candidate low 90
        Bar(high=101.0, low=100.0, close=100.5),  # away 1 (low > 99), new high
        Bar(high=102.0, low=101.0, close=101.5),  # away 2
        Bar(high=103.0, low=102.0, close=102.5),  # away 3 -> BP at low 90
        Bar(high=104.0, low=100.0, close=102.0),
        Bar(high=104.0, low=100.0, close=102.0),
        Bar(high=104.0, low=100.0, close=102.0),
        Bar(high=104.0, low=100.0, close=102.0),
    ]
    return history


def test_the_initial_stop_and_both_fallbacks() -> None:
    history = _with_basing_point()
    sessions = [f"e{index:03d}" for index in range(len(history))]
    state, events = establish(
        sessions, history, entry_session=sessions[-1], distance=0.10, rule="bp3"
    )
    assert state.fallback is False
    assert state.stop == pytest.approx(90.0 * 0.90)
    assert [event.kind for event in events] == ["entry"]
    # no Basing Point: the fallback is the lowest low of the last 21 sessions
    flat = [Bar(high=100.0, low=95.0, close=97.5) for _ in range(64)]
    state, events = establish(
        [f"f{index:03d}" for index in range(64)],
        flat,
        entry_session="f063",
        distance=0.10,
        rule="bp3",
    )
    assert state.fallback is True
    assert state.stop == pytest.approx(95.0 * 0.90)
    assert [event.kind for event in events] == ["fallback", "entry"]
    # a Basing Point whose level is at or above the entry close also falls back
    tail = [Bar(high=100.0, low=95.0, close=97.5) for _ in range(56)]
    tail += [
        Bar(high=99.0, low=90.0, close=92.0),
        Bar(high=101.0, low=100.0, close=100.5),
        Bar(high=102.0, low=101.0, close=101.5),
        Bar(high=103.0, low=102.0, close=102.5),  # BP at low 90, stop 85.5
        Bar(high=89.0, low=80.0, close=82.0),
        Bar(high=85.0, low=79.0, close=80.0),
        Bar(high=84.0, low=78.0, close=79.0),
        Bar(high=83.0, low=77.0, close=78.0),  # entry close 78 < 85.5
    ]
    state, _ = establish(
        [f"g{index:03d}" for index in range(len(tail))],
        tail,
        entry_session=f"g{len(tail) - 1:03d}",
        distance=0.05,
        rule="bp3",
    )
    assert state.fallback is True
    assert state.stop == pytest.approx(77.0 * 0.95)


def _bp_params() -> GuardParams:
    return GuardParams(
        layers=frozenset({1}),
        J=2,
        gamma=0.1,
        delta=0.0,
        sigma_star=0.1,
        m=1.0,
        alpha=0.5,
        vol_window=2,
        stop_rule="bp3",
    )


def test_exit_out_until_the_next_decision_and_fresh_machine_on_reentry() -> None:
    params = _bp_params()
    history = [Bar(high=100.0, low=95.0, close=97.5) for _ in range(64)]
    entry = GuardInput(
        date="d063",
        reference_weights={1: 0.5},
        asset_returns={1: [0.0, 0.0]},
        reference_returns=[0.0, 0.0],
        nav=1.0,
        bars={1: history},
        stop_distances={1: 0.05},
        decision_date=True,
    )
    out = evaluate(initial_state(), entry, params)
    assert out.l1_out == frozenset() and out.weights[1] == pytest.approx(0.5)
    crash = Bar(high=81.0, low=79.0, close=80.0)
    out = evaluate(
        out.state,
        GuardInput(
            date="d064",
            reference_weights={1: 0.5},
            asset_returns={1: [0.0, -0.2]},
            reference_returns=[0.0, -0.2],
            nav=0.9,
            bars={1: [*history, crash]},
            stop_distances={1: 0.05},
            decision_date=False,
        ),
        params,
    )
    assert out.l1_out == frozenset({1}) and 1 not in out.weights
    good = Bar(high=100.0, low=95.0, close=98.0)
    out = evaluate(
        out.state,
        GuardInput(
            date="d065",
            reference_weights={1: 0.5},
            asset_returns={1: [0.0, 0.1]},
            reference_returns=[0.0, 0.1],
            nav=0.9,
            bars={1: [*history, crash, good]},
            stop_distances={1: 0.05},
            decision_date=False,
        ),
        params,
    )
    assert out.l1_out == frozenset({1})  # a non-decision date does not re-enter
    out = evaluate(
        out.state,
        GuardInput(
            date="d066",
            reference_weights={1: 0.5},
            asset_returns={1: [0.1, 0.0]},
            reference_returns=[0.1, 0.0],
            nav=0.9,
            bars={1: [*history, crash, good, good]},
            stop_distances={1: 0.05},
            decision_date=True,
        ),
        params,
    )
    assert out.l1_out == frozenset()
    assert out.weights[1] == pytest.approx(0.5)
    assert [event.kind for event in out.bp_events] == ["fallback", "entry"]


def test_a_missing_bar_neither_updates_nor_triggers() -> None:
    params = _bp_params()
    history = [Bar(high=100.0, low=95.0, close=97.5) for _ in range(64)]
    entry = GuardInput(
        date="d063",
        reference_weights={1: 0.5},
        asset_returns={1: [0.0, 0.0]},
        reference_returns=[0.0, 0.0],
        nav=1.0,
        bars={1: history},
        stop_distances={1: 0.05},
        decision_date=True,
    )
    out = evaluate(initial_state(), entry, params)
    machine = dict(out.state.bp)[1]
    out = evaluate(
        out.state,
        GuardInput(
            date="d064",
            reference_weights={1: 0.5},
            asset_returns={1: [0.0, 0.0]},
            reference_returns=[0.0, 0.0],
            nav=1.0,
            bars={1: [None]},
            stop_distances={1: 0.05},
            decision_date=False,
        ),
        params,
    )
    assert out.bp_missing == 1
    assert dict(out.state.bp)[1] == machine
    assert out.l1_out == frozenset()


def test_the_stop_distance_table_boundaries() -> None:
    assert price_class(100.01) == ">100"
    assert price_class(100.0) == "40-100"
    assert price_class(40.0) == "40-100"
    assert price_class(20.0) == "20-<40"
    assert price_class(10.0) == "10-<20"
    assert price_class(5.0) == "5-<10"
    assert price_class(4.99) == "<5"
    assert sensitivity_class(0.749) == "conservative"
    assert sensitivity_class(0.75) == "median"
    assert sensitivity_class(1.25) == "median"
    assert sensitivity_class(1.251) == "speculative"
    assert stop_distance(100.0, 0.75) == 0.05
    assert stop_distance(50.0, 1.30) == 0.06
    assert stop_distance(25.0, 0.50) == 0.05
    assert stop_distance(15.0, 1.00) == 0.06
    assert stop_distance(7.0, 1.30) == 0.12
    assert stop_distance(4.0, 1.00) == 0.10
    assert distance_cell(100.0, 0.75) == "40-100|median"


def test_the_raw_price_rebuild_across_a_split() -> None:
    splits = [("2020-01-01", 4.0), ("2020-06-01", 0.5)]
    assert unadjusted_price(100.0, splits, "2019-12-31") == pytest.approx(200.0)
    assert unadjusted_price(100.0, splits, "2020-03-01") == pytest.approx(50.0)
    assert unadjusted_price(100.0, splits, "2020-07-01") == pytest.approx(100.0)


def test_the_v4_defaults_leave_the_kl_and_minvar_paths_unchanged() -> None:
    params = driver.load_params("src/cycle/backtest/params.frozen.toml")
    specs = driver._arm_specs(
        [
            "MARKOWITZ_BASE",
            "TOPN_MV_NOGUARD",
            "TOPN_MV_GUARD_NO_NLP",
            "MARKOWITZ_BASE_GUARD",
        ],
        params,
        "TOPN_MV_GUARD_NO_NLP",
    )
    assert all(spec.guard is None or spec.guard.stop_rule == "kl" for spec in specs)
    assert all(spec.shadow_guard is None or spec.shadow_guard.stop_rule == "kl" for spec in specs)
