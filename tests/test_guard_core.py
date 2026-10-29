"""WP-D: ``cycle.guard`` -- the pure GUARD core (BACKTEST_SPEC §4, interface §8.1).

Every case here is hand-computed from the formulas in the module docstring: the values
are chosen as exact binary fractions where a boundary matters (``S == -gamma``,
``r == delta``) so the assertions do not depend on decimal rounding. No DB, no I/O.
"""

from __future__ import annotations

import copy
import inspect
import math
import random
from collections.abc import Callable, Iterable
from dataclasses import FrozenInstanceError, fields, is_dataclass
from typing import Any, cast

import pytest

from cycle.guard import (
    Alert,
    GuardInput,
    GuardOutput,
    GuardParams,
    GuardState,
    NewsCounts,
    NewsParams,
    evaluate,
    initial_state,
)


def _news(**overrides: Any) -> NewsParams:
    base: dict[str, Any] = {
        "k": 3,
        "tau": 0.5,
        "tau_exit": 0.25,
        "n_min": 2,
        "cooldown": 2,
        "trim_fraction": 1.0,
    }
    base.update(overrides)
    return NewsParams(**base)


def _params(layers: Iterable[int] = (1, 2, 3, 4), **overrides: Any) -> GuardParams:
    """A valid baseline parameter set (``m(1 - alpha) > 1``: full exposure at a new high)."""
    base: dict[str, Any] = {
        "layers": frozenset(layers),
        "J": 3,
        "gamma": 0.375,
        "delta": 0.02,
        "sigma_star": 0.10,
        "m": 2.0,
        "alpha": 0.4,
        "vol_window": 3,
        "periods_per_year": 252,
        "news": _news(),
    }
    base.update(overrides)
    return GuardParams(**base)


def _input(
    *,
    weights: dict[int, float] | None = None,
    returns: dict[int, list[float]] | None = None,
    reference_returns: list[float] | None = None,
    nav: float = 100.0,
    news: dict[int, NewsCounts] | None = None,
) -> GuardInput:
    return GuardInput(
        date="2022-03-03",
        reference_weights={1: 0.5, 2: 0.5} if weights is None else weights,
        asset_returns={} if returns is None else returns,
        reference_returns=[] if reference_returns is None else reference_returns,
        nav=nav,
        news=news,
    )


def _fresh_high_water(nav: float = 100.0) -> GuardState:
    """A state whose layer-4 running maximum is ``nav`` (a first call of an empty arm)."""
    return evaluate(initial_state(), _input(nav=nav), _params(layers=(4,))).state


# -- the fixed interface ------------------------------------------------------------------------


def test_interface_matches_spec_8_1() -> None:
    assert [field.name for field in fields(NewsParams)] == [
        "k",
        "tau",
        "tau_exit",
        "n_min",
        "cooldown",
        "trim_fraction",
    ]
    assert [field.name for field in fields(GuardParams)] == [
        "layers",
        "J",
        "gamma",
        "delta",
        "sigma_star",
        "m",
        "alpha",
        "vol_window",
        "periods_per_year",
        "news",
    ]
    assert [field.name for field in fields(NewsCounts)] == ["n_articles", "n_negative"]
    assert [field.name for field in fields(GuardInput)] == [
        "date",
        "reference_weights",
        "asset_returns",
        "reference_returns",
        "nav",
        "news",
    ]
    assert [field.name for field in fields(Alert)] == [
        "layer",
        "asset_id",
        "rule_id",
        "value",
        "threshold",
        "state",
    ]
    assert [field.name for field in fields(GuardOutput)] == [
        "state",
        "weights",
        "cut_assets",
        "scale",
        "s_sigma",
        "s_dd",
        "alerts",
    ]
    assert [field.name for field in fields(GuardState)] == ["l1_out", "l2", "nav_max"]
    assert list(inspect.signature(evaluate).parameters) == ["state", "inp", "params"]
    assert initial_state() == GuardState(l1_out=frozenset(), l2=(), nav_max=None)
    # The defaults and parameter order the spec fixes.
    minimal = GuardParams(
        layers=frozenset(), J=1, gamma=0.1, delta=0.0, sigma_star=0.1, m=1.0, alpha=0.5
    )
    assert (minimal.vol_window, minimal.periods_per_year, minimal.news) == (21, 252, None)
    assert _news().trim_fraction == 1.0
    instances: list[object] = [
        minimal,
        _news(),
        NewsCounts(n_articles=0, n_negative=0),
        _input(),
        Alert(layer=1, asset_id=None, rule_id="X", value=0.0, threshold=0.0, state="ENTER"),
        GuardState(),
        GuardOutput(
            state=GuardState(),
            weights={},
            cut_assets=frozenset(),
            scale=1.0,
            s_sigma=None,
            s_dd=None,
            alerts=(),
        ),
    ]
    assert all(is_dataclass(instance) for instance in instances)
    probe = "probe"
    for instance in instances:
        with pytest.raises(FrozenInstanceError):
            setattr(instance, probe, 1)


