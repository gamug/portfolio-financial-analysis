"""Basing Points stop machines (Edwards, Magee & Bassetti 2007, pp. 441-447).

Pure: no database, no I/O, the inputs are never mutated. The machines are the layer-1
alternative of round 4's ``stop_rule`` parameter; ``kl`` keeps the Kaminski & Lo rule in
:mod:`cycle.guard.core`. One machine per satellite name, advanced after the close of a
session; its decision applies to the next session.

Bassetti's codification (pp. 446-447) of Magee's procedure (pp. 441-443):

* the running high ``H`` since the tracking start, and its session ``h``;
* the candidate low ``L``: the lowest low strictly after ``max(h, b)`` (``b`` = the last
  Basing Point session, the tracking start if none); a strictly lower low replaces it,
  ties keep the earlier session;
* *away days*: sessions after ``L`` whose low is above the high of ``L``; three of them
  (not necessarily consecutive) establish a Basing Point at ``L`` (rule 3);
* rule 4 (``bp4`` only): a peak is recognized at the first session after a new running
  high whose high does not exceed it; a close above ``1.03 x`` that peak's high
  establishes a Basing Point at the current session's low, once per peak;
* stops only move up.

The initial stop is set when the name enters the reference (``entry``): the latest
Basing Point in ``(s0, e]`` times ``(1 - d)``; if there is none, or that level is at or
above the entry close, the fallback is the lowest low of the 21 sessions ending at ``e``
times ``(1 - d)`` (declared, no reference). The tracking start is 63 sessions before
the entry (declared).

A session without a bar neither updates the machine nor triggers (fail open); the
caller counts it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final

#: The entry-tracking look-back (declared; no reference).
TRACKING_SESSIONS: Final = 63
#: The fallback initial stop's look-back (declared).
FALLBACK_SESSIONS: Final = 21
#: Rule 4's close-versus-peak threshold.
RULE4_THRESHOLD: Final = 1.03
#: The three away days of rule 3.
AWAY_DAYS: Final = 3
#: The price-class boundaries (> 100, 40-100, 20-<40, 10-<20, 5-<10, < 5).
PRICE_BOUNDS: Final = (100.0, 40.0, 20.0, 10.0, 5.0)
#: The sensitivity-class boundaries: conservative < 0.75, median 0.75-1.25, speculative > 1.25.
SENSITIVITY_BOUNDS: Final = (0.75, 1.25)

STOP_RULES: Final = ("kl", "bp3", "bp4")
BP_RULES: Final = ("bp3", "bp4")

#: The Table of Stop Distances (p. 437): rows are price classes, columns sensitivity.
#: Each row is (conservative, median, speculative).
_DISTANCE_TABLE: Final[tuple[tuple[float, float, float], ...]] = (
    (0.05, 0.05, 0.05),  # > 100
    (0.05, 0.05, 0.06),  # 40 - 100
    (0.05, 0.05, 0.08),  # 20 - < 40
    (0.05, 0.06, 0.10),  # 10 - < 20
    (0.05, 0.07, 0.12),  # 5 - < 10
    (0.05, 0.10, 0.15),  # < 5
)
#: The price-class labels, in table order (for the per-decision cell counts).
PRICE_CLASSES: Final[tuple[str, ...]] = (
    ">100",
    "40-100",
    "20-<40",
    "10-<20",
    "5-<10",
    "<5",
)
#: The sensitivity-class labels, in table order.
SENSITIVITY_CLASSES: Final[tuple[str, ...]] = ("conservative", "median", "speculative")


@dataclass(frozen=True)
class Bar:
    """One session's high, low and close (split-adjusted, as stored)."""

    high: float
    low: float
    close: float


@dataclass(frozen=True)
class BasingPointState:
    """One name's machine at the close of a session (immutable)."""

    entry_session: str
    running_high: float
    running_high_session: str
    candidate_low: float | None
    candidate_high: float | None
    candidate_session: str | None
    away_days: int
    last_bp_session: str
    last_bp_low: float | None
    stop: float
    peak_high: float | None
    peak_recognized: bool
    peak_used: bool
    out: bool
    fallback: bool


@dataclass(frozen=True)
class BasingPointEvent:
    """One reporting event of the machine (not a GUARD alert by itself)."""

    session: str
    kind: str  # entry | fallback | bp3 | bp4 | exit
    value: float
    asset_id: int | None = None


def price_class(price: float) -> str:
    """The Table of Stop Distances' price row for *price* (boundaries inclusive at the low)."""
    if not math.isfinite(price) or price <= 0.0:
        raise ValueError(f"price must be a finite positive number, got {price!r}")
    if price > PRICE_BOUNDS[0]:
        return PRICE_CLASSES[0]
    for index, bound in enumerate(PRICE_BOUNDS[1:], start=1):
        if price >= bound:
            return PRICE_CLASSES[index]
    return PRICE_CLASSES[5]


