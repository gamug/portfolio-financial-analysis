"""A spec-faithful stand-in for ``cycle.guard`` (BACKTEST_SPEC §8.1), for this branch only.

WP-D writes the real ``src/cycle/guard`` package in a parallel worktree. Until it is merged,
the engine and driver tests import this module (``tests/_guard_double.py``) *only when the
real package is absent* -- see :func:`install_if_missing`. When the real package exists it is
used and this file is inert. Nothing under ``src/`` imports it.
"""

from __future__ import annotations

import importlib.util
import statistics
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import ModuleType


@dataclass(frozen=True)
class NewsParams:
    k: int
    tau: float
    tau_exit: float
    n_min: int
    cooldown: int
    trim_fraction: float = 1.0


@dataclass(frozen=True)
class GuardParams:
    layers: frozenset[int]
    J: int
    gamma: float
    delta: float
    sigma_star: float
    m: float
    alpha: float
    vol_window: int = 21
    periods_per_year: int = 252
    news: NewsParams | None = None


@dataclass(frozen=True)
class NewsCounts:
    n_articles: int
    n_negative: int


@dataclass(frozen=True)
class GuardInput:
    date: str
    reference_weights: Mapping[int, float]
    asset_returns: Mapping[int, Sequence[float]]
    reference_returns: Sequence[float]
    nav: float
    news: Mapping[int, NewsCounts] | None = None


@dataclass(frozen=True)
class Alert:
    layer: int
    asset_id: int | None
    rule_id: str
    value: float
    threshold: float
    state: str


@dataclass(frozen=True)
class GuardState:
    l1_out: frozenset[int] = frozenset()
    l2_active: frozenset[int] = frozenset()
    l2_cooldown: Mapping[int, int] = field(default_factory=dict)
    peak_nav: float | None = None


@dataclass(frozen=True)
class GuardOutput:
    state: GuardState
    weights: dict[int, float]
    cut_assets: frozenset[int]
    scale: float
    s_sigma: float | None
    s_dd: float | None
    alerts: tuple[Alert, ...]


def initial_state() -> GuardState:
    return GuardState()


def _layer1(
    state: GuardState, inp: GuardInput, params: GuardParams
) -> tuple[set[int], list[Alert]]:
    out = set(state.l1_out)
    alerts: list[Alert] = []
    tracked = set(out) | set(inp.reference_weights)
    for asset_id in sorted(tracked):
        series = list(inp.asset_returns.get(asset_id, ()))
        if asset_id in out:
            if series and series[-1] >= params.delta:
                out.discard(asset_id)
                alerts.append(Alert(1, asset_id, "L1_REENTRY", series[-1], params.delta, "EXIT"))
        elif len(series) >= params.J:
            total = sum(series[-params.J :])
            if total < -params.gamma:
                out.add(asset_id)
                alerts.append(Alert(1, asset_id, "L1_STOP_LOSS", total, -params.gamma, "ENTER"))
    return out, alerts


def _layer2(
    state: GuardState, inp: GuardInput, params: GuardParams
) -> tuple[set[int], dict[int, int], list[Alert]]:
    news = inp.news or {}
    active = set(state.l2_active)
    cooldown = dict(state.l2_cooldown)
    alerts: list[Alert] = []
    np = params.news
    assert np is not None
    for asset_id in sorted(inp.reference_weights):
        counts = news.get(asset_id)
        if counts is None or counts.n_articles == 0:
            continue  # missing evidence never triggers and never exits
        q = counts.n_negative / counts.n_articles
        if asset_id not in active:
            if counts.n_articles >= np.n_min and q > np.tau:
                active.add(asset_id)
                cooldown.pop(asset_id, None)
                alerts.append(Alert(2, asset_id, "L2_NEWS", q, np.tau, "ENTER"))
            continue
        if counts.n_articles >= np.n_min and q <= np.tau_exit:
            active.discard(asset_id)
            cooldown[asset_id] = np.cooldown
            alerts.append(Alert(2, asset_id, "L2_NEWS_EXIT", q, np.tau_exit, "EXIT"))
    return active, cooldown, alerts