# -- layer 1: Kaminski & Lo stop-loss / re-entry ------------------------------------------------


def test_layer1_stop_loss_on_the_last_j_returns_strict_boundary() -> None:
    # J=3, gamma=0.375. Exact binary fractions: S=-0.4375 stops, S=-0.375 (== -gamma) does not.
    inp = _input(
        returns={
            1: [-0.125, -0.125, -0.1875],
            2: [-0.125, -0.125, -0.125],
            3: [0.01, -0.125],  # only two returns: shorter than J
        }
    )
    out = evaluate(initial_state(), inp, _params(layers=(1,)))

    assert out.weights == {2: 0.5}
    assert out.cut_assets == frozenset({1})
    assert out.scale == 1.0
    assert out.s_sigma is None and out.s_dd is None
    assert len(out.alerts) == 1
    alert = out.alerts[0]
    assert (alert.layer, alert.asset_id, alert.rule_id, alert.state) == (
        1,
        1,
        "L1_STOP_LOSS",
        "ENTER",
    )
    assert alert.value == pytest.approx(-0.4375)
    assert alert.threshold == -0.375
    assert out.state.l1_out == frozenset({1})
    # No alert for the boundary name (asset 2) nor for the short-history one (asset 3).
    assert [a.asset_id for a in out.alerts] == [1]


def test_layer1_uses_only_the_last_j_returns() -> None:
    # The first two returns are irrelevant: the window is the last J=3.
    inp = _input(returns={1: [0.5, 0.5, -0.125, -0.125, -0.1875], 2: [0.0, 0.0, 0.0]})
    out = evaluate(initial_state(), inp, _params(layers=(1,)))
    assert out.cut_assets == frozenset({1})
    assert out.alerts[0].value == pytest.approx(-0.4375)


def test_layer1_reentry_on_the_assets_own_return_at_the_boundary() -> None:
    params = _params(layers=(1,), delta=0.02)
    stopped = evaluate(
        initial_state(),
        _input(returns={1: [-0.125, -0.125, -0.1875], 2: [0.0, 0.0, 0.0]}),
        params,
    )
    assert stopped.state.l1_out == frozenset({1})

    # The asset's own latest return is exactly delta -> back in, at its reference weight.
    reentered = evaluate(
        stopped.state,
        _input(
            returns={1: [-0.125, -0.1875, 0.02], 2: [0.0, 0.0, 0.0]},
            reference_returns=[0.5, 0.5, 0.5],  # the book's return is irrelevant to layer 1
        ),
        params,
    )
    assert reentered.state.l1_out == frozenset()
    assert reentered.weights == {1: 0.5, 2: 0.5}
    assert reentered.cut_assets == frozenset()
    assert len(reentered.alerts) == 1
    alert = reentered.alerts[0]
    assert (alert.layer, alert.asset_id, alert.rule_id, alert.state) == (
        1,
        1,
        "L1_REENTRY",
        "EXIT",
    )
    assert alert.value == 0.02
    assert alert.threshold == 0.02


def test_layer1_reentry_needs_the_assets_return_not_the_books_and_not_less_than_delta() -> None:
    params = _params(layers=(1,), delta=0.02)
    stopped = evaluate(
        initial_state(),
        _input(returns={1: [-0.125, -0.125, -0.1875], 2: [0.0, 0.0, 0.0]}),
        params,
    ).state
    # Below delta, even with a strongly positive book return -> stays out.
    held = evaluate(
        stopped,
        _input(
            returns={1: [-0.125, -0.1875, 0.019], 2: [0.0, 0.0, 0.0]},
            reference_returns=[0.1, 0.1, 0.1],
        ),
        params,
    )
    assert held.state.l1_out == frozenset({1})
    assert held.alerts == ()
    assert held.cut_assets == frozenset({1})


