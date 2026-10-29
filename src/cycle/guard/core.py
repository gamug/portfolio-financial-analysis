"""GUARD -- the deterministic, four-layer risk throttle of the backtest overlay.

Pure core: **no database, no I/O, and the inputs are never mutated**. numpy is used only
for the layer-3 standard deviation. The interface is fixed by ``BACKTEST_SPEC.md`` §8.1
and the arm semantics by §4; ``evaluate`` is called once per session and per GUARD arm,
after the close of the session, on the reference weights of the NOGUARD twin.

The four layers (each one *inspired by*, never a literal reproduction of, its source):

1. **Stop-loss per asset** -- Kaminski & Lo (2014), *When Do Stop-Loss Rules Stop
   Losses?*, J. Financial Markets 18, 234-254, Def. 1 ("inspired by"). The sum of the
   last ``J`` simple returns of the asset, ``S(J)``, below ``-gamma`` sets the name to
   zero; the asset's own latest one-session return at or above ``delta`` brings it back.
   No cooldown. The state is per asset id and outlives the asset leaving
   ``reference_weights``: a rebalance never clears an alert.
2. **News per asset** -- Tetlock (2007), JF 62(3) 1139-1168, and Loughran & McDonald
   (2011), JF 66(1) 35-65 ("inspired by"): a trim when the negative-article ratio
   ``q = n_negative / n_articles`` exceeds ``tau`` with at least ``n_min`` articles, a
   hysteresis exit at ``tau_exit``, a cooldown of ``cooldown`` sessions still trimmed,
   and reactivation if the trigger returns during the cooldown. Missing evidence neither
   triggers nor exits. The window (close of t-k, close of t] and the URL deduplication
   are the caller's (``NewsParams.k`` is metadata for it); this module only counts what
   it is given.
3. **Volatility of the book** -- Moreira & Muir (2017), JF 72(4) 1611-1644 ("inspired
   by"): ``s_sigma = min(1, sigma_star / sigma_hat)`` with ``sigma_hat`` the annualised
   sample standard deviation (``ddof=1``) of the last ``vol_window`` returns of the
   reference weights held fixed -- an ex-ante estimate, no feedback.
4. **Drawdown of the book** -- Grossman & Zhou (1993), Math. Finance 3(3) 241-276, via
   Yang & Zhong (2012), SSRN 2053854 ("inspired by"):
   ``s_dd = clip(m (nav - alpha * max_nav) / nav, 0, 1)``, ``max_nav`` the running maximum
   of this arm's NAV since formation (the first call sets it to ``nav``).

Combination (spec §4): reference weights -> names cut by layers 1 and 2 go to zero/cash
-> every remaining weight is multiplied by ``s = min(1, s_sigma, s_dd)`` (**the minimum,
never the product**). No weight ever exceeds its reference weight, so GUARD can only
reduce exposure. A layer that is not in ``GuardParams.layers`` emits no alert, cuts
nothing and contributes a scaling term of 1 (reported as ``None``).

Interpretation choices this module fixes (the spec leaves them open; listed in REPORT.md):

* Layer 2's cooldown counts the sessions *after* the exit session: the exit session's
  own target is still trimmed (the trim was in force during it), plus ``cooldown``
  further sessions; with ``cooldown == 0`` the name returns to its reference weight on
  the session after the exit.
* Layer 2 records the ongoing trim every session (``ACTIVE`` / ``COOLDOWN``), which is
  what the ``Alert.state`` vocabulary is for; layer 1 records only its transitions
  (``ENTER`` / ``EXIT``), as the spec names only those two.
* ``news=...`` passed while layer 2 is off is ignored, not an error, so an ablation can
  be built with ``dataclasses.replace(params, layers=...)`` without clearing ``news``.
  Layer 2 being *on* without ``NewsParams`` is a ``ValueError``.
* A non-finite asset return or reference return is "no signal" for that quantity (fail
  open, like ``sigma_hat == 0``); a non-finite ``nav`` is a ``ValueError`` because it
  would poison the running maximum forever.
* ``news=None`` on a session (no evidence source at all) does not advance the layer-2
  machine -- no trigger, no exit, no cooldown decrement -- but a trim already in force
  stands: a missing input is the extreme of "missing evidence", which never exits.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

_LAYER_STOP_LOSS = 1
_LAYER_NEWS = 2
_LAYER_VOL = 3
_LAYER_DD = 4
_LAYER_IDS = frozenset({_LAYER_STOP_LOSS, _LAYER_NEWS, _LAYER_VOL, _LAYER_DD})
_MIN_VOL_WINDOW = 2  # ddof=1 needs two observations

_L1_STOP_LOSS = "L1_STOP_LOSS"
_L1_REENTRY = "L1_REENTRY"
_L2_NEWS = "L2_NEWS"
_L2_NEWS_EXIT = "L2_NEWS_EXIT"
_L3_VOL = "L3_VOL"
_L4_DD = "L4_DD"

_ENTER = "ENTER"
_EXIT = "EXIT"
_ACTIVE = "ACTIVE"
_COOLDOWN = "COOLDOWN"
_SCALE = "SCALE"


# -- parameters ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class NewsParams:
    """Layer-2 (news) parameters, frozen before any window run.

    ``k`` is the look-back window in sessions (the caller builds the count on
    ``(close of t-k, close of t]`` and deduplicates by URL); ``tau`` is the entry
    negative-ratio threshold, ``tau_exit`` the hysteresis exit threshold, ``n_min`` the
    minimum number of articles for any decision, ``cooldown`` the sessions a name stays
    trimmed after an exit, and ``trim_fraction`` the fraction of the reference weight
    moved to cash while the alert is in force (``1.0`` = the whole position).
    """

    k: int
    tau: float
    tau_exit: float
    n_min: int
    cooldown: int
    trim_fraction: float = 1.0


@dataclass(frozen=True)
class GuardParams:
    """GUARD parameters of one arm. ``layers`` is a subset of ``{1, 2, 3, 4}``.

    Layer 1 uses ``J``, ``gamma`` and ``delta`` (positive fractions; ``delta >= 0``);
    layer 3 ``sigma_star`` (annualised) and ``vol_window``; layer 4 ``m`` and
    ``alpha``; ``periods_per_year`` annualises the layer-3 volatility. ``news`` is
    required when, and only used when, layer 2 is enabled.
    """

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

    def __post_init__(self) -> None:
        _validate_params(self)


@dataclass(frozen=True)
class NewsCounts:
    """Article counts of one asset in the caller's window, unique by URL."""

    n_articles: int
    n_negative: int


