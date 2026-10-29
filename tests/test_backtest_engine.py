"""The pure daily engine (BACKTEST_SPEC §3/§4): hand-computed multi-session cases.

The GUARD semantics come from ``cycle.guard`` (WP-D)."""

from __future__ import annotations

import copy

import pytest

from cycle.backtest.engine import (
    ARM_MARKOWITZ_BASE,
    ARM_TOPN_GUARD,
    ARM_TOPN_NOGUARD,
    ArmSpec,
    EngineError,
    EngineInput,
    simulate,
)
from cycle.guard import GuardParams, NewsCounts, NewsParams

D0 = "2022-01-05"
FORMATION = "2022-01-06"
T1 = "2022-01-07"
T2 = "2022-01-10"
T3 = "2022-01-11"
T4 = "2022-01-12"


def _flat_target(*assets: int, weight: float = 1.0) -> dict[int, float]:
    return {a: weight / len(assets) for a in assets}


def _params(**overrides: object) -> GuardParams:
    base: dict[str, object] = {
        "layers": frozenset({1}),
        "J": 2,
        "gamma": 0.05,
        "delta": 0.02,
        "sigma_star": 0.15,
        "m": 2.0,
        "alpha": 0.9,
        "vol_window": 2,
    }
    base.update(overrides)
    return GuardParams(**base)  # type: ignore[arg-type]


def _input(  # noqa: PLR0913 - one builder for the engine's inputs
    *,
    sessions: list[str],
    returns: dict[str, dict[int, float]],
    targets: dict[str, dict[str, dict[int, float]]],
    arms: list[ArmSpec],
    decisions: list[str],
    rf: dict[str, float] | None = None,
    members: dict[str, list[int]] | None = None,
    news: dict[str, dict[int, NewsCounts]] | None = None,
    formation: str = FORMATION,
) -> EngineInput:
    return EngineInput(
        sessions=sessions,
        returns=returns,
        rf=rf or {},
        targets=targets,
        arms=arms,
        formation_date=formation,
        decision_dates=decisions,
        benchmark_members=members or {},
        news=news or {},
    )


def _base_arms(guard: GuardParams | None = None) -> list[ArmSpec]:
    return [
        ArmSpec(ARM_TOPN_NOGUARD, ARM_TOPN_NOGUARD),
        ArmSpec(ARM_TOPN_GUARD, ARM_TOPN_NOGUARD, guard=guard or _params()),
    ]


# -- drift, execution timing and cost ------------------------------------------------


def test_d_plus_one_timing_and_one_cost_per_net_order() -> None:
    sessions = [D0, FORMATION, T1, T2]
    returns = {
        D0: {1: 0.0, 2: 0.0},
        FORMATION: {1: 0.0, 2: 0.0},
        T1: {1: 0.10, 2: 0.0},
        T2: {1: 0.10, 2: 0.0},
    }
    targets = {
        ARM_MARKOWITZ_BASE: {
            D0: {1: 0.5, 2: 0.5},
            T1: {1: 0.0, 2: 1.0},
        }
    }
    inp = _input(
        sessions=sessions,
        returns=returns,
        targets=targets,
        arms=[ArmSpec(ARM_MARKOWITZ_BASE, ARM_MARKOWITZ_BASE)],
        decisions=[D0, T1],
    )
    result = simulate(inp)
    arm = result.arms[ARM_MARKOWITZ_BASE]

    # Formation: the whole book is bought at the close, 10 bps of turnover.
    formation = arm.weights[0]
    assert formation.date == FORMATION
    assert formation.final_target == pytest.approx({1: 0.5, 2: 0.5})
    assert arm.executions[0].date == FORMATION
    assert arm.executions[0].turnover == pytest.approx(1.0)
    assert arm.executions[0].cost_return == pytest.approx(0.001)
    assert arm.performance[0].nav == pytest.approx(0.999)

    # T1's +10 % on asset 1 is earned by the 50/50 book; the new target waits for T2's close.
    # (weights[0] is the formation close, [1] is T1, [2] is T2.)
    t1 = arm.weights[1]
    assert t1.drifted == pytest.approx({1: 0.5 * 1.10 / 1.05, 2: 0.5 / 1.05})
    assert t1.final_target == pytest.approx({2: 1.0})  # zeros are omitted
    assert [e.date for e in arm.executions] == [FORMATION, T2]

    # T2's return is still the drifted book's, and the rebalance is one net order.
    t2 = arm.performance[2]
    gross = 0.5 * 1.10 / 1.05 * 0.10
    drifted_1 = 0.5 * 1.10 / 1.05 * 1.10 / (1.0 + gross)
    turnover = 2.0 * drifted_1  # 0 -> drifted_1 on asset 1, drifted_1 -> 1 on asset 2
    assert t2.gross_return == pytest.approx(gross)
    assert arm.executions[1].turnover == pytest.approx(turnover)
    assert t2.cost_return == pytest.approx(0.001 * turnover)
    assert t2.net_return == pytest.approx((1 + gross) * (1 - 0.001 * turnover) - 1)
    assert arm.weights[2].final_target == pytest.approx({2: 1.0})