def test_layer1_state_survives_the_asset_leaving_the_reference_weights() -> None:
    params = _params(layers=(1,))
    stopped = evaluate(
        initial_state(),
        _input(returns={1: [-0.125, -0.125, -0.1875], 2: [0.0, 0.0, 0.0]}),
        params,
    ).state
    # A rebalance drops asset 1 from the reference book but keeps tracking it.
    out = evaluate(
        stopped,
        _input(weights={2: 0.9}, returns={1: [-0.125, -0.1875, 0.05], 2: [0.0, 0.0, 0.0]}),
        params,
    )
    assert out.state.l1_out == frozenset()
    assert out.weights == {2: 0.9}
    assert [alert.rule_id for alert in out.alerts] == ["L1_REENTRY"]

    # And a rebalance that keeps it out (still no re-entry signal) never clears the alert.
    still_out = evaluate(
        stopped,
        _input(weights={2: 0.9}, returns={1: [-0.125, -0.1875, -0.01], 2: [0.0, 0.0, 0.0]}),
        params,
    )
    assert still_out.state.l1_out == frozenset({1})
    assert still_out.alerts == ()
    assert still_out.weights == {2: 0.9}


def test_layer1_short_history_keeps_the_state() -> None:
    params = _params(layers=(1,), J=3, delta=0.02)
    stopped = evaluate(
        initial_state(),
        _input(returns={1: [-0.125, -0.125, -0.1875], 2: [0.0, 0.0, 0.0]}),
        params,
    ).state
    short = evaluate(
        stopped,
        _input(returns={1: [-0.1875, 0.5], 2: [0.0, 0.0, 0.0]}),
        params,
    )
    assert short.state.l1_out == frozenset({1})  # not re-entered: fewer than J returns
    assert short.alerts == ()
    assert short.cut_assets == frozenset({1})


def test_layer1_missing_returns_and_non_finite_returns_give_no_signal() -> None:
    params = _params(layers=(1,))
    missing = evaluate(initial_state(), _input(returns={2: [0.0, 0.0, 0.0]}), params)
    assert missing.weights == {1: 0.5, 2: 0.5}
    assert missing.alerts == ()
    nan = evaluate(
        initial_state(),
        _input(returns={1: [math.nan, -0.125, -0.1875], 2: [0.0, 0.0, 0.0]}),
        params,
    )
    assert nan.cut_assets == frozenset()
    assert nan.alerts == ()


def test_layer1_has_no_cooldown() -> None:
    params = _params(layers=(1,), delta=0.02)
    stopped = evaluate(
        initial_state(),
        _input(returns={1: [-0.125, -0.125, -0.1875], 2: [0.0, 0.0, 0.0]}),
        params,
    ).state
    reentered = evaluate(
        stopped,
        _input(returns={1: [-0.125, -0.1875, 0.02], 2: [0.0, 0.0, 0.0]}),
        params,
    )
    assert reentered.state.l1_out == frozenset()
    # The same window on the next session does not re-trigger...
    same = evaluate(
        reentered.state,
        _input(returns={1: [-0.125, -0.1875, 0.02], 2: [0.0, 0.0, 0.0]}),
        params,
    )
    assert same.state.l1_out == frozenset()
    assert same.alerts == ()
    # ...but a negative window on the very next session stops the name again: no cooldown.
    again = evaluate(
        same.state,
        _input(returns={1: [-0.1875, -0.125, -0.1875], 2: [0.0, 0.0, 0.0]}),
        params,
    )
    assert again.state.l1_out == frozenset({1})
    assert [alert.rule_id for alert in again.alerts] == ["L1_STOP_LOSS"]


# -- layer 2: news ------------------------------------------------------------------------------


def test_layer2_trigger_needs_the_minimum_evidence_and_strictly_exceeds_tau() -> None:
    params = _params(layers=(2,))
    out = evaluate(
        initial_state(),
        _input(news={1: NewsCounts(n_articles=4, n_negative=3)}),
        params,
    )
    assert out.cut_assets == frozenset({1})
    assert out.weights == {2: 0.5}
    assert len(out.alerts) == 1
    alert = out.alerts[0]
    assert (alert.layer, alert.asset_id, alert.rule_id, alert.state) == (
        2,
        1,
        "L2_NEWS",
        "ENTER",
    )
    assert alert.value == pytest.approx(0.75)
    assert alert.threshold == 0.5

    # q == tau is not a trigger; neither is one article (n_min=2) with q=1.
    at_tau = evaluate(
        initial_state(),
        _input(news={1: NewsCounts(n_articles=4, n_negative=2), 2: NewsCounts(1, 1)}),
        params,
    )
    assert at_tau.cut_assets == frozenset()
    assert at_tau.alerts == ()
    assert at_tau.weights == {1: 0.5, 2: 0.5}


