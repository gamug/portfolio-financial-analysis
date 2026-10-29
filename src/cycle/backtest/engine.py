"""Pure daily simulation of the backtest arms (BACKTEST_SPEC §0, §3, §4).

No database, no I/O. The engine walks the calendar sessions once, in order, and holds one
independent book per arm:

* a **Markowitz book** (``MARKOWITZ_BASE``) rebalances to its target at the close of each
  scheduled execution session and drifts with the market in between;
* a **NOGUARD book** (``TOPN_MV_NOGUARD``) does the same from the cycle's Top-30 target, and
  records the GUARD alerts it *would* have raised as ``ARM_DISABLED`` (a shadow
  :class:`cycle.guard.GuardState`, never applied);
* a **GUARD book** (``TOPN_MV_GUARD`` / ``TOPN_MV_GUARD_NO_NLP``) is an *overlay on its
  NOGUARD twin*: at every close its reference weights are the twin's weights for the next
  session, and it orders only when the GUARD target differs from what it already holds
  (``|Δw| > 1e-12``). With no alert it is identical to the twin and never trades on its own.

Timing (spec §3): the decision is formed at the close of ``t``; the order executes at the
close of ``t+1``; the return of ``t+1`` is earned by the old weights; cost is
``cost_bps x sum |w_target - w_drifted|`` over assets only (cash never pays), one net order per
session. A held asset with no return on a session is valued at its last close and moved to
cash at that close. Cash earns the session's risk-free rate.

All arithmetic is plain ``float``; the module never imports numpy (there is no numerical
work beyond sums), never touches the database and never mutates its inputs.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from cycle.guard import (
    Alert,
    GuardInput,
    GuardParams,
    GuardState,
    NewsCounts,
    evaluate,
    initial_state,
)

#: The four arms fixed by BACKTEST_SPEC §0, in persistence order.
ARM_MARKOWITZ_BASE = "MARKOWITZ_BASE"
ARM_TOPN_NOGUARD = "TOPN_MV_NOGUARD"
ARM_TOPN_GUARD = "TOPN_MV_GUARD"
ARM_TOPN_GUARD_NO_NLP = "TOPN_MV_GUARD_NO_NLP"
ARMS: tuple[str, ...] = (
    ARM_MARKOWITZ_BASE,
    ARM_TOPN_NOGUARD,
    ARM_TOPN_GUARD,
    ARM_TOPN_GUARD_NO_NLP,
)
#: The equal-weight benchmark is context, not an arm.
BENCHMARK = "SP500_EW_INTERNAL"

#: An order is formed only when a weight moves by more than this (spec §4).
WEIGHT_TOL = 1e-12
#: The news layer's number in ``GuardParams.layers``.
NEWS_LAYER = 2

Weights = Mapping[int, float]


class EngineError(RuntimeError):
    """The engine's inputs are inconsistent (a target that cannot be a book, a missing date)."""


@dataclass(frozen=True)
class ArmSpec:
    """One book to simulate.

    ``target_source`` is the arm code whose Markowitz targets drive this book: the two GUARD
    arms read their NOGUARD twin's targets. ``guard`` is set on a GUARD arm; ``shadow_guard``
    on a NOGUARD arm records the alerts its twin would have raised, never applied.
    """

    code: str
    target_source: str
    guard: GuardParams | None = None
    shadow_guard: GuardParams | None = None

    def __post_init__(self) -> None:
        if self.guard is not None and self.shadow_guard is not None:
            raise EngineError(f"{self.code}: guard and shadow_guard are mutually exclusive")