def test_cash_earns_the_session_risk_free_rate() -> None:
    sessions = [D0, FORMATION, T1]
    returns = {D0: {1: 0.0}, FORMATION: {1: 0.0}, T1: {1: 0.02}}
    targets = {ARM_MARKOWITZ_BASE: {D0: {1: 0.4}}}
    rf = {FORMATION: 0.0, T1: 0.001}
    inp = _input(
        sessions=sessions,
        returns=returns,
        targets=targets,
        arms=[ArmSpec(ARM_MARKOWITZ_BASE, ARM_MARKOWITZ_BASE)],
        decisions=[D0],
        rf=rf,
    )
    arm = simulate(inp).arms[ARM_MARKOWITZ_BASE]
    # formation cost = 10 bps on the 0.4 bought; T1 gross = 0.4*2 % + 0.6*0.1 %.
    assert arm.performance[1].gross_return == pytest.approx(0.4 * 0.02 + 0.6 * 0.001)
    assert arm.weights[1].cash_weight == pytest.approx(0.6 * 1.001 / 1.0086)
    assert arm.weights[1].n_equity == 1


def test_a_held_asset_without_a_return_is_valued_at_its_last_close_then_cashed() -> None:
    sessions = [D0, FORMATION, T1, T2]
    returns = {D0: {1: 0.0}, FORMATION: {1: 0.0}, T1: {}, T2: {1: 0.0}}
    targets = {ARM_MARKOWITZ_BASE: {D0: {1: 1.0}}}
    rf = {T1: 0.0, T2: 0.001}
    inp = _input(
        sessions=sessions,
        returns=returns,
        targets=targets,
        arms=[ArmSpec(ARM_MARKOWITZ_BASE, ARM_MARKOWITZ_BASE)],
        decisions=[D0],
        rf=rf,
    )
    arm = simulate(inp).arms[ARM_MARKOWITZ_BASE]
    assert arm.performance[1].gross_return == pytest.approx(0.0)
    # the missing name is liquidated at its last close: one sale of the whole book
    assert arm.executions[-1].date == T1
    assert arm.executions[-1].turnover == pytest.approx(1.0)
    assert arm.weights[1].final_target == {}
    assert arm.weights[1].cash_weight == pytest.approx(1.0)
    assert arm.performance[2].gross_return == pytest.approx(0.001)  # cash now earns RF


def test_formation_is_the_first_simulated_session_and_earns_no_return() -> None:
    sessions = [D0, FORMATION, T1]
    returns = {D0: {1: 0.5}, FORMATION: {1: 0.5}, T1: {1: 0.0}}
    targets = {ARM_MARKOWITZ_BASE: {D0: {1: 1.0}}}
    inp = _input(
        sessions=sessions,
        returns=returns,
        targets=targets,
        arms=[ArmSpec(ARM_MARKOWITZ_BASE, ARM_MARKOWITZ_BASE)],
        decisions=[D0],
    )
    arm = simulate(inp).arms[ARM_MARKOWITZ_BASE]
    assert [p.date for p in arm.performance] == [FORMATION, T1]
    assert arm.performance[0].gross_return == 0.0
    assert arm.performance[0].nav == pytest.approx(0.999)  # only the opening cost