# -- inputs and outputs -------------------------------------------------------------------------


@dataclass(frozen=True)
class GuardInput:
    """One session's inputs, all ending at the close of ``date``.

    ``reference_weights`` are the NOGUARD twin's weights for t+1 (equities only, sum at
    most 1); ``asset_returns`` holds simple returns (oldest first) for every asset in
    ``reference_weights`` and every asset currently out; ``reference_returns`` the last
    ``vol_window`` simple returns of the reference book held fixed; ``nav`` this GUARD
    arm's NAV at the close of t.
    """

    date: str
    reference_weights: Mapping[int, float]
    asset_returns: Mapping[int, Sequence[float]]
    reference_returns: Sequence[float]
    nav: float
    news: Mapping[int, NewsCounts] | None = None


@dataclass(frozen=True)
class Alert:
    """One alert of the session: a state change, an ongoing trim, or an active scaler.

    ``value`` is the measured quantity (``S`` for the stop-loss, the latest return for
    the re-entry, ``q`` for news, ``s_sigma`` / ``s_dd`` for the scalers) and
    ``threshold`` the parameter it was compared with. ``asset_id`` is ``None`` for the
    portfolio-wide layers 3 and 4.
    """

    layer: int
    asset_id: int | None
    rule_id: str
    value: float
    threshold: float
    state: str