def test_layer2_trim_fraction_scales_the_reference_weight_and_is_not_a_cut() -> None:
    params = _params(layers=(2,), news=_news(trim_fraction=0.5))
    out = evaluate(
        initial_state(),
        _input(news={1: NewsCounts(n_articles=4, n_negative=3)}),
        params,
    )
    assert out.weights == {1: 0.25, 2: 0.5}
    assert out.cut_assets == frozenset()
    assert [alert.rule_id for alert in out.alerts] == ["L2_NEWS"]


def test_layer2_hysteresis_missing_evidence_and_the_cooldown() -> None:
    params = _params(layers=(2,), news=_news(cooldown=2))
    state = evaluate(
        initial_state(),
        _input(news={1: NewsCounts(4, 3)}),
        params,
    ).state
    assert state.l2  # active

    # Between tau_exit and tau: the trim stays, with an ACTIVE record.
    between = evaluate(
        state,
        _input(news={1: NewsCounts(4, 2)}),
        params,
    )
    assert between.weights == {2: 0.5}
    assert [(a.rule_id, a.state) for a in between.alerts] == [("L2_NEWS", "ACTIVE")]
    assert between.state.l2 == state.l2

    # No evidence at all (one article) never exits either.
    thin = evaluate(
        between.state,
        _input(news={1: NewsCounts(1, 1)}),
        params,
    )
    assert thin.weights == {2: 0.5}
    assert [(a.rule_id, a.state) for a in thin.alerts] == [("L2_NEWS", "ACTIVE")]

    # q <= tau_exit with enough evidence: EXIT, and the cooldown keeps the name trimmed.
    exited = evaluate(
        thin.state,
        _input(news={1: NewsCounts(4, 1)}),
        params,
    )
    assert exited.weights == {2: 0.5}
    alert = exited.alerts[0]
    assert (alert.rule_id, alert.state) == ("L2_NEWS_EXIT", "EXIT")
    assert alert.value == pytest.approx(0.25)
    assert alert.threshold == 0.25

    # cooldown=2: the exit session plus two more sessions still trimmed, then back.
    day1 = evaluate(exited.state, _input(news={1: NewsCounts(0, 0)}), params)
    day2 = evaluate(day1.state, _input(news={1: NewsCounts(0, 0)}), params)
    day3 = evaluate(day2.state, _input(news={1: NewsCounts(0, 0)}), params)
    assert day1.weights == {2: 0.5}
    assert day2.weights == {2: 0.5}
    assert [(a.rule_id, a.state) for a in day1.alerts] == [("L2_NEWS", "COOLDOWN")]
    assert [(a.rule_id, a.state) for a in day2.alerts] == [("L2_NEWS", "COOLDOWN")]
    assert day3.weights == {1: 0.5, 2: 0.5}
    assert day3.alerts == ()
    assert day3.state.l2 == ()


def test_layer2_reactivates_during_the_cooldown() -> None:
    params = _params(layers=(2,), news=_news(cooldown=2))
    state = evaluate(initial_state(), _input(news={1: NewsCounts(4, 3)}), params).state
    cooling = evaluate(state, _input(news={1: NewsCounts(4, 1)}), params).state
    reactivated = evaluate(cooling, _input(news={1: NewsCounts(4, 4)}), params)
    assert reactivated.weights == {2: 0.5}
    assert [(a.rule_id, a.state) for a in reactivated.alerts] == [("L2_NEWS", "ENTER")]
    assert reactivated.alerts[0].value == pytest.approx(1.0)
    # Back to a plain active state, and the trim still holds.
    held = evaluate(reactivated.state, _input(news={1: NewsCounts(4, 2)}), params)
    assert held.weights == {2: 0.5}
    assert [(a.rule_id, a.state) for a in held.alerts] == [("L2_NEWS", "ACTIVE")]


def test_layer2_inactive_without_news_input_and_the_state_is_frozen() -> None:
    params = _params(layers=(2,))
    state = evaluate(initial_state(), _input(news={1: NewsCounts(4, 3)}), params).state
    paused = evaluate(state, _input(news=None), params)
    assert paused.weights == {2: 0.5}
    assert paused.alerts == ()
    assert paused.state.l2 == state.l2  # no decrement, no exit