def test_inputs_are_never_mutated() -> None:
    sessions = [D0, FORMATION, T1]
    returns = {D0: {1: 0.0}, FORMATION: {1: 0.0}, T1: {1: 0.1}}
    targets = {ARM_TOPN_NOGUARD: {D0: {1: 1.0}}, ARM_TOPN_GUARD: {D0: {1: 1.0}}}
    arms = _base_arms()
    inp = _input(
        sessions=sessions,
        returns=returns,
        targets=targets,
        arms=arms,
        decisions=[D0],
    )
    before = (copy.deepcopy(returns), copy.deepcopy(targets), copy.deepcopy(sessions))
    simulate(inp)
    assert (returns, targets, sessions) == before


# -- GUARD: overlay, stop / re-entry, disabled alerts, minimum-not-product -----------


def test_a_guard_arm_that_never_fires_is_identical_to_its_noguard_twin() -> None:
    sessions = [D0, FORMATION, T1, T2]
    returns = {
        D0: {1: 0.0, 2: 0.0},
        FORMATION: {1: 0.0, 2: 0.0},
        T1: {1: 0.01, 2: -0.01},
        T2: {1: -0.02, 2: 0.02},
    }
    targets = {ARM_TOPN_NOGUARD: {D0: {1: 0.5, 2: 0.5}}}
    inp = _input(
        sessions=sessions,
        returns=returns,
        targets=targets,
        arms=_base_arms(_params(layers=frozenset({3, 4}), sigma_star=10.0, m=10.0, alpha=0.1)),
        decisions=[D0],
    )
    result = simulate(inp)
    noguard = result.arms[ARM_TOPN_NOGUARD]
    guard = result.arms[ARM_TOPN_GUARD]
    assert [p.nav for p in noguard.performance] == pytest.approx([p.nav for p in guard.performance])
    assert [e.date for e in noguard.executions] == [e.date for e in guard.executions] == [FORMATION]
    assert guard.guard_events == ()


def test_layer1_stop_cuts_to_cash_and_reentry_returns_to_the_reference_weight() -> None:
    sessions = [D0, FORMATION, T1, T2, T3, T4]
    returns = {
        D0: {1: 0.0},
        FORMATION: {1: -0.03},
        T1: {1: -0.03},  # sum of the last J=2 = -6 % < -gamma
        T2: {1: -0.20},
        T3: {1: 0.10},  # one-session return >= delta: re-entry
        T4: {1: 0.0},
    }
    targets = {ARM_TOPN_NOGUARD: {D0: {1: 1.0}}}
    inp = _input(
        sessions=sessions,
        returns=returns,
        targets=targets,
        arms=_base_arms(_params(layers=frozenset({1}), J=2, gamma=0.05, delta=0.02)),
        decisions=[D0],
    )
    result = simulate(inp)
    noguard = result.arms[ARM_TOPN_NOGUARD]
    guard = result.arms[ARM_TOPN_GUARD]

    by_date = {w.date: w for w in guard.weights}
    assert by_date[T1].final_target == {}  # stop raised at T1's close
    assert by_date[T1].pre_guard == pytest.approx({1: 1.0})
    assert [e.date for e in guard.executions] == [FORMATION, T2, T4]
    # T2: the exit is executed at its close; the -20 % is still borne by the old weight.
    # performance[0] is the formation close, [1] T1, [2] T2, [3] T3, [4] T4.
    assert guard.performance[2].gross_return == pytest.approx(-0.20)
    assert by_date[T2].cash_weight == pytest.approx(1.0)
    # T3: in cash, misses the +10 %; the re-entry alert is raised at its close.
    assert guard.performance[3].gross_return == pytest.approx(0.0)
    assert by_date[T3].final_target == pytest.approx({1: 1.0})
    # T4: back in the reference weight (it re-enters at T4's close).
    assert noguard.weights[-1].final_target == pytest.approx({1: 1.0})
    statuses = [(e.date, e.alert.rule_id, e.status) for e in guard.guard_events]
    assert statuses == [
        (T1, "L1_STOP_LOSS", "APPLIED"),
        (T3, "L1_REENTRY", "APPLIED"),
    ]
    assert by_date[T4].final_target == pytest.approx({1: 1.0})