@dataclass(frozen=True)
class _L2State:
    """Layer-2 machine of one asset: an active trim, or a cooldown with sessions left."""

    active: bool
    remaining: int = 0

    def in_force(self) -> bool:
        return self.active or self.remaining > 0


_INACTIVE = _L2State(active=False)


@dataclass(frozen=True)
class GuardState:
    """Opaque, immutable GUARD state carried between sessions.

    Construct it with :func:`initial_state` and advance it only through
    :func:`evaluate`; nothing else in the repository should read its fields.
    ``l1_out`` are the names currently out by layer 1, ``l2`` the layer-2 machines (only
    entries that are active or cooling down, sorted by asset id) and ``nav_max`` the
    running maximum of the NAV since formation.
    """

    l1_out: frozenset[int] = frozenset()
    l2: tuple[tuple[int, _L2State], ...] = ()
    nav_max: float | None = None


@dataclass(frozen=True)
class GuardOutput:
    """One session's GUARD decision for t+1.

    ``weights`` are the post-GUARD equity weights (zeros omitted; cash is
    ``1 - sum(weights)`` at the caller), ``cut_assets`` the names at zero because of
    layers 1 and 2, ``scale`` ``min(1, s_sigma, s_dd)``, and ``alerts`` this session's
    state changes, ongoing trims and active scalers. The output never mutates the
    input; the returned ``weights`` dict is new and belongs to the caller.
    """

    state: GuardState
    weights: dict[int, float]
    cut_assets: frozenset[int]
    scale: float
    s_sigma: float | None
    s_dd: float | None
    alerts: tuple[Alert, ...]


def initial_state() -> GuardState:
    """The empty state of a GUARD arm at formation (nothing out, no drawdown high yet)."""
    return GuardState()


# -- validation ---------------------------------------------------------------------------------


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _validate_params(params: GuardParams) -> None:
    """Reject a parameter set that would silently produce a meaningless decision.

    The spec fixes ``J >= 1``, ``0 < alpha < 1``, ``m > 0`` and "news iff layer 2"; the
    remaining checks only cover inputs whose mathematical meaning is undefined
    (``vol_window < 2`` with ``ddof=1``, a non-finite threshold, a layer id outside
    ``{1, 2, 3, 4}``).
    """
    unknown = sorted(set(params.layers) - _LAYER_IDS)
    _require(
        not unknown, f"GuardParams.layers has unknown layer ids {unknown}; allowed: [1, 2, 3, 4]"
    )
    _require(params.J >= 1, f"GuardParams.J must be >= 1, got {params.J!r}")
    _require(
        math.isfinite(params.gamma) and params.gamma > 0.0,
        f"GuardParams.gamma must be a finite positive fraction, got {params.gamma!r}",
    )
    _require(
        math.isfinite(params.delta) and params.delta >= 0.0,
        f"GuardParams.delta must be a finite non-negative fraction, got {params.delta!r}",
    )
    _require(
        math.isfinite(params.sigma_star) and params.sigma_star >= 0.0,
        f"GuardParams.sigma_star must be a finite non-negative annualised volatility, "
        f"got {params.sigma_star!r}",
    )
    _require(
        math.isfinite(params.m) and params.m > 0.0,
        f"GuardParams.m must be a finite positive multiplier, got {params.m!r}",
    )
    _require(
        math.isfinite(params.alpha) and 0.0 < params.alpha < 1.0,
        f"GuardParams.alpha must be in (0, 1), got {params.alpha!r}",
    )
    _require(
        params.vol_window >= _MIN_VOL_WINDOW,
        f"GuardParams.vol_window must be >= {_MIN_VOL_WINDOW}, got {params.vol_window!r}",
    )
    _require(
        params.periods_per_year >= 1,
        f"GuardParams.periods_per_year must be >= 1, got {params.periods_per_year!r}",
    )
    if _LAYER_NEWS in params.layers:
        _require(
            params.news is not None,
            "GuardParams.news is required when layer 2 is enabled",
        )
    if params.news is not None:
        _validate_news_params(params.news)