def sensitivity_class(sensitivity: float) -> str:
    """The table's column: conservative (< 0.75), median (0.75-1.25), speculative (> 1.25)."""
    if not math.isfinite(sensitivity):
        raise ValueError(f"sensitivity must be finite, got {sensitivity!r}")
    if sensitivity < SENSITIVITY_BOUNDS[0]:
        return SENSITIVITY_CLASSES[0]
    if sensitivity <= SENSITIVITY_BOUNDS[1]:
        return SENSITIVITY_CLASSES[1]
    return SENSITIVITY_CLASSES[2]


def stop_distance(price: float, sensitivity: float) -> float:
    """The stop distance ``d`` from the table (fraction of the low, e.g. 0.05)."""
    row = PRICE_CLASSES.index(price_class(price))
    column = SENSITIVITY_CLASSES.index(sensitivity_class(sensitivity))
    return _DISTANCE_TABLE[row][column]


def distance_cell(price: float, sensitivity: float) -> str:
    """The ``price-class|sensitivity-class`` label, for the per-decision cell counts."""
    return f"{price_class(price)}|{sensitivity_class(sensitivity)}"


def _check_bar(bar: Bar) -> None:
    for name, value in (("high", bar.high), ("low", bar.low), ("close", bar.close)):
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(f"bar.{name} must be a finite positive price, got {value!r}")
    if bar.low > bar.high:
        raise ValueError(f"bar low {bar.low!r} > high {bar.high!r}")
    if not bar.low <= bar.close <= bar.high:
        raise ValueError(f"bar close {bar.close!r} is outside [low {bar.low!r}, high {bar.high!r}]")


def _initial_machine(  # noqa: C901, PLR0912, PLR0915 - one linear rules transcript
    sessions: Sequence[str],
    bars: Sequence[Bar | None],
    *,
    entry_session: str,
    distance: float,
    rule: str,
) -> tuple[BasingPointState, list[BasingPointEvent]]:
    """Build the machine at entry from the bars ending at ``entry_session``.

    The bars are the tracking window ``s0..e`` (oldest first, ``None`` for a missing
    session). The latest Basing Point in ``(s0, e]`` sets the initial stop; with none,
    or a level at/above the entry close, the fallback low of the last 21 sessions is used.
    """
    if not 0.0 <= distance < 1.0:
        raise ValueError(f"distance must be in [0, 1), got {distance!r}")
    if len(sessions) != len(bars):
        raise ValueError("sessions and bars must have the same length")
    if not bars:
        raise ValueError("an entry needs at least one bar")
    running_high = -math.inf
    running_session = sessions[0]
    candidate_low = candidate_high = None
    candidate_session = None
    away = 0
    last_bp_session = sessions[0]
    last_bp_low = None
    peak_high: float | None = None
    peak_recognized = False
    peak_used = False
    latest_bp: tuple[str, float] | None = None
    for session, bar in zip(sessions, bars, strict=True):
        if bar is None:
            continue
        _check_bar(bar)
        # (i) recognize a pending peak on a session that does not make a new high
        if running_high > -math.inf and bar.high <= running_high and not peak_recognized:
            peak_high = running_high
            peak_recognized = True
            peak_used = False
        # (ii) rule 4 against the recognized peak's stored high
        if (
            rule == "bp4"
            and peak_high is not None
            and not peak_used
            and bar.close > RULE4_THRESHOLD * peak_high
        ):
            latest_bp = (session, bar.low)
            last_bp_session = session
            last_bp_low = bar.low
            peak_used = True
            candidate_low = candidate_high = None
            candidate_session = None
            away = 0
        # (iii) update the running high (a new pending peak; the recognized level stays)
        if bar.high > running_high:
            running_high = bar.high
            running_session = session
            peak_recognized = False
        # (iv) candidate low / away days / rule 3
        after = running_session < session and last_bp_session < session
        if after and (candidate_low is None or bar.low < candidate_low):
            candidate_low = bar.low
            candidate_high = bar.high
            candidate_session = session
            away = 0
        elif (
            candidate_high is not None
            and candidate_session is not None
            and session > candidate_session
            and bar.low > candidate_high
        ):
            away += 1
            if away >= AWAY_DAYS:
                if candidate_low is None:
                    raise ValueError("candidate high without a candidate low")
                latest_bp = (session, candidate_low)
                last_bp_session = session
                last_bp_low = candidate_low
                candidate_low = candidate_high = None
                candidate_session = None
                away = 0
    close_e = bars[-1].close if bars[-1] is not None else None
    if close_e is None:
        close_e = next((bar.close for bar in reversed(bars) if bar is not None), None)
    stop = 0.0
    fallback = False
    events: list[BasingPointEvent] = []
    if latest_bp is not None:
        stop = latest_bp[1] * (1.0 - distance)
    if latest_bp is None or (close_e is not None and stop >= close_e):
        fallback = True
        lows = [bar.low for bar in bars[-FALLBACK_SESSIONS:] if bar is not None]
        stop = (min(lows) * (1.0 - distance)) if lows else 0.0
        events.append(BasingPointEvent(session=entry_session, kind="fallback", value=stop))
    events.append(BasingPointEvent(session=entry_session, kind="entry", value=stop))
    return (
        BasingPointState(
            entry_session=entry_session,
            running_high=running_high if running_high > -math.inf else 0.0,
            running_high_session=running_session,
            candidate_low=candidate_low,
            candidate_high=candidate_high,
            candidate_session=candidate_session,
            away_days=away,
            last_bp_session=last_bp_session,
            last_bp_low=last_bp_low,
            stop=stop,
            peak_high=peak_high,
            peak_recognized=peak_recognized,
            peak_used=peak_used,
            out=False,
            fallback=fallback,
        ),
        events,
    )