def test_noguard_records_the_alerts_it_would_have_raised_as_arm_disabled() -> None:
    sessions = [D0, FORMATION, T1, T2]
    returns = {D0: {1: 0.0}, FORMATION: {1: -0.03}, T1: {1: -0.03}, T2: {1: 0.0}}
    targets = {ARM_TOPN_NOGUARD: {D0: {1: 1.0}}}
    inp = _input(
        sessions=sessions,
        returns=returns,
        targets=targets,
        arms=[ArmSpec(ARM_TOPN_NOGUARD, ARM_TOPN_NOGUARD, shadow_guard=_params())],
        decisions=[D0],
    )
    arm = simulate(inp).arms[ARM_TOPN_NOGUARD]
    assert [(e.date, e.alert.rule_id, e.status) for e in arm.guard_events] == [
        (T1, "L1_STOP_LOSS", "ARM_DISABLED")
    ]
    # never applied: the book still holds the asset into T2
    assert arm.weights[-1].final_target == pytest.approx({1: 1.0})
    assert all(e.weight_before == e.weight_after for e in arm.guard_events)


def test_scale_is_the_minimum_not_the_product() -> None:
    sessions = [D0, FORMATION, T1, T2]
    returns = {
        D0: {1: 0.0},
        FORMATION: {1: 0.0},
        T1: {1: -0.10},
        T2: {1: 0.0},
    }
    targets = {ARM_TOPN_NOGUARD: {D0: {1: 1.0}}}
    # s_sigma = 0.5 from sd([0, -0.10]) * sqrt(1) with sigma_star = 0.5 * sd;
    # s_dd = 1 * (0.9 - 0.45) / 0.9 = 0.5. minimum = 0.5, product would be 0.25.
    guard = _params(
        layers=frozenset({3, 4}),
        vol_window=2,
        periods_per_year=1,
        sigma_star=0.5 * 0.10 / (2**0.5),
        # m*(1-alpha) = 1 at the peak (no scale at formation), and a 10 % loss gives
        # m*(0.9-alpha)/0.9 = 0.5 at T1.
        m=5.5,
        alpha=9 / 11,
    )
    inp = _input(
        sessions=sessions,
        returns=returns,
        targets=targets,
        arms=_base_arms(guard),
        decisions=[D0],
    )
    arm = simulate(inp).arms[ARM_TOPN_GUARD]
    at_t1 = {w.date: w for w in arm.weights}[T1]
    scale_alerts = [e for e in arm.guard_events if e.alert.layer in (3, 4) and e.date == T1]
    assert {e.alert.rule_id for e in scale_alerts} == {"L3_VOL", "L4_DD"}
    assert all(e.status == "APPLIED" for e in scale_alerts)
    # the minimum (not the product: 0.5 x 0.5 = 0.25) sets the remaining equity weight
    s_sigma = next(e.alert.value for e in scale_alerts if e.alert.layer == 3)
    s_dd = next(e.alert.value for e in scale_alerts if e.alert.layer == 4)
    assert s_sigma == pytest.approx(0.5)
    # The drawdown is measured from NAV 1.0 (the entry cost included), so s_dd is just under
    # 0.5; the point is that the scale is the minimum, never 0.5 x 0.495 = 0.247.
    assert 0.45 < s_dd < 0.55
    assert at_t1.final_target == pytest.approx({1: min(s_sigma, s_dd)})
    assert at_t1.cash_weight == pytest.approx(1 - min(s_sigma, s_dd))


def test_a_scale_and_a_rebalance_on_the_same_session_form_one_order() -> None:
    sessions = [D0, FORMATION, T1, T2]
    returns = {D0: {1: 0.0}, FORMATION: {1: 0.0}, T1: {1: -0.10}, T2: {1: 0.0}}
    targets = {
        ARM_TOPN_NOGUARD: {D0: {1: 1.0}, T1: {1: 1.0}},
        ARM_TOPN_GUARD: {D0: {1: 1.0}, T1: {1: 1.0}},
    }
    guard = _params(layers=frozenset({3}), vol_window=2, periods_per_year=1, sigma_star=0.02)
    inp = _input(
        sessions=sessions,
        returns=returns,
        targets=targets,
        arms=_base_arms(guard),
        decisions=[D0, T1],
    )
    arm = simulate(inp).arms[ARM_TOPN_GUARD]
    # the scale is formed at T1's close together with the rebalance: one order at T2's close.
    assert [e.date for e in arm.executions] == [FORMATION, T2]
    # scale = sigma_star / sd([0, -0.10]) = 0.02 / 0.07071 = 0.28284
    scale = 0.02 / (0.10 / (2**0.5))
    assert {w.date: w for w in arm.weights}[T1].final_target == pytest.approx({1: scale})
    expected_turnover = abs(scale - 1.0)
    assert arm.executions[1].turnover == pytest.approx(expected_turnover)