def test_layer2_state_survives_the_asset_leaving_the_reference_weights() -> None:
    params = _params(layers=(2,))
    state = evaluate(initial_state(), _input(news={1: NewsCounts(4, 3)}), params).state
    out = evaluate(state, _input(weights={2: 0.9}, news={1: NewsCounts(4, 3)}), params)
    assert out.weights == {2: 0.9}
    assert out.cut_assets == frozenset()  # not held, so not a cut of the book
    assert [(a.asset_id, a.state) for a in out.alerts] == [(1, "ACTIVE")]
    still = evaluate(state, _input(weights={2: 0.9}, news={1: NewsCounts(4, 1)}), params)
    assert still.state.l2  # exiting starts the cooldown even while unheld
    assert [(a.rule_id, a.state) for a in still.alerts] == [("L2_NEWS_EXIT", "EXIT")]


# -- layer 3: volatility ------------------------------------------------------------------------


def test_layer3_hand_computed_annualised_vol_scaling() -> None:
    # std([0.01, 0.02, 0.03], ddof=1) = 0.01 exactly; x sqrt(252), sigma* = 0.10.
    expected = 0.10 / (0.01 * math.sqrt(252))
    out = evaluate(
        initial_state(),
        _input(reference_returns=[0.01, 0.02, 0.03]),
        _params(layers=(3,)),
    )
    assert out.s_sigma == pytest.approx(expected)
    assert out.scale == pytest.approx(expected)
    assert out.s_dd is None
    alert = out.alerts[0]
    assert (alert.layer, alert.asset_id, alert.rule_id, alert.state) == (3, None, "L3_VOL", "SCALE")
    assert alert.value == pytest.approx(expected)
    assert alert.threshold == 1.0
    assert out.weights == {1: pytest.approx(0.5 * expected), 2: pytest.approx(0.5 * expected)}


def test_layer3_degenerate_estimates_give_one_and_no_alert() -> None:
    params = _params(layers=(3,), sigma_star=0.5)
    high_sigma_star = evaluate(
        initial_state(), _input(reference_returns=[0.01, 0.02, 0.03]), params
    )
    assert high_sigma_star.s_sigma == 1.0
    assert high_sigma_star.alerts == ()

    short = evaluate(initial_state(), _input(reference_returns=[0.01, 0.02]), params)
    assert short.s_sigma == 1.0
    assert short.alerts == ()

    zero = evaluate(initial_state(), _input(reference_returns=[0.02, 0.02, 0.02]), params)
    assert zero.s_sigma == 1.0
    assert zero.alerts == ()

    nan = evaluate(initial_state(), _input(reference_returns=[0.01, math.nan, 0.03]), params)
    assert nan.s_sigma == 1.0
    assert nan.alerts == ()


def test_layer3_uses_only_the_last_vol_window_returns() -> None:
    params = _params(layers=(3,), sigma_star=0.10, vol_window=3)
    first = evaluate(initial_state(), _input(reference_returns=[0.01, 0.02, 0.03]), params)
    second = evaluate(
        initial_state(),
        _input(reference_returns=[0.9, 0.9, 0.01, 0.02, 0.03]),
        params,
    )
    assert first.s_sigma is not None
    assert second.s_sigma == pytest.approx(first.s_sigma)


# -- layer 4: drawdown --------------------------------------------------------------------------


def test_layer4_hand_computed_path_first_call_sets_the_max() -> None:
    params = _params(layers=(4,))
    first = evaluate(initial_state(), _input(nav=100.0), params)
    # M = nav on the first call: s = clip(m (1 - alpha)) = clip(2 * 0.6) = 1.
    assert first.s_dd == 1.0
    assert first.scale == 1.0
    assert first.alerts == ()
    assert first.state.nav_max == 100.0

    # s = clip(2 * (70 - 0.4 * 100) / 70) = clip(60 / 70).
    down = evaluate(first.state, _input(nav=70.0), params)
    assert down.s_dd == pytest.approx(60.0 / 70.0)
    assert down.alerts[0].rule_id == "L4_DD"
    assert down.alerts[0].state == "SCALE"
    assert down.alerts[0].value == pytest.approx(60.0 / 70.0)
    assert down.alerts[0].threshold == 1.0
    assert down.weights == {
        1: pytest.approx(0.5 * 60.0 / 70.0),
        2: pytest.approx(0.5 * 60.0 / 70.0),
    }

    # A deeper low: s = clip(2 * (60 - 40) / 60).
    deeper = evaluate(down.state, _input(nav=60.0), params)
    assert deeper.s_dd == pytest.approx(40.0 / 60.0)

    # A new high moves the running maximum with it.
    recovered = evaluate(deeper.state, _input(nav=110.0), params)
    assert recovered.state.nav_max == 110.0
    assert recovered.s_dd == 1.0
    assert recovered.alerts == ()


