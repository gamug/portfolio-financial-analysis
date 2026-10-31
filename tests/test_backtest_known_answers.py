"""Known-answer tests of the backtest mechanics (FINISH_REPO section 6b).

Every scenario is synthetic -- simulated returns, simulated targets, simulated FUNDAMENTAL
scores -- and every expected number is written out by hand in the test, independently of the
code under test. The engine must match it to 1e-12. No real data, no network, no LLM.

The scenarios are the ten of section 6b:

1. no-trade identity     constant returns, no GUARD signal -> GUARD == NOGUARD, session by session
2. compounding and cost  two assets, one rebalance -> book value and cost equal the hand formula
3. cash                  a book fully in cash earns exactly the simulated RF
4. layer 1               a stop fires on the session the spec says, not before; delta re-enters;
                         a name with fewer than J real returns never triggers
5. layer 3               a volatility jump scales exposure by sigma_star / sigma_hat, never above 1
6. layer 4               a drawdown path follows m(nav - alpha * peak) / nav, never below 0
7. no lookahead          truncating the data after session t leaves every action up to t unchanged
8. layer 2 off           news signals change nothing when the layer is disabled
9. twin                  the GUARD arm's pre-GUARD targets equal NOGUARD's at every decision date
10. selection            a known score ranking -> the Top-N set and the 0.30 sector cap
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from cycle import construction
from cycle.backtest import engine
from cycle.guard import GuardParams, NewsCounts, NewsParams
from kg_schema import trading_calendar

BASE = "MARKOWITZ_BASE"
NOGUARD = engine.ARM_TOPN_NOGUARD
GUARD = engine.ARM_TOPN_GUARD_NO_NLP


def _sessions(count: int) -> list[str]:
    day = date(2021, 9, 1)
    while not trading_calendar.is_trading_day(day):
        day = date.fromordinal(day.toordinal() + 1)
    out = [day.isoformat()]
    while len(out) < count:
        day = trading_calendar.next_trading_day(day)
        out.append(day.isoformat())
    return out


def _guard(  # noqa: PLR0913 - the guards each scenario varies, all named
    *,
    layers: set[int],
    J: int = 3,
    gamma: float = 0.05,
    delta: float = 0.02,
    sigma_star: float = 0.10,
    m: float = 2.0,
    alpha: float = 0.75,
    vol_window: int = 2,
    news: NewsParams | None = None,
) -> GuardParams:
    return GuardParams(
        layers=frozenset(layers),
        J=J,
        gamma=gamma,
        delta=delta,
        sigma_star=sigma_star,
        m=m,
        alpha=alpha,
        vol_window=vol_window,
        periods_per_year=252,
        news=news,
    )


def _run(  # noqa: PLR0913 - one synthetic window, every input explicit
    sessions: list[str],
    returns: dict[str, dict[int, float]],
    targets: dict[str, dict[str, dict[int, float]]],
    arms: list[engine.ArmSpec],
    *,
    formation: str,
    decision_dates: list[str],
    rf: dict[str, float] | None = None,
    news: dict[str, dict[int, NewsCounts]] | None = None,
    cost_bps: float = 10.0,
) -> engine.EngineResult:
    return engine.simulate(
        engine.EngineInput(
            sessions=sessions,
            returns=returns,
            rf=rf or dict.fromkeys(sessions, 0.0),
            targets=targets,
            arms=arms,
            formation_date=formation,
            decision_dates=decision_dates,
            cost_bps=cost_bps,
            benchmark_members={},
            news=news or {},
        )
    )


def _both(targets: dict[str, dict[int, float]]) -> dict[str, dict[str, dict[int, float]]]:
    """The driver hands BASE's targets to every arm; a GUARD arm reads its NOGUARD twin's."""
    return {BASE: targets, NOGUARD: targets}


def _by_date(result: engine.EngineResult, code: str) -> dict[str, engine.DayWeights]:
    return {day.date: day for day in result.arms[code].weights}


def _perf(result: engine.EngineResult, code: str) -> dict[str, engine.DayPerformance]:
    return {day.date: day for day in result.arms[code].performance}


# 1 --------------------------------------------------------------------------------------


def test_no_trade_identity_when_guard_never_signals() -> None:
    sessions = _sessions(6)
    returns = {s: {1: 0.001, 2: 0.001} for s in sessions[1:]}
    targets = _both({sessions[0]: {1: 0.5, 2: 0.5}})
    arms = [
        engine.ArmSpec(NOGUARD, BASE),
        # gamma far above any attainable sum, so layer 1 can never fire.
        engine.ArmSpec(GUARD, NOGUARD, guard=_guard(layers={1}, gamma=1.0, delta=0.0)),
    ]
    result = _run(
        sessions, returns, targets, arms, formation=sessions[1], decision_dates=[sessions[0]]
    )

    guard = _by_date(result, GUARD)
    noguard = _by_date(result, NOGUARD)
    for session in sessions[1:]:
        assert guard[session].final_target == pytest.approx(noguard[session].final_target)
        assert guard[session].cash_weight == pytest.approx(noguard[session].cash_weight)
    guard_perf = _perf(result, GUARD)
    for session in sessions[1:]:
        assert guard_perf[session].nav == pytest.approx(
            _perf(result, NOGUARD)[session].nav, abs=1e-12
        )
    # No order of its own: the GUARD book trades only when NOGUARD trades.
    assert [e.date for e in result.arms[GUARD].executions] == [
        e.date for e in result.arms[NOGUARD].executions
    ]


# 2 --------------------------------------------------------------------------------------


def test_compounding_and_the_cost_formula() -> None:
    sessions = _sessions(4)
    # The book is first put at risk at the close of the formation session, so the +10 % is
    # earned on the *next* session.
    returns = {sessions[1]: {1: 0.0, 2: 0.0}, sessions[2]: {1: 0.10, 2: 0.0}, sessions[3]: {}}
    targets = _both({sessions[0]: {1: 0.6, 2: 0.4}})
    arms = [engine.ArmSpec(NOGUARD, BASE)]
    result = _run(
        sessions, returns, targets, arms, formation=sessions[1], decision_dates=[sessions[0]]
    )
    perf = _perf(result, NOGUARD)

    # Formation: turnover |0.6 - 0| + |0.4 - 0| = 1.0, cost 10 bps x 1.0.
    assert result.arms[NOGUARD].executions[0].turnover == pytest.approx(1.0, abs=1e-15)
    assert result.arms[NOGUARD].executions[0].cost_return == pytest.approx(0.001, abs=1e-15)
    assert perf[sessions[1]].gross_return == pytest.approx(0.0, abs=1e-15)
    assert perf[sessions[1]].nav == pytest.approx(1.0 * (1.0 - 0.001), abs=1e-15)
    # Next session: 0.6 x 10 % = 6 % on the book, no order, no cost.
    assert perf[sessions[2]].gross_return == pytest.approx(0.06, abs=1e-15)
    assert perf[sessions[2]].cost_return == pytest.approx(0.0, abs=1e-15)
    assert perf[sessions[2]].nav == pytest.approx(0.999 * 1.06, abs=1e-15)
    # The drift: 0.6 x 1.10 / 1.06 and 0.4 / 1.06.
    day = _by_date(result, NOGUARD)[sessions[2]]
    assert day.drifted[1] == pytest.approx(0.6 * 1.10 / 1.06, abs=1e-15)
    assert day.drifted[2] == pytest.approx(0.4 / 1.06, abs=1e-15)
    assert sum(day.final_target.values()) == pytest.approx(1.0, abs=1e-12)


# 3 --------------------------------------------------------------------------------------


def test_a_cash_book_earns_exactly_the_risk_free_rate() -> None:
    sessions = _sessions(5)
    rf = {s: 0.001 for s in sessions}
    targets = _both({sessions[0]: {}})
    arms = [engine.ArmSpec(NOGUARD, BASE)]
    result = _run(
        sessions,
        {},
        targets,
        arms,
        formation=sessions[1],
        decision_dates=[sessions[0]],
        rf=rf,
    )
    perf = _perf(result, NOGUARD)
    expected = 1.0
    for session in sessions[2:]:
        expected *= 1.0 + rf[session]
        assert perf[session].nav == pytest.approx(expected, abs=1e-15)
        assert perf[session].gross_return == pytest.approx(rf[session], abs=1e-15)
        assert perf[session].cash_weight == pytest.approx(1.0, abs=1e-15)


# 4 --------------------------------------------------------------------------------------


def test_layer_one_stop_fires_on_the_specified_session_and_reenters_on_delta() -> None:
    sessions = _sessions(10)
    # The return of session t is earned by the weights held into t; an order formed at the
    # close of t executes at the close of t+1 (the same D+1 convention as a rebalance).
    path = [0.0, 0.0, -0.02, -0.02, -0.02, -0.02, 0.03, 0.05, 0.10, 0.0]
    returns = {session: {1: path[i]} for i, session in enumerate(sessions)}
    targets = _both({sessions[2]: {1: 1.0}})
    arms = [
        engine.ArmSpec(NOGUARD, BASE),
        engine.ArmSpec(GUARD, NOGUARD, guard=_guard(layers={1}, J=3, gamma=0.05, delta=0.02)),
    ]
    result = _run(
        sessions, returns, targets, arms, formation=sessions[3], decision_dates=[sessions[2]]
    )
    weights = _by_date(result, GUARD)
    perf = _perf(result, GUARD)

    # The sums of the last three returns of asset 1:
    #   s3: -0.04 (no stop)  s4: -0.06 (stop)  s5: -0.06  s6: -0.01
    assert weights[sessions[3]].final_target[1] == pytest.approx(1.0, abs=1e-12)
    assert weights[sessions[4]].final_target.get(1, 0.0) == pytest.approx(0.0, abs=1e-12)
    assert weights[sessions[4]].cash_weight == pytest.approx(1.0, abs=1e-12)
    # The cut is an order formed at that close: it executes at the next close, so s5's return
    # is still earned and the book is cash from s6.
    assert perf[sessions[5]].gross_return == pytest.approx(-0.02, abs=1e-12)
    assert perf[sessions[6]].gross_return == pytest.approx(0.0, abs=1e-12)
    assert weights[sessions[5]].final_target.get(1, 0.0) == pytest.approx(0.0, abs=1e-12)
    # s6's own return (+3 %) is at or above delta (2 %): the re-entry is formed at that close,
    # executes at s7's close, and earns s8.
    assert weights[sessions[6]].final_target[1] == pytest.approx(1.0, abs=1e-12)
    assert perf[sessions[7]].gross_return == pytest.approx(0.0, abs=1e-12)
    assert perf[sessions[8]].gross_return == pytest.approx(0.10, abs=1e-12)


def test_layer_one_never_fires_on_a_name_with_fewer_than_j_real_returns() -> None:
    sessions = _sessions(8)
    # Asset 2 is priced on s3 (so the book can buy it) and crashes on s4: two real returns,
    # never the three J requires. Asset 1 is flat and keeps the book valid.
    returns: dict[str, dict[int, float]] = {}
    for index, session in enumerate(sessions):
        day = {1: 0.0}
        if index == 3:
            day[2] = 0.0
        if index == 4:
            day[2] = -0.5
        returns[session] = day
    targets = _both({sessions[2]: {1: 0.5, 2: 0.5}})
    arms = [
        engine.ArmSpec(NOGUARD, BASE),
        engine.ArmSpec(GUARD, NOGUARD, guard=_guard(layers={1}, J=3, gamma=0.05, delta=0.02)),
    ]
    result = _run(
        sessions, returns, targets, arms, formation=sessions[3], decision_dates=[sessions[2]]
    )
    assert [
        e for e in result.arms[GUARD].guard_events if e.alert.layer == 1 and e.alert.asset_id == 2
    ] == []
    # A third real return would arm it: the same asset with a third loss does fire.
    with_three = {session: dict(returns[session]) for session in sessions}
    with_three[sessions[5]] = {1: 0.0, 2: -0.5}
    armed = _run(
        sessions, with_three, targets, arms, formation=sessions[3], decision_dates=[sessions[2]]
    )
    fired = [
        e for e in armed.arms[GUARD].guard_events if e.alert.layer == 1 and e.alert.asset_id == 2
    ]
    assert [e.date for e in fired] == [sessions[5]]


# 5 --------------------------------------------------------------------------------------


def test_layer_three_scales_to_sigma_star_over_sigma_hat() -> None:
    sessions = _sessions(8)
    alternating = [0.02, -0.02] * 4
    returns = {s: {1: alternating[i]} for i, s in enumerate(sessions)}
    targets = _both({sessions[0]: {1: 1.0}})
    arms = [
        engine.ArmSpec(BASE, BASE),
        engine.ArmSpec(NOGUARD, BASE),
        engine.ArmSpec(GUARD, NOGUARD, guard=_guard(layers={3}, sigma_star=0.10, vol_window=2)),
    ]
    result = _run(
        sessions, returns, targets, arms, formation=sessions[1], decision_dates=[sessions[0]]
    )
    weights = _by_date(result, GUARD)

    sigma_hat = float(np.std([0.02, -0.02], ddof=1) * np.sqrt(252))
    expected = min(1.0, 0.10 / sigma_hat)
    assert expected < 1.0  # the fixture really binds
    for session in sessions[2:]:
        assert weights[session].final_target[1] == pytest.approx(expected, abs=1e-12)
        assert weights[session].cash_weight == pytest.approx(1.0 - expected, abs=1e-12)
        assert weights[session].final_target[1] <= 1.0
    # A calm book is never levered: sigma_hat below sigma_star gives exactly 1.
    arms_calm = [
        engine.ArmSpec(BASE, BASE),
        engine.ArmSpec(NOGUARD, BASE),
        engine.ArmSpec(GUARD, NOGUARD, guard=_guard(layers={3}, sigma_star=10.0, vol_window=2)),
    ]
    calm = _run(
        sessions, returns, targets, arms_calm, formation=sessions[1], decision_dates=[sessions[0]]
    )
    assert _by_date(calm, GUARD)[sessions[-1]].final_target[1] == pytest.approx(1.0, abs=1e-15)


# 6 --------------------------------------------------------------------------------------


def test_layer_four_follows_the_drawdown_formula_and_never_goes_below_zero() -> None:
    alpha = 0.75
    m = 1.0 / (1.0 - alpha)  # AVANCE E1: m(1 - alpha) = 1, so s = 1 at a new maximum
    sessions = _sessions(5)
    returns = {
        sessions[1]: {1: 0.0},
        sessions[2]: {1: -0.10},
        sessions[3]: {1: 0.0},
        sessions[4]: {1: 0.0},
    }
    targets = _both({sessions[0]: {1: 1.0}})
    arms = [
        engine.ArmSpec(BASE, BASE),
        engine.ArmSpec(NOGUARD, BASE),
        engine.ArmSpec(GUARD, NOGUARD, guard=_guard(layers={4}, m=m, alpha=alpha, vol_window=2)),
    ]
    result = _run(
        sessions, returns, targets, arms, formation=sessions[1], decision_dates=[sessions[0]]
    )
    weights = _by_date(result, GUARD)
    perf = _perf(result, GUARD)

    # The running maximum starts at the initial capital, 1.0; the reported NAV and the hand
    # formula must agree session by session (the reference weight is always NOGUARD's 1.0).
    peak = 1.0
    for session in sessions[1:]:
        nav = perf[session].nav
        peak = max(peak, nav)
        expected = min(1.0, max(0.0, m * (nav - alpha * peak) / nav))
        assert weights[session].final_target.get(1, 0.0) == pytest.approx(expected, abs=1e-12)
        assert weights[session].cash_weight == pytest.approx(1.0 - expected, abs=1e-12)
    # The -10 % session really moved the scaler: its close is below the 1.0 peak.
    assert weights[sessions[2]].final_target[1] < 1.0
    assert weights[sessions[2]].final_target[1] > 0.0

    # A deep drawdown clips at 0, never negative.
    deep = {
        sessions[1]: {1: 0.0},
        sessions[2]: {1: -0.95},
        sessions[3]: {1: 0.0},
        sessions[4]: {1: 0.0},
    }
    result_deep = _run(
        sessions,
        deep,
        targets,
        [
            engine.ArmSpec(BASE, BASE),
            engine.ArmSpec(NOGUARD, BASE),
            engine.ArmSpec(
                GUARD, NOGUARD, guard=_guard(layers={4}, m=m, alpha=alpha, vol_window=2)
            ),
        ],
        formation=sessions[1],
        decision_dates=[sessions[0]],
    )
    assert _by_date(result_deep, GUARD)[sessions[2]].final_target.get(1, 0.0) == pytest.approx(
        0.0, abs=1e-12
    )


# 7 --------------------------------------------------------------------------------------


def _walk(sessions: list[str], n_assets: int = 3) -> dict[str, dict[int, float]]:
    """A deterministic pseudo-random walk (no RNG, so the test file reproduces exactly)."""
    out: dict[str, dict[int, float]] = {}
    for index, session in enumerate(sessions):
        day: dict[int, float] = {}
        for asset_id in range(1, n_assets + 1):
            step = ((index + 1) * (asset_id + 3) * 7919) % 1000
            day[asset_id] = (step - 500) / 10_000.0
        out[session] = day
    return out


def test_no_lookahead_a_truncated_run_repeats_every_action_up_to_the_cut() -> None:
    sessions = _sessions(40)
    returns = _walk(sessions)
    base = {
        sessions[0]: {1: 0.4, 2: 0.3, 3: 0.3},
        sessions[10]: {1: 0.5, 2: 0.5},
        sessions[20]: {3: 0.6, 1: 0.4},
        sessions[30]: {1: 0.2, 2: 0.4, 3: 0.4},
    }
    targets = _both(base)
    params = _guard(layers={1, 3, 4}, J=5, gamma=0.10, delta=0.0, sigma_star=0.20, vol_window=5)
    arms = [
        engine.ArmSpec(NOGUARD, BASE),
        engine.ArmSpec(GUARD, NOGUARD, guard=params),
    ]
    full = _run(
        sessions,
        returns,
        targets,
        arms,
        formation=sessions[1],
        decision_dates=[sessions[0], sessions[10], sessions[20], sessions[30]],
    )
    cut = 25
    truncated = _run(
        sessions[:cut],
        {s: returns[s] for s in sessions[:cut]},
        _both({d: t for d, t in base.items() if d < sessions[cut]}),
        arms,
        formation=sessions[1],
        decision_dates=[d for d in (sessions[0], sessions[10], sessions[20]) if d < sessions[cut]],
    )
    for code in (NOGUARD, GUARD):
        full_weights = _by_date(full, code)
        cut_weights = _by_date(truncated, code)
        for session in sessions[1:cut]:
            assert cut_weights[session].final_target == pytest.approx(
                full_weights[session].final_target, abs=1e-12
            )
            assert cut_weights[session].cash_weight == pytest.approx(
                full_weights[session].cash_weight, abs=1e-12
            )
        full_perf = _perf(full, code)
        cut_perf = _perf(truncated, code)
        for session in sessions[1:cut]:
            assert cut_perf[session].nav == pytest.approx(full_perf[session].nav, abs=1e-12)
        full_events = [e for e in full.arms[code].guard_events if e.date < sessions[cut]]
        cut_events = [e for e in truncated.arms[code].guard_events if e.date < sessions[cut]]
        assert [(e.date, e.alert.rule_id, e.status) for e in cut_events] == [
            (e.date, e.alert.rule_id, e.status) for e in full_events
        ]


# 8 --------------------------------------------------------------------------------------


def test_layer_two_off_ignores_news_signals() -> None:
    sessions = _sessions(10)
    returns = _walk(sessions)
    targets = _both({sessions[0]: {1: 0.5, 2: 0.5}})
    news = {
        s: {
            1: NewsCounts(n_articles=10, n_negative=10),
            2: NewsCounts(n_articles=10, n_negative=10),
        }
        for s in sessions
    }
    off = _guard(layers={1, 3, 4}, J=5, gamma=0.50, sigma_star=10.0, vol_window=5)
    with_news = _run(
        sessions,
        returns,
        targets,
        [engine.ArmSpec(GUARD, NOGUARD, guard=off), engine.ArmSpec(NOGUARD, BASE)],
        formation=sessions[1],
        decision_dates=[sessions[0]],
        news=news,
    )
    without = _run(
        sessions,
        returns,
        targets,
        [engine.ArmSpec(GUARD, NOGUARD, guard=off), engine.ArmSpec(NOGUARD, BASE)],
        formation=sessions[1],
        decision_dates=[sessions[0]],
    )
    for code in (GUARD, NOGUARD):
        assert _by_date(with_news, code)[sessions[-1]].final_target == pytest.approx(
            _by_date(without, code)[sessions[-1]].final_target, abs=1e-15
        )
        assert [e.alert.rule_id for e in with_news.arms[code].guard_events] == [
            e.alert.rule_id for e in without.arms[code].guard_events
        ]
    # Not vacuous: the same news *with* layer 2 on does cut the names.
    on = _guard(
        layers={1, 2, 3, 4},
        J=5,
        gamma=0.50,
        sigma_star=10.0,
        vol_window=5,
        news=NewsParams(k=1, tau=0.5, tau_exit=0.4, n_min=3, cooldown=2),
    )
    armed = _run(
        sessions,
        returns,
        targets,
        [engine.ArmSpec(GUARD, NOGUARD, guard=on), engine.ArmSpec(NOGUARD, BASE)],
        formation=sessions[1],
        decision_dates=[sessions[0]],
        news=news,
    )
    assert _by_date(armed, GUARD)[sessions[2]].final_target.get(1, 0.0) == pytest.approx(
        0.0, abs=1e-12
    )


# 9 --------------------------------------------------------------------------------------


def test_the_guard_twin_starts_from_the_noguard_targets_at_every_decision() -> None:
    sessions = _sessions(30)
    returns = _walk(sessions)
    decisions = [sessions[0], sessions[10], sessions[20]]
    recorded = [sessions[10], sessions[20]]  # the weight rows start at formation
    base = {
        sessions[0]: {1: 0.4, 2: 0.3, 3: 0.3},
        sessions[10]: {1: 0.6, 3: 0.2},
        sessions[20]: {2: 0.5, 3: 0.3},
    }
    targets = _both(base)
    params = _guard(layers={1, 3, 4}, J=5, gamma=0.10, sigma_star=0.15, vol_window=5)
    result = _run(
        sessions,
        returns,
        targets,
        [engine.ArmSpec(NOGUARD, BASE), engine.ArmSpec(GUARD, NOGUARD, guard=params)],
        formation=sessions[1],
        decision_dates=decisions,
    )
    guard = _by_date(result, GUARD)
    noguard = _by_date(result, NOGUARD)
    for decision in recorded:
        assert guard[decision].pre_guard == pytest.approx(noguard[decision].final_target, abs=1e-15)
        assert guard[decision].markowitz_target == pytest.approx(base[decision], abs=1e-15)


# 10 -------------------------------------------------------------------------------------


def _candidates(scores: list[tuple[str, str, float]]) -> list[construction.BookCandidate]:
    return [
        construction.BookCandidate(asset_id=i, ticker=ticker, blended_score=score, sector=sector)
        for i, (ticker, sector, score) in enumerate(scores, start=1)
    ]


def test_selection_top_n_and_the_sector_cap_on_simulated_scores() -> None:
    # 4 sectors x 15 names, scores interleaved A1 B1 C1 D1 A2 ...: the top 30 holds at most
    # 8 names per sector, under the 9-name fullness threshold of a 0.30 cap at n = 30.
    scores: list[tuple[str, str, float]] = []
    for rank in range(15):
        for sector in "ABCD":
            scores.append((f"{sector}{rank + 1}", sector, 1000.0 - rank * 4 - "ABCD".index(sector)))
    candidates = _candidates(scores)
    book = construction.build_book(candidates, n=30, max_sector_weight=0.30)
    assert book.n_held == 30
    assert book.shortfall == 0
    assert book.max_sector_weight == pytest.approx(0.30)
    assert book.relaxations == ()
    by_id = {c.asset_id: c for c in candidates}
    expected = {
        c.asset_id
        for c in sorted(candidates, key=lambda c: (-c.blended_score, c.ticker, c.asset_id))[:30]
    }
    assert set(book.weights) == expected
    sector_weight: dict[str, float] = {}
    for asset_id, weight in book.weights.items():
        sector = by_id[asset_id].sector or ""
        sector_weight[sector] = sector_weight.get(sector, 0.0) + weight
    assert max(sector_weight.values()) <= 0.30 + 1e-9

    # One sector with 20 of the top names: the fullness rule skips its 10th name, the book fills
    # by rank (the declared overflow) and the sector cap relaxes to the smallest feasible value.
    skewed = _candidates(
        [("A" + str(i), "A", 1000.0 - i) for i in range(1, 21)]
        + [("B" + str(i), "B", 1000.0 - 20 - i) for i in range(1, 11)]
        + [("C" + str(i), "C", 1000.0 - 30 - i) for i in range(1, 11)]
    )
    skewed_book = construction.build_book(skewed, n=30, max_sector_weight=0.30)
    assert skewed_book.n_held == 30
    assert skewed_book.relaxations  # the cap could not be met and is recorded, never hidden
    assert skewed_book.max_sector_weight > 0.30
    sectors = {c.asset_id: (c.sector or "") for c in skewed}
    counts: dict[str, int] = {}
    for asset_id in skewed_book.weights:
        sector = sectors[asset_id]
        counts[sector] = counts.get(sector, 0) + 1
    assert counts == {"A": 12, "B": 9, "C": 9}