def test_news_layer_trims_and_exits_with_a_cooldown() -> None:
    sessions = [D0, FORMATION, T1, T2, T3]
    returns = {s: {1: 0.0} for s in sessions}
    targets = {ARM_TOPN_NOGUARD: {D0: {1: 1.0}}}
    news_params = NewsParams(k=1, tau=0.5, tau_exit=0.4, n_min=2, cooldown=2)
    guard = _params(layers=frozenset({2}), news=news_params)
    news = {
        FORMATION: {},
        T1: {1: NewsCounts(2, 2)},  # q = 1 > 0.5: trim
        T2: {1: NewsCounts(2, 0)},  # q = 0 <= 0.4: exit, cooldown starts
        T3: {1: NewsCounts(2, 0)},  # still in cooldown: trimmed, a state alert only
    }
    inp = _input(
        sessions=sessions,
        returns=returns,
        targets=targets,
        arms=_base_arms(guard),
        decisions=[D0],
        news=news,
    )
    arm = simulate(inp).arms[ARM_TOPN_GUARD]
    by_date = {w.date: w for w in arm.weights}
    assert by_date[T1].final_target == {}
    assert by_date[T2].final_target == {}
    assert by_date[T3].final_target == {}
    assert [(e.date, e.alert.rule_id, e.alert.state, e.status) for e in arm.guard_events] == [
        (T1, "L2_NEWS", "ENTER", "APPLIED"),
        (T2, "L2_NEWS_EXIT", "EXIT", "APPLIED"),
        (T3, "L2_NEWS", "COOLDOWN", "APPLIED"),
    ]


# -- validation -----------------------------------------------------------------------


def test_an_invalid_target_is_refused() -> None:
    sessions = [D0, FORMATION]
    returns = {D0: {1: 0.0}, FORMATION: {1: 0.0}}
    targets = {ARM_MARKOWITZ_BASE: {D0: {1: 1.5}}}
    inp = _input(
        sessions=sessions,
        returns=returns,
        targets=targets,
        arms=[ArmSpec(ARM_MARKOWITZ_BASE, ARM_MARKOWITZ_BASE)],
        decisions=[D0],
    )
    with pytest.raises(EngineError, match="sum"):
        simulate(inp)


def test_formation_must_be_a_simulated_session() -> None:
    inp = _input(
        sessions=[D0],
        returns={D0: {1: 0.0}},
        targets={ARM_MARKOWITZ_BASE: {D0: {1: 1.0}}},
        arms=[ArmSpec(ARM_MARKOWITZ_BASE, ARM_MARKOWITZ_BASE)],
        decisions=[D0],
        formation="2022-01-09",
    )
    with pytest.raises(EngineError, match="formation date"):
        simulate(inp)


def test_the_guard_evaluates_the_formation_target_at_the_decision_close() -> None:
    """A stop already visible at the formation decision cuts the formation order itself: the
    GUARD book does not spend its first session holding a name it would have stopped."""
    sessions = [D0, FORMATION, T1]
    returns = {D0: {1: -0.10}, FORMATION: {1: 0.0}, T1: {1: 0.0}}
    targets = {ARM_TOPN_NOGUARD: {D0: {1: 1.0}}}
    inp = _input(
        sessions=sessions,
        returns=returns,
        targets=targets,
        arms=_base_arms(_params(layers=frozenset({1}), J=1, gamma=0.05, delta=0.03)),
        decisions=[D0],
    )
    result = simulate(inp)
    guard = result.arms[ARM_TOPN_GUARD]
    noguard = result.arms[ARM_TOPN_NOGUARD]
    assert guard.weights[0].date == FORMATION
    assert guard.weights[0].final_target == {}  # the formation order was already cut
    assert noguard.weights[0].final_target == pytest.approx({1: 1.0})
    first = next(iter(guard.guard_events))
    assert (first.date, first.alert.rule_id, first.status) == (
        D0,
        "L1_STOP_LOSS",
        "APPLIED",
    )
    assert guard.performance[1].gross_return == pytest.approx(0.0)
    assert noguard.performance[1].gross_return == pytest.approx(0.0)