def test_layer4_clip_floor_and_the_ruin_case() -> None:
    params = _params(layers=(4,))
    state = _fresh_high_water(100.0)
    floor = evaluate(state, _input(nav=40.0), params)  # 2 * (40 - 40) / 40 = 0
    assert floor.s_dd == 0.0
    assert floor.weights == {}
    assert floor.cut_assets == frozenset()
    ruin = evaluate(state, _input(nav=0.0), params)
    assert ruin.s_dd == 0.0
    assert ruin.alerts[0].value == 0.0


# -- combination --------------------------------------------------------------------------------


def test_combination_uses_the_minimum_never_the_product() -> None:
    # sigma* = 0.5 * std([0.01, 0.02, 0.03]) * sqrt(252) -> s_sigma = 0.5.
    sigma_star = 0.5 * 0.01 * math.sqrt(252)
    params = _params(layers=(3, 4), sigma_star=sigma_star)
    state = evaluate(
        initial_state(), _input(nav=100.0, reference_returns=[0.01, 0.02, 0.03]), params
    )
    # s = clip(2 * (200/3 - 40) / (200/3)) = 0.8 at NAV 200/3 with M=100.
    out = evaluate(
        state.state,
        _input(nav=200.0 / 3.0, reference_returns=[0.01, 0.02, 0.03]),
        params,
    )
    assert out.s_sigma == pytest.approx(0.5)
    assert out.s_dd == pytest.approx(0.8)
    assert out.scale == pytest.approx(0.5)
    assert out.scale != pytest.approx(0.4)  # the product would be 0.40
    assert out.weights == {1: pytest.approx(0.25), 2: pytest.approx(0.25)}


def test_combination_cut_then_scale_and_never_above_reference() -> None:
    params = _params(layers=(1, 2, 3, 4), sigma_star=0.5 * 0.01 * math.sqrt(252))
    reference = {1: 0.4, 2: 0.3, 3: 0.3}
    out = evaluate(
        initial_state(),
        _input(
            weights=reference,
            returns={
                1: [-0.125, -0.125, -0.1875],
                2: [0.0, 0.0, 0.0],
                3: [0.0, 0.0, 0.0],
            },
            reference_returns=[0.01, 0.02, 0.03],
            news={2: NewsCounts(4, 4)},
        ),
        params,
    )
    assert out.cut_assets == frozenset({1, 2})  # layer 1 cuts 1; layer 2 cuts 2 (trim 1.0)
    assert out.weights == {3: pytest.approx(0.3 * 0.5)}
    assert out.scale == pytest.approx(0.5)
    for asset_id, weight in out.weights.items():
        assert weight <= reference[asset_id]
    assert sum(out.weights.values()) <= 1.0


def test_inactive_layers_give_no_alert_and_no_action() -> None:
    inp = _input(
        weights={1: 0.5, 2: 0.5},
        returns={1: [-0.125, -0.125, -0.1875], 2: [0.0, 0.0, 0.0]},
        reference_returns=[0.01, 0.02, 0.03],
        nav=40.0,
        news={1: NewsCounts(4, 4), 2: NewsCounts(4, 4)},
    )
    state = _fresh_high_water(100.0)  # so layer 4 is deep in a drawdown at nav=40
    # Every layer would fire on this input.
    fired = evaluate(state, inp, _params())
    assert fired.cut_assets == frozenset({1, 2})
    assert fired.scale == 0.0
    assert {alert.layer for alert in fired.alerts} == {1, 2, 3, 4}

    off = evaluate(state, inp, _params(layers=()))
    assert off.alerts == ()
    assert off.cut_assets == frozenset()
    assert off.weights == {1: 0.5, 2: 0.5}
    assert off.scale == 1.0
    assert off.s_sigma is None and off.s_dd is None
    assert off.state.l1_out == frozenset()
    assert off.state.l2 == ()

    no_news = evaluate(state, inp, _params(layers=(1, 3, 4)))
    assert [alert.layer for alert in no_news.alerts] == [1, 3, 4]
    assert no_news.cut_assets == frozenset({1})

    no_l1 = evaluate(state, inp, _params(layers=(2, 3, 4)))
    assert [alert.layer for alert in no_l1.alerts] == [2, 2, 3, 4]
    assert no_l1.cut_assets == frozenset({1, 2})

    no_vol = evaluate(state, inp, _params(layers=(1, 2, 4)))
    assert no_vol.s_sigma is None
    assert no_vol.s_dd == 0.0
    assert no_vol.scale == 0.0

    no_dd = evaluate(state, inp, _params(layers=(1, 2, 3)))
    assert no_dd.s_dd is None
    assert no_dd.state.nav_max == 100.0  # the running maximum is state, tracked either way
    assert no_dd.scale == pytest.approx(no_dd.s_sigma or 0.0)
    assert no_dd.scale > 0.0