def _validate_news_params(news: NewsParams) -> None:
    _require(news.k >= 1, f"NewsParams.k must be >= 1, got {news.k!r}")
    _require(
        math.isfinite(news.tau) and 0.0 <= news.tau <= 1.0,
        f"NewsParams.tau must be in [0, 1], got {news.tau!r}",
    )
    _require(
        math.isfinite(news.tau_exit) and 0.0 <= news.tau_exit <= 1.0,
        f"NewsParams.tau_exit must be in [0, 1], got {news.tau_exit!r}",
    )
    _require(news.n_min >= 1, f"NewsParams.n_min must be >= 1, got {news.n_min!r}")
    _require(news.cooldown >= 0, f"NewsParams.cooldown must be >= 0, got {news.cooldown!r}")
    _require(
        math.isfinite(news.trim_fraction) and 0.0 <= news.trim_fraction <= 1.0,
        f"NewsParams.trim_fraction must be in [0, 1], got {news.trim_fraction!r}",
    )


def _reference_weights(reference: Mapping[int, float]) -> dict[int, float]:
    """Positive, finite reference weights, as a new dict (the caller's mapping is read only)."""
    out: dict[int, float] = {}
    for asset_id, weight in reference.items():
        value = float(weight)
        _require(
            math.isfinite(value) and value >= 0.0,
            f"reference_weights[{asset_id}] = {value!r} is not a finite non-negative weight",
        )
        if value > 0.0:
            out[int(asset_id)] = value
    return out


# -- layers ------------------------------------------------------------------------------------


def _layer1(
    l1_out: frozenset[int],
    reference: Mapping[int, float],
    asset_returns: Mapping[int, Sequence[float]],
    params: GuardParams,
) -> tuple[frozenset[int], list[Alert]]:
    """K-L transitions for the names held and for every name currently out.

    An asset with fewer than ``J`` returns gives no signal either way and keeps its
    state. A non-finite sum or latest return fails the comparisons, i.e. no signal.
    """
    out = set(l1_out)
    alerts: list[Alert] = []
    for asset_id in sorted(set(reference) | out):
        history = asset_returns.get(asset_id)
        if history is None or len(history) < params.J:
            continue
        window = [float(value) for value in history[-params.J :]]
        total = math.fsum(window)
        latest = window[-1]
        if asset_id in out:
            if latest >= params.delta:
                out.discard(asset_id)
                alerts.append(
                    Alert(
                        layer=_LAYER_STOP_LOSS,
                        asset_id=asset_id,
                        rule_id=_L1_REENTRY,
                        value=latest,
                        threshold=params.delta,
                        state=_EXIT,
                    )
                )
        elif total < -params.gamma:
            out.add(asset_id)
            alerts.append(
                Alert(
                    layer=_LAYER_STOP_LOSS,
                    asset_id=asset_id,
                    rule_id=_L1_STOP_LOSS,
                    value=total,
                    threshold=-params.gamma,
                    state=_ENTER,
                )
            )
    return frozenset(out), alerts


def _news_evidence(counts: NewsCounts | None, n_min: int) -> tuple[float, bool]:
    """The negative ratio ``q`` (0 with no articles) and whether the evidence suffices."""
    if counts is None or counts.n_articles <= 0:
        return 0.0, False
    return counts.n_negative / counts.n_articles, counts.n_articles >= n_min