@dataclass(frozen=True)
class EngineInput:
    """Everything the simulation needs, already resolved from the database by the driver.

    ``sessions`` are NYSE sessions in ascending order; ``returns`` maps each session to
    ``{asset_id: simple total return}`` (a missing asset is a data gap, not a zero);
    ``rf`` maps each session to that session's daily risk-free fraction. ``targets`` maps an
    arm code to ``{decision_date: {asset_id: weight}}`` -- the book's Markowitz target formed
    at that decision date. ``benchmark_members`` maps each session to the asset ids that were
    members at the previous close (the EW benchmark's universe).
    """

    sessions: Sequence[str]
    returns: Mapping[str, Mapping[int, float]]
    rf: Mapping[str, float]
    targets: Mapping[str, Mapping[str, Mapping[int, float]]]
    arms: Sequence[ArmSpec]
    formation_date: str
    decision_dates: Sequence[str]
    cost_bps: float = 10.0
    benchmark_members: Mapping[str, Sequence[int]] = field(default_factory=dict)
    news: Mapping[str, Mapping[int, NewsCounts]] = field(default_factory=dict)


@dataclass(frozen=True)
class DayWeights:
    """One book's weights at the close of a session, i.e. the book held into the next one."""

    date: str
    markowitz_target: dict[int, float]
    drifted: dict[int, float]
    pre_guard: dict[int, float]
    final_target: dict[int, float]
    cash_weight: float
    n_equity: int


@dataclass(frozen=True)
class DayPerformance:
    """One book's realized session: the return earned by the weights held into it."""

    date: str
    gross_return: float
    turnover: float
    cost_return: float
    net_return: float
    nav: float
    drawdown: float
    cash_weight: float
    n_equity: int
    benchmark_return: float


@dataclass(frozen=True)
class Execution:
    """A net order executed at the close of *date*."""

    date: str
    turnover: float
    cost_return: float
    target: dict[int, float]


@dataclass(frozen=True)
class GuardEvent:
    """One alert and the action this book took on it."""

    date: str
    alert: Alert
    status: str  # APPLIED | ARM_DISABLED | NOT_HELD | SUPERSEDED
    weight_before: float
    weight_after: float


@dataclass(frozen=True)
class ArmResult:
    """Everything one arm produced, in session order."""

    code: str
    weights: tuple[DayWeights, ...]
    performance: tuple[DayPerformance, ...]
    executions: tuple[Execution, ...]
    guard_events: tuple[GuardEvent, ...]


@dataclass(frozen=True)
class EngineResult:
    """The four books plus the EW benchmark series (context only)."""

    arms: Mapping[str, ArmResult]
    benchmark: Mapping[str, float]
    formation_date: str


def _validate_target(target: Weights, label: str) -> dict[int, float]:
    out: dict[int, float] = {}
    for asset_id, weight in target.items():
        w = float(weight)
        if not math.isfinite(w) or w < -WEIGHT_TOL:
            raise EngineError(f"{label}: asset {asset_id} has an invalid weight {weight!r}")
        if w > WEIGHT_TOL:
            out[int(asset_id)] = w
    total = sum(out.values())
    if total > 1.0 + 1e-6:
        raise EngineError(f"{label}: weights sum to {total:.9f} > 1")
    return out


def _cash_of(weights: Mapping[int, float]) -> float:
    return max(0.0, 1.0 - sum(weights.values()))


def _positives(weights: Mapping[int, float]) -> dict[int, float]:
    return {int(a): float(w) for a, w in weights.items() if float(w) > WEIGHT_TOL}