def evaluate(  # noqa: C901, PLR0912 - one linear pass per layer, as the spec describes
    state: GuardState, inp: GuardInput, params: GuardParams
) -> GuardOutput:
    alerts: list[Alert] = []
    l1_out: set[int] = set(state.l1_out)
    l2_active: set[int] = set(state.l2_active)
    cooldown = dict(state.l2_cooldown)
    if 1 in params.layers:
        l1_out, l1_alerts = _layer1(state, inp, params)
        alerts.extend(l1_alerts)
    if 2 in params.layers and params.news is not None:
        l2_active, cooldown, l2_alerts = _layer2(
            GuardState(
                l1_out=frozenset(l1_out),
                l2_active=frozenset(l2_active),
                l2_cooldown=cooldown,
                peak_nav=state.peak_nav,
            ),
            inp,
            params,
        )
        alerts.extend(l2_alerts)
    peak = inp.nav if state.peak_nav is None else max(state.peak_nav, inp.nav)

    l2_cut = frozenset(l2_active) | frozenset(a for a, n in cooldown.items() if n > 0)
    trim = params.news.trim_fraction if params.news is not None else 1.0
    cut = frozenset(l1_out) | (l2_cut if trim >= 1.0 else frozenset())
    weights: dict[int, float] = {}
    for asset_id, ref in inp.reference_weights.items():
        if asset_id in l1_out:
            continue
        if asset_id in cut:
            if trim >= 1.0:
                continue
            weights[asset_id] = ref * trim
        else:
            weights[asset_id] = ref

    s_sigma: float | None = None
    if 3 in params.layers:
        series = list(inp.reference_returns)
        if len(series) >= params.vol_window:
            sd = statistics.stdev(series[-params.vol_window :]) * (params.periods_per_year**0.5)
            s_sigma = 1.0 if sd == 0 else min(1.0, params.sigma_star / sd)
        else:
            s_sigma = 1.0
        if s_sigma < 1.0:
            alerts.append(Alert(3, None, "L3_VOL", s_sigma, 1.0, "SCALE"))

    s_dd: float | None = None
    if 4 in params.layers and inp.nav > 0:
        s_dd = min(1.0, max(0.0, params.m * (inp.nav - params.alpha * peak) / inp.nav))
        if s_dd < 1.0:
            alerts.append(Alert(4, None, "L4_DD", s_dd, 1.0, "SCALE"))

    scale = min(1.0, s_sigma if s_sigma is not None else 1.0, s_dd if s_dd is not None else 1.0)
    if scale < 1.0:
        weights = {a: w * scale for a, w in weights.items() if w * scale > 1e-12}

    started = dict(state.l2_cooldown)
    next_cooldown = {a: n - 1 for a, n in started.items() if a not in l2_active and n - 1 > 0}
    next_cooldown.update({a: n for a, n in cooldown.items() if a not in started})
    return GuardOutput(
        state=GuardState(
            l1_out=frozenset(l1_out),
            l2_active=frozenset(l2_active),
            l2_cooldown=next_cooldown,
            peak_nav=peak,
        ),
        weights=weights,
        cut_assets=cut,
        scale=scale,
        s_sigma=s_sigma,
        s_dd=s_dd,
        alerts=tuple(alerts),
    )


def _module() -> ModuleType:
    module = ModuleType("cycle.guard")
    module.NewsParams = NewsParams  # type: ignore[attr-defined]
    module.GuardParams = GuardParams  # type: ignore[attr-defined]
    module.NewsCounts = NewsCounts  # type: ignore[attr-defined]
    module.GuardInput = GuardInput  # type: ignore[attr-defined]
    module.Alert = Alert  # type: ignore[attr-defined]
    module.GuardState = GuardState  # type: ignore[attr-defined]
    module.GuardOutput = GuardOutput  # type: ignore[attr-defined]
    module.initial_state = initial_state  # type: ignore[attr-defined]
    module.evaluate = evaluate  # type: ignore[attr-defined]
    return module


def install_if_missing() -> bool:
    """Install the double as ``cycle.guard`` when WP-D's package is absent. Returns True if
    installed."""
    if "cycle.guard" in sys.modules:
        return False
    if importlib.util.find_spec("cycle.guard") is not None:
        return False
    sys.modules["cycle.guard"] = _module()
    return True