def _layer2(
    l2_state: tuple[tuple[int, _L2State], ...],
    reference: Mapping[int, float],
    news: Mapping[int, NewsCounts],
    news_params: NewsParams,
) -> tuple[tuple[tuple[int, _L2State], ...], frozenset[int], list[Alert]]:
    """Advance every layer-2 machine over the names held and the names it still tracks."""
    known = dict(l2_state)
    updated: dict[int, _L2State] = {}
    trimmed: set[int] = set()
    alerts: list[Alert] = []
    for asset_id in sorted(set(reference) | set(known)):
        previous = known.get(asset_id, _INACTIVE)
        q, enough = _news_evidence(news.get(asset_id), news_params.n_min)
        triggered = enough and q > news_params.tau
        if previous.active:
            trimmed.add(asset_id)
            if enough and q <= news_params.tau_exit:
                updated[asset_id] = _L2State(active=False, remaining=news_params.cooldown)
                alerts.append(
                    Alert(
                        layer=_LAYER_NEWS,
                        asset_id=asset_id,
                        rule_id=_L2_NEWS_EXIT,
                        value=q,
                        threshold=news_params.tau_exit,
                        state=_EXIT,
                    )
                )
            else:
                updated[asset_id] = previous
                alerts.append(
                    Alert(
                        layer=_LAYER_NEWS,
                        asset_id=asset_id,
                        rule_id=_L2_NEWS,
                        value=q,
                        threshold=news_params.tau,
                        state=_ACTIVE,
                    )
                )
        elif previous.remaining > 0:
            trimmed.add(asset_id)
            if triggered:
                updated[asset_id] = _L2State(active=True)
                alerts.append(
                    Alert(
                        layer=_LAYER_NEWS,
                        asset_id=asset_id,
                        rule_id=_L2_NEWS,
                        value=q,
                        threshold=news_params.tau,
                        state=_ENTER,
                    )
                )
            else:
                updated[asset_id] = _L2State(active=False, remaining=previous.remaining - 1)
                alerts.append(
                    Alert(
                        layer=_LAYER_NEWS,
                        asset_id=asset_id,
                        rule_id=_L2_NEWS,
                        value=q,
                        threshold=news_params.tau_exit,
                        state=_COOLDOWN,
                    )
                )
        elif triggered:
            trimmed.add(asset_id)
            updated[asset_id] = _L2State(active=True)
            alerts.append(
                Alert(
                    layer=_LAYER_NEWS,
                    asset_id=asset_id,
                    rule_id=_L2_NEWS,
                    value=q,
                    threshold=news_params.tau,
                    state=_ENTER,
                )
            )
        # else: inactive and not triggered -> nothing to record.
    state = tuple(
        sorted((asset_id, machine) for asset_id, machine in updated.items() if machine.in_force())
    )
    return state, frozenset(trimmed), alerts


def _layer3(reference_returns: Sequence[float], params: GuardParams) -> tuple[float, Alert | None]:
    """Moreira-Muir scaling of the reference book; 1.0 (no alert) on a degenerate estimate."""
    if len(reference_returns) < params.vol_window:
        return 1.0, None
    window = np.asarray(reference_returns[-params.vol_window :], dtype=float)
    sigma_hat = float(np.std(window, ddof=1)) * math.sqrt(params.periods_per_year)
    if not math.isfinite(sigma_hat) or sigma_hat <= 0.0:
        return 1.0, None
    scale = min(1.0, params.sigma_star / sigma_hat)
    if scale >= 1.0:
        return 1.0, None
    return scale, Alert(
        layer=_LAYER_VOL,
        asset_id=None,
        rule_id=_L3_VOL,
        value=scale,
        threshold=1.0,
        state=_SCALE,
    )


def _layer4(nav: float, nav_max: float, params: GuardParams) -> tuple[float, Alert | None]:
    """Grossman-Zhou drawdown scaling of this arm's NAV; 0.0 at ruin, never negative."""
    if nav <= 0.0:
        scale = 0.0
    else:
        scale = min(1.0, max(0.0, params.m * (nav - params.alpha * nav_max) / nav))
    if scale >= 1.0:
        return 1.0, None
    return scale, Alert(
        layer=_LAYER_DD,
        asset_id=None,
        rule_id=_L4_DD,
        value=scale,
        threshold=1.0,
        state=_SCALE,
    )