class _Book:
    """One arm's mutable simulation state."""

    __slots__ = (
        "_session",
        "cash",
        "events_out",
        "executions_out",
        "guard_state",
        "markowitz",
        "nav",
        "out_assets",
        "peak",
        "pending",
        "performance_out",
        "spec",
        "weights",
        "weights_out",
    )

    def __init__(self, spec: ArmSpec) -> None:
        with_state = spec.guard is not None or spec.shadow_guard is not None
        self.spec = spec
        self.weights: dict[int, float] = {}
        self.cash = 1.0
        self.nav = 1.0
        self.peak = 1.0
        self.pending: dict[int, float] | None = None
        self.markowitz: dict[int, float] = {}
        self.guard_state: GuardState | None = initial_state() if with_state else None
        self.out_assets: frozenset[int] = frozenset()
        self.weights_out: list[DayWeights] = []
        self.performance_out: list[DayPerformance] = []
        self.executions_out: list[Execution] = []
        self.events_out: list[GuardEvent] = []

    @property
    def _guarded(self) -> bool:
        return self.spec.guard is not None

    def mark_to_market(
        self, date: str, returns: Mapping[int, float], rf: float
    ) -> tuple[float, dict[int, float], float, set[int]]:
        """Step 1: drift the book through *date*.

        Returns ``(gross, drifted, drifted_cash, missing)``. A held asset without a return is
        valued at its last close (return 0) and listed in *missing*: the caller moves it to
        cash at the close (spec §0)."""
        gross = self.cash * rf
        missing: set[int] = set()
        for asset_id, weight in self.weights.items():
            ret = returns.get(asset_id)
            if ret is None:
                missing.add(asset_id)
                ret = 0.0
            gross += weight * ret
        if gross <= -1.0:
            raise EngineError(f"{self.spec.code}: session {date} wipes the book out ({gross=})")
        scale = 1.0 + gross
        drifted = {a: w * (1.0 + returns.get(a, 0.0)) / scale for a, w in self.weights.items()}
        drifted_cash = self.cash * (1.0 + rf) / scale
        return gross, drifted, drifted_cash, missing