def establish(
    sessions: Sequence[str],
    bars: Sequence[Bar | None],
    *,
    entry_session: str,
    distance: float,
    rule: str,
) -> tuple[BasingPointState, list[BasingPointEvent]]:
    """Public entry: the machine of a name first seen in the reference at *entry_session*."""
    if rule not in BP_RULES:
        raise ValueError(f"rule must be one of {BP_RULES}, got {rule!r}")
    if sessions[-1] != entry_session:
        raise ValueError(f"the last session {sessions[-1]!r} is not the entry {entry_session!r}")
    return _initial_machine(
        sessions, bars, entry_session=entry_session, distance=distance, rule=rule
    )


def advance(  # noqa: C901 - one linear rules transcript
    state: BasingPointState,
    session: str,
    bar: Bar,
    *,
    distance: float,
    rule: str,
) -> tuple[BasingPointState, list[BasingPointEvent]]:
    """Advance one session (the bar exists); returns the new state and the events."""
    if rule not in BP_RULES:
        raise ValueError(f"rule must be one of {BP_RULES}, got {rule!r}")
    _check_bar(bar)
    if state.out:
        return state, []
    events: list[BasingPointEvent] = []
    machine = state
    # (i) recognize a pending peak on a session that does not make a new high
    if (
        machine.running_high > 0.0
        and bar.high <= machine.running_high
        and not machine.peak_recognized
    ):
        machine = replace(
            machine,
            peak_high=machine.running_high,
            peak_recognized=True,
            peak_used=False,
        )
    # (ii) rule 4 against the recognized peak's stored high
    if (
        rule == "bp4"
        and machine.peak_high is not None
        and not machine.peak_used
        and bar.close > RULE4_THRESHOLD * machine.peak_high
    ):
        stop = max(machine.stop, bar.low * (1.0 - distance))
        events.append(BasingPointEvent(session=session, kind="bp4", value=bar.low))
        machine = replace(
            machine,
            stop=stop,
            last_bp_session=session,
            last_bp_low=bar.low,
            peak_used=True,
            candidate_low=None,
            candidate_high=None,
            candidate_session=None,
            away_days=0,
        )
    # (iii) update the running high (a new pending peak; the recognized level stays)
    if bar.high > machine.running_high:
        machine = replace(
            machine,
            running_high=bar.high,
            running_high_session=session,
            peak_recognized=False,
        )
    # (iv) candidate low / away days / rule 3
    after = machine.running_high_session < session and machine.last_bp_session < session
    if after and (machine.candidate_low is None or bar.low < machine.candidate_low):
        machine = replace(
            machine,
            candidate_low=bar.low,
            candidate_high=bar.high,
            candidate_session=session,
            away_days=0,
        )
    elif (
        machine.candidate_high is not None
        and machine.candidate_session is not None
        and session > machine.candidate_session
        and bar.low > machine.candidate_high
    ):
        away = machine.away_days + 1
        machine = replace(machine, away_days=away)
        if away >= AWAY_DAYS:
            candidate_low = machine.candidate_low
            if candidate_low is None:  # a candidate high always has a low
                raise ValueError("candidate high without a candidate low")
            stop = max(machine.stop, candidate_low * (1.0 - distance))
            events.append(BasingPointEvent(session=session, kind="bp3", value=candidate_low))
            machine = replace(
                machine,
                stop=stop,
                last_bp_session=session,
                last_bp_low=candidate_low,
                candidate_low=None,
                candidate_high=None,
                candidate_session=None,
                away_days=0,
            )
    if bar.close < machine.stop:
        machine = replace(machine, out=True)
        events.append(BasingPointEvent(session=session, kind="exit", value=machine.stop))
    return machine, events


def reseed(
    sessions: Sequence[str],
    bars: Sequence[Bar | None],
    *,
    entry_session: str,
    distance: float,
    rule: str,
) -> tuple[BasingPointState, list[BasingPointEvent]]:
    """A re-entry at a decision date: a fresh machine, exactly as a first entry."""
    return establish(sessions, bars, entry_session=entry_session, distance=distance, rule=rule)