def _combine(
    reference: Mapping[int, float],
    l1_out: frozenset[int],
    l2_trimmed: frozenset[int],
    trim_fraction: float,
    scale: float,
) -> tuple[dict[int, float], frozenset[int]]:
    """Apply the cuts, then the scalar, to the reference weights.

    A name is cut by layer 1 (weight 0) or trimmed by layer 2 (``trim_fraction`` of its
    reference weight to cash), and every surviving weight is multiplied by ``scale``;
    therefore no weight exceeds its reference weight. ``cut_assets`` holds the names at
    zero *because of layers 1 and 2* only.
    """
    weights: dict[int, float] = {}
    cut: set[int] = set()
    for asset_id in sorted(reference):
        weight = reference[asset_id]
        if asset_id in l1_out:
            cut.add(asset_id)
            continue
        if asset_id in l2_trimmed:
            weight *= 1.0 - trim_fraction
            if weight <= 0.0:
                cut.add(asset_id)
                continue
        weight *= scale
        if weight > 0.0:
            weights[asset_id] = weight
    return weights, frozenset(cut)


# -- the entry point ----------------------------------------------------------------------------


def evaluate(state: GuardState, inp: GuardInput, params: GuardParams) -> GuardOutput:
    """Evaluate GUARD at the close of ``inp.date`` and return the target for the next session.

    ``state`` is never mutated (a new :class:`GuardState` is returned) and neither is
    ``inp``. Layers absent from ``params.layers`` neither alert nor act.
    """
    _require(isinstance(state, GuardState), "state must be a cycle.guard.GuardState")
    _validate_params(params)
    _require(math.isfinite(inp.nav), f"GuardInput.nav must be finite, got {inp.nav!r}")
    reference = _reference_weights(inp.reference_weights)
    alerts: list[Alert] = []

    l1_out = state.l1_out
    if _LAYER_STOP_LOSS in params.layers:
        l1_out, layer1_alerts = _layer1(state.l1_out, reference, inp.asset_returns, params)
        alerts.extend(layer1_alerts)

    l2_state = state.l2
    l2_trimmed: frozenset[int] = frozenset()
    if _LAYER_NEWS in params.layers and params.news is not None:
        if inp.news is None:
            # No evidence source at all this session: the machine does not advance (no
            # trigger, no exit, no cooldown decrement) but a trim already in force stands.
            l2_trimmed = frozenset(asset_id for asset_id, machine in state.l2 if machine.in_force())
        else:
            l2_state, l2_trimmed, layer2_alerts = _layer2(
                state.l2, reference, inp.news, params.news
            )
            alerts.extend(layer2_alerts)

    s_sigma: float | None = None
    if _LAYER_VOL in params.layers:
        s_sigma, vol_alert = _layer3(inp.reference_returns, params)
        if vol_alert is not None:
            alerts.append(vol_alert)

    nav_max = inp.nav if state.nav_max is None else max(state.nav_max, inp.nav)
    s_dd: float | None = None
    if _LAYER_DD in params.layers:
        s_dd, dd_alert = _layer4(inp.nav, nav_max, params)
        if dd_alert is not None:
            alerts.append(dd_alert)

    scale = min(1.0, s_sigma if s_sigma is not None else 1.0, s_dd if s_dd is not None else 1.0)
    trim_fraction = params.news.trim_fraction if params.news is not None else 0.0
    weights, cut_assets = _combine(
        reference,
        l1_out if _LAYER_STOP_LOSS in params.layers else frozenset(),
        l2_trimmed if _LAYER_NEWS in params.layers else frozenset(),
        trim_fraction,
        scale,
    )
    return GuardOutput(
        state=GuardState(l1_out=l1_out, l2=l2_state, nav_max=nav_max),
        weights=weights,
        cut_assets=cut_assets,
        scale=scale,
        s_sigma=s_sigma,
        s_dd=s_dd,
        alerts=tuple(alerts),
    )