def _benchmark_returns(inp: EngineInput, sessions: Sequence[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for date in sessions:
        members = inp.benchmark_members.get(date, ())
        day = inp.returns.get(date, {})
        priced = [day[int(a)] for a in members if int(a) in day]
        out[date] = sum(priced) / len(priced) if priced else 0.0
    # The arms are first put at risk at the formation close: the benchmark starts with them,
    # so its formation-day return is 0 and its first return is the next session's.
    out[inp.formation_date] = 0.0
    return out


def _aligned_returns(inp: EngineInput) -> dict[int, list[float]]:
    """Per-asset simple returns aligned to ``inp.sessions`` (0.0 where the asset has no row)."""
    aligned: dict[int, list[float]] = {}
    for index, date in enumerate(inp.sessions):
        for asset_id, ret in inp.returns.get(date, {}).items():
            series = aligned.get(int(asset_id))
            if series is None:
                series = [0.0] * len(inp.sessions)
                aligned[int(asset_id)] = series
            series[index] = float(ret)
    return aligned


def _reference_returns(
    ref: Mapping[int, float], aligned: Mapping[int, Sequence[float]], index: int, window: int
) -> list[float]:
    start = max(0, index - window + 1)
    out: list[float] = []
    for i in range(start, index + 1):
        out.append(sum(w * aligned[a][i] for a, w in ref.items() if a in aligned))
    return out


def _asset_return_window(
    asset_id: int, aligned: Mapping[int, Sequence[float]], index: int, length: int
) -> list[float]:
    series = aligned.get(asset_id)
    if series is None:
        return []
    start = max(0, index - length + 1)
    return list(series[start : index + 1])


def _execute(
    drifted: Mapping[int, float],
    target: Mapping[int, float] | None,
    missing: set[int],
    returns: Mapping[int, float],
    cost_bps: float,
) -> tuple[dict[int, float], float, float]:
    """Step 2: one net order at the close. Returns ``(weights, turnover, cost)``.

    A target asset with no return on the session cannot be priced at the close: it is dropped
    to cash (the same data-gap rule as a held name). With no pending target, only the missing
    names are liquidated; the rest of the book is untouched."""
    if target is None:
        wanted = {a: w for a, w in drifted.items() if a not in missing}
    else:
        wanted = {a: w for a, w in target.items() if a in returns}
    keys = set(wanted) | set(drifted)
    turnover = sum(abs(wanted.get(a, 0.0) - drifted.get(a, 0.0)) for a in keys)
    if turnover <= WEIGHT_TOL:
        return dict(drifted), 0.0, 0.0
    return wanted, turnover, cost_bps / 10_000.0 * turnover


def _weight_of(weights: Mapping[int, float], asset_id: int | None) -> float:
    return 0.0 if asset_id is None else float(weights.get(asset_id, 0.0))


def _action_status(
    arm: ArmSpec, alert: Alert, cut: frozenset[int], scale: float, reference: Weights
) -> str:
    if arm.shadow_guard is not None:
        return "ARM_DISABLED"
    if alert.asset_id is not None and alert.asset_id not in reference:
        return "NOT_HELD"
    if alert.asset_id is None:  # a portfolio scale (L3 / L4)
        return "APPLIED" if scale < 1.0 - WEIGHT_TOL else "SUPERSEDED"
    if alert.layer == NEWS_LAYER:
        # A news state change is applied even when the cooldown keeps the name trimmed.
        return "APPLIED"
    if alert.state == "EXIT":
        return "APPLIED" if alert.asset_id not in cut else "SUPERSEDED"
    return "APPLIED" if alert.asset_id in cut else "SUPERSEDED"


def _guard_event_weight(alert: Alert, before: Weights, after: Weights) -> tuple[float, float]:
    if alert.asset_id is None:
        return sum(before.values()), sum(after.values())
    return _weight_of(before, alert.asset_id), _weight_of(after, alert.asset_id)


def _guard_input(  # noqa: PLR0913, PLR0917 - the overlay's inputs, all named
    book: _Book,
    reference: Weights,
    date: str,
    index: int,
    aligned: Mapping[int, Sequence[float]],
    inp: EngineInput,
    params: GuardParams,
) -> GuardInput:
    tracked = set(reference) | set(book.out_assets)
    return GuardInput(
        date=date,
        reference_weights=dict(reference),
        asset_returns={
            asset_id: _asset_return_window(asset_id, aligned, index, params.J)
            for asset_id in tracked
        },
        reference_returns=_reference_returns(reference, aligned, index, params.vol_window),
        nav=book.nav,
        news=inp.news.get(date),
    )


def _apply_guard(  # noqa: PLR0913, PLR0917 - the overlay's inputs, all named
    book: _Book,
    reference: Weights,
    date: str,
    index: int,
    aligned: Mapping[int, Sequence[float]],
    inp: EngineInput,
    params: GuardParams,
) -> dict[int, float]:
    """Evaluate the overlay and record its actions; returns the target for the next close."""
    assert book.guard_state is not None
    out = evaluate(
        book.guard_state, _guard_input(book, reference, date, index, aligned, inp, params), params
    )
    book.guard_state = out.state
    book.out_assets = out.cut_assets
    final = dict(out.weights)
    for alert in out.alerts:
        status = _action_status(book.spec, alert, out.cut_assets, out.scale, reference)
        before, after = _guard_event_weight(alert, reference, final)
        book.events_out.append(GuardEvent(date, alert, status, before, after))
    return final


def _record_shadow(  # noqa: PLR0913, PLR0917 - the overlay's inputs, all named
    book: _Book,
    reference: Weights,
    date: str,
    index: int,
    aligned: Mapping[int, Sequence[float]],
    inp: EngineInput,
    params: GuardParams,
) -> None:
    """Evaluate the twin's overlay on a NOGUARD book without ever applying it."""
    assert book.guard_state is not None
    out = evaluate(
        book.guard_state, _guard_input(book, reference, date, index, aligned, inp, params), params
    )
    book.guard_state = out.state
    book.out_assets = out.cut_assets
    for alert in out.alerts:
        before, after = _guard_event_weight(alert, reference, reference)
        book.events_out.append(GuardEvent(date, alert, "ARM_DISABLED", before, after))


def simulate(inp: EngineInput) -> EngineResult:  # noqa: C901, PLR0912, PLR0915
    """Run the whole window. Pure: *inp* is never mutated.

    One linear walk of the sessions: the order of the steps (drift, execute, GUARD, form the
    next target) is the specification, so it is kept in one place rather than split."""
    sessions = list(inp.sessions)
    if not sessions:
        raise EngineError("no sessions")
    if inp.formation_date not in sessions:
        raise EngineError(f"formation date {inp.formation_date} is not a session")
    if len(set(sessions)) != len(sessions):
        raise EngineError("duplicate sessions")
    if sessions != sorted(sessions):
        raise EngineError("sessions must be ascending")
    formation_index = sessions.index(inp.formation_date)

    known = {spec.code for spec in inp.arms}
    if len(known) != len(inp.arms):
        raise EngineError("duplicate arm codes")
    for spec in inp.arms:
        if spec.target_source not in inp.targets:
            raise EngineError(f"{spec.code}: no targets for source {spec.target_source}")
    for spec in inp.arms:
        if spec.guard is not None and spec.guard.layers & {1, 2, 3, 4} != spec.guard.layers:
            raise EngineError(f"{spec.code}: guard layers must be a subset of 1..4")
    decision_dates = list(dict.fromkeys(inp.decision_dates))
    for date in decision_dates:
        if date not in sessions:
            raise EngineError(f"decision date {date} is not a session")

    books: dict[str, _Book] = {spec.code: _Book(spec) for spec in inp.arms}
    by_code = {spec.code: spec for spec in inp.arms}
    # Non-guard books first: a GUARD book reads its twin's target for the same close.
    order = [s.code for s in inp.arms if s.guard is None] + [
        s.code for s in inp.arms if s.guard is not None
    ]
    for code in order:
        spec = by_code[code]
        if spec.guard is not None:
            twin_code = spec.target_source
            if twin_code not in books:
                raise EngineError(f"{code}: twin arm {twin_code} is not simulated")
            if books[twin_code].spec.guard is not None:
                raise EngineError(f"{code}: twin arm {twin_code} is itself guarded")
        elif code not in inp.targets:
            raise EngineError(f"{code}: no targets")

    aligned = _aligned_returns(inp)
    benchmark = _benchmark_returns(inp, sessions[formation_index:])

    # The formation order was formed at the close of the last decision before formation; the
    # GUARD arms evaluate that proposed target there, exactly as on any later decision date.
    decision_index = formation_index - 1
    if decision_index < 0:
        raise EngineError("the formation date needs a decision session before it")
    first_decision = sessions[decision_index]
    for book in books.values():
        target_dates = [d for d in inp.targets[book.spec.target_source] if d < inp.formation_date]
        if not target_dates:
            raise EngineError(f"{book.spec.code}: no target before formation")
        first = max(target_dates)
        if first != first_decision:
            raise EngineError(
                f"{book.spec.code}: the formation decision {first} is not the session before "
                f"formation ({first_decision})"
            )
        target = _validate_target(
            inp.targets[book.spec.target_source][first], f"{book.spec.code}@{first}"
        )
        book.pending = target
        book.markowitz = target
    for code in order:
        book = books[code]
        spec = book.spec
        if spec.guard is not None:
            book.pending = _apply_guard(
                book,
                dict(book.pending or {}),
                first_decision,
                decision_index,
                aligned,
                inp,
                spec.guard,
            )
        elif spec.shadow_guard is not None:
            _record_shadow(
                book,
                dict(book.pending or {}),
                first_decision,
                decision_index,
                aligned,
                inp,
                spec.shadow_guard,
            )

    for index in range(formation_index, len(sessions)):
        date = sessions[index]
        day_returns = inp.returns.get(date, {})
        rf = float(inp.rf.get(date, 0.0))

        # -- step 1/2: drift, then execute the one net order formed at the previous close.
        for book in books.values():
            executed: dict[int, float] | None = None
            if date == inp.formation_date:
                gross = 0.0  # the book's capital is first put at risk at this close
                drifted: dict[int, float] = {}
                drifted_cash = 1.0
                missing: set[int] = set()
            else:
                gross, drifted, drifted_cash, missing = book.mark_to_market(date, day_returns, rf)
            pending_target = book.pending
            weights, turnover, cost = _execute(
                drifted, pending_target, missing, day_returns, inp.cost_bps
            )
            if pending_target is not None or missing:
                executed = weights
                book.executions_out.append(Execution(date, turnover, cost, dict(weights)))
            book.pending = None
            book.weights = weights
            book.cash = (
                max(0.0, 1.0 - sum(weights.values()))
                if pending_target is not None or missing
                else drifted_cash
            )
            net = (1.0 + gross) * (1.0 - cost) - 1.0
            book.nav *= 1.0 + net
            book.peak = max(book.peak, book.nav)
            book._session = (  # type: ignore[attr-defined]
                gross,
                turnover,
                cost,
                net,
                executed,
                dict(drifted),
            )

        # -- step 3/4/5: the targets for the next close.
        for code in order:
            book = books[code]
            spec = book.spec
            gross, turnover, cost, net, executed, drifted_weights = book._session  # type: ignore[attr-defined]
            decision = date in decision_dates
            if decision:
                target = _validate_target(
                    inp.targets[spec.target_source][date], f"{spec.code}@{date}"
                )
                book.markowitz = target
            elif executed is not None:
                target = executed
            else:
                target = dict(book.weights)
            reference = target if decision or executed is not None else dict(book.weights)

            if spec.guard is None:
                pre_guard = reference
                final = pre_guard
                if decision:
                    book.pending = final
            else:
                twin_book = books[spec.target_source]
                if not twin_book.weights_out or twin_book.weights_out[-1].date != date:
                    raise EngineError(
                        f"{code}: twin {twin_book.spec.code} was not simulated at {date}"
                    )
                ref = twin_book.weights_out[-1].final_target
                pre_guard = dict(ref)
                final = _apply_guard(book, ref, date, index, aligned, inp, spec.guard)
                current = dict(book.weights)
                if (
                    any(
                        abs(final.get(a, 0.0) - current.get(a, 0.0)) > WEIGHT_TOL
                        for a in set(final) | set(current)
                    )
                    or abs(_cash_of(final) - book.cash) > WEIGHT_TOL
                ):
                    book.pending = final

            if spec.shadow_guard is not None:
                _record_shadow(book, final, date, index, aligned, inp, spec.shadow_guard)

            book.weights_out.append(
                DayWeights(
                    date=date,
                    markowitz_target=dict(book.markowitz),
                    drifted=dict(drifted_weights),
                    pre_guard=dict(pre_guard),
                    final_target=dict(final),
                    cash_weight=_cash_of(final),
                    n_equity=sum(1 for w in final.values() if w > WEIGHT_TOL),
                )
            )
            book.performance_out.append(
                DayPerformance(
                    date=date,
                    gross_return=gross,
                    turnover=turnover,
                    cost_return=cost,
                    net_return=net,
                    nav=book.nav,
                    drawdown=1.0 - book.nav / book.peak if book.peak > 0 else 0.0,
                    cash_weight=book.cash,
                    n_equity=sum(1 for w in book.weights.values() if w > WEIGHT_TOL),
                    benchmark_return=benchmark.get(date, 0.0),
                )
            )

    results = {
        code: ArmResult(
            code=code,
            weights=tuple(book.weights_out),
            performance=tuple(book.performance_out),
            executions=tuple(book.executions_out),
            guard_events=tuple(book.events_out),
        )
        for code, book in books.items()
    }
    return EngineResult(arms=results, benchmark=benchmark, formation_date=inp.formation_date)