def test_alerts_are_ordered_by_layer_then_asset() -> None:
    out = evaluate(
        _fresh_high_water(100.0),
        _input(
            weights={1: 0.4, 2: 0.3, 3: 0.3},
            returns={
                1: [-0.125, -0.125, -0.1875],
                2: [-0.125, -0.125, -0.1875],
                3: [0.0, 0.0, 0.0],
            },
            reference_returns=[0.01, 0.02, 0.03],
            nav=40.0,
            news={2: NewsCounts(4, 4), 3: NewsCounts(4, 4)},
        ),
        _params(),
    )
    assert [(alert.layer, alert.asset_id) for alert in out.alerts] == [
        (1, 1),
        (1, 2),
        (2, 2),
        (2, 3),
        (3, None),
        (4, None),
    ]


# -- purity -------------------------------------------------------------------------------------


def test_inputs_and_state_are_not_mutated_and_evaluate_is_deterministic() -> None:
    # A live state: asset 4 out by layer 1, asset 5 trimmed by layer 2, max NAV 100.
    state = evaluate(
        initial_state(),
        _input(
            weights={4: 0.2, 5: 0.2},
            returns={4: [-0.125, -0.125, -0.1875], 5: [0.0, 0.0, 0.0]},
            nav=100.0,
            news={5: NewsCounts(4, 4)},
        ),
        _params(layers=(1, 2)),
    ).state
    inp = _input(
        weights={1: 0.4, 2: 0.3, 3: 0.3},
        returns={
            1: [-0.125, -0.125, -0.1875],
            2: [0.0, 0.0, 0.0],
            3: [0.0, 0.0, 0.0],
            4: [0.0, 0.0, 0.02],
            5: [0.0, 0.0, 0.0],
        },
        reference_returns=[0.01, 0.02, 0.03],
        nav=70.0,
        news={3: NewsCounts(4, 4), 5: NewsCounts(4, 1)},
    )
    state_before = copy.deepcopy(state)
    inp_before = copy.deepcopy(inp)

    first = evaluate(state, inp, _params())
    second = evaluate(state, inp, _params())

    assert state == state_before
    assert inp == inp_before
    assert inp.reference_weights == {1: 0.4, 2: 0.3, 3: 0.3}
    assert inp.asset_returns[1] == [-0.125, -0.125, -0.1875]
    assert first.state == second.state
    assert first.weights == second.weights
    assert first.alerts == second.alerts
    assert first.cut_assets == second.cut_assets
    assert first.scale == second.scale


def test_a_zero_reference_weight_is_not_held_and_a_bad_one_is_rejected() -> None:
    params = _params(layers=(1,))
    out = evaluate(
        initial_state(),
        _input(weights={1: 0.5, 2: 0.0}, returns={1: [0.0, 0.0, 0.0], 2: [0.0, 0.0, 0.0]}),
        params,
    )
    assert out.weights == {1: 0.5}
    with pytest.raises(ValueError, match="non-negative weight"):
        evaluate(initial_state(), _input(weights={1: -0.1}), params)
    with pytest.raises(ValueError, match="non-negative weight"):
        evaluate(initial_state(), _input(weights={1: math.nan}), params)


def test_a_non_finite_nav_and_a_bad_state_are_rejected() -> None:
    with pytest.raises(ValueError, match="nav must be finite"):
        evaluate(initial_state(), _input(nav=math.nan), _params(layers=(1,)))
    with pytest.raises(ValueError, match="GuardState"):
        evaluate(cast(Any, None), _input(), _params(layers=(1,)))


# -- validation ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        lambda: _params(J=0),
        lambda: _params(gamma=0.0),
        lambda: _params(gamma=-0.1),
        lambda: _params(gamma=math.nan),
        lambda: _params(delta=-1e-9),
        lambda: _params(alpha=0.0),
        lambda: _params(alpha=1.0),
        lambda: _params(alpha=math.nan),
        lambda: _params(m=0.0),
        lambda: _params(m=-1.0),
        lambda: _params(sigma_star=-0.01),
        lambda: _params(sigma_star=math.inf),
        lambda: _params(vol_window=1),
        lambda: _params(periods_per_year=0),
        lambda: _params(layers=(1, 5)),
        lambda: _params(layers=(2,), news=None),
        lambda: _params(layers=(), news=_news(k=0)),
        lambda: _params(layers=(), news=_news(tau=1.5)),
        lambda: _params(layers=(), news=_news(tau=math.nan)),
        lambda: _params(layers=(), news=_news(tau_exit=-0.1)),
        lambda: _params(layers=(), news=_news(n_min=0)),
        lambda: _params(layers=(), news=_news(cooldown=-1)),
        lambda: _params(layers=(), news=_news(trim_fraction=1.5)),
    ],
)
def test_invalid_parameters_raise_value_error(build: Callable[[], GuardParams]) -> None:
    with pytest.raises(ValueError):
        build()


def test_news_while_layer_two_is_off_is_allowed_and_ignored() -> None:
    # An ablation is `dataclasses.replace(params, layers=...)`; requiring news=None there
    # would break the seam, and an unused parameter changes no result.
    params = _params(layers=(1, 3, 4), news=_news(trim_fraction=1.0))
    out = evaluate(
        initial_state(),
        _input(weights={1: 1.0}, news={1: NewsCounts(4, 4)}, returns={1: [0.0, 0.0, 0.0]}),
        params,
    )
    assert out.weights == {1: 1.0}
    assert out.cut_assets == frozenset()
    assert out.state.l2 == ()


# -- invariants over a fixed-seed random sweep ---------------------------------------------------


def test_invariants_hold_over_random_multi_session_arms() -> None:
    """Fixed-seed sweep: no result may break a guarantee the spec states.

    Every case is a chain of sessions of one arm, so the state (cooldowns, re-entries, the
    running maximum) is carried exactly as the engine will carry it.
    """
    rng = random.Random(20261028)
    for _ in range(30):
        layers = frozenset(rng.sample([1, 2, 3, 4], rng.randint(0, 4)))
        news_params = NewsParams(
            k=rng.randint(1, 5),
            tau=rng.choice([0.0, 0.3, 0.5, 0.9]),
            tau_exit=rng.choice([0.0, 0.1, 0.4]),
            n_min=rng.randint(1, 3),
            cooldown=rng.randint(0, 3),
            trim_fraction=rng.choice([0.0, 0.25, 0.5, 1.0]),
        )
        params = _params(
            layers=layers,
            J=rng.randint(1, 5),
            gamma=rng.choice([0.01, 0.05, 0.2]),
            delta=rng.choice([0.0, 0.01, 0.05]),
            sigma_star=rng.choice([0.0, 0.1, 0.5]),
            m=rng.choice([0.5, 1.0, 2.0]),
            alpha=rng.choice([0.1, 0.4, 0.9]),
            vol_window=rng.randint(2, 5),
            news=news_params if 2 in layers else None,
        )
        state = initial_state()
        nav = 100.0
        for _session in range(6):
            held = rng.sample(range(1, 9), rng.randint(1, 5))
            reference = {asset_id: round(rng.uniform(0.02, 0.4), 3) for asset_id in held}
            assets = sorted(
                set(held) | {asset_id for asset_id in range(1, 9) if rng.random() < 0.2}
            )
            returns = {
                asset_id: [rng.gauss(0.0, 0.05) for _ in range(rng.randint(0, 6))]
                for asset_id in assets
            }
            counts: dict[int, NewsCounts] = {}
            for asset_id in assets:
                n_articles = rng.randint(0, 5)
                counts[asset_id] = NewsCounts(
                    n_articles=n_articles, n_negative=rng.randint(0, n_articles)
                )
            nav *= 1.0 + rng.gauss(0.0, 0.08)
            inp = _input(
                weights=reference,
                returns=returns,
                reference_returns=[rng.gauss(0.0, 0.01) for _ in range(rng.randint(0, 7))],
                nav=nav,
                news=counts,
            )
            before = copy.deepcopy(inp)
            out = evaluate(state, inp, params)
            assert inp == before

            assert 0.0 <= out.scale <= 1.0
            expected_scale = min(
                1.0,
                out.s_sigma if out.s_sigma is not None else 1.0,
                out.s_dd if out.s_dd is not None else 1.0,
            )
            assert out.scale == expected_scale
            assert set(out.weights) <= set(reference)
            assert out.cut_assets <= set(reference)
            assert not set(out.weights) & out.cut_assets
            for asset_id, weight in out.weights.items():
                assert 0.0 < weight <= reference[asset_id] + 1e-15
            assert sum(out.weights.values()) <= sum(reference.values()) + 1e-15
            assert 1 in layers or out.state.l1_out == state.l1_out
            assert 2 in layers or out.state.l2 == state.l2
            assert out.state.nav_max == pytest.approx(max(nav, state.nav_max or nav))
            state = out.state
